"""The board plan's vocabulary: an item's ``plan`` and a board's ``releases``.

A conductor plans its board the way a scrum team does: epics split into stories
and tasks (``level`` and ``parent``), a ranked backlog of work not started yet
(``backlog`` and ``rank``), the step each item is at (``step``), and the release
each merged item ships in (``release``), plus free ``tags``. The board header
holds the ``releases`` list itself.

Here, and not in the store or the fold, because both read these shapes and
neither may import the other (the rule ``work_vocab`` follows). Nothing here
touches a file.
"""

from __future__ import annotations

import math
import re
from typing import Any

#: An item's size. ``story`` is what an item with no ``level`` reads as.
PLAN_LEVELS: tuple[str, ...] = ("epic", "story", "task")
#: Where an item is in the process, in order. ``todo`` is not started.
PLAN_STEPS: tuple[str, ...] = ("todo", "mocks", "build", "review", "merged", "released")
#: The keys a plan may hold. Every one is optional.
PLAN_KEYS: tuple[str, ...] = ("level", "parent", "rank", "backlog", "step", "release", "tags")

PLAN_MAX_TAGS: int = 8
PLAN_TAG_CHARS: int = 32
PLAN_RELEASE_CHARS: int = 40
#: How deep ``parent`` links may chain: epic > story > task.
PLAN_MAX_PARENT_DEPTH: int = 3
#: A rank's magnitude bound: a drag halves the gap between two neighbours, so a
#: bounded range keeps every rank exact enough to order.
PLAN_MAX_RANK: float = 1e9

#: The board's release list.
RELEASE_STATES: tuple[str, ...] = ("planned", "shipped")
RELEASE_NOTE_CHARS: int = 200
MAX_RELEASES: int = 16
_STAMP_CHARS: int = 64

_ITEM_ID_RE = re.compile(r"^it_[0-9a-f]{8}$")


def plan_problem(raw: Any) -> tuple[str, str] | None:
    """Why *raw* is not a plan a writer may store, as ``(field, message)``, or ``None``.

    A key with ``None`` clears that key, so ``None`` is valid for every key. The
    store and the record route both call this, so a caller is refused before
    anything is written instead of having a value silently dropped.
    """
    if not isinstance(raw, dict):
        return "plan", "plan must be an object"
    for key in raw:
        if key not in PLAN_KEYS:
            return f"plan.{key}", f"unknown plan key {key!r}; expected one of {list(PLAN_KEYS)}"
    for key, value in raw.items():
        if value is None:
            continue
        if key == "level" and value not in PLAN_LEVELS:
            return "plan.level", f"level must be one of {list(PLAN_LEVELS)}"
        if key == "step" and value not in PLAN_STEPS:
            return "plan.step", f"step must be one of {list(PLAN_STEPS)}"
        if key == "parent" and (not isinstance(value, str) or not _ITEM_ID_RE.match(value)):
            return "plan.parent", "parent must be an item id (it_ and 8 hex digits)"
        if key == "backlog" and not isinstance(value, bool):
            return "plan.backlog", "backlog must be true or false"
        if key == "rank" and rank_value(value) is None:
            return "plan.rank", f"rank must be a finite number within ±{PLAN_MAX_RANK:g}"
        if key == "release" and (
            not isinstance(value, str) or not value.strip() or len(value) > PLAN_RELEASE_CHARS
        ):
            return "plan.release", f"release must be a name of 1 to {PLAN_RELEASE_CHARS} chars"
        if key == "tags":
            if not isinstance(value, list) or len(value) > PLAN_MAX_TAGS:
                return "plan.tags", f"tags must be a list of at most {PLAN_MAX_TAGS}"
            for n, tag in enumerate(value):
                if not isinstance(tag, str) or not tag.strip() or len(tag) > PLAN_TAG_CHARS:
                    return f"plan.tags[{n}]", f"a tag is 1 to {PLAN_TAG_CHARS} chars"
    return None


def rank_value(value: Any) -> float | None:
    """*value* as a usable rank, or ``None``. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an int too large for a float
        return None
    if not math.isfinite(number) or abs(number) > PLAN_MAX_RANK:
        return None
    return number


def coerce_plan(raw: Any) -> dict[str, Any] | None:
    """Stored or folded *raw* as a clean plan, or ``None`` when nothing is set.

    NEVER raises: a bad key is dropped, the rest kept, so one torn value cannot
    hide the whole plan. ``None`` (not ``{}``) for an empty plan, because the
    record serializes a plan only while it holds one: an item that was never
    planned is stored exactly as it was before the field existed.
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    level = raw.get("level")
    if level in PLAN_LEVELS:
        out["level"] = level
    parent = raw.get("parent")
    if isinstance(parent, str) and _ITEM_ID_RE.match(parent):
        out["parent"] = parent
    rank = rank_value(raw.get("rank"))
    if rank is not None:
        out["rank"] = rank
    if raw.get("backlog") is True:
        out["backlog"] = True
    step = raw.get("step")
    if step in PLAN_STEPS:
        out["step"] = step
    release = raw.get("release")
    if isinstance(release, str) and release.strip():
        out["release"] = release.strip()[:PLAN_RELEASE_CHARS]
    tags = raw.get("tags")
    if isinstance(tags, list):
        kept = [t.strip()[:PLAN_TAG_CHARS] for t in tags if isinstance(t, str) and t.strip()]
        if kept:
            out["tags"] = list(dict.fromkeys(kept))[:PLAN_MAX_TAGS]
    return out or None


def merge_plan(current: dict[str, Any] | None, change: dict[str, Any]) -> dict[str, Any] | None:
    """*current* with *change* laid over it: a given key replaces, ``None`` clears."""
    merged = dict(current or {})
    for key, value in change.items():
        if value is None or value is False and key == "backlog":
            merged.pop(key, None)
        else:
            merged[key] = value
    return coerce_plan(merged)


def counts_toward_concurrency(plan: dict[str, Any] | None) -> bool:
    """Whether an open item holds a seat in the goal's ``concurrency_cap``.

    A backlog item has not started, an epic is never worked itself (its stories
    are), and a sub-task is done by its story's worker, so none of them takes a
    seat, and none can be bound (the store refuses that bind); the store's own open
    bound still counts them. A seat is therefore a worker the board can run.
    """
    if not plan:
        return True
    return not plan.get("backlog") and plan.get("level") not in ("epic", "task")


def release_problem(raw: Any) -> tuple[str, str] | None:
    """Why *raw* is not a release write, as ``(field, message)``, or ``None``."""
    if not isinstance(raw, dict):
        return "release", "release must be an object: {name, state?, note?}"
    for key in raw:
        if key not in ("name", "state", "note"):
            return f"release.{key}", "a release takes name, state and note"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > PLAN_RELEASE_CHARS:
        return "release.name", f"name must be 1 to {PLAN_RELEASE_CHARS} chars"
    state = raw.get("state")
    if state is not None and state not in RELEASE_STATES:
        return "release.state", f"state must be one of {list(RELEASE_STATES)}"
    note = raw.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > RELEASE_NOTE_CHARS):
        return "release.note", f"note must be at most {RELEASE_NOTE_CHARS} chars"
    return None


def coerce_releases(raw: Any) -> list[dict[str, Any]]:
    """Stored or folded *raw* as a clean release list, oldest first. NEVER raises."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()[:PLAN_RELEASE_CHARS]
        if name in seen:
            continue
        seen.add(name)
        state = entry.get("state") if entry.get("state") in RELEASE_STATES else "planned"
        clean: dict[str, Any] = {"name": name, "state": state}
        note = entry.get("note")
        if isinstance(note, str) and note:
            clean["note"] = note[:RELEASE_NOTE_CHARS]
        shipped_at = entry.get("shipped_at")
        if state == "shipped" and isinstance(shipped_at, str) and shipped_at:
            clean["shipped_at"] = shipped_at[:_STAMP_CHARS]
        out.append(clean)
    return out[-MAX_RELEASES:]


def upsert_release(
    releases: list[dict[str, Any]], change: dict[str, Any], now: str
) -> list[dict[str, Any]]:
    """*releases* with *change* written into the entry it names, or appended.

    Shipping stamps ``shipped_at`` once; a later write that keeps it shipped keeps
    the first stamp, and moving it back to planned drops it.
    """
    name = change["name"].strip()
    out = [dict(r) for r in coerce_releases(releases)]
    entry = next((r for r in out if r["name"] == name), None)
    if entry is None:
        entry = {"name": name, "state": "planned"}
        out.append(entry)
    if change.get("state") is not None:
        entry["state"] = change["state"]
    if "note" in change:
        if change["note"]:
            entry["note"] = change["note"]
        else:
            entry.pop("note", None)
    if entry["state"] == "shipped":
        entry.setdefault("shipped_at", now)
    else:
        entry.pop("shipped_at", None)
    return coerce_releases(out)


def widest_plan() -> dict[str, Any]:
    """The longest plan a write can commit, for the record's fit probe."""
    return {
        "level": "story",
        "parent": "it_00000000",
        "rank": -PLAN_MAX_RANK + 0.123456789,
        "backlog": True,
        "step": "released",
        "release": "r" * PLAN_RELEASE_CHARS,
        "tags": [f"{n}" * PLAN_TAG_CHARS for n in range(PLAN_MAX_TAGS)],
    }


def widest_releases() -> list[dict[str, Any]]:
    """The longest release list a board can hold, for the record's fit probe."""
    return [
        {
            "name": f"{n:02d}".ljust(PLAN_RELEASE_CHARS, "r"),
            "state": "shipped",
            "note": "n" * RELEASE_NOTE_CHARS,
            "shipped_at": "s" * _STAMP_CHARS,
        }
        for n in range(MAX_RELEASES)
    ]


def next_pick(items: list[Any]) -> str | None:
    """The item the conductor dispatches next, or ``None``: the top of the backlog.

    The lowest-ranked open backlog story with no worker: an epic and a sub-task are
    never bound, so neither is a pick.
    A backlog item with no rank sorts after every ranked one, oldest first, so a
    board that never ranked still has a next pick. *items* are store records or
    fold dicts.
    """

    def get(item: Any, name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

    best: tuple[int, float, str, str] | None = None
    for item in items:
        plan = coerce_plan(get(item, "plan")) or {}
        if get(item, "state") != "open" or not plan.get("backlog"):
            continue
        if plan.get("level") in ("epic", "task") or get(item, "worker_session_key"):
            continue
        rank = plan.get("rank")
        key = (
            0 if rank is not None else 1,
            rank if rank is not None else 0.0,
            str(get(item, "created_at") or ""),
            str(get(item, "item_id") or ""),
        )
        if best is None or key < best:
            best = key
    return best[3] if best else None
