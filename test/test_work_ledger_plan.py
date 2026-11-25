"""The board plan: an item's ``plan``, the board's ``releases``, and a person's rank.

Board A plans a conductor's board the way a scrum team does: epic > story > task,
a ranked backlog stored before dispatch, the step each item is at, and the release
it ships in. These pin the store's writes and refusals, the next-pick rule, that an
item which was never planned is stored exactly as before, and that every plan write
the routes record is the one a rebuild restores.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import work_ledger_projection_helpers
from aiohttp.test_utils import make_mocked_request
from work_ledger_projection_helpers import (
    _SLOTS,
    ACCEPTANCE,
    CONDUCTOR,
    WORKER,
    _real_units,
    _record,
    _Slot,
)

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.handlers import crew_board_rank, remote_dispatch_routes, remote_ingest
from kiro_crew.dashboard.handlers import work_ledger_board as board_routes
from kiro_crew.probes import work_ledger as probe
from kiro_crew.work_ledger_rules import is_stale
from kiro_crew.work_plan_vocab import (
    MAX_RELEASES,
    coerce_plan,
    in_backlog,
    next_pick,
    plan_problem,
    rank_value,
    show_step,
    shown_step,
)

_isolated = work_ledger_projection_helpers._isolated


def _board(cap: int = 2) -> None:
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    wl.apply_conductor_action(CONDUCTOR, "goal", concurrency_cap=cap)


def _create(title: str, plan: dict[str, Any] | None = None) -> str:
    result = wl.apply_conductor_action(
        CONDUCTOR, "create", title=title, acceptance={"kind": "human_approval"}, plan=plan
    )
    return result["item"].item_id


# -- the store ---------------------------------------------------------------


def test_an_item_never_planned_is_stored_without_a_plan_key() -> None:
    _board()
    item_id = _create("plain")
    raw = wl.item_path(CONDUCTOR, item_id).read_text(encoding="utf-8")
    assert '"plan"' not in raw
    header = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    assert '"releases"' not in header.read_text(encoding="utf-8")


def test_backlog_items_and_epics_take_no_seat_in_the_goals_cap() -> None:
    _board(cap=1)
    _create("epic", {"level": "epic"})
    _create("later", {"backlog": True, "rank": 2})
    _create("in the sprint")
    with pytest.raises(wl.WorkLedgerError) as caught:
        _create("one too many")
    assert caught.value.code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED
    # A backlog item still lands: it waits for a seat instead of taking one.
    _create("also later", {"backlog": True})


def test_pulling_from_the_backlog_takes_a_seat_and_is_refused_when_none_is_free() -> None:
    _board(cap=1)
    later = _create("later", {"backlog": True})
    _create("in the sprint")
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_plan_write(CONDUCTOR, later, {"backlog": False})
    assert caught.value.code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED
    assert (wl.read_work_item(CONDUCTOR, later).plan or {}).get("backlog") is True


def test_a_backlog_item_cannot_be_bound_until_it_is_pulled() -> None:
    _board()
    later = _create("later", {"backlog": True})
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=later, worker_session_key="chat-9-w")
    assert caught.value.field == "plan.backlog"
    wl.apply_plan_write(CONDUCTOR, later, {"backlog": False, "step": "build"})
    # ``false`` is kept: it holds the story in the sprint until its worker binds.
    assert wl.read_work_item(CONDUCTOR, later).plan == {"backlog": False, "step": "build"}
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=later, worker_session_key="chat-9-w")


def test_a_sub_task_rides_its_storys_seat_and_neither_it_nor_an_epic_can_be_bound() -> None:
    _board(cap=1)
    epic = _create("epic", {"level": "epic"})
    story = _create("story", {"parent": epic})
    task = _create("task", {"level": "task", "parent": story})
    _create("another task", {"level": "task", "parent": story})
    for item_id in (epic, task):
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_conductor_action(
                CONDUCTOR, "bind", item_id=item_id, worker_session_key="chat-9-w"
            )
        assert caught.value.field == "plan.level"
    # A sub-task promoted to a story needs a seat, and the story holds the only one.
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_plan_write(CONDUCTOR, task, {"level": None, "parent": epic})
    assert caught.value.code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED


def test_a_bound_item_keeps_its_seat_until_it_closes() -> None:
    _board()
    story = _create("story")
    other = _create("other story")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=story, worker_session_key="chat-9-w")
    for plan in ({"level": "task", "parent": other}, {"level": "epic"}, {"backlog": True}):
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_plan_write(CONDUCTOR, story, plan)
        assert caught.value.field == "plan"
    wl.apply_plan_write(CONDUCTOR, story, {"step": "build"})


def test_a_plan_write_lays_keys_over_the_plan_and_null_clears_one() -> None:
    _board()
    item_id = _create("story", {"tags": ["ui"], "step": "todo"})
    wl.apply_plan_write(CONDUCTOR, item_id, {"step": "review", "release": "0.5"})
    assert wl.read_work_item(CONDUCTOR, item_id).plan == {
        "step": "review",
        "release": "0.5",
        "tags": ["ui"],
    }
    result = wl.apply_plan_write(CONDUCTOR, item_id, {"tags": None, "release": None})
    assert result["item"].plan == {"step": "review"}
    assert result["event"].kind == "plan"


@pytest.mark.parametrize(
    ("plan", "field"),
    [
        ({"level": "saga"}, "plan.level"),
        ({"step": "shipping"}, "plan.step"),
        ({"rank": float("nan")}, "plan.rank"),
        ({"rank": True}, "plan.rank"),
        ({"tags": ["x" * 33]}, "plan.tags[0]"),
        ({"sprint": 3}, "plan.sprint"),
        ({"parent": "epic-1"}, "plan.parent"),
    ],
)
def test_a_bad_plan_key_is_refused_by_name(plan: dict[str, Any], field: str) -> None:
    assert plan_problem(plan) is not None and plan_problem(plan)[0] == field
    _board()
    with pytest.raises(wl.WorkLedgerError) as caught:
        _create("bad", plan)
    assert caught.value.field == field


def _tick_the_clock(monkeypatch) -> None:
    """One second per stamp, so ``created_at`` orders items by when they were made."""
    from kiro_crew import work_ledger_store, work_ledger_writes

    ticks = iter(range(10_000))
    start = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)

    def _tick() -> str:
        return (start + timedelta(seconds=next(ticks))).isoformat(timespec="seconds")

    for module in (work_ledger_store, work_ledger_writes):
        monkeypatch.setattr(module, "_now_iso", _tick)


def test_each_parent_link_goes_exactly_one_level_down(monkeypatch) -> None:
    # The store lists items by ``created_at``, so the open task below is the older
    # child by the clock, not by its random id.
    _tick_the_clock(monkeypatch)
    _board(cap=8)
    epic = _create("epic", {"level": "epic"})
    other = _create("other epic", {"level": "epic"})
    story = _create("story", {"level": "story", "parent": epic})
    task = _create("task", {"level": "task", "parent": story})
    with pytest.raises(wl.WorkLedgerError, match="sits under an epic"):
        _create("too deep", {"parent": task})
    with pytest.raises(wl.WorkLedgerError, match="sits under a story"):
        _create("task under an epic", {"level": "task", "parent": epic})
    # Re-parenting the top of a tree would make it four deep, or a loop.
    with pytest.raises(wl.WorkLedgerError, match="takes no parent"):
        wl.apply_plan_write(CONDUCTOR, epic, {"parent": task})
    with pytest.raises(wl.WorkLedgerError, match="own parent"):
        wl.apply_plan_write(CONDUCTOR, story, {"parent": story})
    with pytest.raises(wl.WorkLedgerError, match="not an item"):
        _create("orphan", {"parent": "it_00000000"})
    # A sub-task with no story would hold no seat and could never be bound.
    with pytest.raises(wl.WorkLedgerError, match="sits under a story"):
        _create("loose task", {"level": "task"})
    with pytest.raises(wl.WorkLedgerError, match="sits under a story"):
        wl.apply_plan_write(CONDUCTOR, task, {"parent": None})
    # A level change may not strand the items under it.
    loose = _create("loose story")
    _create("its task", {"level": "task", "parent": loose})
    for item_id, plan in ((loose, {"level": "epic"}), (epic, {"level": None})):
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_plan_write(CONDUCTOR, item_id, plan)
        assert caught.value.field == "plan.level"
    wl.apply_plan_write(CONDUCTOR, story, {"parent": other})
    assert wl.read_work_item(CONDUCTOR, story).plan["parent"] == other
    # A closed child takes no more writes, so it holds its parent's level for good,
    # and the refusal says so rather than advising a move that would be refused.
    done = _create("done story")
    older = _create("open task", {"level": "task", "parent": done})
    shipped = _create("shipped task", {"level": "task", "parent": done})
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=shipped, state="abandoned")
    # The open task lists first, so a first-match check would advise moving it.
    order = [i.item_id for i in wl.list_work_items(CONDUCTOR)]
    assert order.index(older) < order.index(shipped)
    with pytest.raises(wl.WorkLedgerError, match="closed and takes no further write") as caught:
        wl.apply_plan_write(CONDUCTOR, done, {"level": "epic"})
    assert caught.value.field == "plan.level"


def test_a_release_is_added_then_shipped_once() -> None:
    _board()
    wl.apply_release_write(CONDUCTOR, {"name": "0.5", "note": "after a bug hunt"})
    live = wl.apply_release_write(CONDUCTOR, {"name": "0.5", "state": "shipped"})["conductor"]
    (entry,) = live.releases
    stamp = entry["shipped_at"]
    assert entry["state"] == "shipped" and entry["note"] == "after a bug hunt" and stamp
    again = wl.apply_release_write(CONDUCTOR, {"name": "0.5", "state": "shipped"})["conductor"]
    assert again.releases[0]["shipped_at"] == stamp
    assert wl.read_conductor(CONDUCTOR).releases == again.releases
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_release_write(CONDUCTOR, {"name": "0.6", "state": "gone"})


def test_a_release_past_the_cap_is_refused_and_none_is_evicted() -> None:
    _board()
    for n in range(MAX_RELEASES):
        wl.apply_release_write(CONDUCTOR, {"name": f"0.{n}", "note": f"note {n}"})
    before = wl.read_conductor(CONDUCTOR).releases
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_release_write(CONDUCTOR, {"name": "1.0"})
    assert caught.value.field == "release.name"
    assert wl.read_conductor(CONDUCTOR).releases == before
    # A release already on the board still updates at the cap.
    wl.apply_release_write(CONDUCTOR, {"name": "0.0", "state": "shipped"})
    assert wl.read_conductor(CONDUCTOR).releases[0]["state"] == "shipped"


def test_a_person_rank_moves_only_the_rank() -> None:
    _board()
    item_id = _create("later", {"backlog": True, "rank": 3, "tags": ["bug"]})
    wl.apply_person_rank(CONDUCTOR, item_id, 0.5)
    assert wl.read_work_item(CONDUCTOR, item_id).plan == {
        "backlog": True,
        "rank": 0.5,
        "tags": ["bug"],
    }
    for bad in ("first", 10**400):
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_person_rank(CONDUCTOR, item_id, bad)
        assert caught.value.field == "plan.rank"
    held = _create("held in the sprint", {"backlog": False})
    bound = _create("a worker is on it")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=bound, worker_session_key="chat-9-w")
    for sprint in (held, bound):
        before = wl.read_work_item(CONDUCTOR, sprint).plan
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_person_rank(CONDUCTOR, sprint, 1)
        assert caught.value.field == "plan.backlog"
        assert wl.read_work_item(CONDUCTOR, sprint).plan == before
    # An epic marked backlog is ranked as it always was: the mark is what it reads.
    epic = _create("an epic for later", {"level": "epic", "backlog": True})
    wl.apply_person_rank(CONDUCTOR, epic, 2)
    assert wl.read_work_item(CONDUCTOR, epic).plan == {"level": "epic", "backlog": True, "rank": 2}


def test_the_board_counts_the_seats_the_cap_admission_counts() -> None:
    """A story with no plan and no worker is a backlog row, yet it holds a seat."""
    _board(cap=2)
    bound = _create("a worker is on it")
    waiting = _create("waiting, never planned")
    _create("marked for later", {"backlog": True})
    _create("an epic", {"level": "epic"})
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=bound, worker_session_key="chat-9-w")
    items = wl.list_work_items(CONDUCTOR)
    assert wl.count_seated(items) == 2
    assert wl.count_seated(items, exclude=waiting) == 1
    # The count is what the cap refuses on: no third seat, though one worker is on.
    with pytest.raises(wl.WorkLedgerError) as caught:
        _create("one more")
    assert caught.value.code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED


def test_a_story_no_worker_is_on_is_a_backlog_row_its_conductor_never_planned(
    monkeypatch,
) -> None:
    """Blick's board: no plan on any row. The waiting stories rank and pick as backlog."""
    # The unranked pick is the oldest, so each create needs its own second.
    _tick_the_clock(monkeypatch)
    _board(cap=8)
    first, second, third = _create("first"), _create("second"), _create("third")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=first, worker_session_key="chat-9-w")
    items = wl.list_work_items(CONDUCTOR)
    assert next_pick(items) == second
    wl.apply_person_rank(CONDUCTOR, third, 1)
    # Ranking writes only the rank: the row stays derived, and nothing else is stored.
    assert wl.read_work_item(CONDUCTOR, third).plan == {"rank": 1}
    assert wl.read_work_item(CONDUCTOR, second).plan is None
    assert next_pick(wl.list_work_items(CONDUCTOR)) == third
    # A derived backlog row binds as it always did: only the stored mark refuses.
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=second, worker_session_key="chat-8-w")
    assert next_pick(wl.list_work_items(CONDUCTOR)) == third


@pytest.mark.parametrize(
    ("plan", "state", "bound", "want"),
    [
        (None, "open", False, True),
        (None, "open", True, False),
        ({"rank": 3}, "open", False, True),
        ({"backlog": True}, "open", False, True),
        ({"backlog": False}, "open", False, False),
        ({"backlog": False}, "open", True, False),
        (None, "accepted", False, False),
        ({"backlog": True}, "abandoned", False, False),
        ({"level": "epic"}, "open", False, False),
        ({"level": "task", "parent": "it_00000001"}, "open", False, False),
        ({"level": "task", "backlog": True}, "open", False, False),
        ({"level": "story"}, "open", False, True),
    ],
)
def test_in_backlog_reads_the_mark_then_the_worker(plan, state, bound, want) -> None:
    assert in_backlog(plan, state=state, worker_bound=bound) is want


def test_a_stored_backlog_false_survives_the_plan_round_trip() -> None:
    assert coerce_plan({"backlog": False}) == {"backlog": False}
    assert coerce_plan({"backlog": "no"}) is None


def test_an_oversized_integer_rank_is_refused_not_raised() -> None:
    assert rank_value(10**400) is None
    assert plan_problem({"rank": 10**400})[0] == "plan.rank"
    assert coerce_plan({"rank": 10**400, "step": "build"}) == {"step": "build"}


def test_no_bind_writer_binds_an_item_that_holds_no_seat() -> None:
    _board(cap=4)
    epic = _create("epic", {"level": "epic"})
    story = _create("story", {"parent": epic})
    later = _create("later", {"backlog": True})
    task = _create("task", {"level": "task", "parent": story})
    for item_id, field in ((later, "plan.backlog"), (epic, "plan.level"), (task, "plan.level")):
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_remote_bind(
                CONDUCTOR, item_id, worker_session_key="chat-1-peer", worker_instance_id="crew-b"
            )
        assert caught.value.field == field
        assert wl.read_work_item(CONDUCTOR, item_id).worker_session_key is None
    wl.apply_remote_bind(
        CONDUCTOR, story, worker_session_key="chat-1-peer", worker_instance_id="crew-b"
    )


@pytest.mark.asyncio
async def test_the_hub_refuses_a_seatless_item_before_it_spawns_a_peer_slot() -> None:
    _board()
    later = _create("later", {"backlog": True})
    with pytest.raises(remote_dispatch_routes.CrewCallError) as caught:
        await remote_ingest.open_unbound_item(CONDUCTOR, later)
    assert caught.value.status == 409 and caught.value.code == wl.CODE_INVALID_VALUE


# -- the next pick --------------------------------------------------------------


def test_the_next_pick_is_the_lowest_ranked_open_backlog_story() -> None:
    rows = [
        {"item_id": "it_00000001", "state": "open", "plan": {"backlog": True}, "created_at": "1"},
        {"item_id": "it_00000002", "state": "open", "plan": {"backlog": True, "rank": 5}},
        {"item_id": "it_00000003", "state": "open", "plan": {"backlog": True, "rank": 2}},
        {"item_id": "it_00000004", "state": "open", "plan": {"level": "epic", "backlog": True}},
        {"item_id": "it_00000005", "state": "done", "plan": {"backlog": True, "rank": 0}},
        {"item_id": "it_00000006", "state": "open", "plan": {"backlog": False, "rank": 1}},
        {"item_id": "it_00000008", "state": "open", "plan": {"rank": 0}, "worker_session_key": "w"},
        {"item_id": "it_00000009", "state": "open", "created_at": "2"},
        {"item_id": "it_00000007", "state": "open", "plan": {"level": "task", "backlog": True}},
    ]
    rows[-1]["plan"]["rank"] = 0
    assert next_pick(rows) == "it_00000003"
    # Unranked, a story with no plan and no worker sorts by age among the marked ones.
    assert next_pick([rows[0], rows[7]]) == "it_00000001"
    assert next_pick(rows[5:8]) == "it_00000009"
    assert next_pick(rows[:1]) == "it_00000001"
    assert next_pick([]) is None


# -- the patrol: an item no worker owes a report on wakes nobody ---------------


def test_an_unbound_item_that_holds_no_seat_is_never_stale() -> None:
    _board(cap=4)
    epic = _create("epic", {"level": "epic"})
    story = _create("story", {"parent": epic})
    later = _create("later", {"backlog": True})
    task = _create("task", {"level": "task", "parent": story})
    # Any moment past every item's window: the items were stamped by the real clock.
    later_still = datetime(2100, 1, 1, tzinfo=timezone.utc)
    stale = {
        item_id: is_stale(
            wl.read_work_item(CONDUCTOR, item_id), worker_running=False, now=later_still
        )
        for item_id in (epic, later, task, story)
    }
    assert stale == {epic: False, later: False, task: False, story: True}


def test_the_person_wait_hold_ignores_seatless_items_until_a_seat_frees() -> None:
    _board(cap=1)
    _create("epic", {"level": "epic"})
    asking = _create("asking")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=asking, worker_session_key=WORKER)
    wl.apply_worker_report(
        CONDUCTOR, asking, status="question", summary="which?", reason="needs_human"
    )
    _create("later", {"backlog": True})
    # The only seat waits on the person, so the backlog cannot move either: held.
    assert probe.person_wait_fingerprint(CONDUCTOR) is not None
    # A free seat makes the backlog story the conductor's next move.
    wl.apply_conductor_action(CONDUCTOR, "goal", concurrency_cap=2)
    assert probe.person_wait_fingerprint(CONDUCTOR) is None


def test_a_torn_plan_keeps_its_good_keys() -> None:
    assert coerce_plan({"level": "saga", "step": "build", "rank": "x"}) == {"step": "build"}
    assert coerce_plan({}) is None and coerce_plan("story") is None


# -- the record: what the routes commit is what a rebuild restores -------------


@pytest.mark.asyncio
async def test_every_plan_write_rides_the_record_and_a_rebuild_restores_it(monkeypatch) -> None:
    _real_units(monkeypatch)
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR,
        {
            "action": "create",
            "title": "epic",
            "acceptance": ACCEPTANCE,
            "plan": {"level": "epic"},
        },
    )
    assert status == 200, body
    epic = body["item"]["item_id"]
    status, body = await _record(
        CONDUCTOR,
        {
            "action": "create",
            "title": "story",
            "acceptance": ACCEPTANCE,
            "plan": {"parent": epic, "backlog": True, "rank": 1, "tags": ["ui"]},
        },
    )
    assert status == 200, body
    story = body["item"]["item_id"]
    status, body = await _record(
        CONDUCTOR, {"action": "plan", "item_id": story, "plan": {"step": "mocks"}}
    )
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR, {"action": "release", "release": {"name": "0.5", "state": "shipped"}}
    )
    assert status == 200, body
    monkeypatch.setattr(crew_board_rank, "_steer_unit", lambda state, keys, key: "u-conductor")
    plan, code, message = await crew_board_rank.record_person_rank(object(), CONDUCTOR, story, 0.25)
    assert plan is not None, (code, message)

    committed = wl.read_work_item(CONDUCTOR, story).plan
    assert committed == {
        "parent": epic,
        "rank": 0.25,
        "backlog": True,
        "step": "mocks",
        "tags": ["ui"],
    }
    releases = wl.read_conductor(CONDUCTOR).releases
    wl.rebuild_from_projection(CONDUCTOR)
    assert wl.read_work_item(CONDUCTOR, story).plan == committed
    assert wl.read_work_item(CONDUCTOR, epic).plan == {"level": "epic"}
    assert wl.read_conductor(CONDUCTOR).releases == releases

    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": story, "worker_session_key": WORKER}
    )
    assert status != 200 and body.get("field") == "plan.backlog", body
    status, body = await _record(
        CONDUCTOR, {"action": "plan", "item_id": story, "plan": {"backlog": None, "rank": None}}
    )
    assert status == 200, body
    wl.rebuild_from_projection(CONDUCTOR)
    assert wl.read_work_item(CONDUCTOR, story).plan == {
        "parent": epic,
        "step": "mocks",
        "tags": ["ui"],
    }


# -- the rank route: the person's drag, over HTTP -----------------------------

OWNER = "owner-subject"


async def _rank_post(item_id: str, rank: Any, *, user: str = OWNER) -> tuple[int, dict[str, Any]]:
    state = SimpleNamespace(get_slot=lambda key: None, owner_id=OWNER)
    request = make_mocked_request("POST", "/api/crew-board/action", app={"state": state})
    request["user"] = user
    request["app"] = ""
    body = {"conductor": CONDUCTOR, "item_id": item_id, "action": "rank", "rank": rank}

    async def _json() -> dict[str, Any]:
        return body

    request.json = _json
    response = await board_routes.api_work_ledger_board_action(request)
    return response.status, json.loads(response.body.decode())


@pytest.mark.asyncio
async def test_the_rank_route_moves_a_backlog_row_and_refuses_the_rest(monkeypatch) -> None:
    _real_units(monkeypatch)
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    ids = {}
    for title, plan in (
        ("later", {"backlog": True, "rank": 2}),
        ("now", {"backlog": False}),
        ("waiting", None),
    ):
        payload = {"action": "create", "title": title, "acceptance": ACCEPTANCE}
        status, body = await _record(CONDUCTOR, {**payload, **({"plan": plan} if plan else {})})
        assert status == 200, body
        ids[title] = body["item"]["item_id"]
    monkeypatch.setattr(crew_board_rank, "_steer_unit", lambda state, keys, key: "u-conductor")

    status, body = await _rank_post(ids["later"], 0.5)
    assert status == 200 and body["plan"] == {"backlog": True, "rank": 0.5}, body
    # A story its conductor never planned, with no worker, is a backlog row too.
    status, body = await _rank_post(ids["waiting"], 3)
    assert status == 200 and body["plan"] == {"rank": 3}, body
    wl.rebuild_from_projection(CONDUCTOR)
    assert wl.read_work_item(CONDUCTOR, ids["later"]).plan == {"backlog": True, "rank": 0.5}
    assert wl.read_work_item(CONDUCTOR, ids["now"]).plan == {"backlog": False}
    assert wl.read_work_item(CONDUCTOR, ids["waiting"]).plan == {"rank": 3}

    for item_id, rank, want in (
        (ids["later"], "first", (400, "invalid_value")),
        (ids["later"], 10**400, (400, "invalid_value")),
        (ids["now"], 1, (400, "invalid_value")),
        ("it_00000000", 1, (404, "item_not_found")),
    ):
        status, body = await _rank_post(item_id, rank)
        assert (status, body.get("code")) == want, body
    assert wl.read_work_item(CONDUCTOR, ids["later"]).plan == {"backlog": True, "rank": 0.5}

    status, _ = await _rank_post(ids["later"], 9, user="someone-else")
    assert status == 403
    assert wl.read_work_item(CONDUCTOR, ids["later"]).plan["rank"] == 0.5


@pytest.mark.parametrize(
    ("plan", "pr", "state", "acceptance", "want"),
    [
        # A report that carried a pull request reaches review.
        ({"step": "build"}, 7, "open", None, "review"),
        ({"backlog": True}, 7, "open", None, "review"),
        # Accepted with a pull request, or on a pr_merged bar, reaches merged.
        ({"step": "review"}, 7, "accepted", None, "merged"),
        ({"step": "build"}, None, "accepted", {"kind": "pr_merged", "pr": 7}, "merged"),
        # A closed item that was not accepted keeps its stored step.
        ({"step": "build"}, 7, "rejected", None, "build"),
        ({"step": "build"}, 7, "abandoned", None, "build"),
        # Accepted with no pull request anywhere moves nothing.
        ({"step": "build"}, None, "accepted", {"kind": "file", "path": "x"}, "build"),
        # A merged item whose release shipped reaches released; a planned one waits.
        ({"step": "merged", "release": "0.5"}, None, "open", None, "released"),
        ({"release": "0.5"}, 7, "accepted", None, "released"),
        ({"step": "merged", "release": "0.6"}, None, "open", None, "merged"),
        ({"step": "review", "release": "0.5"}, 7, "open", None, "review"),
        # A step set by hand that is further along is never moved back.
        ({"step": "merged"}, 7, "open", None, "merged"),
        ({"step": "released"}, 7, "accepted", None, "released"),
        # Nothing happened: the stored step, or none.
        ({"step": "mocks"}, None, "open", None, "mocks"),
        ({"backlog": True}, None, "open", None, None),
    ],
)
def test_the_shown_step_moves_forward_on_what_happened(plan, pr, state, acceptance, want) -> None:
    releases = [{"name": "0.5", "state": "shipped"}, {"name": "0.6", "state": "planned"}]
    got = shown_step(plan, pr=pr, state=state, acceptance=acceptance, releases=releases)
    assert got == want


def test_a_row_with_no_plan_is_left_unplanned() -> None:
    row = {"pr": 7, "state": "accepted", "acceptance": None}
    show_step(row, [])
    assert "plan" not in row
    planned = {"pr": 7, "state": "open", "acceptance": None, "plan": {"rank": 1.0}}
    show_step(planned, [])
    assert planned["plan"] == {"rank": 1.0, "step": "review"}


@pytest.mark.parametrize(
    ("plan", "pr", "state", "bound", "merged", "want"),
    [
        # The Board's read: a story's step from its facts, with no plan written.
        (None, None, "open", False, False, "todo"),
        (None, None, "open", True, False, "build"),
        (None, 7, "open", True, False, "review"),
        (None, 7, "open", True, True, "merged"),
        # GitHub's merged on a closed item says nothing a close did not.
        (None, None, "abandoned", True, True, None),
        # A hand-written step wins where it is further along, and Mocks is design
        # work its bound worker does: Build never passes it. A pull request still does.
        ({"step": "build"}, None, "open", False, False, "build"),
        ({"step": "mocks"}, None, "open", True, False, "mocks"),
        ({"step": "mocks"}, 7, "open", True, False, "review"),
        ({"step": "todo"}, None, "open", True, False, "build"),
        # An epic and a sub-task are never bound: no To do or Build of their own.
        ({"level": "epic"}, None, "open", False, False, None),
        ({"level": "task", "parent": "it_00000001"}, None, "open", False, False, None),
    ],
)
def test_the_board_s_step_fills_from_facts(plan, pr, state, bound, merged, want) -> None:
    row = {"pr": pr, "state": state, "acceptance": None, **({"plan": plan} if plan else {})}
    show_step(row, [], worker_bound=bound, pr_merged=merged)
    assert (row.get("plan") or {}).get("step") == want
