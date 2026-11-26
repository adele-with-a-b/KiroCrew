"""The owner's typed message goes into the running turn, whoever opened it.

A note from another session, a wake or an automation opens a turn, and the owner
types while it runs. The message is steered into that turn and the agent reads it
when the tool call in flight ends, the next step, rather than after every step the
turn still takes.

The owner's rights stay as they were: a steer never gives a turn the owner's mark, so
inside that turn the text runs without it, and a ``crew=`` step it asks for is
refused there. The row says so (``steerWithoutOwnerRights``), so the cost is in
front of the owner rather than found later, and a refusal brings it back as their
own turn (``test_owner_steer_delivered_again.py``).

Two places keep the queue at the front instead, because going in there is worse
for the owner:

* **A Bot conversation** (the responsive path): its queue arm stops the running
  turn and runs the message at once, as the owner's own turn, with their rights.
* **A manual ``/compact``**: no prompt runs that a steer could join.

An open approval card in that turn is not a third: the steer goes in and the card
stays open for its own sender; in the owner's own turn the steer still rejects it.

The tests send the owner's message through the real ``api_chat`` handler into a real
running ``_run_chat`` turn, notes and wakes through the real
``session_deliver.deliver``; only the model is stubbed, and it reads the turn's
mark and the gate's own slot check (``owner_unverifiable_reason``).
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_typed_message_runs_alone import REFUSED, _Live, _note

from kiro_crew import steer_refusal
from kiro_crew.dashboard import chat_delivery, chat_delivery_owner_steer
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers.remote_dispatch_routes import owner_unverifiable_reason
from kiro_crew.dashboard.steer_supersedes_approval import SUPERSEDED_REJECTION_REASON

#: The row and ``steer_push`` meta key the transcript reads (``UserMessage.tsx``).
#: Spelled out: it is a wire name two codebases share.
STEER_WITHOUT_OWNER_RIGHTS_META = "steerWithoutOwnerRights"


def _marked_as_the_owners(wire: str, text: str) -> bool:
    """*wire* is *text* between the markers that name it as the owner's own words."""
    found = re.fullmatch(
        r"\[The owner's own message ([0-9a-f]{16}): .*?\]\n(.*)\n\[end of the owner's message \1\]",
        wire,
        re.S,
    )
    return found is not None and found.group(2) == text


def _row(slot, text: str) -> dict:
    (row,) = [m for m in slot.messages if m.get("content") == text]
    return row


def _steer_pushes(state) -> list[dict]:
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"]


# ── the reported case: a turn the owner did not open takes the message now ──


@pytest.mark.asyncio
@pytest.mark.parametrize("opener", ["note", "notice", "wake"])
async def test_typed_during_a_turn_the_owner_did_not_open_goes_in_at_the_next_step(
    tmp_path, monkeypatch, opener: str
) -> None:
    live = _Live(tmp_path, monkeypatch)
    await live.open_with(opener)

    receipt = await live.type("Q1a Go.")

    assert receipt.get("steered") is True and "queued" not in receipt
    live.agent.steer.assert_awaited_once()
    assert _marked_as_the_owners(live.agent.steer.await_args.args[0], "Q1a Go.")
    assert live.waiting() == [], "nothing waits for the turn to end"
    row = _row(live.slot, "Q1a Go.")
    assert row["role"] == "user" and row["meta"]["steer"] is True
    live.slot._pending_steers.clear()  # the stubbed agent sends no consumed echo
    await live.finish()
    assert len(live.turns) == 1, "it ran inside the turn, not as a turn of its own"


@pytest.mark.asyncio
@pytest.mark.parametrize("opener", ["note", "wake"])
async def test_the_owners_rights_stay_as_they_were_inside_that_turn(
    tmp_path, monkeypatch, opener: str
) -> None:
    """Nothing is granted: the turn keeps no owner mark and the gate still refuses it."""
    live = _Live(tmp_path, monkeypatch)
    await live.open_with(opener)

    await live.type("Q1a Go.")

    assert live.slot._turn_owner_sent is False
    assert live.slot._turn_user_sent is False
    assert owner_unverifiable_reason(live.slot) == REFUSED
    live.slot._pending_steers.clear()
    await live.finish()


@pytest.mark.asyncio
async def test_the_owners_steer_leaves_a_wakes_patrol_mark(tmp_path, monkeypatch) -> None:
    """The owner's words take nothing from a patrol wake: its remote work goes on."""
    live = _Live(tmp_path, monkeypatch)
    await live.open_with("wake")
    assert live.slot._turn_patrol_wake is True

    await live.type("Q1a Go.")

    assert live.slot._turn_patrol_wake is True
    assert live.slot._turn_owner_sent is False, "and it grants the owner's mark to nothing"
    live.slot._pending_steers.clear()
    await live.finish()


@pytest.mark.asyncio
async def test_the_row_says_it_went_in_without_the_owners_rights(tmp_path, monkeypatch) -> None:
    live = _Live(tmp_path, monkeypatch)
    await live.open_with("note")
    live.state.broadcast_ws = MagicMock()

    await live.type("Q1a Go.")

    assert _row(live.slot, "Q1a Go.")["meta"][STEER_WITHOUT_OWNER_RIGHTS_META] is True
    (push,) = _steer_pushes(live.state)
    assert push["meta"][STEER_WITHOUT_OWNER_RIGHTS_META] is True, "open tabs draw it live"
    live.slot._pending_steers.clear()
    await live.finish()


@pytest.mark.asyncio
async def test_a_steer_into_the_owners_own_turn_says_nothing_new(tmp_path, monkeypatch) -> None:
    live = _Live(tmp_path, monkeypatch)
    await live.open_with("owner")
    live.state.broadcast_ws = MagicMock()

    await live.type("and also this")

    assert STEER_WITHOUT_OWNER_RIGHTS_META not in _row(live.slot, "and also this")["meta"]
    (push,) = _steer_pushes(live.state)
    assert STEER_WITHOUT_OWNER_RIGHTS_META not in push.get("meta", {})
    assert live.slot._turn_owner_sent is True
    live.slot._pending_steers.clear()
    await live.finish()


@pytest.mark.asyncio
async def test_an_owners_turn_a_peers_steer_joined_takes_the_next_message_too(
    tmp_path, monkeypatch
) -> None:
    """A peer's steer ended the mark; their own turn announced no owner mark to frame
    their next message with, so it waits and runs next as their own turn."""
    from kiro_crew.dashboard import session_deliver

    live = _Live(tmp_path, monkeypatch)
    await live.open_with("owner")
    outcome = await session_deliver.deliver(
        live.state,
        live.slot,
        "a peer's steer",
        session_deliver.peer(steer=True, sender="dashboard:other"),
        admission=sc.containment_meta(live.state, live.slot),
    )
    assert outcome == session_deliver.DELIVERED_STEERED

    receipt = await live.type("Q1a Go.")

    assert receipt.get("queued") is True and "steered" not in receipt
    assert receipt.get("steerRefusal") == steer_refusal.NOT_OWNERS_TURN
    live.agent.steer.assert_awaited_once()  # the peer's steer, never the owner's words
    live.slot._pending_steers.clear()
    await live.finish()
    assert live.turns[1][:2] == ("Q1a Go.", True), "next, as the owner's own turn"


@pytest.mark.asyncio
async def test_a_steer_the_turn_ends_before_reading_runs_next_as_the_owners_own_turn(
    tmp_path, monkeypatch
) -> None:
    """Unread when the turn ends: it comes back as the owner's own turn, with the mark,
    ahead of a note that was waiting."""
    live = _Live(tmp_path, monkeypatch)
    await live.open_with("note")
    await _note(live.state, live.slot, "a later note")

    await live.type("Q1a Go.")
    assert live.slot._pending_steers == ["Q1a Go."]

    await live.finish()

    assert [prompt for prompt, _, _ in live.turns][1] == "Q1a Go."
    assert "a later note" in live.turns[2][0]
    assert live.marks()[1:] == [(True, ""), (False, REFUSED)]


# ── where the front of the queue stays better for the owner ──


def _foreign_turn(tmp_path):
    """A busy slot whose running turn the owner did not send, with a steerable agent."""
    from test_acp_tool_identity import _stub_state

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot._acp_client = MagicMock(supports_steer=True, steer_needs_loss_recovery=False)
    slot._acp_client.steer = AsyncMock(return_value=True)
    slot._acp_client.has_active_turn = lambda: True
    # Its prompt streamed with the gateway's opening, which announced the turn's mark.
    chat_delivery_owner_steer.announce(slot, nested=False)
    return state, slot


async def _owner_steer(state, slot, **extra) -> str:
    return await chat_delivery.steer_into_running_turn(
        state,
        slot,
        "redirect",
        user_origin=True,
        owner_origin=True,
        admission=sc.containment_meta(state, slot),
        **extra,
    )


@pytest.mark.asyncio
async def test_a_bot_conversation_still_puts_it_first_and_runs_it_at_once(tmp_path) -> None:
    """The responsive path's queue arm stops the turn and runs it now, with their rights."""
    state, slot = _foreign_turn(tmp_path)

    outcome = await _owner_steer(state, slot, responsive=True)

    assert chat_delivery.steer_refusal_of(outcome) == steer_refusal.NOT_OWNERS_TURN
    slot._acp_client.steer.assert_not_awaited()
    assert slot._pending_steers == [] and slot._turn_owner_sent is False


@pytest.mark.asyncio
async def test_a_manual_compact_still_queues_it(tmp_path) -> None:
    state, slot = _foreign_turn(tmp_path)
    slot._compact_command_turn = True

    outcome = await _owner_steer(state, slot)

    assert chat_delivery.steer_refusal_of(outcome) == steer_refusal.COMPACTING
    slot._acp_client.steer.assert_not_awaited()


def _open_card(slot, request_id: str = "req-1"):
    fut = asyncio.get_running_loop().create_future()
    slot.register_approval(request_id, fut, {})
    return fut


@pytest.mark.asyncio
async def test_an_open_card_in_a_turn_the_owner_did_not_open_stays_theirs_to_answer(
    tmp_path, monkeypatch
) -> None:
    """The text goes in; the card is not decided by it, and is read after they answer."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state, slot = _foreign_turn(tmp_path)
    fut = _open_card(slot)

    outcome = await _owner_steer(state, slot)

    assert outcome == chat_delivery.STEER_STEERED
    slot._acp_client.steer.assert_awaited_once()
    assert _marked_as_the_owners(slot._acp_client.steer.await_args.args[0], "redirect")
    assert not fut.done(), "the card waits for the owner"
    fut.cancel()


@pytest.mark.asyncio
async def test_an_open_card_in_the_owners_own_turn_is_still_answered_by_the_steer(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state, slot = _foreign_turn(tmp_path)
    slot._turn_user_sent = slot._turn_owner_sent = True
    fut = _open_card(slot)

    outcome = await _owner_steer(state, slot)

    assert outcome == chat_delivery.STEER_STEERED
    assert fut.done() and fut.result() == "rejected"
    assert slot.take_rejection_reason("req-1", fut) == SUPERSEDED_REJECTION_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["_created_by", "_lineage_minted"])
async def test_a_workers_chat_keeps_its_row_as_it_was(tmp_path, field: str) -> None:
    """A chat opened for another caller: the gate refuses it whoever sent the turn,
    so the owner's steer there loses no crew right and its row stays as it was."""
    state, slot = _foreign_turn(tmp_path)
    setattr(slot, field, "dashboard:conductor" if field == "_created_by" else True)

    assert await _owner_steer(state, slot) == chat_delivery.STEER_STEERED

    (row,) = [m for m in slot.messages if m.get("content") == "redirect"]
    assert STEER_WITHOUT_OWNER_RIGHTS_META not in row["meta"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        {"user_origin": True, "owner_origin": False},
        {"user_origin": False, "owner_origin": False},
        {"user_origin": True, "owner_origin": True, "channel_origin": True},
    ],
    ids=["another dashboard user", "a peer", "a channel"],
)
async def test_nobody_elses_row_says_it(tmp_path, origin: dict) -> None:
    """The line is about the owner's rights; another sender has none of theirs to lose."""
    state, slot = _foreign_turn(tmp_path)

    outcome = await chat_delivery.steer_into_running_turn(
        state, slot, "redirect", admission=sc.containment_meta(state, slot), **origin
    )

    assert outcome == chat_delivery.STEER_STEERED
    rows = [m for m in slot.messages if m.get("meta", {}).get("steer")]
    assert rows and all(STEER_WITHOUT_OWNER_RIGHTS_META not in m["meta"] for m in rows)


def test_the_module_names_the_same_key() -> None:
    assert chat_delivery.STEER_WITHOUT_OWNER_RIGHTS_META == STEER_WITHOUT_OWNER_RIGHTS_META
