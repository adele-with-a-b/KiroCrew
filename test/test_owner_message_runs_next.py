"""The owner's queued message runs next, ahead of every row no person sent.

A Conductor's queue fills with rows agents send it: workers' notes and loop
wakes. The owner's message must not wait behind them, however it came to be
queued, and a wake or note the ending turn never read must not be put back
ahead of it:

* **Queued, any cause.** ``queue_for_next_turn`` puts an owner's entry at the
  front with his earlier waiting entries, in his order: his own Queue pick, a
  steer refused for any reason, not only ``steer_refusal.NOT_OWNERS_TURN``.
* **Held on an idle slot.** The send route's hold branch moves his send the
  same way.
* **Put back.** ``_requeue_unconsumed_steers`` puts a cut-in the turn did not
  read after the owner's entries at the front, never at index 0 ahead of them;
  his own unread steer goes behind his waiting entries, in the order he sent.

What still runs before him: the runner's own retry of the turn that already ran,
and a sub-agent completion queued before his first entry.

The breaking case runs through the real seams: notes through the peer delivery
(``session_deliver.deliver``), the wake steered into the running turn through the
same delivery and requeued by the real teardown step, the owner's Queue pick
through ``queue_for_next_turn``, and every turn through ``_start_next_queued_turn``
with only the model stubbed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from test_typed_message_runs_alone import _drain, _note, _prompts, _slot

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_deliver
from kiro_crew.dashboard.chat_delivery import queue_for_next_turn
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    _dequeue_next_message,
)
from kiro_crew.dashboard.chat_utils_injected_rows import SYNTHETIC_RECOVERY_KIND
from kiro_crew.dashboard.slot_queue_repository import (
    RESTORED_QUEUE_KEY,
    RESTORED_SEND_CHOSEN_KEY,
    after_owner_front,
    is_owner_queue_entry,
    put_owner_entries_first,
)

WAKE = "[work-ledger wake] item=it_1 status=question"
OWNER = "Q1a Go."


def _queue_pick(state, slot, text: str = OWNER) -> str:
    """The owner picks Queue in the composer: no steer was tried, so no refusal code."""
    return queue_for_next_turn(
        state,
        slot,
        text,
        directive_user_origin=True,
        directive_owner_origin=True,
        composer_send=True,
    )


def _steerable(slot) -> None:
    """The running turn takes steers and has not read any yet."""
    slot._acp_client = MagicMock(supports_steer=True, steer_needs_loss_recovery=False)
    slot._acp_client.steer = AsyncMock(return_value=True)
    slot._acp_client.has_active_turn = lambda: True


async def _wake_cut_in(state, slot) -> None:
    """A work-ledger wake steered into the running turn, which never reads it."""
    outcome = await session_deliver.deliver(
        state,
        slot,
        WAKE,
        session_deliver.automation(1, "loop-ledger"),
        admission=sc.containment_meta(state, slot),
    )
    assert outcome == session_deliver.DELIVERED_STEERED
    assert slot._pending_steers == [WAKE]


def _turn_ends_unread(state, slot) -> None:
    """The running turn ends without reading its cut-ins: the real teardown step."""
    cr._requeue_unconsumed_steers(state, slot)
    assert slot._pending_steers == []


@pytest.mark.asyncio
@pytest.mark.parametrize("merge", [False, True])
@pytest.mark.parametrize("owner_first", [True, False])
async def test_the_owners_queue_pick_runs_before_three_notes_and_a_requeued_wake(
    tmp_path, merge: bool, owner_first: bool
) -> None:
    """The reported case. Three workers' notes wait, a wake cut into the running
    turn was never read, and the owner picks Queue. Whichever came first, his
    message is the next turn, alone, as his own; the rest keep their order."""
    turns: list[tuple[str, bool, str]] = []
    state, slot = _slot(tmp_path, turns)
    _steerable(slot)
    for n in (1, 2, 3):
        await _note(state, slot, f"worker note {n}")
    await _wake_cut_in(state, slot)
    if owner_first:
        # He queues while the turn runs; the turn then ends with the wake unread.
        _queue_pick(state, slot)
        _turn_ends_unread(state, slot)
    else:
        # The wake is already back in the queue when he queues.
        _turn_ends_unread(state, slot)
        _queue_pick(state, slot)
    assert is_owner_queue_entry(slot._queue[0]) and slot._queue[0]["content"] == OWNER
    slot._acp_client = None

    await _drain(state, slot, merge=merge)

    first, first_mark, first_gate = turns[0]
    assert first.endswith(OWNER), "his message is the next turn"
    assert "worker note" not in first and WAKE not in first, "and it runs alone"
    assert (first_mark, first_gate) == (True, ""), "as his own turn"
    rest = _prompts(turns)[1:]
    assert WAKE in rest[0], "the unread wake comes right after him"
    joined = "\n".join(rest)
    positions = [joined.index(f"worker note {n}") for n in (1, 2, 3)]
    assert positions == sorted(positions), "the notes keep their order"
    assert all(mark is False for _, mark, _ in turns[1:])
    assert slot.queue_depth == 0


def test_an_owner_send_queued_for_any_cause_goes_ahead_of_waiting_notes(tmp_path) -> None:
    """No refusal code at all, and an earlier owner entry behind a note still
    comes forward with it, in his order; open tabs get the order."""
    turns: list = []
    state, slot = _slot(tmp_path, turns)
    slot.queue_append("note one")
    _queue_pick(state, slot, "do A")
    slot.queue_append("note two")
    state.broadcast_ws.reset_mock()

    _queue_pick(state, slot, "then B")

    assert [item["content"] for item in slot._queue] == ["do A", "then B", "note one", "note two"]
    frames = [c.args for c in state.broadcast_ws.call_args_list]
    assert [f[0] for f in frames] == ["queue_push", "queue_reorder"]
    assert frames[1][1]["order"] == [item["id"] for item in slot._queue]


def test_only_the_owners_own_marks_move_an_entry(tmp_path) -> None:
    """An app, cron or crew send stamped with an actor, a person who is not the
    owner, and a channel's text all stay where they were queued."""
    turns: list = []
    state, slot = _slot(tmp_path, turns)
    slot.queue_append("a waiting note")
    for actor in ("app", "cron", "crew"):
        queue_for_next_turn(
            state,
            slot,
            f"an {actor} send",
            directive_user_origin=True,
            directive_owner_origin=True,
            turn_actor=actor,
        )
    queue_for_next_turn(state, slot, "a person, not the owner", directive_user_origin=True)
    queue_for_next_turn(
        state,
        slot,
        "a channel's",
        directive_user_origin=True,
        directive_owner_origin=True,
        directive_channel_origin=True,
    )
    assert slot._queue[0]["content"] == "a waiting note"
    assert not any(is_owner_queue_entry(item) for item in slot._queue)


def test_the_runners_retry_of_the_turn_that_ran_stays_ahead_of_him(tmp_path) -> None:
    """A retry at the head finishes the turn that already ran; he runs after it."""
    turns: list = []
    state, slot = _slot(tmp_path, turns)
    slot.queue_insert(0, "retry the turn", kind=SYNTHETIC_RECOVERY_KIND)
    slot.queue_append("a waiting note")

    _queue_pick(state, slot)

    assert [item["content"] for item in slot._queue] == [
        "retry the turn",
        OWNER,
        "a waiting note",
    ]


@pytest.mark.asyncio
async def test_a_held_subagent_completion_still_runs_before_him(tmp_path) -> None:
    """His message goes ahead of a waiting note but behind a queued sub-agent
    completion, and the drain's hold takes the completion first: moving him
    never skips that hold."""
    turns: list = []
    state, slot = _slot(tmp_path, turns)
    await _note(state, slot, "a waiting note")
    slot.queue_append("[sub-agent done] child finished", kind=SUBAGENT_COMPLETION_KIND)
    queue_for_next_turn(state, slot, OWNER, directive_user_origin=True, directive_owner_origin=True)
    assert [item["content"] for item in slot._queue] == [
        "[sub-agent done] child finished",
        OWNER,
        "a waiting note",
    ]

    content, consumed = cr._dequeue_next_system_message(slot)

    assert [item["kind"] for item in consumed] == [SUBAGENT_COMPLETION_KIND]
    assert [item["content"] for item in slot._queue] == [OWNER, "a waiting note"]


@pytest.mark.asyncio
async def test_a_requeued_note_and_wake_go_after_his_entries_in_their_order(tmp_path) -> None:
    """His messages run in the order he sent them: his unread steer goes back
    behind his two waiting messages. Two cut-ins the turn never read, a peer's
    note then a wake, go behind all of his, ahead of the older queued note, in
    the order they cut in. Open tabs get the order."""
    turns: list = []
    state, slot = _slot(tmp_path, turns)
    _queue_pick(state, slot, "do A")
    _queue_pick(state, slot, "then B")
    slot.queue_append("older note")
    for text, owner in (("peer cut-in", False), ("his steer", True), (WAKE, False)):
        slot._pending_steers.append(text)
        slot._steer_user_origin[text] = owner
        slot._steer_owner_origin[text] = owner
        slot._steer_admissions[text] = sc.containment_meta(state, slot)
    slot._steer_automation[WAKE] = {"cycle": 1, "loop_id": "loop-ledger"}
    state.broadcast_ws.reset_mock()

    _turn_ends_unread(state, slot)

    assert [item["content"] for item in slot._queue] == [
        "do A",
        "then B",
        "his steer",
        "peer cut-in",
        WAKE,
        "older note",
    ]
    frames = [c.args for c in state.broadcast_ws.call_args_list]
    assert frames[-1][0] == "queue_reorder"
    assert frames[-1][1]["order"] == [item["id"] for item in slot._queue]
    # Each drain takes one row, and his rows never share a turn with the rest.
    first, _ = _dequeue_next_message(slot, merge_enabled=True)
    assert "do A" in first and "peer cut-in" not in first and WAKE not in first, first


def test_only_a_completion_queued_before_him_moves_up_with_him(tmp_path) -> None:
    """A child's result that arrived before his message still runs before it,
    so it moves up with him: the one hop over another row. A result queued
    after his first entry, and every other row, keep their order."""
    turns: list = []
    state, slot = _slot(tmp_path, turns)
    slot.queue_append("cron report", kind=CRON_NOTIFICATION_KIND)
    slot.queue_append("a waiting note")
    slot.queue_append("[sub-agent done] first child", kind=SUBAGENT_COMPLETION_KIND)
    _queue_pick(state, slot, "do A")
    slot.queue_append("a later note")
    slot.queue_append("[sub-agent done] second child", kind=SUBAGENT_COMPLETION_KIND)

    _queue_pick(state, slot, "then B")

    assert [item["content"] for item in slot._queue] == [
        "[sub-agent done] first child",
        "do A",
        "then B",
        "cron report",
        "a waiting note",
        "a later note",
        "[sub-agent done] second child",
    ]


def test_his_queue_pick_passes_another_persons_waiting_message(tmp_path) -> None:
    """In a shared crew, a message another person queued is not the owner's:
    his Queue pick runs before it. Intended: the owner's message runs next."""
    turns: list = []
    state, slot = _slot(tmp_path, turns)
    queue_for_next_turn(state, slot, "another person's message", directive_user_origin=True)

    _queue_pick(state, slot)

    assert [item["content"] for item in slot._queue] == [OWNER, "another person's message"]
    assert not is_owner_queue_entry(slot._queue[1])


def test_a_restored_card_he_chose_today_runs_after_a_requeued_wake(tmp_path) -> None:
    """Pins today's behaviour, not the goal: a restored card he chose with
    "run this next" carries no owner mark (a proven peer note carries the same
    chosen mark), so a wake the turn never read goes back ahead of it."""
    turns: list = []
    state, slot = _slot(tmp_path, turns)
    slot.queue_append("his restored card")
    slot._queue[0][RESTORED_QUEUE_KEY] = True
    slot._queue[0][RESTORED_SEND_CHOSEN_KEY] = True
    slot._pending_steers.append(WAKE)
    slot._steer_admissions[WAKE] = sc.containment_meta(state, slot)
    slot._steer_automation[WAKE] = {"cycle": 1, "loop_id": "loop-ledger"}

    _turn_ends_unread(state, slot)

    assert [item["content"] for item in slot._queue] == [WAKE, "his restored card"]


@pytest.mark.asyncio
async def test_his_send_held_on_an_idle_slot_runs_before_the_parked_notes(
    tmp_path, monkeypatch
) -> None:
    """The idle-slot hold branch of the send route: two notes were parked while
    a child was live, the child is now flagged stalled, and he sends. His
    message is the turn the send starts, the notes wait in their order, and
    open tabs get the order after the card's push."""
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app, _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    direct: list = []

    async def fake_run_chat(st, sl, msg, *, _directive_user_origin, _directive_owner_origin=False):
        direct.append(msg)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
    drained = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(cr, "_run_chat", drained)
    monkeypatch.setattr(cr, "spawn_guarded_turn", lambda *a, **k: None)
    load = MagicMock()
    load.return_value.dashboard.merge_queued_messages = False
    monkeypatch.setattr(cr.KiroCrewConfig, "load", load)
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=[{"id": "dead", "stalled": True}])
    state = _make_state(tmp_path, subagents=subs)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("s1")
    slot.queue_append("note A, parked while the child was live")
    slot.queue_append("note B")

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat?ws=1", json={"message": OWNER, "slot": "s1"})
        assert (await resp.json()).get("queued") is True

    assert direct == []
    assert drained.call_args.args[2] == OWNER
    assert [item["content"] for item in slot._queue] == [
        "note A, parked while the child was live",
        "note B",
    ]
    events = [c.args[0] for c in state.broadcast_ws.call_args_list]
    assert events.index("queue_reorder") > events.index("queue_push")


def test_the_helpers_on_a_queue_with_no_owner_entries() -> None:
    """Nothing to move: the order and the insert point are the head as before."""
    queue = [{"id": "1", "content": "a"}, {"id": "2", "content": "b"}]
    assert put_owner_entries_first(queue) is False
    assert [item["id"] for item in queue] == ["1", "2"]
    assert after_owner_front(queue) == 0
