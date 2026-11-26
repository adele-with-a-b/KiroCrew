"""A message the owner typed and queued runs as the owner's own turn.

Two places could run it as someone else's, and each keeps it apart:

* **The queue.** A steer that did not go in (a Bot conversation's turn the owner did
  not send, ``steer_refusal.NOT_OWNERS_TURN``, or any other cause) falls back to
  the queue seam (``queue_for_next_turn``), which puts it first: it runs next,
  alone, with the mark. A note queued before it runs after it, without the mark.
  Typed into any other running turn, their message goes in at the next step and
  runs there without their rights (``test_owner_message_read_at_next_step.py``).
* **The merge at the drain.** With ``dashboard.merge_queued_messages`` on, a run
  of waiting entries becomes one turn, and its owner mark is ``all`` over them.
  The merge (``_dequeue_next_message``) takes the entries a person sent first,
  without the entries no person sent.

The tests run real turns through ``_run_chat``, send the owner's message through
the real ``api_chat`` handler, queue notes through the real peer delivery
(``session_deliver.deliver``) and drain through the real
``_start_next_queued_turn``. Only the model is stubbed, and it reads the slot's
mark and the gate's own slot check (``owner_unverifiable_reason``) while its
turn runs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from test_acp_tool_identity import _stub_state

from kiro_crew import steer_refusal
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import chat_delivery
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_deliver
from kiro_crew.dashboard.chat_delivery import queue_for_next_turn
from kiro_crew.dashboard.chat_utils import _dequeue_next_message
from kiro_crew.dashboard.handlers.remote_dispatch_routes import owner_unverifiable_reason

REFUSED = "the running turn was not sent by the owner"
#: The longest any wait on a stubbed turn may take before the test fails: a bound on
#: a signal the test itself sets, never a barrier.
BOUND = 60


def _busy(slot):
    """``running`` is derived (``task is not None and not task.done()``)."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _slot(tmp_path, turns: list[tuple[str, bool, str]]):
    """A busy slot whose model records ``(prompt, owner mark, gate reason)`` per turn."""
    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

    state = _stub_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot._titled = True

    async def _stream(message):
        turns.append((message, slot._turn_owner_sent, owner_unverifiable_reason(slot)))
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield AcpEvent(kind=EVENT_COMPLETE)

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    return state, slot


def _type(state, slot, text: str) -> str:
    """The owner types *text* into the busy slot's composer: the real queue seam."""
    return queue_for_next_turn(
        state,
        slot,
        text,
        directive_user_origin=True,
        directive_owner_origin=True,
        composer_send=True,
    )


async def _note(state, slot, text: str) -> None:
    """Another session's ``session_send`` reaches the busy slot: the real peer delivery."""
    outcome = await session_deliver.deliver(
        state,
        slot,
        text,
        session_deliver.peer(steer=False, sender="dashboard:other"),
        admission=sc.containment_meta(state, slot),
    )
    assert outcome == session_deliver.DELIVERED_QUEUED


def _requeued_steer(state, slot, text: str) -> None:
    """The owner's steer the running turn ended before taking: the real requeue."""
    slot._pending_steers.append(text)
    slot._steer_user_origin[text] = True
    slot._steer_channel_origin[text] = False
    slot._steer_owner_origin[text] = True
    slot._steer_admissions[text] = sc.containment_meta(state, slot)
    cr._requeue_unconsumed_steers(state, slot)


async def _drain(state, slot, *, merge: bool = True) -> list[bool]:
    """End the running turn and run every turn the queue then starts, in order."""
    config = KiroCrewConfig()
    config.dashboard.merge_queued_messages = merge
    slot.task = None
    started: list[bool] = []
    with patch.object(KiroCrewConfig, "load", return_value=config):
        while slot._queue:
            ran = await cr._start_next_queued_turn(state, slot)
            started.append(ran)
            if not ran:
                break
            # The turn's own tail drains the next entry and replaces ``slot.task``.
            while slot.task is not None and not slot.task.done():
                await slot.task
    return started


def _prompts(turns: list[tuple[str, bool, str]]) -> list[str]:
    return [prompt for prompt, _, _ in turns]


@pytest.mark.asyncio
@pytest.mark.parametrize("note_first", [True, False])
async def test_a_typed_message_and_a_waiting_note_drain_as_two_turns(
    tmp_path, note_first: bool
) -> None:
    """The reported case: the person's go keeps its mark whichever arrived first."""
    turns: list[tuple[str, bool, str]] = []
    state, slot = _slot(tmp_path, turns)
    if note_first:
        await _note(state, slot, "a worker's note")
        _type(state, slot, "Q1a Go.")
    else:
        _type(state, slot, "Q1a Go.")
        await _note(state, slot, "a worker's note")

    await _drain(state, slot)

    assert len(turns) == 2, "the typed message and the note are never one turn"
    (typed, typed_mark, typed_gate), (note, note_mark, note_gate) = turns
    assert typed.endswith("Q1a Go.") and "a worker's note" not in typed
    assert (typed_mark, typed_gate) == (True, ""), "the person's turn, admitted by the gate"
    assert "a worker's note" in note and "Q1a Go." not in note
    assert (note_mark, note_gate) == (False, REFUSED), "the note's turn stays refused"
    assert slot.queue_depth == 0


@pytest.mark.asyncio
async def test_a_note_alone_has_no_mark(tmp_path) -> None:
    turns: list[tuple[str, bool, str]] = []
    state, slot = _slot(tmp_path, turns)
    await _note(state, slot, "a worker's note")

    await _drain(state, slot)

    assert [(mark, gate) for _, mark, gate in turns] == [(False, REFUSED)]


@pytest.mark.asyncio
@pytest.mark.parametrize("merge", [True, False])
async def test_a_note_never_gains_the_mark_by_waiting_beside_typed_messages(
    tmp_path, merge: bool
) -> None:
    """Notes before, between and after the person's messages: none runs marked."""
    turns: list[tuple[str, bool, str]] = []
    state, slot = _slot(tmp_path, turns)
    await _note(state, slot, "note one")
    _type(state, slot, "typed one")
    await _note(state, slot, "note two")
    _type(state, slot, "typed two")
    await _note(state, slot, "note three")

    await _drain(state, slot, merge=merge)

    for prompt, mark, gate in turns:
        if "note" in prompt:
            assert (mark, gate) == (False, REFUSED), prompt
            assert "typed" not in prompt, "a note's turn carries none of the person's words"
        else:
            assert (mark, gate) == (True, ""), prompt
    assert sum("note" in prompt for prompt in _prompts(turns)) >= 1
    assert slot.queue_depth == 0


@pytest.mark.asyncio
async def test_the_persons_messages_keep_their_order_and_the_notes_keep_theirs(tmp_path) -> None:
    """Several of each: the person's run first, in order, then the notes, in order."""
    turns: list[tuple[str, bool, str]] = []
    state, slot = _slot(tmp_path, turns)
    await _note(state, slot, "note one")
    _type(state, slot, "typed one")
    await _note(state, slot, "note two")
    _type(state, slot, "typed two")
    _type(state, slot, "typed three")

    await _drain(state, slot)

    (typed, typed_mark, _), (notes, notes_mark, _) = turns
    assert typed == "[3 queued messages merged]\n\ntyped one\n\ntyped two\n\ntyped three"
    assert typed_mark is True
    assert notes.startswith("[2 queued messages merged]\n\n")
    assert notes.index("note one") < notes.index("note two")
    assert notes_mark is False


@pytest.mark.asyncio
async def test_a_steer_requeued_at_the_turns_end_drains_as_the_persons_own_turn(
    tmp_path,
) -> None:
    """The steer path: the person's steer the turn never took, beside a waiting note."""
    turns: list[tuple[str, bool, str]] = []
    state, slot = _slot(tmp_path, turns)
    await _note(state, slot, "a worker's note")
    _requeued_steer(state, slot, "Q1a Go.")

    await _drain(state, slot)

    (typed, typed_mark, typed_gate), (note, note_mark, note_gate) = turns
    assert typed == "Q1a Go."
    assert (typed_mark, typed_gate) == (True, "")
    assert "a worker's note" in note
    assert (note_mark, note_gate) == (False, REFUSED)


@pytest.mark.asyncio
async def test_a_stop_between_the_two_turns_loses_neither(tmp_path, monkeypatch) -> None:
    """The person's turn is stopped before the note's starts: the note still waits,
    with its sender, and runs as its own unmarked turn at the next drain."""
    turns: list[tuple[str, bool, str]] = []
    state, slot = _slot(tmp_path, turns)
    await _note(state, slot, "a worker's note")
    _type(state, slot, "Q1a Go.")

    real_run = cr._run_chat
    ran: list[str] = []

    async def _stopped_after_the_first(the_state, the_slot, message, **kwargs):
        # The first turn's Stop: the turn ends with the queue held, as a soft
        # Stop leaves it (``_stopping`` up, the queue kept for the next drain).
        ran.append(message)
        if len(ran) == 1:
            the_slot._turn_owner_sent = kwargs.get("_directive_owner_origin", False)
            turns.append((message, the_slot._turn_owner_sent, owner_unverifiable_reason(the_slot)))
            the_slot._turn_owner_sent = False
            the_slot._stopping = True
            the_slot.task = None
            return
        await real_run(the_state, the_slot, message, **kwargs)

    monkeypatch.setattr(cr, "_run_chat", _stopped_after_the_first)
    config = KiroCrewConfig()
    config.dashboard.merge_queued_messages = True
    slot.task = None
    with patch.object(KiroCrewConfig, "load", return_value=config):
        assert await cr._start_next_queued_turn(state, slot) is True
        await slot.task
    assert _prompts(turns) == ["Q1a Go."], "the person's message ran, alone"
    (waiting,) = slot._queue
    assert waiting["content"] == "a worker's note", "the stop kept the note"
    assert "_directive_owner_origin" not in waiting and "_directive_user_origin" not in waiting

    await _drain(state, slot)

    assert len(turns) == 2
    note, note_mark, note_gate = turns[1]
    assert "a worker's note" in note and "Q1a Go." not in note
    assert (note_mark, note_gate) == (False, REFUSED)
    assert slot.queue_depth == 0


def test_the_merge_takes_the_persons_entries_and_leaves_the_rest_in_place(tmp_path) -> None:
    """The one merge point, by itself: what it takes and what it leaves queued."""
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.queue_append("note one")
    slot.queue_append("typed one", directive_user_origin=True, directive_owner_origin=True)
    slot.queue_append("note two")

    text, consumed = _dequeue_next_message(slot, merge_enabled=True)
    assert (text, [item["content"] for item in consumed]) == ("typed one", ["typed one"])
    assert [item["content"] for item in slot._queue] == ["note one", "note two"]

    text, consumed = _dequeue_next_message(slot, merge_enabled=True)
    assert text == "[2 queued messages merged]\n\nnote one\n\nnote two"
    assert all("_directive_owner_origin" not in item for item in consumed)
    assert slot._queue == []


# ── typed while a turn runs: the steer seam, and the mark it never grants ──


class _Live:
    """A slot whose FIRST turn is real, running, and held open until released.

    The turn's agent takes steers, so a typed message reaches the steer seam's
    own decision and not an earlier fall-back.
    """

    def __init__(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        self.turns: list[tuple[str, bool, str]] = []
        self.state = _stub_state(tmp_path)
        self.slot = self.state.get_or_create_slot("chat-1")
        self.slot._titled = True
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.agent = MagicMock(supports_steer=True, steer_needs_loss_recovery=False)
        self.agent.steer = AsyncMock(return_value=True)
        self.agent.has_active_turn = lambda: True
        live = self

        async def _stream(message):
            slot = live.slot
            live.turns.append((message, slot._turn_owner_sent, owner_unverifiable_reason(slot)))
            if len(live.turns) == 1:
                live.entered.set()
                await asyncio.wait_for(live.release.wait(), BOUND)
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield AcpEvent(kind=EVENT_COMPLETE)

        provider = MagicMock()
        provider.stream = _stream
        provider.stream_command = _stream
        provider.context_usage_pct = MagicMock(return_value=1.0)
        provider.client = self.agent
        self.state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        config = KiroCrewConfig()
        config.dashboard.merge_queued_messages = False
        monkeypatch.setattr(KiroCrewConfig, "load", lambda *a, **k: config)

    async def open_with(self, opener: str) -> None:
        """Start the first turn the way *opener* does and wait until its model runs."""
        state, slot = self.state, self.slot
        if opener == "note":
            outcome = await session_deliver.deliver(
                state,
                slot,
                "a worker's note",
                session_deliver.peer(steer=False, sender="dashboard:other"),
                admission=sc.containment_meta(state, slot),
            )
            assert outcome == session_deliver.DELIVERED_TURN
        elif opener == "notice":
            outcome = await session_deliver.deliver(
                state, slot, "the nightly job finished", session_deliver.cron("nightly")
            )
            assert outcome == session_deliver.DELIVERED_TURN
        elif opener == "wake":
            slot.task = asyncio.ensure_future(
                cr._run_chat(state, slot, "check the work ledger", _directive_patrol_wake=True)
            )
        else:
            slot.task = asyncio.ensure_future(
                cr._run_chat(
                    state,
                    slot,
                    "the owner's own request",
                    _directive_user_origin=True,
                    _directive_owner_origin=True,
                )
            )
        await asyncio.wait_for(self.entered.wait(), BOUND)

    async def type(self, text: str) -> dict:
        """The owner types *text* into the running chat and asks for a steer."""
        async with TestClient(TestServer(_make_app(self.state))) as http:
            resp = await http.post(
                "/api/chat?ws=1", json={"message": text, "slot": self.slot.key, "steer": True}
            )
            assert resp.status == 200
            return await resp.json()

    async def finish(self) -> None:
        """Let the first turn end and run every turn its queue then starts."""
        self.release.set()
        while self.slot.task is not None and not self.slot.task.done():
            await asyncio.wait_for(asyncio.shield(self.slot.task), BOUND)

    def waiting(self) -> list[str]:
        return [item["content"] for item in self.slot._queue]

    def marks(self) -> list[tuple[bool, str]]:
        return [(mark, gate) for _, mark, gate in self.turns]


@pytest.mark.asyncio
async def test_ledger_news_arriving_mid_turn_leaves_the_owners_turn_its_mark(
    tmp_path, monkeypatch
) -> None:
    """A work-ledger wake is held out of a turn that holds the owner's mark: not
    delivered now (its sender keeps it owed for the turn's end), nothing written
    to the agent, and the turn's rights stand until it ends."""
    live = _Live(tmp_path, monkeypatch)
    await live.open_with("owner")

    outcome = await session_deliver.deliver(
        live.state,
        live.slot,
        "[work-ledger wake] item=it_1 status=done",
        session_deliver.automation(1, "loop-1"),
        admission=sc.containment_meta(live.state, live.slot),
    )

    assert outcome == session_deliver.NOT_DELIVERED
    live.agent.steer.assert_not_awaited()
    assert live.slot._pending_steers == [] and live.waiting() == []
    assert live.slot._turn_owner_sent is True
    assert owner_unverifiable_reason(live.slot) == ""
    await live.finish()
    assert live.marks() == [(True, "")]
    assert live.slot._turn_owner_sent is False, "the mark ends with the turn, as always"


@pytest.mark.asyncio
@pytest.mark.parametrize("opener", ["note", "wake"])
async def test_ledger_news_still_joins_a_turn_the_owner_did_not_send(
    tmp_path, monkeypatch, opener: str
) -> None:
    """Only the owner's marked turn is left alone: every other turn takes the wake."""
    live = _Live(tmp_path, monkeypatch)
    await live.open_with(opener)

    outcome = await session_deliver.deliver(
        live.state,
        live.slot,
        "[work-ledger wake] item=it_1 status=done",
        session_deliver.automation(1, "loop-1"),
        admission=sc.containment_meta(live.state, live.slot),
    )

    assert outcome == session_deliver.DELIVERED_STEERED
    live.agent.steer.assert_awaited_once()
    live.slot._pending_steers.clear()  # the stubbed agent sends no consumed echo
    await live.finish()


@pytest.mark.asyncio
async def test_typed_during_the_owners_own_turn_steers_as_before(tmp_path, monkeypatch) -> None:
    live = _Live(tmp_path, monkeypatch)
    await live.open_with("owner")

    receipt = await live.type("and also this")

    assert receipt.get("steered") is True
    live.agent.steer.assert_awaited_once()
    assert live.waiting() == []
    assert live.slot._turn_owner_sent is True, "the owner's steer leaves the mark"
    live.slot._pending_steers.clear()  # the stubbed agent sends no consumed echo
    await live.finish()
    assert live.marks() == [(True, "")]


@pytest.mark.asyncio
async def test_a_stop_of_the_running_turn_loses_neither_message(tmp_path, monkeypatch) -> None:
    """A Stop that resolved while the turn wound up, their steer still unread: the
    drain still runs it next, as their own turn, and the note after it."""
    live = _Live(tmp_path, monkeypatch)
    await live.open_with("note")
    await _note(live.state, live.slot, "a later note")
    await live.type("Q1a Go.")
    live.slot._stopping = True

    await live.finish()

    assert [prompt for prompt, _, _ in live.turns][1] == "Q1a Go."
    assert "a later note" in live.turns[2][0]
    assert live.marks()[1:] == [(True, ""), (False, REFUSED)]


def _foreign_turn(tmp_path):
    """A busy slot whose running turn the owner did not send, with a steerable agent."""
    state = _stub_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot._acp_client = MagicMock(supports_steer=True, steer_needs_loss_recovery=False)
    slot._acp_client.steer = AsyncMock(return_value=True)
    slot._acp_client.has_active_turn = lambda: True
    return state, slot


async def _steer(state, slot, **origin) -> str:
    return await chat_delivery.steer_into_running_turn(
        state, slot, "redirect", admission=sc.containment_meta(state, slot), **origin
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        {"user_origin": True, "owner_origin": True},
        {"user_origin": True, "owner_origin": False},
        {"user_origin": False, "owner_origin": False},
        {"user_origin": True, "owner_origin": True, "channel_origin": True},
    ],
    ids=["the owner", "another dashboard user", "a peer", "a channel"],
)
async def test_a_steer_never_gives_the_turn_the_mark(tmp_path, origin: dict) -> None:
    """Never the reverse fix: every steer goes in, and none earns the mark."""
    state, slot = _foreign_turn(tmp_path)

    assert await _steer(state, slot, **origin) == chat_delivery.STEER_STEERED
    assert slot._turn_owner_sent is False and slot._turn_user_sent is False
    slot._acp_client.steer.assert_awaited_once()
    (wire,) = slot._acp_client.steer.await_args.args
    if origin == {"user_origin": True, "owner_origin": True}:
        # Only the owner's words are marked as theirs in a turn another sender opened.
        assert wire.startswith("[The owner's own message ")
        assert wire.endswith("]\nredirect\n[end of the owner's message " + wire[25:37] + "]")
    else:
        assert wire == "redirect"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["_created_by", "_lineage_minted"])
async def test_a_chat_opened_for_another_caller_still_takes_the_owners_steer(
    tmp_path, field: str
) -> None:
    """A worker's chat: the gate refuses its slot whoever sent the turn, so the
    owner's steer loses no crew right there and keeps redirecting the worker."""
    state, slot = _foreign_turn(tmp_path)
    setattr(slot, field, "dashboard:conductor" if field == "_created_by" else True)

    outcome = await _steer(state, slot, user_origin=True, owner_origin=True)

    assert outcome == chat_delivery.STEER_STEERED
    assert slot._turn_owner_sent is False


def test_only_the_owners_entry_goes_first_whatever_code_a_caller_names(tmp_path) -> None:
    """The front of the queue is not for sale: the code alone moves nothing."""
    state, slot = _foreign_turn(tmp_path)
    slot.queue_append("a waiting note")
    code = steer_refusal.NOT_OWNERS_TURN

    queue_for_next_turn(state, slot, "no marks", steer_refused=code)
    queue_for_next_turn(
        state, slot, "a person, not the owner", directive_user_origin=True, steer_refused=code
    )
    queue_for_next_turn(
        state,
        slot,
        "a channel's",
        directive_user_origin=True,
        directive_owner_origin=True,
        directive_channel_origin=True,
        steer_refused=code,
    )
    assert [item["content"] for item in slot._queue][0] == "a waiting note"

    queue_for_next_turn(
        state,
        slot,
        "the owner's",
        directive_user_origin=True,
        directive_owner_origin=True,
        steer_refused=code,
    )
    assert [item["content"] for item in slot._queue] == [
        "the owner's",
        "a waiting note",
        "no marks",
        "a person, not the owner",
        "a channel's",
    ]


def test_a_kept_out_message_never_passes_the_owners_earlier_one(tmp_path) -> None:
    """The owner's "do A" queued for another cause behind a note, then "then B"
    kept out: A comes to the front with B and still runs first, both before
    every note, and open tabs get the order."""
    state, slot = _foreign_turn(tmp_path)
    slot.queue_append("a waiting note")
    queue_for_next_turn(
        state,
        slot,
        "do A",
        directive_user_origin=True,
        directive_owner_origin=True,
        steer_refused=steer_refusal.BETWEEN_STEPS,
    )
    queue_for_next_turn(state, slot, "a later note")
    state.broadcast_ws.reset_mock()

    queue_for_next_turn(
        state,
        slot,
        "then B",
        directive_user_origin=True,
        directive_owner_origin=True,
        steer_refused=steer_refusal.NOT_OWNERS_TURN,
    )

    assert [item["content"] for item in slot._queue] == [
        "do A",
        "then B",
        "a waiting note",
        "a later note",
    ]
    frames = [c.args for c in state.broadcast_ws.call_args_list]
    assert [f[0] for f in frames] == ["queue_push", "queue_reorder"]
    assert frames[1][1]["order"] == [item["id"] for item in slot._queue]


def test_a_kept_out_message_already_last_sends_no_reorder(tmp_path) -> None:
    state, slot = _foreign_turn(tmp_path)
    queue_for_next_turn(
        state,
        slot,
        "only one",
        directive_user_origin=True,
        directive_owner_origin=True,
        steer_refused=steer_refusal.NOT_OWNERS_TURN,
    )
    assert [c.args[0] for c in state.broadcast_ws.call_args_list] == ["queue_push"]


def test_the_sender_is_told_it_runs_next_as_their_own_turn() -> None:
    sentence = steer_refusal.describe(steer_refusal.NOT_OWNERS_TURN)
    assert "you did not start the running turn" in sentence
    assert "It runs next, as your own turn" in sentence
