"""Board planning writes: an item's ``plan``, the board's ``releases``, a person's rank.

The conductor writes an item's plan with ``work_ledger_record action=plan`` (or on
``create``) and the board's release list with ``action=release``. A person's drag
on a backlog row writes only ``plan.rank`` (:func:`apply_person_rank`). Shapes and
the next-pick rule live in :mod:`kiro_crew.work_plan_vocab`.

:mod:`kiro_crew.work_ledger` binds every name defined here, so each one reads,
imports and patches as ``kiro_crew.work_ledger.<name>``.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.work_ledger_records import (
    CODE_GOAL_CONCURRENCY_CAP_REACHED,
    CODE_INVALID_VALUE,
    CODE_ITEM_CLOSED,
    CODE_UNKNOWN_ITEM,
    MAX_GOAL_CONCURRENCY_CAP,
    TERMINAL_ITEM_STATES,
    ConductorRecord,
    WorkItem,
    WorkLedgerError,
    _now_iso,
)
from kiro_crew.work_ledger_store import (
    _CONDUCTOR_FILE,
    _commit_item_locked,
    _existing_conductor_lock,
    _header_under_lock,
    _require_item_id,
    _write_record,
    conductor_dir,
    item_lock,
    list_work_items,
    read_work_item,
)
from kiro_crew.work_plan_vocab import (
    MAX_RELEASES,
    counts_toward_concurrency,
    in_backlog,
    merge_plan,
    plan_problem,
    rank_value,
    release_problem,
    upsert_release,
)


def require_plan_change(raw: Any) -> dict[str, Any]:
    """*raw* as a plan change (keys to set, ``None`` to clear), or the refusal."""
    problem = plan_problem(raw)
    if problem is not None:
        field, message = problem
        raise WorkLedgerError(message, code=CODE_INVALID_VALUE, field=field)
    assert isinstance(raw, dict)
    return dict(raw)


#: The level an item's parent must have: a story sits under an epic, a sub-task
#: under a story, and an epic under nothing. No level reads as a story.
_LEVEL_ABOVE: dict[str, str] = {"story": "epic", "task": "story"}
_A: dict[str, str] = {"epic": "an epic", "story": "a story", "task": "a task"}


def _level(plan: dict[str, Any] | None) -> str:
    return (plan or {}).get("level") or "story"


def check_parent(slot_key: str, item_id: str | None, plan: dict[str, Any]) -> None:
    """Refuse a *plan* whose level and parent break the epic > story > task shape.

    *plan* is the item's whole plan as it would be stored. Its parent must be an item
    on this board one level up (an epic takes none, a sub-task needs one: with no
    story it would hold no seat and could never be bound), and on an existing item every
    item already under it, a closed one included, must stay one level down, so a level change cannot strand
    its children. A tree whose every link goes exactly one level down cannot loop
    and is never deeper than ``PLAN_MAX_PARENT_DEPTH``. *item_id* is ``None`` on a
    create, which has no children yet.
    """
    level = _level(plan)
    parent = plan.get("parent")
    if level == "task" and not parent:
        raise WorkLedgerError(
            "a sub-task sits under a story and rides its worker: give it plan.parent, "
            "or make it a story",
            code=CODE_INVALID_VALUE,
            field="plan.parent",
        )
    if parent:
        if parent == item_id:
            raise WorkLedgerError(
                "an item cannot be its own parent", code=CODE_INVALID_VALUE, field="plan.parent"
            )
        want = _LEVEL_ABOVE.get(level)
        if want is None:
            raise WorkLedgerError(
                "an epic sits at the top of the tree and takes no parent",
                code=CODE_INVALID_VALUE,
                field="plan.parent",
            )
        holder = read_work_item(slot_key, parent)
        if holder is None:
            raise WorkLedgerError(
                f"parent {parent!r} is not an item on this board",
                code=CODE_INVALID_VALUE,
                field="plan.parent",
            )
        have = _level(holder.plan)
        if have != want:
            raise WorkLedgerError(
                f"a {level} sits under {_A[want]}, and {parent!r} is {_A[have]}: "
                "the tree is epic > story > task",
                code=CODE_INVALID_VALUE,
                field="plan.parent",
            )
    if item_id is None:
        return
    stranded = [
        child
        for child in list_work_items(slot_key)
        if (child.plan or {}).get("parent") == item_id
        and _LEVEL_ABOVE.get(_level(child.plan)) != level
    ]
    if not stranded:
        return
    # A closed child takes no further write, so it cannot be moved first: it keeps
    # its parent's level, and the board's readers keep a tree whose every link goes
    # one level down. Named before any open one, so the advice is never a move that
    # a closed sibling then refuses.
    child = next((c for c in stranded if c.is_terminal), stranded[0])
    remedy = (
        "it is closed and takes no further write, so this item keeps its level"
        if child.is_terminal
        else "move or clear that child's parent first"
    )
    raise WorkLedgerError(
        f"{child.item_id!r} is a {_level(child.plan)} under {item_id!r}, so {item_id!r} "
        f"cannot become {_A[level]}: {remedy}",
        code=CODE_INVALID_VALUE,
        field="plan.level",
    )


def refuse_unbindable(item: WorkItem) -> None:
    """Refuse binding a worker to *item* when its plan holds no seat.

    A backlog item is pulled into the sprint first; an epic and a sub-task are never
    bound. Every bind writer calls it -- the local ``bind``, the remote bind and the
    hub's check before it spawns a peer slot -- so no path binds past the goal's cap.
    """
    if (item.plan or {}).get("backlog"):
        raise WorkLedgerError(
            f"item {item.item_id!r} is in the backlog; pull it into the sprint "
            'first with action=plan plan={"backlog": false}, then bind it',
            code=CODE_INVALID_VALUE,
            field="plan.backlog",
        )
    if not counts_toward_concurrency(item.plan):
        raise WorkLedgerError(
            f"item {item.item_id!r} is an epic or a sub-task, which holds no seat: "
            "bind a worker to a story, and track an epic's stories or a "
            "story's sub-tasks on the plan",
            code=CODE_INVALID_VALUE,
            field="plan.level",
        )


def seated_open_items(slot_key: str, *, exclude: str | None = None) -> int:
    """Open items that hold a seat in the goal's ``concurrency_cap``.

    A backlog item, an epic and a sub-task hold none (``counts_toward_concurrency``).
    """
    return count_seated(list_work_items(slot_key), exclude=exclude)


def count_seated(items: list[WorkItem], *, exclude: str | None = None) -> int:
    """How many of *items* hold a seat: the count the goal's cap admission refuses on.

    The board sends it beside its own worker count, because a story with no plan and
    no worker is a backlog row on the page yet still holds a seat here.
    """
    return sum(
        1
        for item in items
        if item.state not in TERMINAL_ITEM_STATES
        and item.item_id != exclude
        and counts_toward_concurrency(item.plan)
    )


def refuse_if_no_seat(slot_key: str, live: ConductorRecord, *, exclude: str | None) -> None:
    """Refuse taking one more seat on a board whose goal set a cap. HOLD THE CONDUCTOR LOCK."""
    if live.goal_version <= 0:
        return
    held = seated_open_items(slot_key, exclude=exclude)
    if held >= live.concurrency_cap:
        raise WorkLedgerError(
            f"this goal holds {held} items in its sprint and its concurrency cap is "
            f"{live.concurrency_cap}. Pull nothing new out of the backlog until one "
            "lands: wait for a seat, do not ask to raise it. Only a bound the user sets "
            "changes it, with work_ledger_record action=goal concurrency_cap=<new cap> "
            f"(at most {MAX_GOAL_CONCURRENCY_CAP})",
            code=CODE_GOAL_CONCURRENCY_CAP_REACHED,
            field="concurrency_cap",
        )


def _plan_event_text(change: dict[str, Any]) -> str:
    parts = []
    for key, value in change.items():
        if value is None:
            parts.append(f"{key} cleared")
        elif isinstance(value, list):
            parts.append(f"{key}={','.join(value)}")
        else:
            parts.append(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
    return "plan " + ("; ".join(parts) if parts else "unchanged")


def apply_plan_write(slot_key: str, item_id: str | None, plan: Any) -> dict[str, Any]:
    """The conductor's ``plan`` action: lay *plan* over the item's plan.

    A key given replaces, a key given as ``None`` clears, a key left out keeps.
    Taking an item out of the backlog takes a sprint seat, so it is refused when
    the goal's cap is full. Under the conductor lock (that count is a property of
    the set), then the item's. Returns ``{"conductor", "item", "event"}``.
    """
    checked_id = _require_item_id(item_id or "")
    change = require_plan_change(plan)
    with _existing_conductor_lock(slot_key):
        live = _header_under_lock(slot_key, "no item can be planned in it")
        with item_lock(slot_key, checked_id, create=False):
            item = _open_item(slot_key, checked_id)
            merged = merge_plan(item.plan, change)
            if "parent" in change or "level" in change:
                check_parent(slot_key, checked_id, merged or {})
            if not counts_toward_concurrency(item.plan) and counts_toward_concurrency(merged):
                refuse_if_no_seat(slot_key, live, exclude=checked_id)
            if item.worker_session_key and not counts_toward_concurrency(merged):
                # A bound worker keeps its seat until its item closes: a plan that
                # made it an epic, a sub-task or a backlog item would let the board
                # run one worker past its cap.
                raise WorkLedgerError(
                    f"item {checked_id!r} has a worker bound, so it cannot become an "
                    "epic, a sub-task or a backlog item until it closes",
                    code=CODE_INVALID_VALUE,
                    field="plan",
                )
            item.plan = merged
            event = _commit_item_locked(slot_key, item, "plan", _plan_event_text(change))
    return {"conductor": live, "item": item, "event": event}


def apply_person_rank(slot_key: str, item_id: str, rank: Any) -> dict[str, Any]:
    """A person's drag: set only ``plan.rank`` on one open item.

    The one plan key a person writes: the backlog order is theirs to set, the rest
    of the plan is the conductor's, so an item outside the backlog is refused. A
    story with no worker and no ``backlog`` key is a backlog row (:func:`in_backlog`),
    so a board whose conductor never planned is ranked as well. Any item marked
    ``backlog: true`` is ranked too, an epic or a sub-task included.
    Returns ``{"item", "event"}``.
    """
    checked_id = _require_item_id(item_id)
    number = rank_value(rank)
    if number is None:
        raise WorkLedgerError(
            "rank must be a finite number", code=CODE_INVALID_VALUE, field="plan.rank"
        )
    with item_lock(slot_key, checked_id, create=False):
        item = _open_item(slot_key, checked_id)
        marked = (item.plan or {}).get("backlog") is True
        bound = bool(item.worker_session_key)
        if not marked and not in_backlog(item.plan, state=item.state, worker_bound=bound):
            raise WorkLedgerError(
                f"item {checked_id!r} is not in the backlog: only a backlog row is ranked",
                code=CODE_INVALID_VALUE,
                field="plan.backlog",
            )
        item.plan = merge_plan(item.plan, {"rank": number})
        event = _commit_item_locked(slot_key, item, "plan", PERSON_RANK_TEXT.format(rank=number))
    return {"item": item, "event": event}


#: The text of the one ``plan`` line a person writes (:func:`apply_person_rank`).
#: A ledger line records no writer, so this text is what tells a person's drag from
#: the conductor's own ``plan`` writes (which read ``plan …``, :func:`_plan_event_text`).
#: Readers test a line with :func:`is_person_rank_text`, never a copy of the string.
PERSON_RANK_TEXT = "ranked {rank:g} by the person"


def is_person_rank_text(text: str) -> bool:
    """Whether a ``plan`` line's *text* is a person's drag, as the writer words it."""
    head, tail = PERSON_RANK_TEXT.split("{rank:g}")
    if len(text) <= len(head) + len(tail) or not (text.startswith(head) and text.endswith(tail)):
        return False
    try:
        float(text[len(head) : len(text) - len(tail)])
    except ValueError:
        return False
    return True


def apply_release_write(slot_key: str, release: Any) -> dict[str, Any]:
    """The conductor's ``release`` action: add or update one release on the board.

    ``{name, state?, note?}``; ``state`` ``shipped`` stamps ``shipped_at`` once.
    A header write, like ``goal``: no item and no event. Returns
    ``{"conductor", "item": None, "event": None}``.
    """
    problem = release_problem(release)
    if problem is not None:
        field, message = problem
        raise WorkLedgerError(message, code=CODE_INVALID_VALUE, field=field)
    assert isinstance(release, dict)
    with _existing_conductor_lock(slot_key):
        live = _header_under_lock(slot_key, "no release can be recorded in it")
        name = release["name"].strip()
        if len(live.releases) >= MAX_RELEASES and all(r.get("name") != name for r in live.releases):
            raise WorkLedgerError(
                f"this board already holds {MAX_RELEASES} releases; update one of them "
                "instead of adding another",
                code=CODE_INVALID_VALUE,
                field="release.name",
            )
        live.releases = upsert_release(live.releases, release, _now_iso())
        _write_record(conductor_dir(slot_key) / _CONDUCTOR_FILE, live.to_dict())
    return {"conductor": live, "item": None, "event": None}


def _open_item(slot_key: str, item_id: str) -> WorkItem:
    item = read_work_item(slot_key, item_id)
    if item is None:
        raise WorkLedgerError(f"unknown item {item_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id")
    if item.is_terminal:
        raise WorkLedgerError(f"item {item_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
    return item
