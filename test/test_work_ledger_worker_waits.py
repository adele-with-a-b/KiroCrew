"""A bound worker that stops on the person without reporting it wakes its conductor.

The gateway files the ``question`` report the worker did not
(``work_ledger_waits``): a turn that ends on a Q-label line or an ``[OPTIONS: ...]``
line, a tool approval it is parked on, a question card it put up. Pinned here: the
item moves to ``question`` with a person reason, the conductor's ``work-ledger`` probe
wakes on it, the Board's ``awaits_person`` reads it, the person's answer clears it,
and nothing is filed when a report already holds the conductor, when the same
question is asked again, when a person started the turn, or for an unbound session.
Reuses the two-conductor fixture of ``test_work_ledger_tools``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_work_ledger_tools import (  # noqa: F401 - the autouse fixtures ride along
    CONDUCTOR_A,
    CONDUCTOR_B,
    WORKER_A,
    _clean_slots,
    _isolated_home,
    _open_route,
    _record,
    _report,
    two_by_two,
)

from conftest import assert_rejected_without_backtracking
from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.chat_runner import _note_worker_question
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.dashboard.handlers import work_ledger_waits as waits
from kiro_crew.dashboard.handlers import work_ledger_worker as worker_routes
from kiro_crew.dashboard.handlers.work_ledger_board import awaits_person

#: A real worker reply that ended its turn on a question and filed no report.
BLICK_REPLY = (
    "Status update:\n\n| Job | State |\n|---|---|\n| Baseline suites | stopped |\n\n"
    "[R2] A test home of 4.0 GB was left behind. My delete was refused.\n\n"
    "[Q2a] May I delete that 4.0 GB test home? I'd say yes. Nothing reads it now."
)


@pytest.fixture(autouse=True)
def _steer_into_the_board(_floor_monkeypatch):
    # A person's answer resolves a live crew-log unit; a mocked state has none.
    _floor_monkeypatch.setattr(worker_routes, "_steer_unit", lambda state, keys, board: "unit:x")


def _state() -> Any:
    state = MagicMock()
    state._background_tasks = set()
    return state


async def _settle(state: Any) -> None:
    """Await the ledger writes the hooks scheduled, bounded."""
    tasks = list(state._background_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)


def _turn(
    state: Any,
    text: str,
    *,
    person: bool = False,
    stop: str = "end_turn",
    refused: list[tuple[str, str]] | None = None,
) -> Any:
    meta: dict[str, Any] = {"human": True} if person else {"relay": {"via": "session_send"}}
    return SimpleNamespace(
        _stop_reason=stop,
        _noticed_leak=False,
        _refusal_reasons=list(refused or []),
        assistant_text=text,
        _current_message={"role": "user", "content": "go on", "meta": meta},
        state=state,
        slot=SimpleNamespace(key=WORKER_A),
    )


def _item(board: str, item_id: str) -> wl.WorkItem:
    item = wl.read_work_item(board, item_id)
    assert item is not None
    return item


def _events(board: str, item_id: str) -> list[dict]:
    return [e.to_dict() for e in wl.read_events(board, item_id)]


def _reports(board: str, item_id: str) -> list[dict]:
    return [e for e in _events(board, item_id) if e.get("kind") == "report"]


def _waits(board: str, item_id: str) -> bool:
    return awaits_person(_item(board, item_id), _events(board, item_id))


def _conductor_wakes(board: str) -> str:
    """What the conductor's ``work-ledger`` watch would wake it with, this tick."""
    import json

    from kiro_crew.probes.work_ledger import WorkLedgerProbe

    ctx = SimpleNamespace(message=json.dumps({"conductor": board}), job=SimpleNamespace(id="l"))
    probe = WorkLedgerProbe(worker_running=lambda _key: False)
    probe.identity(ctx)
    return "\n".join(o.brief for o in probe.observe(ctx).observations)


async def _moving() -> dict[str, str]:
    ids = await two_by_two()
    status, _ = await _report(WORKER_A, {"status": "progress", "summary": "Suites running."})
    assert status == 200
    # A progress report wakes nobody: the item reads "moving" from here on.
    assert f"item={ids['item_a']}" not in _conductor_wakes(CONDUCTOR_A)
    return ids


@pytest.mark.asyncio
async def test_a_turn_ending_on_a_q_label_wakes_its_conductor_at_question():
    ids = await _moving()
    before_b = wl.item_path(CONDUCTOR_B, ids["item_b"]).read_bytes()
    state = _state()

    _note_worker_question(_turn(state, BLICK_REPLY))
    await _settle(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.reason) == ("question", "needs_human")
    assert "[Q2a] May I delete that 4.0 GB test home?" in item.summary
    assert item.summary.startswith("Gateway:")
    assert item.artifacts == {waits.WAIT_ARTIFACT: waits.WAIT_IN_TEXT}
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True
    wakes = _conductor_wakes(CONDUCTOR_A)
    assert f"item={ids['item_a']}" in wakes and "status=question" in wakes
    # The other conductor's ledger is byte-identical.
    assert wl.item_path(CONDUCTOR_B, ids["item_b"]).read_bytes() == before_b


@pytest.mark.asyncio
async def test_a_call_a_policy_blocked_in_that_turn_is_filed_beside_the_question():
    """A hard block puts up no approval: the report is where the conductor sees it."""
    ids = await _moving()
    state = _state()
    blocked = ("rm -rf /tmp/x/h-render", "Blocked by security policy: rm -rf /.*")

    _note_worker_question(_turn(state, BLICK_REPLY, refused=[blocked]))
    await _settle(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert item.status == "question"
    assert item.artifacts[waits.WAIT_ARTIFACT] == waits.WAIT_IN_TEXT
    assert item.artifacts[waits.REFUSED_ARTIFACT] == (
        "rm -rf /tmp/x/h-render — Blocked by security policy: rm -rf /.*"
    )


def test_the_refused_calls_fit_one_artifact_value():
    text = routes._refused_text(["x" * 400, "y" * 400])
    assert len(text) == wl.MAX_ARTIFACT_VALUE_CHARS and text.endswith("…")
    assert routes._refused_text([" ", ""]) == ""


@pytest.mark.asyncio
async def test_a_turn_ending_on_an_options_line_files_its_choices():
    ids = await _moving()
    state = _state()

    _note_worker_question(_turn(state, "Two ways on.\n\n[OPTIONS: Merge it now | Wait for CI]"))
    await _settle(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert item.status == "question"
    assert "Choices: Merge it now | Wait for CI" in item.summary


@pytest.mark.asyncio
async def test_a_pending_approval_wakes_its_conductor_and_the_answer_clears_it():
    ids = await _moving()
    state = _state()

    routes.note_approval_waiting(state, WORKER_A)
    await _settle(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.reason) == ("question", "approval")
    assert item.artifacts == {waits.WAIT_ARTIFACT: waits.WAIT_ON_APPROVAL}
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True
    wakes = _conductor_wakes(CONDUCTOR_A)
    assert f"item={ids['item_a']}" in wakes and "status=question" in wakes

    # The person answers the approval in the worker's chat: the existing approval
    # answer path clears it, because the report carries reason ``approval``.
    assert await worker_routes.record_approval_answered(object(), WORKER_A) == ids["item_a"]
    assert _waits(CONDUCTOR_A, ids["item_a"]) is False


@pytest.mark.asyncio
async def test_a_question_card_wakes_its_conductor_and_its_answer_clears_it():
    ids = await _moving()
    state = _state()

    routes.note_question_card(state, WORKER_A, [{"question": "Which account?", "options": []}])
    await _settle(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.reason) == ("question", "needs_human")
    assert "Which account?" in item.summary
    assert _waits(CONDUCTOR_A, ids["item_a"]) is True

    assert await routes.record_question_card_answered(object(), WORKER_A) == ids["item_a"]
    assert _waits(CONDUCTOR_A, ids["item_a"]) is False
    # A second answer to the same card writes nothing more.
    assert await routes.record_question_card_answered(object(), WORKER_A) == ""


@pytest.mark.asyncio
async def test_the_same_question_asked_again_is_filed_once_and_a_new_one_replaces_it():
    ids = await _moving()
    state = _state()

    _note_worker_question(_turn(state, BLICK_REPLY))
    await _settle(state)
    _note_worker_question(_turn(state, "Grid done.\n\n" + BLICK_REPLY.rsplit("\n\n", 1)[1]))
    await _settle(state)
    assert len(_reports(CONDUCTOR_A, ids["item_a"])) == 2  # progress, then one question

    _note_worker_question(_turn(state, "[Q3a] Ship the grid fixes in this PR? Yes."))
    await _settle(state)
    assert len(_reports(CONDUCTOR_A, ids["item_a"])) == 3
    assert "[Q3a]" in _item(CONDUCTOR_A, ids["item_a"]).summary


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["question", "blocked", "done"])
async def test_a_report_of_the_workers_own_that_holds_the_conductor_stands(status):
    ids = await two_by_two()
    body: dict[str, Any] = {"status": status, "summary": "My own words."}
    if status == "done":
        body["artifacts"] = {"pr": "1"}
    assert (await _report(WORKER_A, body))[0] == 200
    state = _state()

    _note_worker_question(_turn(state, BLICK_REPLY))
    routes.note_approval_waiting(state, WORKER_A)
    await _settle(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.summary) == (status, "My own words.")


@pytest.mark.asyncio
async def test_a_done_that_failed_its_check_is_the_workers_move_so_a_question_is_filed():
    ids = await two_by_two()
    done = {"status": "done", "summary": "Shipped.", "artifacts": {"pr": "1"}}
    assert (await _report(WORKER_A, done))[0] == 200
    verdict = {"action": "verdict", "item_id": ids["item_a"], "verdict": "fail", "fails": 1}
    assert (await _record(CONDUCTOR_A, verdict))[0] == 200
    state = _state()

    _note_worker_question(_turn(state, BLICK_REPLY))
    await _settle(state)

    assert _item(CONDUCTOR_A, ids["item_a"]).status == "question"


@pytest.mark.asyncio
async def test_a_fresh_done_after_a_failed_check_stands():
    """The round-1 ``fail`` verdict stays on the item; the round-2 ``done`` is unchecked."""
    ids = await two_by_two()
    done = {"status": "done", "summary": "Shipped.", "artifacts": {"pr": "1"}}
    assert (await _report(WORKER_A, done))[0] == 200
    verdict = {"action": "verdict", "item_id": ids["item_a"], "verdict": "fail", "fails": 1}
    assert (await _record(CONDUCTOR_A, verdict))[0] == 200
    again = {"status": "done", "summary": "Reworked.", "artifacts": {"pr": "2"}}
    assert (await _report(WORKER_A, again))[0] == 200
    state = _state()

    _note_worker_question(_turn(state, BLICK_REPLY))
    await _settle(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.summary) == ("done", "Reworked.")


@pytest.mark.asyncio
async def test_a_report_landing_before_the_lock_is_read_again_not_overwritten(monkeypatch):
    """The worker's newer discussion is kept: the gateway builds again from it."""
    ids = await _moving()
    real_record = worker_routes._record_report
    calls = {"n": 0}

    async def report_first(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            newer = {"status": "progress", "summary": "Asking.", "discussion": {"phase": "asking"}}
            assert (await _report(WORKER_A, newer))[0] == 200
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(waits, "_record_report", report_first)
    state = _state()

    assert (
        await routes.record_worker_wait(
            state, WORKER_A, where=waits.WAIT_IN_TEXT, question="[Q1a] Go?"
        )
        == ids["item_a"]
    )

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert calls["n"] == 2 and item.status == "question"
    assert (item.discussion or {}).get("phase") == "asking"


@pytest.mark.asyncio
async def test_the_workers_own_artifacts_and_discussion_ride_along():
    ids = await two_by_two()
    own = {"branch": "worker-question", "commit": "d54ed408f9"}
    discussion = {"phase": "asking"}
    body = {"status": "progress", "summary": "Fixing.", "artifacts": own, "discussion": discussion}
    assert (await _report(WORKER_A, body))[0] == 200
    kept = _item(CONDUCTOR_A, ids["item_a"]).discussion
    state = _state()

    _note_worker_question(_turn(state, BLICK_REPLY))
    await _settle(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert item.status == "question"
    assert item.artifacts == {**own, waits.WAIT_ARTIFACT: waits.WAIT_IN_TEXT}
    assert item.discussion == kept


def test_the_gateway_keys_keep_their_seat_in_a_full_artifact_map():
    own = {f"k{n}": "v" for n in range(wl.MAX_ARTIFACT_KEYS)}
    merged = routes._with_gateway_keys(own, {waits.WAIT_ARTIFACT: waits.WAIT_IN_TEXT})
    assert len(merged) == wl.MAX_ARTIFACT_KEYS
    assert merged[waits.WAIT_ARTIFACT] == waits.WAIT_IN_TEXT


@pytest.mark.asyncio
async def test_a_worker_cannot_write_the_gateways_marker():
    """A worker's own question with the marker would read as the gateway's and be replaced."""
    ids = await two_by_two()
    spoof = {waits.WAIT_ARTIFACT: waits.WAIT_IN_TEXT, waits.REFUSED_ARTIFACT: "x", "pr": "1"}
    body = {"status": "question", "summary": "My own question.", "artifacts": spoof}
    assert (await _report(WORKER_A, body))[0] == 200
    assert _item(CONDUCTOR_A, ids["item_a"]).artifacts == {"pr": "1"}
    state = _state()

    _note_worker_question(_turn(state, BLICK_REPLY))
    await _settle(state)

    item = _item(CONDUCTOR_A, ids["item_a"])
    assert (item.status, item.summary) == ("question", "My own question.")


@pytest.mark.asyncio
async def test_nothing_is_filed_for_a_person_turn_a_plain_reply_or_a_stop():
    ids = await _moving()
    state = _state()

    _note_worker_question(_turn(state, BLICK_REPLY, person=True))
    _note_worker_question(_turn(state, "Suites still running; the grid is done."))
    _note_worker_question(_turn(state, BLICK_REPLY, stop="cancelled"))
    await _settle(state)

    assert _item(CONDUCTOR_A, ids["item_a"]).status == "progress"


@pytest.mark.asyncio
async def test_an_unbound_session_files_nothing():
    await two_by_two()
    state = _state()

    assert (
        await routes.record_worker_wait(
            state, "chat-not-a-worker", where=waits.WAIT_IN_TEXT, question="[Q1a] x?"
        )
        == ""
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[Q2a] May I delete it? Yes.", "[Q2a] May I delete it? Yes."),
        ("- **[Q3b]** Ship it? Yes.", "[Q3b] Ship it? Yes."),
        ("## Q4a Which name?", "[Q4a] Which name?"),
        ("[Q1a] One?\n[Q1b] Two?", "[Q1a] One? [Q1b] Two?"),
        ("the merge [Q2a] answer is in", ""),
        ("```\n[Q1a] in a code block\n```", ""),
        ("[R1] A finding, not a question.", ""),
        ("Done.\n\n[OPTIONS: Go | Stop]", "Choices: Go | Stop"),
        ("All green, merged.", ""),
    ],
)
def test_what_counts_as_a_question_in_a_reply(text, expected):
    assert routes.asked_in_text(text) == expected


def test_a_long_blank_line_is_read_in_linear_time():
    def reject(text: str) -> None:
        assert routes.asked_in_text(text) == ""

    assert_rejected_without_backtracking(reject, lambda n: " " * n + "- Q [Q1")
    assert_rejected_without_backtracking(reject, lambda n: "- " + " " * n + "## Q" + " " * n)
