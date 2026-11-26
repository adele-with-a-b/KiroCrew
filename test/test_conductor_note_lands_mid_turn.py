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
the real ``steer_into_running_turn``, the real turn release
(``_turn_release_handles``) and requeue, and the real carry run here; only the
adapter's stdio is scripted, and the rest of the hand-off (starting the next
turn) is driven by each test.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
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

from kiro_crew import ledger_wake
from kiro_crew.acp import client_turn
from kiro_crew.acp.types import EVENT_COMPLETE, STOP_REASON_COMPACTION_FAILED, JsonRpcMessage
from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import chat_runner, queue_age
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_deliver, steer_carry
from kiro_crew.dashboard.chat_delivery import (
    STEER_STATE_REQUEUED,
    STEER_STATE_SUPERSEDED,
    STEER_STEERED,
    steer_into_running_turn,
)
from kiro_crew.probes import work_ledger as work_ledger_probe
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
    """The turn's real release (``_turn_release_handles``), then the first step of
    its hand-off (``_hand_off_queue`` runs ``_requeue_unconsumed_steers`` first).
    The rest of the hand-off starts the next turn, which these tests drive
    themselves."""
    worker._acp_client = client
    turn = SimpleNamespace(
        slot=worker,
        state=state,
        client=MagicMock(),
        _native_card_output={},
        _native_tracker={},
    )
    await chat_runner._turn_release_handles(turn)
    assert worker._acp_client is None
    worker.task = None
    chat_runner._requeue_unconsumed_steers(state, worker)


@pytest.mark.asyncio
async def test_nine_notes_in_a_long_turn_each_go_in_once_in_order(tmp_path, monkeypatch):
    state, conductor, worker = _pair(tmp_path)
    told_about: list[str] = []
    notify = sc.notify_send_origin_discarded

    def _told(state, **kw):
        told_about.append(kw["text"])
        return notify(state, **kw)

    monkeypatch.setattr(sc, "notify_send_origin_discarded", _told)
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
    # The Conductor is told, once per note and oldest first, that it may not have run.
    told = _notices(conductor)
    assert len(told) == len(NOTES)
    assert all("may not have run" in line for line in told)
    assert told_about == NOTES
    # Its unread records are kept in the same order, so the cap drops the oldest.
    sent = [at for _origin, at in worker._steer_unconfirmed_waits.values()]
    assert sent == sorted(sent)


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


@pytest.mark.parametrize(
    "stop_reason", ["max_tokens", "end_turn", "cancelled", "cancelled_unprompted", "refusal"]
)
@pytest.mark.asyncio
async def test_the_client_keeps_a_taken_steer_only_when_the_turn_fails(stop_reason):
    """``end_turn`` reports it consumed; a stop or a refusal hands it back to be
    queued as before; any other ending leaves it taken but unconfirmed. A
    ``cancelled`` stop Crew never asked for (the adapter's own, when a person
    declines a plan) is a stop too, not a failed turn."""
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
        wire.end_turn(stop_reason.removesuffix("_unprompted"))
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


@pytest.mark.parametrize(
    "sender",
    ["hub_conductor", "app_answer", "closed", "key_reused"],
)
@pytest.mark.asyncio
async def test_a_taken_steer_whose_sender_cannot_be_told_is_requeued(tmp_path, sender):
    """A steer is settled unconfirmed only once its sender's notice landed. One with
    no send origin (a hub conductor's, an app's question-card answer) or whose sender
    is gone (closed, or its key now held by another tab) is queued again, as before,
    so it is never dropped with nobody told."""
    state, conductor, worker = _pair(tmp_path)
    agent = _Took()
    _running(worker, agent)
    if sender == "hub_conductor":
        admission = {**sc.containment_meta(state, worker), cd.TURN_ACTOR_META_KEY: "crew"}
        outcome = await steer_into_running_turn(
            state, worker, NOTES[0], user_origin=False, admission=admission
        )
    elif sender == "app_answer":
        outcome = await steer_into_running_turn(
            state,
            worker,
            NOTES[0],
            user_origin=False,
            admission=sc.containment_meta(state, worker),
            app_label="an-app",
            app_actor=True,
        )
    else:
        outcome = await _note(state, worker, conductor, NOTES[0])
    assert outcome == STEER_STEERED
    if sender == "closed":
        state._slots.pop(conductor.key)
    elif sender == "key_reused":
        conductor._tab_id = "another-tab"

    await _teardown(state, worker, agent)

    assert [entry["content"] for entry in worker._queue] == [NOTES[0]]
    (row,) = _steer_rows(worker)
    assert row["meta"]["steerState"] == STEER_STATE_REQUEUED
    assert _notices(conductor) == []
    # Queued again, it counts as a plain unread wait, never an unconfirmed one.
    found = queue_age.sender_unread(state, worker.key, conductor.key)
    assert found is None or not ledger_wake.is_unconfirmed_wait(found[2])


def _probe(state):
    """The work-ledger watch, reading the worker's slot through the real resolver,
    and the item it watches. Its ``_spend`` is the real one, writing the rate file
    under this test's crew home."""
    probe = work_ledger_probe.WorkLedgerProbe(
        worker_unread=lambda w, c: queue_age.sender_unread(state, w, c)
    )
    return probe, SimpleNamespace(item_id="it_00000001", worker_instance_id="")


@pytest.mark.asyncio
async def test_a_settled_note_wakes_its_conductor_at_once(tmp_path, monkeypatch):
    """Nothing runs a settled note again, so the Conductor's work-ledger watch is
    pulled forward and its tick wakes it with ``reason=message_unconfirmed`` now,
    not after the unread threshold, and only once. The wait counts until the
    Conductor sends to the worker again."""
    from kiro_crew import conductor_wake

    pushed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        conductor_wake,
        "wake_for_unconfirmed_note",
        lambda worker_key, sender_key: pushed.append((worker_key, sender_key)),
    )
    state, conductor, worker = _pair(tmp_path)
    agent = _Took()
    _running(worker, agent)
    for text in NOTES[:2]:
        assert await _note(state, worker, conductor, text) == STEER_STEERED

    await _teardown(state, worker, agent)
    await async_wait_until(lambda: len(pushed) == 2)

    assert worker._queue == []
    assert pushed == [(worker.key, conductor.key)] * 2
    count, oldest_secs, oldest_id = queue_age.sender_unread(state, worker.key, conductor.key)
    assert count == 2
    assert ledger_wake.is_unconfirmed_wait(oldest_id)
    # Overdue at once, whatever the age: a fresh note wakes too.
    assert oldest_secs is not None and oldest_secs < ledger_wake.UNREAD_WAKE_SECS
    assert ledger_wake.is_unread_overdue(oldest_secs, oldest_id)
    # Another sender's read sees none of them.
    assert queue_age.sender_unread(state, worker.key, "chat-someone-else") is None

    probe, item = _probe(state)
    item.worker_session_key = worker.key
    (wake,) = probe._unread_observations(conductor.key, item)
    assert "reason=message_unconfirmed count=2" in wake.brief
    # The next tick reads the same wait and the real spend refuses it: one wake.
    assert probe._unread_observations(conductor.key, item) == []

    # A turn the worker starts meanwhile does not answer it; the sender's next send does.
    worker._turns_started += 1
    assert queue_age.sender_unread(state, worker.key, conductor.key) is not None
    queue_age.forget_unconfirmed(worker, "chat-someone-else")
    assert queue_age.sender_unread(state, worker.key, conductor.key) is not None
    queue_age.forget_unconfirmed(worker, conductor.key)
    assert queue_age.sender_unread(state, worker.key, conductor.key) is None


@pytest.mark.asyncio
async def test_a_settled_note_wakes_its_conductor_when_the_next_turn_starts_at_once(
    tmp_path, monkeypatch
):
    """The worker's queue held another message when the turn failed, so the hand-off
    starts that turn straight away. The settled notes still wake the Conductor."""
    from unittest.mock import patch

    from kiro_crew import conductor_wake

    monkeypatch.setattr(conductor_wake, "wake_for_unconfirmed_note", lambda *a: None)
    state, conductor, worker = _pair(tmp_path)
    agent = _Took()
    _running(worker, agent)
    for text in NOTES[:7]:
        assert await _note(state, worker, conductor, text) == STEER_STEERED
    worker.queue_append("a message queued behind the turn")
    started = worker._turns_started

    worker._acp_client = agent
    turn = SimpleNamespace(
        slot=worker, state=state, client=MagicMock(), _native_card_output={}, _native_tracker={}
    )
    await chat_runner._turn_release_handles(turn)
    worker.task = None
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
        patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
    ):
        await chat_runner._hand_off_queue(state, worker, drain=True, allow_automatic_successor=True)
    assert spawn.call_count == 1, "the queued message started the next turn"
    assert worker._queue == []
    # The started turn passes its dispatch gates (``turn_dispatch_gates``).
    worker._turns_started = started + 1

    probe, item = _probe(state)
    item.worker_session_key = worker.key
    (wake,) = probe._unread_observations(conductor.key, item)
    assert "reason=message_unconfirmed count=7" in wake.brief


@pytest.mark.asyncio
async def test_an_unconfirmed_wake_does_not_re_arm_an_older_unread_one(tmp_path, monkeypatch):
    """A queued note of the Conductor's already woke it as unread. A later note the
    failed turn took in wakes it again, as news; once that one stops counting, the
    older queued note does not wake it a second time."""
    from kiro_crew import conductor_wake

    monkeypatch.setattr(conductor_wake, "wake_for_unconfirmed_note", lambda *a: None)
    state, conductor, worker = _pair(tmp_path)
    agent = _Took()
    _running(worker, agent)
    worker.queue_append(
        "an older note",
        meta={**sc.send_origin_meta(state, conductor.key), queue_age.STEER_WANTED_META: True},
    )
    (entry,) = worker._queue
    queue_age.note_queued(worker, entry["id"])
    worker._queued_at[entry["id"]] -= ledger_wake.UNREAD_WAKE_SECS + 1

    probe, item = _probe(state)
    item.worker_session_key = worker.key
    (first,) = probe._unread_observations(conductor.key, item)
    assert "reason=message_unread" in first.brief

    assert await _note(state, worker, conductor, NOTES[0]) == STEER_STEERED
    await _teardown(state, worker, agent)
    assert [e["content"] for e in worker._queue] == ["an older note"]
    (second,) = probe._unread_observations(conductor.key, item)
    assert "reason=message_unconfirmed" in second.brief

    queue_age.forget_unconfirmed(worker, conductor.key)
    assert queue_age.sender_unread(state, worker.key, conductor.key) is not None
    assert probe._unread_observations(conductor.key, item) == []


def test_past_the_cap_the_oldest_unconfirmed_record_goes(tmp_path):
    state, conductor, worker = _pair(tmp_path)
    admission = _admission(state, worker, conductor)
    for n in range(queue_age._MAX_UNCONFIRMED + 1):
        worker._steered_at[f"n{n}"] = (float(n + 1), True)
        queue_age.note_unconfirmed(worker, f"n{n}", admission)
    sent = [at for _origin, at in worker._steer_unconfirmed_waits.values()]
    assert sent == [float(n + 1) for n in range(1, queue_age._MAX_UNCONFIRMED + 1)]


def test_the_push_reaches_only_the_conductor_the_worker_is_bound_to(monkeypatch):
    from kiro_crew import conductor_wake, work_ledger

    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(
        conductor_wake, "wake_for_undelivered_report", lambda c, i: fired.append((c, i))
    )
    monkeypatch.setattr(work_ledger, "read_binding", lambda key: ("chat-conductor", "it_00000001"))
    conductor_wake.wake_for_unconfirmed_note("chat-worker", "dashboard_chat-conductor")
    conductor_wake.wake_for_unconfirmed_note("chat-worker", "chat-someone-else")
    monkeypatch.setattr(work_ledger, "read_binding", lambda key: None)
    conductor_wake.wake_for_unconfirmed_note("chat-worker", "chat-conductor")
    assert fired == [("chat-conductor", "it_00000001")]


@pytest.mark.asyncio
async def test_a_refused_carry_puts_a_requeued_note_back_with_no_receipt(tmp_path):
    """The requeue withheld the note's receipt (the harness reports a read only at a
    clean end, so the turn may have read it). A carry whose steer is refused puts
    it back the same way: still no receipt, so a restart never sends it again on
    that proof."""
    import json

    from kiro_crew.dashboard import peer_note_receipts

    state, conductor, worker = _pair(tmp_path)

    class _Agent:
        supports_steer = True
        steer_needs_loss_recovery = True
        steers_taken_unconfirmed = frozenset()
        refuse = False

        def has_active_turn(self):
            return True

        async def steer(self, text):
            return not self.refuse

    agent = _Agent()
    _running(worker, agent)
    assert await _note(state, worker, conductor, NOTES[0]) == STEER_STEERED
    await _teardown(state, worker, agent)
    (entry,) = worker._queue

    def _receipts() -> dict:
        path = peer_note_receipts._path()
        return json.loads(path.read_text("utf-8")) if path.exists() else {}

    assert entry["id"] not in _receipts()

    agent.refuse = True
    _running(worker, agent)
    origin = session_deliver.peer(steer=True, carry=steer_carry.from_session(state, conductor.key))
    await session_deliver.deliver(
        state,
        worker,
        NOTES[1],
        origin,
        admission=_admission(state, worker, conductor),
        relay_meta={"senderKey": conductor.key, "via": "session_send"},
    )

    assert entry in worker._queue
    assert entry["id"] not in _receipts()
