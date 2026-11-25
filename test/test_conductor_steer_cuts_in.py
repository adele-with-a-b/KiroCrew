"""A conductor's steer reaches a busy worker, and its receipt says what happened.

Three pieces, each pinned here where it is decided:

* a peer's steer that meets a turn with no prompt yet waits a bounded number of
  tries for that same turn instead of sitting in the queue for the whole turn
  (``session_deliver._steer_once_the_turn_has_a_prompt``);
* a hub conductor's agent-origin steer on this crew takes the peer's steer with
  the guards the local send keeps around its own (``dashboard.agent_steer``);
* a steer the agent holds behind a tool call is reported as held
  (``steer_hold``, ``send_to_target``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
    _steerable,
)

from kiro_crew import steer_refusal
from kiro_crew.dashboard import agent_steer
from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import chat_handlers as ch
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_deliver, steer_hold
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

# ── a turn that is still starting ────────────────────────────────────────────


class _Steers:
    """``steer_into_running_turn`` answering a scripted list, one per call."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def __call__(self, state, slot, text, **kwargs):
        self.calls.append(kwargs)
        return self.outcomes.pop(0)


@pytest.fixture
def waits(monkeypatch):
    """The pauses the wait took, without spending them: eight tries of 0.25 s for
    a broadcast's steer, twelve for a single send's."""
    taken: list[float] = []
    hooks: list = []

    async def _pause(seconds):
        taken.append(seconds)
        for hook in hooks:
            hook(len(taken))

    monkeypatch.setattr(session_deliver, "_pause", _pause)
    monkeypatch.setattr(
        steer_refusal, "START_WAIT_SECS", 2.0
    )  # flake-ok: sets a try count; _pause is faked
    monkeypatch.setattr(
        steer_refusal, "START_WAIT_PATIENT_SECS", 3.0
    )  # flake-ok: sets a try count; _pause is faked
    monkeypatch.setattr(
        steer_refusal, "START_POLL_SECS", 0.25
    )  # flake-ok: sets a try count; _pause is faked
    return SimpleNamespace(taken=taken, hooks=hooks)


def _starting():
    return cd.SteerUnavailable(steer_refusal.BETWEEN_STEPS)


def _deliver_steer(state, slot, text="rebase first", patient=False):
    origin = session_deliver.peer(steer=True, patient=patient)
    return asyncio.run(session_deliver.deliver(state, slot, text, origin))


def test_a_steer_to_a_starting_turn_goes_in_once_its_prompt_is_up(tmp_path, monkeypatch, waits):
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    steers = _Steers(_starting(), _starting(), cd.STEER_STEERED)
    monkeypatch.setattr(cd, "steer_into_running_turn", steers)

    assert _deliver_steer(state, slot) == session_deliver.DELIVERED_STEERED
    assert waits.taken == [0.25, 0.25]
    # Every try is a peer's steer: none of them carries the person's rights.
    assert [call["user_origin"] for call in steers.calls] == [False, False, False]


@pytest.mark.parametrize(("patient", "tries"), [(False, 8), (True, 12)])
def test_a_start_slower_than_the_wait_queues_with_that_reason(
    tmp_path, monkeypatch, waits, patient, tries
):
    """A broadcast's steer keeps the short wait its per-target budget allows; a
    single send waits longer. Either way the wait is a counted number of tries."""
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    steers = _Steers(*[_starting() for _ in range(tries + 1)])
    monkeypatch.setattr(cd, "steer_into_running_turn", steers)

    outcome = _deliver_steer(state, slot, patient=patient)

    assert outcome == session_deliver.NOT_DELIVERED
    assert cd.steer_refusal_of(outcome) == steer_refusal.BETWEEN_STEPS
    assert len(waits.taken) == tries and len(steers.calls) == tries + 1


@pytest.mark.parametrize("ended", ["done", "replaced"])
def test_a_turn_that_ends_during_the_wait_is_not_steered_into_its_successor(
    tmp_path, monkeypatch, waits, ended
):
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    steers = _Steers(_starting(), cd.STEER_STEERED)
    monkeypatch.setattr(cd, "steer_into_running_turn", steers)

    def _end(_count):
        if ended == "done":
            slot.task.done.return_value = True
        else:
            _busy(slot)  # a new turn task: the next turn began

    waits.hooks.append(_end)

    outcome = _deliver_steer(state, slot)

    assert cd.steer_refusal_of(outcome) == steer_refusal.BETWEEN_STEPS
    assert len(steers.calls) == 1 and waits.taken == [0.25]


@pytest.mark.parametrize(
    "first",
    [
        cd.SteerUnavailable(steer_refusal.NO_STEER),
        cd.SteerUnavailable(steer_refusal.PERMISSION_WAIT),
        cd.STEER_STEERED,
        cd.STEER_REQUEUED,
    ],
)
def test_only_a_turn_with_no_prompt_yet_is_waited_for(tmp_path, monkeypatch, waits, first):
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    steers = _Steers(first)
    monkeypatch.setattr(cd, "steer_into_running_turn", steers)

    _deliver_steer(state, slot)

    assert waits.taken == [] and len(steers.calls) == 1


def test_a_slot_whose_turn_has_no_agent_yet_reads_as_starting(tmp_path):
    """The real refusal, through the real helper: a running slot with no agent
    published is a turn that is starting, which is the case the wait is for."""
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    assert slot._acp_client is None

    outcome = _deliver_steer(state, slot)

    assert cd.steer_refusal_of(outcome) == steer_refusal.BETWEEN_STEPS


# ── a hub conductor's steer on this crew ─────────────────────────────────────


@pytest.fixture
def steer_rig(tmp_path, monkeypatch):
    """A busy slot and an ``agent_steer.steer`` whose delivery and containment are scripted."""
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    slot._turn_user_sent = slot._turn_owner_sent = False  # a worker's turn: no person sent it
    rig = SimpleNamespace(
        state=state,
        slot=slot,
        admission={"mirror": "", "channel": ""},
        outcome=session_deliver.DELIVERED_STEERED,
        changed=[],
        delivered=[],
        fences_at_write={},
        stop=AsyncMock(),
    )

    async def _deliver(st, sl, text, origin, **kwargs):
        rig.delivered.append((text, origin, kwargs))
        rig.fences_at_write = dict(sl._steer_audience_fences)
        return rig.outcome

    monkeypatch.setattr(sc, "containment_meta", lambda st, sl: rig.admission)
    monkeypatch.setattr(sc, "containment_snapshot", lambda st, sl, **kw: {"now": True})
    monkeypatch.setattr(sc, "newly_held_constraints", lambda now, admitted: rig.changed)
    monkeypatch.setattr(session_deliver, "deliver", _deliver)
    monkeypatch.setattr(ch, "stop_slot_turn", rig.stop)
    return rig


def _agent_steer(rig, text="take the other branch"):
    return asyncio.run(agent_steer.steer(rig.state, rig.slot, text))


def test_an_agent_steer_goes_in_as_a_peers_under_a_recorded_admission(steer_rig):
    reply, refusal, held_for_person = _agent_steer(steer_rig)

    assert (reply, refusal, held_for_person) == ({"ok": True, "steered": True}, "", False)
    ((text, origin, kwargs),) = steer_rig.delivered
    assert text == "take the other branch"
    assert origin == session_deliver.peer(steer=True, patient=True)
    assert kwargs == {"admission": steer_rig.admission}
    # Recorded before the write, and kept for the turn the text is now inside.
    assert list(steer_rig.fences_at_write.values()) == [steer_rig.admission]
    assert list(steer_rig.slot._steer_audience_fences.values()) == [steer_rig.admission]
    steer_rig.stop.assert_not_awaited()


@pytest.mark.parametrize("standing", ["_turn_user_sent", "_turn_owner_sent"])
def test_a_turn_a_person_is_driving_is_not_steered_into(steer_rig, standing):
    """That turn's directives keep reading the person's standing, so an agent's
    text does not go inside it: nothing is written and the caller queues it."""
    setattr(steer_rig.slot, standing, True)

    reply, refusal, held_for_person = _agent_steer(steer_rig)

    assert (reply, refusal, held_for_person) == (None, "", True)
    assert steer_rig.delivered == []
    assert steer_rig.slot._steer_audience_fences == {}


def test_a_steer_the_agent_holds_behind_a_tool_call_says_so(steer_rig):
    steer_rig.slot._acp_client = _steerable()
    steer_rig.slot._acp_client.steer_held_behind_tool = True

    reply, _, _ = _agent_steer(steer_rig)

    assert reply == {"ok": True, "steered": True, "held": True}


def test_an_audience_that_appeared_during_the_write_stops_that_turn(steer_rig):
    steer_rig.changed = ["channel"]

    reply, _, _ = _agent_steer(steer_rig)

    assert reply == {"ok": True, "steered": True}
    steer_rig.stop.assert_awaited_once_with(
        steer_rig.state, steer_rig.slot, source="agent_steer_containment", escalate=False
    )


def test_a_later_turn_is_not_stopped_for_the_steered_ones_audience(steer_rig, monkeypatch):
    steer_rig.changed = ["channel"]
    deliver = session_deliver.deliver

    async def _deliver_then_next_turn(st, sl, text, origin, **kwargs):
        outcome = await deliver(st, sl, text, origin, **kwargs)
        sl._turn_generation += 1
        return outcome

    monkeypatch.setattr(session_deliver, "deliver", _deliver_then_next_turn)

    _agent_steer(steer_rig)

    steer_rig.stop.assert_not_awaited()


def test_a_stop_that_fails_leaves_the_steer_reported_as_delivered(steer_rig):
    steer_rig.changed = ["channel"]
    steer_rig.stop.side_effect = RuntimeError("no stop")

    reply, _, _ = _agent_steer(steer_rig)

    assert reply == {"ok": True, "steered": True}
    assert list(steer_rig.slot._steer_audience_fences.values()) == [steer_rig.admission]


def test_a_steer_the_teardown_queued_is_reported_queued(steer_rig):
    steer_rig.outcome = session_deliver.DELIVERED_REQUEUED

    reply, refusal, held_for_person = _agent_steer(steer_rig)

    assert (reply, refusal, held_for_person) == ({"ok": True, "queued": True}, "", False)
    assert steer_rig.slot._steer_audience_fences == {}
    steer_rig.stop.assert_not_awaited()


def test_a_steer_queued_on_a_chat_replaced_during_the_write_is_refused(steer_rig, monkeypatch):
    """The teardown queued the text on a slot object its key does not name any more, so no
    drain reaches it: the answer is the local send's refusal, never ``queued``."""
    steer_rig.outcome = session_deliver.DELIVERED_REQUEUED
    monkeypatch.setitem(steer_rig.state._slots, steer_rig.slot.key, MagicMock())

    reply, refusal, held_for_person = _agent_steer(steer_rig)

    assert (reply, refusal, held_for_person) == (None, agent_steer.TARGET_MOVED, False)


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (cd.SteerUnavailable(steer_refusal.PERMISSION_WAIT), steer_refusal.PERMISSION_WAIT),
        (cd.SteerUnavailable(steer_refusal.PEER_STEER_OFF), steer_refusal.PEER_STEER_OFF),
        (session_deliver.NOT_DELIVERED, steer_refusal.REFUSED),
    ],
)
def test_a_refused_steer_is_handed_back_to_be_queued_with_its_reason(steer_rig, outcome, code):
    steer_rig.outcome = outcome

    reply, refusal, held_for_person = _agent_steer(steer_rig)

    assert reply is None and refusal == code and held_for_person is False
    assert steer_rig.slot._steer_audience_fences == {}
    steer_rig.stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "queued_after"),
    [
        (cd.SteerUnavailable(steer_refusal.TURN_ENDED), True),
        (session_deliver.DELIVERED_STEERED, False),
        (session_deliver.DELIVERED_REQUEUED, False),
    ],
)
async def test_a_request_that_goes_away_mid_write_loses_no_message(
    tmp_path, monkeypatch, outcome, queued_after
):
    """The write is shielded, so it settles after the request is cancelled. A steer
    that then turns out not to have gone in is queued: the sender got no answer and
    cannot know to send again."""
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    slot._turn_user_sent = slot._turn_owner_sent = False
    in_write = asyncio.Event()
    release = asyncio.Event()
    delivered: list = []

    async def _deliver(st, sl, text, origin, **kwargs):
        delivered.append(origin)
        if origin.steer:
            in_write.set()
            await asyncio.wait_for(release.wait(), FALLBACK_TIMEOUT_SECS)
            return outcome
        return session_deliver.DELIVERED_QUEUED

    monkeypatch.setattr(sc, "containment_meta", lambda st, sl: {})
    monkeypatch.setattr(session_deliver, "deliver", _deliver)

    request = asyncio.ensure_future(agent_steer.steer(state, slot, "take the other branch"))
    await asyncio.wait_for(in_write.wait(), FALLBACK_TIMEOUT_SECS)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    expected = 2 if queued_after else 1
    await async_wait_until(
        lambda: len(delivered) == expected and not agent_steer._ORPHANED,
        describe=lambda: f"deliveries={len(delivered)} orphaned={len(agent_steer._ORPHANED)}",
    )

    assert [origin.steer for origin in delivered] == ([True, False] if queued_after else [True])
    assert agent_steer._ORPHANED == set()
    assert bool(slot._steer_audience_fences) is (outcome == session_deliver.DELIVERED_STEERED)


def test_a_single_send_is_patient_and_a_broadcast_is_not(tmp_path, monkeypatch):
    """The real ``send_to_target`` names which wait its steer gets."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))
    target._acp_client = _steerable()
    seen: list = []
    deliver = session_deliver.deliver

    async def _spy(st, sl, text, origin, **kwargs):
        seen.append(origin)
        return await deliver(st, sl, text, origin, **kwargs)

    monkeypatch.setattr(session_deliver, "deliver", _spy)
    for via in ("session_send", sc.BROADCAST_VIA):
        asyncio.run(
            sc.send_to_target(
                state,
                caller_session_key=_key(caller),
                target="chat-2",
                message=f"note through {via}",
                steer=True,
                via=via,
            )
        )

    assert [(origin.steer, origin.patient) for origin in seen] == [(True, True), (True, False)]


# ── the local receipt for a held steer ───────────────────────────────────────


@pytest.mark.parametrize("held", [True, False])
def test_the_local_send_reports_a_steer_held_behind_a_tool_call(tmp_path, held):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))
    target._acp_client = _steerable()
    target._acp_client.steer_held_behind_tool = held

    out = asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            message="check the other branch first",
            steer=True,
        )
    )

    assert out["steered"] is True
    assert out.get("held", False) is held


def test_only_a_real_hold_reads_as_held():
    assert steer_hold.held_behind_tool(MagicMock(_acp_client=None)) is False
    # A stand-in agent that answers every attribute is not a hold.
    assert steer_hold.held_behind_tool(MagicMock()) is False
    client = MagicMock(steer_held_behind_tool=True)
    assert steer_hold.held_behind_tool(MagicMock(_acp_client=client)) is True


def test_the_held_note_promises_no_time_and_no_cut():
    note = steer_refusal.held_note()
    assert "has not read your message yet" in note
    assert "when that call returns" in note and "Nothing was cut short" in note
