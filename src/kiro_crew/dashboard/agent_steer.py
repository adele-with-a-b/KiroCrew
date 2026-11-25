"""A hub conductor's steer into a turn running on this crew.

A hub conductor reaches a worker here through this crew's own ``POST /api/chat``,
marked agent-origin (``dashboard/agent_origin.py``). The composer's steer branch is
a person's: it hands the turn the composer's rights, so an agent-origin send never
took it and a conductor's ``steer=true`` waited in the queue for the whole turn.

This is the steer such a send gets instead: the peer arm of
``session_deliver.deliver``, which a conductor's ``session_send`` to a worker on
its own crew already takes. The text goes in as a peer's (``user_origin=False``),
so it holds no right a person's steer holds and a requeue of it meets the drain
unexempted.

It never goes into a turn a person is driving (``holds_persons_standing``): that
turn's own ``_directive_user_origin`` is what its directives keep reading, so an
agent's text inside it would be acted on with the person's standing. There the
send queues and the reply says a person is driving the turn. A worker's turns are
started by the conductor's own sends, which is the case this steer is for.

The same three guards the local send keeps around its steer are kept here:

* the admission the steer was allowed under is recorded on the slot before the
  write, so the turn's reply legs withhold a channel audience that appears while
  the write is suspended (``publication_withheld`` reads it);
* a steer the turn took is followed by one comparison of the containment now with
  that admission, and a constraint that newly holds stops that same turn;
* the write is shielded, so a request that goes away mid-write still lets the
  steer settle its own bookkeeping, and a steer that then turns out not to have
  gone in is queued instead of lost.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from kiro_crew import steer_refusal
from kiro_crew.dashboard import session_deliver, steer_hold

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: The refusal for a steer the teardown queued on a chat that was since replaced.
TARGET_MOVED = "target_moved"

#: Shielded deliveries whose request went away, kept so the loop does not drop them.
_ORPHANED: set["asyncio.Future[Any]"] = set()


def started_mark(queue_id: str, started_ids: set[str]) -> dict[str, bool]:
    """``{"started": True}`` when the route's drain began a turn on *queue_id*, else ``{}``.

    An agent-origin send this crew queued can be started by the route's own drain
    before the route answers: the turn it waited on ended, or the hold it waited
    behind lifted. *started_ids* is what that drain consumed into a turn
    (``_start_next_queued_turn``), so an entry it dropped, or one taken back
    while it waited, reads as queued. The hub reads ``started`` beside
    ``queued`` as a running turn, so its receipt says the message started one.
    """
    return {"started": True} if queue_id in started_ids else {}


async def steer(
    state: "DashboardState", slot: "_ChatSlot", message: str
) -> tuple[dict[str, Any] | None, str, bool]:
    """Steer an agent-origin *message* into *slot*'s running turn.

    Returns ``(reply, refusal, held_for_person)``. *reply* is the route's answer
    when the send is settled here: ``steered`` when the text is in the running turn
    (with ``held`` when the agent keeps it behind a tool call the turn is in,
    ``steer_hold``), or ``queued`` when the turn's teardown queued it. ``None``
    means it did not go in and the caller queues it: *refusal* is the
    ``kiro_crew.steer_refusal`` code that says why, or *held_for_person* is True
    because a person is driving the turn and no steer was tried. The one refusal
    the caller must not queue is :data:`TARGET_MOVED`: the text is gone with a
    replaced chat, and the caller refuses the send.
    """
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.chat_delivery import steer_refusal_of
    from kiro_crew.dashboard.session_control import (
        containment_meta,
        containment_snapshot,
        holds_persons_standing,
        newly_held_constraints,
    )

    if holds_persons_standing(slot):
        return None, "", True
    admission = containment_meta(state, slot)
    turn_generation = slot._turn_generation
    fence = uuid.uuid4().hex
    slot._steer_audience_fences[fence] = admission
    delivery = asyncio.ensure_future(
        session_deliver.deliver(
            state,
            slot,
            message,
            session_deliver.peer(steer=True, patient=True),
            admission=admission,
        )
    )
    try:
        outcome = await asyncio.shield(delivery)
    except asyncio.CancelledError:
        _keep_orphan(state, slot, message, fence, delivery)
        raise
    if outcome == session_deliver.DELIVERED_STEERED:
        changed = newly_held_constraints(
            containment_snapshot(state, slot, on_probe_failure=True), admission
        )
        if changed and slot._turn_generation == turn_generation:
            await _stop_for_new_audience(state, slot, changed)
        reply: dict[str, Any] = {"ok": True, "steered": True}
        if steer_hold.held_behind_tool(slot):
            reply["held"] = True
        return reply, "", False
    # No text of this send is inside the running turn, so the record protects nothing.
    slot._steer_audience_fences.pop(fence, None)
    if outcome == session_deliver.DELIVERED_REQUEUED:
        if state._slots.get(slot.key) is not slot:
            # The teardown queued the text on this slot object, and the chat was
            # closed and opened again under the same key during the write: no drain
            # reaches that queue. The local send refuses the same way.
            return None, TARGET_MOVED, False
        return {"ok": True, "queued": True}, "", False
    return None, steer_refusal_of(outcome) or steer_refusal.REFUSED, False


async def _stop_for_new_audience(
    state: "DashboardState", slot: "_ChatSlot", changed: list[str]
) -> None:
    """Stop the turn a steer went into once a containment constraint newly holds.

    One cooperative cancel, as the local send does. The steer is already in the
    turn, so a stop that fails changes nothing about the delivery: the admission
    recorded before the write still withholds the turn's cross-surface reply.
    """
    # circular import: chat_handlers imports this package's modules at module level.
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    try:
        await stop_slot_turn(state, slot, source="agent_steer_containment", escalate=False)
    except Exception:
        logger.exception(
            "containment stop failed after an agent-origin steer (slot=%s, newly held=%s)",
            slot.key,
            changed,
        )


def _keep_orphan(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    fence: str,
    delivery: "asyncio.Future[Any]",
) -> None:
    """Let a shielded steer finish after its request went away, and queue what it lost.

    The sender got no answer, so it cannot know to send again. A steer that went
    in keeps its recorded admission for the turn; one that did not is queued on the
    same slot object, as a peer's text.
    """
    _ORPHANED.add(delivery)

    def _done(finished: "asyncio.Future[Any]") -> None:
        _ORPHANED.discard(finished)
        if finished.cancelled() or finished.exception() is not None:
            slot._steer_audience_fences.pop(fence, None)
            logger.warning("agent-origin steer to %s did not finish cleanly", slot.key)
            return
        outcome = finished.result()
        if outcome == session_deliver.DELIVERED_STEERED:
            return
        slot._steer_audience_fences.pop(fence, None)
        if outcome == session_deliver.DELIVERED_REQUEUED or state._slots.get(slot.key) is not slot:
            return
        queued = asyncio.ensure_future(
            session_deliver.deliver(state, slot, message, session_deliver.peer(steer=False))
        )
        _ORPHANED.add(queued)
        queued.add_done_callback(_ORPHANED.discard)

    delivery.add_done_callback(_done)
