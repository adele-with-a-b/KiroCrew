"""A peer's note put back in the queue keeps the receipt a queued note has.

A ``session_send`` steer the running turn ended without reading is put back by
the teardown (``_requeue_unconsumed_steers``, through ``queue_insert``), and a
carried note whose steer was refused is put back by ``steer_carry``. Each is the
same unsent note, so a restart proves it and sends it once, as it does a note
that was queued from the start. A steer the turn may have read (marked possibly
delivered, its RPC still out, or taken by a harness that reports a read only at
a clean turn end) gets no receipt and waits for a person: the proof does not
send it twice.

The steer goes through the real ``steer_into_running_turn`` and the real
teardown; the restart through the real sanitizer and boot drain. Only the model
is stubbed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_peer_note_receipts import NOTE, _boot, _notices, _pair, _restart, _saved

from kiro_crew import steer_refusal
from kiro_crew.dashboard import chat_runner, peer_note_receipts
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import steer_carry
from kiro_crew.dashboard.chat_delivery import (
    STEER_STEERED,
    SteerUnavailable,
    steer_into_running_turn,
)
from kiro_crew.dashboard.chat_utils_injected_rows import (
    AUTOMATION_STEER_META,
    REQUEUED_STEER_ROW_MID_META,
    STEER_POSSIBLY_DELIVERED_META,
)
from kiro_crew.dashboard.slot_queue_repository import is_held_queue_entry


def _running(slot, *, read_at_clean_end: bool = False) -> None:
    """A turn is running and takes steers; it has read none yet.

    *read_at_clean_end*: the harness reports a steer read only when its turn
    ends cleanly (``steer_needs_loss_recovery``, the Claude backend)."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot._acp_client = MagicMock(supports_steer=True, steer_needs_loss_recovery=read_at_clean_end)
    slot._acp_client.steer = AsyncMock(return_value=True)
    slot._acp_client.has_active_turn = lambda: True


def _ended(slot) -> None:
    slot.task = None
    slot._acp_client = None


async def _peer_steer(state, target, sender, text=NOTE) -> None:
    """What ``session_send``'s steer arm hands the steer: the gate's containment
    and the sender's stamp as the requeue baseline, and the relay label."""
    outcome = await steer_into_running_turn(
        state,
        target,
        text,
        user_origin=False,
        admission={
            **sc.containment_meta(state, target),
            **sc.send_origin_meta(state, sender.key),
        },
        relay_meta={"senderKey": sender.key, "via": "session_send"},
    )
    assert outcome == STEER_STEERED
    assert target._pending_steers == [text]


def _receipts() -> dict:
    path = peer_note_receipts._path()
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def _restored_meta(monkeypatch, key: str) -> list[dict]:
    """The meta of *key*'s queue entries as the boot's proof pass hands them to the drain."""
    seen: list[dict] = []
    real = chat_runner._drain_parked_queues

    async def _spy(state, keys, *args, **kwargs):
        if key in keys:
            seen.extend(dict(item.get("meta") or {}) for item in state.get_slot(key)._queue)
        return await real(state, keys, *args, **kwargs)

    monkeypatch.setattr(chat_runner, "_drain_parked_queues", _spy)
    return seen


@pytest.mark.asyncio
async def test_a_peers_note_its_turn_never_read_is_sent_once_after_a_restart(tmp_path, monkeypatch):
    state, target, sender = _pair(tmp_path)
    _running(target)
    await _peer_steer(state, target, sender)

    chat_runner._requeue_unconsumed_steers(state, target)
    _ended(target)

    (entry,) = target._queue
    (record,) = _receipts().values()
    assert record["requeued"] is True
    assert record["target"] == target.key and record["sender"] == sender.key
    row_mid = entry["meta"].get(REQUEUED_STEER_ROW_MID_META)
    assert row_mid and record["row_mid"] == row_mid

    meta = _restored_meta(monkeypatch, target.key)
    restored = _restart(tmp_path, _saved(target, sender))
    assert await _boot(restored, monkeypatch) == [(target.key, NOTE)]
    # Restored as the requeued steer it was: never as a session_send queued
    # entry, which its sender could move, and naming the row the turn retires.
    (restored_meta,) = meta
    assert sc.SESSION_SEND_QUEUED_META not in restored_meta
    assert restored_meta[REQUEUED_STEER_ROW_MID_META] == row_mid
    assert restored.get_slot(target.key)._queue == []
    assert _notices(restored.get_slot(sender.key)) == []

    # Its turn ran, so its receipt is spent: a second restart sends nothing.
    again = _restart(tmp_path, _saved(restored.get_slot(target.key), restored.get_slot(sender.key)))
    assert await _boot(again, monkeypatch) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["possibly_delivered", "rpc_in_flight"])
async def test_a_requeued_note_its_turn_may_have_read_waits_for_a_person(
    tmp_path, monkeypatch, why
):
    state, target, sender = _pair(tmp_path)
    _running(target)
    await _peer_steer(state, target, sender)
    if why == "possibly_delivered":
        target._steer_possibly_delivered.add(NOTE)
    else:
        target._steer_rpc_in_flight.add(NOTE)

    chat_runner._requeue_unconsumed_steers(state, target)
    _ended(target)

    assert target._queue[0]["meta"][STEER_POSSIBLY_DELIVERED_META] is True
    assert _receipts() == {}
    restored = _restart(tmp_path, _saved(target, sender))
    assert await _boot(restored, monkeypatch) == []
    (card,) = restored.get_slot(target.key)._queue
    assert card["content"] == NOTE and is_held_queue_entry(card)


@pytest.mark.asyncio
async def test_a_steer_taken_where_reads_show_only_at_a_clean_end_gets_no_receipt(
    tmp_path, monkeypatch
):
    """The harness took the steer (``steer`` answered True) but reports a read
    only when its turn ends cleanly, and the gateway going away cut the turn:
    the model may have read the note. Requeued, it gets no receipt, so the
    restart sends nothing and tells its sender nothing."""
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_delivery

    monkeypatch.setattr(
        chat_delivery.live,
        "snapshot",
        lambda: SimpleNamespace(agent=SimpleNamespace(peer_steer_loss_recovery=True)),
    )
    state, target, sender = _pair(tmp_path)
    _running(target, read_at_clean_end=True)
    await _peer_steer(state, target, sender)
    target._acp_client.steer.assert_awaited_once()

    monkeypatch.setattr(chat_runner, "_gateway_shutdown_requested", lambda _state: True)
    await chat_runner._hand_off_queue(state, target, drain=False, allow_automatic_successor=False)
    _ended(target)

    (entry,) = target._queue
    assert entry["content"] == NOTE
    assert _receipts() == {}
    restored = _restart(tmp_path, _saved(target, sender))
    assert await _boot(restored, monkeypatch) == []
    assert _notices(restored.get_slot(sender.key)) == []
    (card,) = restored.get_slot(target.key)._queue
    assert card["content"] == NOTE and is_held_queue_entry(card)


def test_a_requeued_entry_without_a_whole_sender_stamp_gets_no_receipt(tmp_path):
    """The insert itself: a person's or a channel's text, a loop's wake, a
    stampless entry, a kind and a session_send queued entry get none here."""
    state, target, sender = _pair(tmp_path)
    stamp = sc.send_origin_meta(state, sender.key)
    target.queue_insert(0, "a person's", meta=dict(stamp), directive_user_origin=True)
    target.queue_insert(0, "a channel's", meta=dict(stamp), directive_channel_origin=True)
    target.queue_insert(0, "no stamp", meta={})
    target.queue_insert(0, "a wake", meta={**stamp, AUTOMATION_STEER_META: {"source": "loop"}})
    target.queue_insert(0, "a notice", meta=dict(stamp), kind="cron_notification")
    target.queue_insert(0, "queued", meta={**stamp, sc.SESSION_SEND_QUEUED_META: True})
    assert _receipts() == {}
    target.queue_insert(0, NOTE, meta=dict(stamp))
    (record,) = _receipts().values()
    assert record["requeued"] is True


@pytest.mark.asyncio
async def test_a_carried_note_put_back_after_a_refused_steer_keeps_its_receipt(
    tmp_path, monkeypatch
):
    state, target, sender = _pair(tmp_path)
    target.queue_append(
        NOTE,
        meta={
            **sc.containment_meta(state, target),
            **sc.send_origin_meta(state, sender.key),
            sc.SESSION_SEND_QUEUED_META: True,
            "relay": {"senderKey": sender.key, "via": "session_send"},
        },
    )
    (queued_id,) = _receipts()
    _running(target)
    carry = steer_carry.from_session(state, sender.key)
    assert carry is not None

    async def refused(_text, _admission, _relay):
        return SteerUnavailable(steer_refusal.COMPACTING)

    answer = await steer_carry.take_earlier_in(state, target, carry, refused)
    _ended(target)

    assert steer_refusal.COMPACTING == getattr(answer, "reason", "")
    assert [item["id"] for item in target._queue] == [queued_id]
    assert set(_receipts()) == {queued_id}
    restored = _restart(tmp_path, _saved(target, sender))
    assert await _boot(restored, monkeypatch) == [(target.key, NOTE)]
