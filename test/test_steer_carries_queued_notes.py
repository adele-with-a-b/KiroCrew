"""A cut-in note carries the sender's earlier queued notes in with it, in order.

Through the real delivery (``send_to_target`` and ``agent_steer.steer``); only the
agent's own steer RPC is stubbed, so what is asserted is what the target's agent
is handed and what is left on its queue.
"""

from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _slot,
    _steerable,
)

from kiro_crew import steer_refusal
from kiro_crew.dashboard import agent_steer
from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_deliver, steer_carry
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.queue_edit_hold import EDIT_HOLD_KEY, _clock
from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY


def _setup(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    worker = state.get_or_create_slot("chat-2")
    worker._created_by = caller.key
    _busy(worker)
    worker._turn_user_sent = worker._turn_owner_sent = False  # no person drives the turn
    worker._acp_client = _steerable()
    state.broadcast_ws = MagicMock()
    return state, caller, worker


def _send(state, caller, worker, message, *, steer):
    return asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=slot_history_key(caller),
            target=worker.key,
            message=message,
            steer=steer,
        )
    )


def _queue_notes(state, caller, worker, *notes):
    for note in notes:
        assert _send(state, caller, worker, note, steer=False)["steered"] is False
    return [entry["id"] for entry in worker._queue]


def _handed(worker):
    """The texts the agent's steer RPC was handed, in order."""
    return [call.args[0] for call in worker._acp_client.steer.await_args_list]


def _body(text):
    """*text* without the sender envelope ``session_send`` put in front of it."""
    return text.split("]\n\n", 1)[-1]


def _queued(worker):
    return [entry["content"] for entry in worker._queue]


def _cancelled(state):
    return [
        c.args[1]["queue_id"]
        for c in state.broadcast_ws.call_args_list
        if c.args[0] == "queue_cancel"
    ]


def test_the_senders_queued_notes_go_in_first_oldest_first_and_leave_the_queue(tmp_path):
    state, caller, worker = _setup(tmp_path)
    ids = _queue_notes(state, caller, worker, "first: use branch a", "second: branch a is gone")

    result = _send(state, caller, worker, "third: this replaces my earlier notes", steer=True)

    assert result["steered"] is True and result["carried"] == 2
    handed = _handed(worker)
    assert [_body(text) for text in handed] == [
        "first: use branch a",
        "second: branch a is gone",
        "third: this replaces my earlier notes",
    ]
    # Each keeps the sender's envelope, so none reads as the person's typing.
    assert all(text.startswith("[sent by session ") for text in handed)
    assert worker._queue == []
    assert _cancelled(state) == ids
    # Nothing runs later: no stamp or card is left for the carried entries.
    assert not set(ids) & set(getattr(worker, "_queued_at", {}))


def test_only_entries_the_sender_queued_with_session_send_move(tmp_path):
    state, caller, worker = _setup(tmp_path)
    _queue_notes(state, caller, worker, "mine, queued")
    (own,) = worker._queue

    def _plant(text, *, edit=None, kind="", **flags):
        meta = copy.deepcopy(own["meta"])
        if edit:
            edit(meta)
        return worker.queue_append(text, kind=kind, meta=meta, **flags)

    def _other_sender(meta):
        meta[sc.SEND_ORIGIN_META_KEY]["slot"] = "chat-9"

    def _lost_stamp(meta):
        meta.pop(sc.SEND_ORIGIN_META_KEY)

    def _requeued_steer(meta):
        meta.pop(sc.SESSION_SEND_QUEUED_META)

    _plant("a person typed this", directive_user_origin=True)
    _plant("from chat-9", edit=_other_sender)
    _plant("restored after a restart", edit=_lost_stamp)
    _plant("a steer the teardown requeued", edit=_requeued_steer)
    _plant("a cron card", kind="cron_notification")
    _plant("an app message", kind="mcp_app_message")
    stay = _queued(worker)[1:]

    result = _send(state, caller, worker, "newest", steer=True)

    # The sender's own requeued steer goes in too, in its place; nothing else moves.
    assert result["steered"] is True and result["carried"] == 2
    assert [_body(text) for text in _handed(worker)] == [
        "mine, queued",
        "a steer the teardown requeued",
        "newest",
    ]
    assert _queued(worker) == [text for text in stay if text != "a steer the teardown requeued"]


def test_a_refused_older_note_stays_and_the_ones_behind_it_are_not_tried(tmp_path):
    """The turn's agent refuses the first older note: it goes back where it was,
    the second is not tried, and the new note queues behind both instead of
    slipping past them."""
    state, caller, worker = _setup(tmp_path)
    ids = _queue_notes(state, caller, worker, "first", "second")
    worker.queue_insert(0, "someone else's, ahead of them")
    worker._acp_client.steer = AsyncMock(return_value=False)

    result = _send(state, caller, worker, "third", steer=True)

    assert result["steered"] is False and "carried" not in result
    assert len(_handed(worker)) == 1 and _handed(worker)[0].endswith("first")
    assert [_body(text) for text in _queued(worker)] == [
        "someone else's, ahead of them",
        "first",
        "second",
        "third",
    ]
    # The same entries, not copies: ids, cards and waits are untouched.
    assert [entry["id"] for entry in worker._queue[1:3]] == ids
    assert _cancelled(state) == []
    assert worker._pending_steers == []


def test_an_older_note_whose_text_is_already_in_flight_does_not_hold_the_new_one(tmp_path):
    state, caller, worker = _setup(tmp_path)
    _queue_notes(state, caller, worker, "first", "second")
    worker._pending_steers.append(worker._queue[0]["content"])

    result = _send(state, caller, worker, "third", steer=True)

    assert result["steered"] is True and "carried" not in result
    assert [_body(text) for text in _handed(worker)] == ["third"]
    assert [_body(text) for text in _queued(worker)] == ["first", "second"]


def test_the_steer_cap_takes_the_oldest_that_fit_and_keeps_room_for_the_new_note(tmp_path):
    state, caller, worker = _setup(tmp_path)
    _queue_notes(state, caller, worker, "first", "second", "third")
    worker._pending_steers.extend(f"pending {n}" for n in range(cd.MAX_PENDING_STEERS - 3))

    result = _send(state, caller, worker, "fourth", steer=True)

    assert result["steered"] is True and result["carried"] == 2
    assert [_body(text) for text in _handed(worker)] == ["first", "second", "fourth"]
    assert [_body(text) for text in _queued(worker)] == ["third"]


@pytest.mark.parametrize("held", ["editor", "audience", "restored"])
def test_a_note_that_may_not_go_stays_and_the_new_one_still_goes_in(tmp_path, monkeypatch, held):
    state, caller, worker = _setup(tmp_path)
    _queue_notes(state, caller, worker, "first")
    (entry,) = worker._queue
    if held == "editor":
        entry[EDIT_HOLD_KEY] = _clock() + 60.0
    elif held == "audience":
        # A constraint that holds now and did not when the note was queued: the
        # drain would drop it, so the carry must not deliver it.
        monkeypatch.setattr(sc, "newly_held_constraints", lambda now, meta, **kw: ["linked"])
    else:
        entry[RESTORED_QUEUE_KEY] = True
        entry["meta"].pop(sc.SEND_ORIGIN_META_KEY)

    result = _send(state, caller, worker, "second", steer=True)

    assert result["steered"] is True and "carried" not in result
    assert [_body(text) for text in _handed(worker)] == ["second"]
    assert worker._queue == [entry]


def test_a_turn_that_ends_under_a_carried_note_does_not_take_the_new_one(tmp_path, monkeypatch):
    """The teardown requeued the carried note (at the head, as its own entry), so
    the original leaves the queue and the new note is not steered into whatever
    turn comes next: it queues behind its elders."""
    state, caller, worker = _setup(tmp_path)
    ids = _queue_notes(state, caller, worker, "first", "second")
    calls: list[str] = []

    async def _requeues(st, slot, text, **kwargs):
        calls.append(text)
        slot.queue_insert(0, text)  # what the teardown does with an unread steer
        return cd.STEER_REQUEUED

    monkeypatch.setattr(cd, "steer_into_running_turn", _requeues)
    carry = steer_carry.from_session(state, caller.key)

    outcome = asyncio.run(
        session_deliver.deliver(
            state, worker, "third", session_deliver.peer(steer=True, carry=carry)
        )
    )

    assert cd.steer_refusal_of(outcome) == steer_refusal.TURN_ENDED
    assert len(calls) == 1 and calls[0].endswith("first")
    assert carry.count == 0
    # Once each, in order: the requeued copy stands in for the original.
    assert [_body(text) for text in _queued(worker)] == ["first", "second"]
    assert [entry["id"] for entry in worker._queue][1:] == ids[1:]
    assert ids[0] not in [entry["id"] for entry in worker._queue]


def _a_start_wait(monkeypatch):
    """The suite's floor sets the start wait to zero; these tests need one."""
    monkeypatch.setattr(steer_refusal, "START_WAIT_SECS", 2.0)
    monkeypatch.setattr(steer_refusal, "START_WAIT_PATIENT_SECS", 2.0)
    monkeypatch.setattr(steer_refusal, "START_POLL_SECS", 0.25)


def test_a_note_sent_again_cuts_in_and_its_queued_twin_stays(tmp_path):
    """Steered first, the twin would leave the new note refused as the same text
    in flight; so it ends the carry and the note cuts in as it always did."""
    state, caller, worker = _setup(tmp_path)
    ids = _queue_notes(state, caller, worker, "first", "again", "after")

    result = _send(state, caller, worker, "again", steer=True)

    assert result["steered"] is True and result["carried"] == 1
    assert [_body(text) for text in _handed(worker)] == ["first", "again"]
    assert [entry["id"] for entry in worker._queue] == ids[1:]


def test_a_note_waits_on_the_queue_while_the_turn_has_no_prompt_yet(tmp_path, monkeypatch):
    """``between_steps`` is waited out with the entry back on the queue, so a
    turn that ends during the wait drains it in its place."""
    state, caller, worker = _setup(tmp_path)
    ids = _queue_notes(state, caller, worker, "first", "second")
    outcomes = iter([cd.SteerUnavailable(steer_refusal.BETWEEN_STEPS)])
    handed: list[str] = []
    during_the_wait: list[list[str]] = []

    async def _steers(st, slot, text, **kwargs):
        outcome = next(outcomes, cd.STEER_STEERED)
        if outcome == cd.STEER_STEERED:
            handed.append(_body(text))
        return outcome

    async def _pause(secs):
        during_the_wait.append([entry["id"] for entry in worker._queue])

    monkeypatch.setattr(cd, "steer_into_running_turn", _steers)
    monkeypatch.setattr(session_deliver, "_pause", _pause)
    _a_start_wait(monkeypatch)

    result = _send(state, caller, worker, "third", steer=True)

    assert during_the_wait == [ids]
    assert handed == ["first", "second", "third"]
    assert result["steered"] is True and result["carried"] == 2
    assert worker._queue == []


def test_a_turn_that_ends_during_that_wait_keeps_every_note_in_order(tmp_path, monkeypatch):
    state, caller, worker = _setup(tmp_path)
    ids = _queue_notes(state, caller, worker, "first", "second")
    calls: list[str] = []

    async def _no_prompt_yet(st, slot, text, **kwargs):
        calls.append(text)
        return cd.SteerUnavailable(steer_refusal.BETWEEN_STEPS)

    async def _the_turn_ends(secs):
        worker.task = MagicMock()

    monkeypatch.setattr(cd, "steer_into_running_turn", _no_prompt_yet)
    monkeypatch.setattr(session_deliver, "_pause", _the_turn_ends)
    _a_start_wait(monkeypatch)
    carry = steer_carry.from_session(state, caller.key)

    outcome = asyncio.run(
        session_deliver.deliver(
            state, worker, "third", session_deliver.peer(steer=True, carry=carry)
        )
    )

    assert cd.steer_refusal_of(outcome) == steer_refusal.TURN_ENDED
    assert len(calls) == 1 and carry.count == 0
    assert [entry["id"] for entry in worker._queue] == ids


@pytest.mark.parametrize("registered", ["pending", "read", ""])
def test_a_steer_that_raises_leaves_the_note_in_exactly_one_place(
    tmp_path, monkeypatch, registered
):
    """Registered means the agent was handed it or the teardown requeues it, so
    the original must not come back; unregistered, it goes back where it was."""
    state, caller, worker = _setup(tmp_path)
    ids = _queue_notes(state, caller, worker, "first", "second")

    async def _raises(st, slot, text, **kwargs):
        if registered == "pending":
            slot._pending_steers.append(text)
        elif registered == "read":
            slot._steer_delivery_ids[text] = "d1"  # the turn read it; its record stays
        raise RuntimeError("the row could not be written")

    monkeypatch.setattr(cd, "steer_into_running_turn", _raises)
    carry = steer_carry.from_session(state, caller.key)

    with pytest.raises(RuntimeError):
        asyncio.run(
            session_deliver.deliver(
                state, worker, "third", session_deliver.peer(steer=True, carry=carry)
            )
        )

    assert [entry["id"] for entry in worker._queue] == (ids[1:] if registered else ids)


def test_a_carried_note_keeps_the_admission_it_was_queued_under(tmp_path, monkeypatch):
    """A requeue of a carried note must face the drain with the note's own
    baseline, never one read off the slot at carry time."""
    state, caller, worker = _setup(tmp_path)
    _queue_notes(state, caller, worker, "first")
    meta = copy.deepcopy(worker._queue[0]["meta"])
    seen: list[dict] = []

    async def _steers(st, slot, text, **kwargs):
        seen.append(kwargs)
        return cd.STEER_STEERED

    monkeypatch.setattr(cd, "steer_into_running_turn", _steers)

    _send(state, caller, worker, "second", steer=True)

    carried = seen[0]
    assert carried["user_origin"] is False
    assert carried["admission"] == {
        sc.QUEUED_CONTAINMENT_META_KEY: meta[sc.QUEUED_CONTAINMENT_META_KEY],
        sc.SEND_ORIGIN_META_KEY: meta[sc.SEND_ORIGIN_META_KEY],
        steer_carry.WAS_STEERED_META: True,
    }
    assert carried["relay_meta"] == meta["relay"]


def test_a_hard_stop_under_a_carried_note_discards_it_with_the_rest(tmp_path, monkeypatch):
    """The person pressed "discard everything" while the note was off the queue
    for its steer: it must not come back onto the emptied queue and run later."""
    state, caller, worker = _setup(tmp_path)
    _queue_notes(state, caller, worker, "first", "second")
    told: list[tuple[str, str]] = []

    async def _killed(st, slot, text, **kwargs):
        slot._stop_state = "killing"
        slot._queue.clear()
        return cd.SteerUnavailable(steer_refusal.TURN_ENDED)

    def _notify(st, *, entry_meta, target_slot, text, cause):
        told.append((_body(text), cause))
        return True

    monkeypatch.setattr(cd, "steer_into_running_turn", _killed)
    monkeypatch.setattr(sc, "notify_send_origin_discarded", _notify)
    carry = steer_carry.from_session(state, caller.key)

    outcome = asyncio.run(
        session_deliver.deliver(
            state, worker, "third", session_deliver.peer(steer=True, carry=carry)
        )
    )

    assert cd.steer_refusal_of(outcome) == steer_refusal.TURN_ENDED
    assert worker._queue == [] and carry.count == 0
    assert told == [("first", sc.SEND_DISCARD_HARD_STOP)]


def test_a_note_queued_during_a_hard_stop_stays_for_the_next_turn(tmp_path, monkeypatch):
    """The stop discards what was queued when it was pressed. A note that arrived
    after the press stays queued, and a cut-in during the kill carries nothing."""
    state, caller, worker = _setup(tmp_path)
    worker._stop_state = "killing"
    ids = _queue_notes(state, caller, worker, "first")
    calls: list[str] = []

    async def _never(st, slot, text, **kwargs):
        calls.append(text)
        return cd.SteerUnavailable(steer_refusal.TURN_ENDED)

    monkeypatch.setattr(cd, "steer_into_running_turn", _never)
    carry = steer_carry.from_session(state, caller.key)

    outcome = asyncio.run(
        session_deliver.deliver(
            state, worker, "second", session_deliver.peer(steer=True, carry=carry)
        )
    )

    assert cd.steer_refusal_of(outcome) == steer_refusal.TURN_ENDED
    assert calls == [] and [entry["id"] for entry in worker._queue] == ids


# ── a hub conductor's steer on this crew ─────────────────────────────────────


def _crew_worker(tmp_path):
    state = _make_state(tmp_path)
    worker = _busy(_slot(state, "chat-2"))
    worker._turn_user_sent = worker._turn_owner_sent = False
    worker._acp_client = _steerable()
    state.broadcast_ws = MagicMock()
    return state, worker


def test_a_conductors_steer_on_a_crew_carries_its_queued_sends(tmp_path):
    state, worker = _crew_worker(tmp_path)
    first = cd.queue_for_next_turn(state, worker, "first", turn_actor="crew")
    person = cd.queue_for_next_turn(state, worker, "a person's", directive_user_origin=True)
    second = cd.queue_for_next_turn(state, worker, "second", turn_actor="crew")
    app = cd.queue_for_next_turn(state, worker, "an app's", turn_actor="app")
    state.broadcast_ws.reset_mock()

    reply, refusal, held_for_person = asyncio.run(agent_steer.steer(state, worker, "third"))

    assert (reply, refusal, held_for_person) == (
        {"ok": True, "steered": True, "carried": 2},
        "",
        False,
    )
    assert _handed(worker) == ["first", "second", "third"]
    assert [entry["id"] for entry in worker._queue] == [person, app]
    assert _cancelled(state) == [first, second]
    # Text of the conductor's is in the turn, so the audience record stays.
    assert len(worker._steer_audience_fences) == 1


def test_a_carried_crew_note_keeps_its_actor_for_a_requeue(tmp_path):
    """Unread when the turn ends, a steer is requeued from its own records; with
    no actor there the conductor's note would run as the person's turn."""
    state, worker = _crew_worker(tmp_path)
    cd.queue_for_next_turn(state, worker, "first", turn_actor="crew")

    asyncio.run(agent_steer.steer(state, worker, "second"))

    assert "first" in worker._pending_steers
    # The record the teardown copies onto the entry it makes for an unread steer.
    assert worker._steer_admissions["first"][cd.TURN_ACTOR_META_KEY] == "crew"


def test_a_requeued_crew_steer_goes_in_ahead_of_the_new_one(tmp_path):
    """The running turn takes it, then the new note: never refused ``turn_ended``."""
    state, worker = _crew_worker(tmp_path)
    cd.queue_for_next_turn(state, worker, "first", turn_actor="crew")
    (entry,) = worker._queue
    entry["meta"]["steer_delivery_id"] = "d1"  # as the teardown stamps a requeued steer

    reply, refusal, _ = asyncio.run(agent_steer.steer(state, worker, "second"))

    assert (reply, refusal) == ({"ok": True, "steered": True, "carried": 1}, "")
    assert _handed(worker) == ["first", "second"] and worker._queue == []


def test_a_requeued_steer_the_turn_may_have_read_stays_and_the_new_one_goes_in(tmp_path):
    from kiro_crew.dashboard.chat_utils_injected_rows import STEER_POSSIBLY_DELIVERED_META

    state, worker = _crew_worker(tmp_path)
    cd.queue_for_next_turn(state, worker, "first", turn_actor="crew")
    (entry,) = worker._queue
    entry["meta"]["steer_delivery_id"] = "d1"
    entry["meta"][STEER_POSSIBLY_DELIVERED_META] = True

    reply, refusal, _ = asyncio.run(agent_steer.steer(state, worker, "second"))

    assert (reply, refusal) == ({"ok": True, "steered": True}, "")
    assert _handed(worker) == ["second"] and worker._queue == [entry]


def test_a_conductors_steer_requeued_unwritten_goes_in_ahead_of_the_next_one(tmp_path):
    """The usual requeue: the turn ended before the agent took the steer. That
    entry has no delivery id, only what the steer's admission recorded."""
    state, worker = _crew_worker(tmp_path)
    from kiro_crew.dashboard import chat_runner as cr

    asyncio.run(agent_steer.steer(state, worker, "first"))
    cr._requeue_unconsumed_steers(state, worker)  # the teardown, for the unread steer
    (entry,) = worker._queue
    assert "steer_delivery_id" not in entry["meta"]
    assert entry["meta"][cd.TURN_ACTOR_META_KEY] == "crew"

    reply, refusal, _ = asyncio.run(agent_steer.steer(state, worker, "second"))

    assert (reply, refusal) == ({"ok": True, "steered": True, "carried": 1}, "")
    assert _handed(worker) == ["first", "first", "second"] and worker._queue == []


def test_a_hub_conductors_taken_steer_is_still_requeued_by_an_error_end(tmp_path):
    """The harness took it (claude answered ``injected``) and the turn ended
    without ``end_turn``. A hub conductor's steer stamps no sender this crew can
    write a notice to, so it is requeued as before rather than dropped unheard."""
    from kiro_crew.dashboard import chat_runner as cr
    from kiro_crew.dashboard.chat_turn.steer_queue import _mark_held_steers_possibly_delivered

    state, worker = _crew_worker(tmp_path)
    asyncio.run(agent_steer.steer(state, worker, "first"))
    worker._acp_client.steers_taken_unconfirmed = frozenset({"first"})
    _mark_held_steers_possibly_delivered(worker, worker._acp_client)
    cr._requeue_unconsumed_steers(state, worker)

    assert [entry["content"] for entry in worker._queue] == ["first"]


def test_a_requeued_steer_the_turn_may_have_read_does_not_hold_back_the_senders_queued_notes(
    tmp_path,
):
    """It stays for the drain; the sender's queued note behind it still goes in
    before the new one, so the turn reads them in send order."""
    from kiro_crew.dashboard.chat_utils_injected_rows import STEER_POSSIBLY_DELIVERED_META

    state, caller, worker = _setup(tmp_path)
    _queue_notes(state, caller, worker, "first", "second")
    requeued = worker._queue[0]
    requeued["meta"].pop(sc.SESSION_SEND_QUEUED_META)  # as a requeued steer's meta is
    requeued["meta"][STEER_POSSIBLY_DELIVERED_META] = True
    carry = steer_carry.from_session(state, caller.key)

    outcome = asyncio.run(
        session_deliver.deliver(
            state, worker, "third", session_deliver.peer(steer=True, carry=carry)
        )
    )

    assert outcome == session_deliver.DELIVERED_STEERED
    assert [_body(text) for text in _handed(worker)] == ["second", "third"]
    assert carry.count == 1 and worker._queue == [requeued]


def test_a_typed_entry_with_the_senders_stamp_is_not_moved(tmp_path):
    """A recovery replay or an app message carries the sender's stamp with a kind
    and callbacks of its own; steering its text would drop both."""
    state, caller, worker = _setup(tmp_path)
    _queue_notes(state, caller, worker, "replayed")
    typed = worker._queue[0]
    typed["meta"].pop(sc.SESSION_SEND_QUEUED_META)
    typed["kind"] = "synthetic_recovery"
    carry = steer_carry.from_session(state, caller.key)

    outcome = asyncio.run(
        session_deliver.deliver(
            state, worker, "next", session_deliver.peer(steer=True, carry=carry)
        )
    )

    assert outcome == session_deliver.DELIVERED_STEERED
    assert [_body(text) for text in _handed(worker)] == ["next"]
    assert carry.count == 0 and worker._queue == [typed]


def test_a_conductors_steer_records_its_actor_for_a_requeue(tmp_path):
    state, worker = _crew_worker(tmp_path)

    reply, _, _ = asyncio.run(agent_steer.steer(state, worker, "second"))

    assert reply == {"ok": True, "steered": True}
    assert worker._steer_admissions["second"][cd.TURN_ACTOR_META_KEY] == "crew"


def test_a_requeued_session_steer_goes_in_first_with_the_senders_later_notes(tmp_path):
    """A steer of the sender's that the teardown requeued goes into the running
    turn first, then the sender's later queued note, then the new cut-in."""
    state, caller, worker = _setup(tmp_path)
    ids = _queue_notes(state, caller, worker, "first", "second")
    requeued = worker._queue[0]
    requeued["meta"].pop(sc.SESSION_SEND_QUEUED_META)  # as a requeued steer's meta is
    carry = steer_carry.from_session(state, caller.key)

    outcome = asyncio.run(
        session_deliver.deliver(
            state, worker, "third", session_deliver.peer(steer=True, carry=carry)
        )
    )

    assert outcome == session_deliver.DELIVERED_STEERED
    assert [_body(text) for text in _handed(worker)] == ["first", "second", "third"]
    assert carry.count == 2 and worker._queue == []
    assert _cancelled(state) == ids


def test_a_restored_crew_entry_is_nobodys_and_stays(tmp_path):
    state, worker = _crew_worker(tmp_path)
    cd.queue_for_next_turn(state, worker, "first", turn_actor="crew")
    (entry,) = worker._queue
    entry[RESTORED_QUEUE_KEY] = True

    reply, _, _ = asyncio.run(agent_steer.steer(state, worker, "second"))

    assert reply == {"ok": True, "steered": True}
    assert _handed(worker) == ["second"] and worker._queue == [entry]


def test_the_carried_notes_keep_the_audience_record_when_the_new_note_is_refused(tmp_path):
    """The conductor's earlier send is inside the turn even though the new one
    was refused, so the turn's reply legs still read the recorded admission."""
    state, worker = _crew_worker(tmp_path)
    cd.queue_for_next_turn(state, worker, "first", turn_actor="crew")
    worker._acp_client.steer = AsyncMock(side_effect=[True, False])

    reply, refusal, _ = asyncio.run(agent_steer.steer(state, worker, "second"))

    assert reply is None and refusal
    assert _handed(worker) == ["first", "second"] and worker._queue == []
    assert len(worker._steer_audience_fences) == 1
    # The route queues the new note; its reply still says what went in, once.
    assert agent_steer.queued_marks(refusal, "q1", set()) == {"carried": 1}
    assert agent_steer.queued_marks(refusal, "q1", {"q1"}) == {"started": True, "carried": 1}
    # The count travels with this send's own refusal: a plain one carries none.
    assert isinstance(refusal, str) and steer_refusal.known(refusal) == refusal
    assert agent_steer.queued_marks(str(refusal), "q1", set()) == {}


# ── what the sender reads ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("count", "said"),
    [
        (
            1,
            " 1 earlier queued note of yours went in ahead of it, oldest first, and left the queue.",
        ),
        (
            3,
            " 3 earlier queued notes of yours went in ahead of it, oldest first, and left the queue.",
        ),
        (0, ""),
        (None, ""),
        (True, ""),
        ("2", ""),
    ],
)
def test_the_receipt_counts_the_notes_that_went_in_with_it(count, said):
    assert steer_refusal.carried_note(count) == said


def test_the_local_tool_reply_says_how_many_went_in_with_it():
    from kiro_crew.mcp_dashboard import TABLE
    from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
    from kiro_crew.mcp_tools.table import Caller, ToolContext

    def _reply(resp):
        dash = InMemoryDashboardClient({"POST /api/session-control/send": resp})
        ctx = ToolContext(dash, Caller.strict("dashboard:chat-1-100"))
        return TABLE.call("session_send", {"target": "chat-2", "message": "go", "steer": True}, ctx)

    base = {"ok": True, "target": "chat-2", "started": False, "steered": True}
    assert "2 earlier queued notes of yours went in ahead of it" in _reply({**base, "carried": 2})
    assert "earlier queued" not in _reply(base)
    # The new note fell back to the queue, and one older note is in the turn.
    queued = _reply({**base, "steered": False, "steerRefusal": "same_pending", "carried": 1})
    assert "Queued" in queued and "1 earlier queued note of yours went in" in queued


def test_the_crew_tool_reply_says_how_many_went_in_with_it():
    from types import SimpleNamespace

    from kiro_crew import mcp_session_crew

    def _reply(resp):
        client = SimpleNamespace(post=lambda path, body, session_key="": resp)
        ctx = SimpleNamespace(client=client, caller_key="dashboard:chat-1")
        args = {"crew": "lab", "target": "chat-77", "message": "go", "steer": True}
        return mcp_session_crew.run_crew_send(args, ctx)

    base = {"target": "chat-77", "crew": "inst", "steered": True}
    out = _reply({**base, "carried": 2})
    assert "steered into its running turn" in out
    assert "2 earlier queued notes of yours went in ahead of it" in out
    assert "earlier queued" not in _reply(base)
    # The new note fell back to the queue, and one older note is in the turn.
    queued = _reply({**base, "steered": False, "steerRefusal": "refused", "carried": 1})
    assert "queued, not steered" in queued
    assert "1 earlier queued note of yours went in" in queued


@pytest.mark.parametrize(
    ("crew_reply", "carried"),
    [
        ({"ok": True, "steered": True, "carried": 2}, 2),
        ({"ok": True, "steered": True}, None),
        ({"ok": True, "steered": True, "carried": "2"}, None),
        ({"ok": True, "queued": True, "steerRefusal": "refused", "carried": 2}, 2),
    ],
)
def test_the_hub_passes_the_crews_count_to_the_sender(monkeypatch, crew_reply, carried):
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    async def _peer_call(mgr, instance_id, method, path, **kwargs):
        return 200, crew_reply

    monkeypatch.setattr(routes, "peer_call", _peer_call)

    result = asyncio.run(routes._send(None, "inst", "chat-77", {"message": "go", "steer": True}))

    assert result.get("carried") == carried
