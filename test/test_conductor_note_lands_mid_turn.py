"""A Conductor's notes reach its busy worker's running turn at the next step, once each.

The case this pins: a Conductor sends nine notes to a claude worker inside one long
turn of tool calls. Each note is a peer's steer (``session_send``'s default). One
sent while a tool call is open is held and written when that call returns; one
sent between calls is written at once. The turn then ends with an error, not a
clean ``end_turn`` (a compaction failure), so the harness never reports any of
them consumed. Each was on the turn's input (claude answered ``injected``), so:

* each note is written to the harness once, in send order, at the next step;
* the teardown queues none of them to run again: each row says it went in and
  is unconfirmed, and the Conductor is told each may not have run;
* the next note the Conductor sends goes into the next running turn, never
  refused as if that turn had ended (``turn_ended``).

On the code before this change all nine came back at the head of the queue, ran a
second time one turn each, and every later note was refused ``turn_ended`` behind
them (``steer_carry``'s wait rule).

The real claude client (``AcpClient``, its held-steer sender and its read loop),
the real ``steer_into_running_turn``, the real teardown and the real carry run
here; only the adapter's stdio is scripted.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from test_claude_steering_request import (
    PROMPT_ID,
    _client,
    _close_tool,
    _open_tool,
    _start_turn,
    _steered_texts,
    _Tasks,
    _wait_closed,
    _Wire,
)
from test_peer_note_receipts import _home, _notices, _state  # noqa: F401 -- _home is autouse

from kiro_crew.acp import client_turn
from kiro_crew.acp.types import EVENT_COMPLETE, STOP_REASON_COMPACTION_FAILED, JsonRpcMessage
from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_deliver, steer_carry
from kiro_crew.dashboard.chat_delivery import (
    STEER_STATE_REQUEUED,
    STEER_STATE_SUPERSEDED,
    STEER_STEERED,
    steer_into_running_turn,
)
from kiro_crew.dashboard.chat_turn.steer_queue import _mark_held_steers_possibly_delivered
from kiro_crew.testing.wait import async_wait_until, await_until

#: The row state the dashboard reads (``UserMessage``), pinned as its wire value.
STEER_STATE_UNCONFIRMED = "unconfirmed"
NOTES = [f"note {n}: check stage {n}" for n in range(1, 10)]
#: The notes sent while a tool call is open; the others go out between calls.
HELD = {2, 4, 5, 6}


@pytest.fixture(autouse=True)
def _peer_steers_on(monkeypatch):
    """The live case: claude takes another session's steer only with the key on."""
    monkeypatch.setattr(
        cd.live, "snapshot", lambda: MagicMock(agent=MagicMock(peer_steer_loss_recovery=True))
    )


def _pair(tmp_path):
    state = _state(tmp_path)
    return state, state.get_or_create_slot("conductor"), state.get_or_create_slot("worker")


def _running(slot, client) -> None:
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot._acp_client = client


def _admission(state, worker, conductor) -> dict:
    return {**sc.containment_meta(state, worker), **sc.send_origin_meta(state, conductor.key)}


async def _note(state, worker, conductor, text) -> str:
    """What ``session_send``'s steer arm does with one note."""
    return await steer_into_running_turn(
        state,
        worker,
        text,
        user_origin=False,
        admission=_admission(state, worker, conductor),
        relay_meta={"senderKey": conductor.key, "via": "session_send"},
    )


def _steer_rows(slot) -> list[dict]:
    return [r for r in slot.messages if (r.get("meta") or {}).get("steer") is True]


def _fail_compaction(client, wire, monkeypatch) -> None:
    """The adapter goes silent after a failed compaction, past its budget: the
    client ends the turn itself, with no ``end_turn`` (``client_turn``)."""
    monkeypatch.setattr(client_turn, "_COMPACTION_FAILED_TURN_BUDGET", -1.0)
    client._compaction_failed_at = 0.0
    # Any frame makes the read loop go round once more and see the budget spent.
    wire.inbound.put_nowait(JsonRpcMessage(id=987654, result={}))


async def _teardown(state, worker, client) -> None:
    """The turn's release (``_turn_release_handles``) and hand-off, in their order."""
    _mark_held_steers_possibly_delivered(worker, client)
    worker._acp_client = None
    worker.task = None
    chat_runner._requeue_unconsumed_steers(state, worker)


@pytest.mark.asyncio
async def test_nine_notes_in_a_long_turn_each_go_in_once_in_order(tmp_path, monkeypatch):
    state, conductor, worker = _pair(tmp_path)
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        _running(worker, client)

        for n, text in enumerate(NOTES, start=1):
            tool = f"tc{n}"
            wire.inbound.put_nowait(_open_tool(tool))
            await async_wait_until(lambda t=tool: t in client._active_tool_calls)
            if n in HELD:
                # Sent while the call runs: held, so the model is not handed it
                # until the call returns, and the send answers at once.
                assert await _note(state, worker, conductor, text) == STEER_STEERED
                assert _steered_texts(wire) == NOTES[: n - 1]
                wire.inbound.put_nowait(_close_tool(tool))
            else:
                wire.inbound.put_nowait(_close_tool(tool))
                await _wait_closed(client, tool)
                sent = asyncio.ensure_future(_note(state, worker, conductor, text))
                spawned.append(sent)
            # Written at the step after the call, and only then.
            await async_wait_until(lambda n=n: len(wire.steering_frames()) == n)
            wire.answer(wire.steering_frames()[-1]["id"], "injected")
            if n not in HELD:
                assert await await_until(sent, what=f"note {n}") == STEER_STEERED
            await async_wait_until(lambda n=n: len(client._steers_accepted) == n)

        # Each note went to the harness once, in the order it was sent.
        assert _steered_texts(wire) == NOTES

        _fail_compaction(client, wire, monkeypatch)
        events = await await_until(turn, what="the turn")
        assert [e.stop_reason for e in events if e.kind == EVENT_COMPLETE] == [
            STOP_REASON_COMPACTION_FAILED
        ]
        await _teardown(state, worker, client)

    # Nothing comes back to run a second time.
    assert worker._queue == []
    assert worker._pending_steers == []
    # Each row says it went in and is not confirmed, once per note.
    rows = _steer_rows(worker)
    assert [r["content"] for r in rows] == NOTES
    assert {r["meta"]["steerState"] for r in rows} == {STEER_STATE_UNCONFIRMED}
    # The Conductor is told, once per note, that it may not have run.
    told = _notices(conductor)
    assert len(told) == len(NOTES)
    assert all("may not have run" in line for line in told)


@pytest.mark.asyncio
async def test_the_next_note_after_a_failed_turn_goes_into_the_next_turn(tmp_path, monkeypatch):
    """The note sent while the next turn runs is not refused ``turn_ended``."""
    state, conductor, worker = _pair(tmp_path)
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        _running(worker, client)
        sent = asyncio.ensure_future(_note(state, worker, conductor, NOTES[0]))
        spawned.append(sent)
        wire.answer(await wire.next_steering_id(), "injected")
        assert await await_until(sent, what="the first note") == STEER_STEERED
        _fail_compaction(client, wire, monkeypatch)
        await await_until(turn, what="the failed turn")
        await _teardown(state, worker, client)
        assert worker._queue == []

        # The next turn runs; a tool call is open when the next note comes.
        turn = await _start_turn(client, spawned)
        _running(worker, client)
        wire.inbound.put_nowait(_open_tool("next"))
        await async_wait_until(lambda: "next" in client._active_tool_calls)
        origin = session_deliver.peer(
            steer=True, carry=steer_carry.from_session(state, conductor.key)
        )
        outcome = await session_deliver.deliver(
            state,
            worker,
            NOTES[1],
            origin,
            admission=_admission(state, worker, conductor),
            relay_meta={"senderKey": conductor.key, "via": "session_send"},
        )
        assert outcome == session_deliver.DELIVERED_STEERED
        wire.inbound.put_nowait(_close_tool("next"))
        await async_wait_until(lambda: len(wire.steering_frames()) == 2)
        assert _steered_texts(wire) == NOTES[:2]
        wire.answer(wire.steering_frames()[-1]["id"], "injected")
        await async_wait_until(lambda: len(client._steers_accepted) == 1)
        wire.inbound.put_nowait(JsonRpcMessage(id=PROMPT_ID, result={"stopReason": "end_turn"}))
        await await_until(turn, what="the next turn")


@pytest.mark.asyncio
async def test_a_requeued_note_is_carried_in_ahead_of_the_new_one(tmp_path):
    """A note the turn never wrote (held when it ended) is requeued, as before. The
    Conductor's next note, sent while the next turn runs, takes it in first, then
    itself: never refused ``turn_ended`` behind it, and its old row stops reading
    "runs as its own message"."""
    state, conductor, worker = _pair(tmp_path)
    steered: list[str] = []

    class _Agent:
        supports_steer = True
        steer_needs_loss_recovery = True
        steers_taken_unconfirmed = frozenset()

        def has_active_turn(self):
            return True

        async def steer(self, text):
            steered.append(text)
            return True

    _running(worker, _Agent())
    assert await _note(state, worker, conductor, NOTES[0]) == STEER_STEERED
    # The turn ended before the note was written: nothing took it.
    await _teardown(state, worker, _Agent())
    (entry,) = worker._queue
    (old_row,) = _steer_rows(worker)
    assert old_row["meta"]["steerState"] == STEER_STATE_REQUEUED

    # The next turn runs (the drain started it with another entry).
    _running(worker, _Agent())
    origin = session_deliver.peer(steer=True, carry=steer_carry.from_session(state, conductor.key))
    outcome = await session_deliver.deliver(
        state,
        worker,
        NOTES[1],
        origin,
        admission=_admission(state, worker, conductor),
        relay_meta={"senderKey": conductor.key, "via": "session_send"},
    )

    assert outcome == session_deliver.DELIVERED_STEERED
    assert origin.carry.count == 1
    assert steered == [NOTES[0], NOTES[0], NOTES[1]]
    assert worker._queue == []
    assert old_row["meta"]["steerState"] == STEER_STATE_SUPERSEDED
    successor = _steer_rows(worker)[-2]
    assert successor["content"] == NOTES[0] and successor is not old_row
    assert old_row["meta"].get("supersededBy") == successor["meta"]["mid"]


@pytest.mark.parametrize("stop_reason", ["max_tokens", "end_turn", "cancelled", "refusal"])
@pytest.mark.asyncio
async def test_the_client_keeps_a_taken_steer_only_when_the_turn_fails(stop_reason):
    """``end_turn`` reports it consumed; a stop or a refusal hands it back to be
    queued as before; any other ending leaves it taken but unconfirmed."""
    async with _Tasks() as spawned:
        client = _client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        steer = asyncio.ensure_future(client.steer("keep going"))
        spawned.append(steer)
        wire.answer(await wire.next_steering_id(), "injected")
        assert await await_until(steer, what="the steer") is True
        if stop_reason == "cancelled":
            client._cancelled = True
        wire.end_turn(stop_reason)
        await await_until(turn, what="the turn")
        expected = {"keep going"} if stop_reason == "max_tokens" else set()
        assert client.steers_taken_unconfirmed == expected
        # The next turn starts with nothing taken.
        await _start_turn(client, spawned)
        assert client.steers_taken_unconfirmed == frozenset()
        wire.end_turn()


class _Took:
    """An agent whose failed turn took every steer it was handed."""

    supports_steer = True
    steer_needs_loss_recovery = True

    def __init__(self):
        self.steers_taken_unconfirmed: frozenset[str] = frozenset()

    def has_active_turn(self):
        return True

    async def steer(self, text):
        self.steers_taken_unconfirmed |= {text}
        return True


@pytest.mark.asyncio
async def test_a_persons_taken_steer_is_still_queued_by_an_error_end(tmp_path):
    """Only a peer's steer is settled unconfirmed: a person's typing runs again."""
    state, _conductor, worker = _pair(tmp_path)
    agent = _Took()
    _running(worker, agent)
    assert await steer_into_running_turn(state, worker, "mine", user_origin=True) == STEER_STEERED
    await _teardown(state, worker, agent)
    assert [entry["content"] for entry in worker._queue] == ["mine"]


@pytest.mark.asyncio
async def test_an_automation_steer_taken_is_still_queued_by_an_error_end(tmp_path):
    state, _conductor, worker = _pair(tmp_path)
    agent = _Took()
    _running(worker, agent)
    outcome = await steer_into_running_turn(
        state, worker, "a nudge", nudge_meta={"kind": "autonudge"}
    )
    assert outcome == STEER_STEERED
    await _teardown(state, worker, agent)
    assert [entry["content"] for entry in worker._queue] == ["a nudge"]
