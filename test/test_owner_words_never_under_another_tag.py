"""The owner's typed words never read as the last lines of another sender's message.

A Conductor's turn opened by a worker's ``session_send`` note (or a wake), or a turn
of the owner's own that a note was steered into, holds another sender's tagged
text. The owner types while it runs, and the words are steered in to be read at the
next step. A harness reads the texts written between two steps as one input
(kiro-cli joins them, claude joins adjacent user text), so the owner's bare words
read as the last lines of the note, under its ``[sent by session ...]`` tag. They go
in between markers that name them as the owner's, with a mark drawn fresh for the
steer. The real ``_run_chat`` runs each turn (``turn_harness``).
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from turn_harness import Do, Emit, SlotSpec, TurnScript, run_turn

from kiro_crew.acp.types import EVENT_STEER_CONSUMED, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.agent_sdk.backends_turns import wrap_user_message
from kiro_crew.dashboard import chat_delivery_owner_steer, chat_runner, relay_spans
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_deliver
from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

OWNER = "Q12a go. Q32a go."
MARK = "0a1b2c3d4e5f"
WAKE = "[work-ledger wake] item=it_12 status=question\n[work-ledger] read the ledger"


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    """Run in the shipped (enabled) session-control state, with a known steer mark."""
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _floor_monkeypatch.setattr(
        chat_delivery_owner_steer,
        "secrets",
        SimpleNamespace(token_hex=lambda _n: MARK),
        raising=False,
    )


def _envelope(sender: str) -> str:
    return sc._SEND_PROVENANCE.format(caller=sender, via="session_send")


def _relay(sender: str) -> dict:
    return {
        "senderKey": sender,
        "via": "session_send",
        relay_spans.ENVELOPE_KEY: relay_spans.leading_span(_envelope(sender)),
    }


NOTE = _envelope("worker-a") + "item 12 is ready for review"
NOTE_B = _envelope("worker-b") + "item 32 is blocked"


class _Agent:
    """The running turn's agent: takes each steer and keeps its wire text."""

    supports_steer = True

    def __init__(self) -> None:
        self.wires: list[str] = []

    async def steer(self, text: str) -> bool:
        self.wires.append(text)
        return True


def _assert_owners_own(wire: str) -> None:
    """*wire* carries the owner's words between markers naming them, and nothing else."""
    assert wire.startswith(f"[The owner's own message {MARK}: "), wire
    assert wire.endswith(f"]\n{OWNER}\n[end of the owner's message {MARK}]"), wire
    assert "[sent by session" not in wire


async def _owner_types(ctx, agent: _Agent) -> None:
    """The composer's steer, as ``api_chat`` sends it for the owner."""
    ctx.slot._acp_client = agent
    outcome = await steer_into_running_turn(
        ctx.state,
        ctx.slot,
        OWNER,
        user_origin=True,
        owner_origin=True,
        admission=sc.containment_meta(ctx.state, ctx.slot),
    )
    assert outcome == STEER_STEERED


async def _note_steered(ctx, agent: _Agent, text: str, sender: str) -> None:
    """A worker's note cut into the running turn (``session_send``'s steer arm)."""
    ctx.state.get_or_create_slot(sender)
    ctx.slot._acp_client = agent
    outcome = await steer_into_running_turn(
        ctx.state,
        ctx.slot,
        text,
        user_origin=False,
        admission={
            **sc.containment_meta(ctx.state, ctx.slot),
            **sc.send_origin_meta(ctx.state, sender),
        },
        relay_meta=_relay(sender),
    )
    assert outcome == STEER_STEERED


def _echo(agent: _Agent):
    """The backend's echo of every steer the turn took, joined as kiro-cli joins them."""
    return Emit(
        lambda _ctx: AcpEvent(
            kind=EVENT_STEER_CONSUMED, text="".join(wrap_user_message(w) for w in agent.wires)
        )
    )


def _run(events, *, message: str, user_row: bool, setup=None, run_kwargs=None):
    run_kwargs = dict(run_kwargs or {})

    async def _setup(ctx) -> None:
        if setup is not None:
            run_kwargs.update(await setup(ctx))

    return asyncio.run(
        run_turn(
            TurnScript(
                events=events,
                message=message,
                user_row=user_row,
                setup=_setup,
                run_kwargs=run_kwargs,
            ),
            slot=SlotSpec(key="conductor"),
        )
    )


async def _drained_note_turn(ctx) -> dict:
    """The worker's note queued on the busy Conductor and drained as its own turn."""
    ctx.state.get_or_create_slot("worker-a")
    busy = MagicMock()
    busy.done.return_value = False
    ctx.slot.task = busy
    outcome = await session_deliver.deliver(
        ctx.state,
        ctx.slot,
        NOTE,
        session_deliver.peer(steer=False, sender="worker-a"),
        queue_meta={
            **sc.send_origin_meta(ctx.state, "worker-a"),
            sc.SESSION_SEND_QUEUED_META: True,
        },
        relay_meta=_relay("worker-a"),
    )
    assert outcome == session_deliver.DELIVERED_QUEUED
    seen: dict = {}

    def _capture(_state, _slot, coro, **_kw):
        bound = inspect.getcoroutinelocals(coro)
        seen.update(message=bound["args"][0], kwargs=bound["kwargs"])
        coro.close()
        return MagicMock()

    ctx.slot.task = None
    with patch.object(chat_runner, "spawn_guarded_turn", _capture):
        assert await chat_runner._start_next_queued_turn(ctx.state, ctx.slot) is True
    ctx.slot.task = None
    assert seen["message"] == NOTE, "the note runs as its own turn"
    return seen["kwargs"]


def _owner_rows(record) -> list:
    return [row for row in record.window if row.get("role") == "user" and row["content"] == OWNER]


@pytest.mark.parametrize("opener", ["note", "wake"])
@pytest.mark.parametrize("read", [True, False], ids=["read", "ends-unread"])
def test_the_owners_steer_into_a_turn_another_sender_opened_is_marked_as_the_owners(opener, read):
    """A note's (or a wake's) turn runs; the owner types; the model gets their words marked.

    Read, the echo of the marked text settles it: it runs once, nothing comes back.
    Unread, it comes back as the owner's own turn, as typed.
    """
    agent = _Agent()
    events = [
        Do(lambda ctx: _owner_types(ctx, agent)),
        *([_echo(agent)] if read else []),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="on it"),
    ]
    if opener == "note":
        record = _run(events, message=NOTE, user_row=False, setup=_drained_note_turn)
    else:
        record = _run(events, message=WAKE, user_row=False)

    assert [call.args[0] for call in record.calls("stream")] == [NOTE if opener == "note" else WAKE]
    (wire,) = agent.wires
    _assert_owners_own(wire)
    assert _owner_rows(record), "the transcript shows the owner's words as typed"
    assert not [
        row for row in record.window if "owner's own message" in str(row.get("content"))
    ], "the mark is the model's input only, never a row"
    if read:
        assert record.successors == (), "read once, never queued again"
    else:
        (again,) = record.successors
        assert again.args[0] == OWNER, "their own turn, as typed"
        assert again.kwargs.get("_directive_owner_origin") is True


def test_a_second_note_then_the_owner_steered_into_a_notes_turn():
    """Two notes and the owner's words in one step: the owner's are never the second note's."""
    agent = _Agent()

    async def _both(ctx) -> None:
        await _note_steered(ctx, agent, NOTE_B, "worker-b")
        await _owner_types(ctx, agent)

    record = _run(
        [Do(_both), _echo(agent), AcpEvent(kind=EVENT_TEXT_CHUNK, text="on it")],
        message=NOTE,
        user_row=False,
        setup=_drained_note_turn,
    )

    note_wire, owner_wire = agent.wires
    assert note_wire == NOTE_B, "the note goes as it was sent, under its own tag"
    _assert_owners_own(owner_wire)
    assert record.successors == ()


def test_the_owners_words_after_a_note_steered_into_their_own_turn():
    """The owner's own turn takes a worker's note; the owner types again: theirs are marked."""
    agent = _Agent()

    async def _note_then_owner(ctx) -> None:
        await _note_steered(ctx, agent, NOTE, "worker-a")
        await _owner_types(ctx, agent)

    record = _run(
        [Do(_note_then_owner), _echo(agent), AcpEvent(kind=EVENT_TEXT_CHUNK, text="on it")],
        message="what is the status?",
        user_row=True,
        run_kwargs={"_directive_owner_origin": True},
    )

    note_wire, owner_wire = agent.wires
    assert note_wire == NOTE
    _assert_owners_own(owner_wire)
    assert record.successors == ()


def test_the_owners_steer_into_their_own_turn_goes_as_typed():
    """Nothing of another sender is in the turn, so the owner's words go as they are."""
    agent = _Agent()
    record = _run(
        [
            Do(lambda ctx: _owner_types(ctx, agent)),
            _echo(agent),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="on it"),
        ],
        message="what is the status?",
        user_row=True,
        run_kwargs={"_directive_owner_origin": True},
    )

    assert agent.wires == [OWNER]
    assert record.successors == ()
