"""A peer's cut-in note takes the same sender's earlier queued notes in with it.

Session A sends notes to busy session B. Some wait on B's queue: sent to queue,
or a steer that fell back. A later note from A that cuts into B's running turn
used to go in alone, so B read "this replaces my earlier notes" without having
seen them, and the old ones ran after the turn, stale and out of order.

So before the new note is steered, A's own entries still queued on B are steered
into the same turn, oldest first, and each one that went in leaves B's queue and
its card leaves every open dashboard. B reads them in the order they were sent,
the newest last.

Whose entries move is decided by the entry's own stamp, never by its text:

* a local sender's are the ones ``session_send``'s queue arms wrote with the
  sender's slot and tab (``session_control_queue._queue_entry_from``);
* on a crew a hub conductor reaches, they are the entries the agent-origin route
  queued (the ``crew`` turn actor): only the conductor that opened that session
  can send to it.

A person's entry, a cron card, an app message, another session's entry, a
requeued steer and anything restored after a restart (the restore drops both
stamps) never match, so they never move.

The first entry that cannot go ends the carry, and it and the ones after it stay
queued as they were: an editor open on its card, a row already written, a
containment constraint that newly holds since it was queued, no room under the
pending-steer cap (one place is kept for the new note), or a refused steer. An
entry is off the queue only for one steer attempt, which registers the text
before it can suspend, so a turn that ends meanwhile cannot also start it; a
refusal puts the same entry back where it was, and the wait for a turn that has
no prompt yet is spent with the entry on the queue. Nothing is dropped and
nothing runs twice.

A refusal that is about the turn (it ended, it has no prompt yet, its agent
takes no steer) is the new note's answer too: the note is not tried, so it
queues behind its elders instead of slipping past them into the turn or its
successor. Only ``same_pending``, which is about that one text, lets the new
note still go in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: In the admission a steer records: the text went out as a steer. The teardown
#: copies that record onto the entry it makes for an unread steer, so this is
#: what tells a requeued steer from a note that was queued, whichever way the
#: turn ended: the delivery id and the possibly-delivered mark are only on a
#: steer whose call was still in flight then.
WAS_STEERED_META = "wasSteered"


@dataclass
class Carry:
    """Which of a target's queued entries the sender of a steer queued.

    ``count`` is how many of them went into the running turn ahead of the new
    note; the caller reads it once the delivery has returned.
    """

    mine: "Callable[[dict[str, Any]], bool]"
    count: int = 0
    #: The sender's entries that stay queued and that nothing of the sender's may
    #: pass: a steer of theirs the teardown requeued. One ahead of the rest ends
    #: the carry and keeps the new note out of the turn, so it queues behind.
    waits: "Callable[[dict[str, Any]], bool] | None" = None


def from_session(state: "DashboardState", caller_key: str) -> Carry | None:
    """The carry for a local ``session_send`` from *caller_key*, or None.

    None for a caller with no tab identity: it owns no queue entry, for the
    reason ``send_origin_meta`` stamps nothing for one.
    """
    # Deferred: both import the session_control facade, which loads this package.
    from kiro_crew.dashboard.chat_delivery import queue_entry_is_user_origin
    from kiro_crew.dashboard.session_control import SESSION_SEND_QUEUED_META, send_origin_slot
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        send_origin_meta,
        send_origin_tab,
    )
    from kiro_crew.dashboard.session_control_queue import QUEUE_FROM_CALLER, _queue_entry_from

    tab = send_origin_tab(send_origin_meta(state, caller_key))
    if not tab:
        return None

    def _requeued_steer_of_the_caller(entry: dict[str, Any]) -> bool:
        # The requeue copies the send's stamp from the steer's admission, which
        # never holds the queued marker (``_queue_entry_from``).
        meta = entry.get("meta")
        return (
            isinstance(meta, dict)
            and not queue_entry_is_user_origin(entry)
            and meta.get(SESSION_SEND_QUEUED_META) is not True
            and send_origin_slot(meta) == caller_key
            and send_origin_tab(meta) == tab
        )

    return Carry(
        lambda entry: _queue_entry_from(entry, caller_key, tab) == QUEUE_FROM_CALLER,
        waits=_requeued_steer_of_the_caller,
    )


def from_hub_conductor() -> Carry:
    """The carry for a hub conductor's agent-origin steer on this crew."""
    return Carry(_queued_by_hub_conductor, waits=_requeued_steer_of_hub_conductor)


def _requeued_steer_of_hub_conductor(entry: dict[str, Any]) -> bool:
    """A conductor's steer the teardown requeued with its steer row still open."""
    from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY, queue_entry_is_user_origin
    from kiro_crew.dashboard.chat_utils_injected_rows import STEER_POSSIBLY_DELIVERED_META

    meta = entry.get("meta")
    return (
        isinstance(meta, dict)
        and not queue_entry_is_user_origin(entry)
        and meta.get(TURN_ACTOR_META_KEY) == "crew"
        and (
            bool(meta.get(WAS_STEERED_META))
            or "steer_delivery_id" in meta
            or bool(meta.get(STEER_POSSIBLY_DELIVERED_META))
        )
    )


def _queued_by_hub_conductor(entry: dict[str, Any]) -> bool:
    from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY, queue_entry_is_user_origin
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY
    from kiro_crew.shared_crew.admission_entry import QUEUED_WORK_META_KEY

    if queue_entry_is_user_origin(entry) or entry.get(RESTORED_QUEUE_KEY):
        return False
    if entry.get("kind") or "_on_consumed" in entry or "_on_irreversibly_consumed" in entry:
        return False
    meta = entry.get("meta")
    if not isinstance(meta, dict) or meta.get(TURN_ACTOR_META_KEY) != "crew":
        return False
    if _requeued_steer_of_hub_conductor(entry):
        # A steer the teardown requeued: it already has a steer row, and the
        # drain is what settles that row. It is never steered a second time.
        return False
    # shortcut: an entry fair admission parked work for stays queued, because
    # steering it would leave that work row unsettled; settle the row here once
    # Shared Crew admission is on for a crew that takes conductor steers.
    return QUEUED_WORK_META_KEY not in meta


def retained_steers(slot: Any) -> int:
    """How many distinct steers *slot* still holds a record of.

    The count ``MAX_PENDING_STEERS`` bounds: every per-steer store, not only the
    pending list, because a store that outlives its steer is still held memory.
    """
    return len(_retained(slot))


def _retained(slot: Any) -> set[Any]:
    """The texts of every steer *slot* holds a record of, pending or consumed."""
    return set(slot._pending_steers).union(
        slot._steer_delivery_ids,
        slot._steer_send_ids,
        slot._steer_user_origin,
        slot._steer_channel_origin,
        slot._steer_owner_origin,
        slot._steer_admissions,
        slot._steer_attachment_meta,
        slot._steer_act_now,
        slot._steer_decision_strips,
        slot._steer_automation,
    )


def _may_go(state: "DashboardState", slot: Any, entry: dict[str, Any]) -> bool:
    """Whether *entry* may be steered now: nobody is editing it, the transcript
    does not already hold it, the cap leaves room for it and the new note, and no
    containment constraint holds that its own admission never saw."""
    from kiro_crew.dashboard import responsive_chat
    from kiro_crew.dashboard.chat_delivery import MAX_PENDING_STEERS
    from kiro_crew.dashboard.queue_edit_hold import edit_held
    from kiro_crew.dashboard.session_control import containment_snapshot, newly_held_constraints

    content = entry.get("content")
    if not isinstance(content, str) or not content:
        return False
    if edit_held(entry) or responsive_chat.row_already_written(slot, entry):
        return False
    if retained_steers(slot) + 2 > MAX_PENDING_STEERS:
        return False
    # The drain would drop an entry whose audience widened while it waited, and
    # tell its sender. Steering it here must not be a way around that drop.
    now = containment_snapshot(state, slot, on_probe_failure=True)
    return not newly_held_constraints(now, entry.get("meta"))


def _baseline(meta: Any) -> dict[str, Any]:
    """The admission a carried entry's steer records: the one it was queued under."""
    from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY
    from kiro_crew.dashboard.session_control import (
        QUEUED_CONTAINMENT_META_KEY,
        SEND_ORIGIN_META_KEY,
    )

    if not isinstance(meta, dict):
        return {WAS_STEERED_META: True}
    # The teardown copies this record onto the entry it makes for an unread
    # steer, from the moment the steer registers. The actor stamp rides it, or
    # a requeued conductor's note would drain as the person's turn.
    keep = (QUEUED_CONTAINMENT_META_KEY, SEND_ORIGIN_META_KEY, TURN_ACTOR_META_KEY)
    return {**{k: meta[k] for k in keep if k in meta}, WAS_STEERED_META: True}


def _being_killed(slot: Any) -> bool:
    """Whether a hard stop of *slot* is under way (from its press to its end)."""
    return getattr(slot, "_stop_state", "") == "killing"


def _hard_stop_mark(slot: Any) -> Any:
    """The card a hard stop's escalation is scoped to; None when none is."""
    return getattr(slot, "_stop_escalated_card_id", None)


def _hard_stopped(slot: Any, before: Any) -> bool:
    """Whether a hard stop ("discard everything") was PRESSED on *slot* since
    *before* was read, on a slot that was not being killed then.

    A press sets the state and scopes a card; the end of a kill and a soft stop
    only clear the card, so a mark that went back to None is not a press.
    """
    mark = _hard_stop_mark(slot)
    return _being_killed(slot) or (mark is not None and mark != before)


def _discarded_by_hard_stop(
    state: "DashboardState", slot: Any, entry: dict[str, Any], queue_id: str
) -> None:
    """A hard stop emptied the queue while *entry* was off it for its steer.

    The stop discards what was queued when it was pressed, and this entry was:
    it goes with the rest, its card leaves, and its sender is told as the stop
    tells the senders of the entries it took.
    """
    from kiro_crew.dashboard.session_control import (
        SEND_DISCARD_HARD_STOP,
        notify_send_origin_discarded,
    )

    _left_the_queue(state, slot, queue_id)
    try:
        notify_send_origin_discarded(
            state,
            entry_meta=entry.get("meta"),
            target_slot=slot,
            text=entry.get("content") or "",
            cause=SEND_DISCARD_HARD_STOP,
        )
    except Exception:
        logger.warning("could not tell the sender of a discarded carry", exc_info=True)


def _put_back(slot: Any, entry: dict[str, Any], ahead: list[str], stamp: Any) -> None:
    """Return *entry* to *slot*'s queue behind the last survivor of *ahead*, the
    ids that were in front of it, with the wait stamp it had."""
    queue = slot._queue
    at = 0
    for index, other in enumerate(queue):
        if isinstance(other, dict) and other.get("id") in ahead:
            at = index + 1
    queue.insert(at, entry)
    stamps = getattr(slot, "_queued_at", None)
    if stamp is not None and isinstance(stamps, dict):
        stamps[str(entry.get("id") or "")] = stamp


def _left_the_queue(state: "DashboardState", slot: Any, queue_id: str) -> None:
    """Take a carried entry's card and side records away, as a cancel does.

    The ``queue_cancel`` frame carries no content, so no composer takes the text
    back. A failing push is logged, never raised: the text is already delivered.
    """
    from kiro_crew.dashboard import queue_age
    from kiro_crew.dashboard.chat_utils_injected_rows import _remove_queued_by_id

    if _remove_queued_by_id(slot.messages, queue_id):
        slot.invalidate_source_links()
    queue_age.forget(slot, [queue_id])
    try:
        state.broadcast_ws("queue_cancel", {"slot": slot.key, "queue_id": queue_id})
    except Exception:
        logger.warning("could not cancel a carried queue card on %s", slot.key, exc_info=True)
    try:
        state.push_slots_update()
    except Exception:
        logger.warning("could not push the queue of %s after a carry", slot.key, exc_info=True)


async def take_earlier_in(
    state: "DashboardState",
    slot: Any,
    carry: Carry,
    steer: "Callable[[str, dict | None, dict | None], Awaitable[str]]",
    *,
    ahead_of: str = "",
    wait_secs: float = 0.0,
    step_secs: float = 0.0,
    pause: "Callable[[float], Awaitable[None]] | None" = None,
) -> str | None:
    """Steer the sender's queued entries into *slot*'s running turn, oldest first.

    *steer* is ONE attempt of the peer arm's own steer, ``(text, admission,
    relay_meta)``, so a carried entry meets every guard the new note meets.
    ``carry.count`` grows by one per entry that went in. Returns None when the
    new note should be steered next, or the refusal that is the new note's
    answer too.

    An entry whose text is *ahead_of*, the new note's own, ends the carry and
    stays queued: steered first it would leave the new note refused as the same
    text already in flight, so a note sent again cuts in as it always did.

    A turn with no prompt yet (``between_steps``) is waited for here, up to
    *wait_secs* for the whole carry in *step_secs* pauses, with the entry back on
    the queue: a turn that ends during the wait drains it in its place.
    """
    from kiro_crew import steer_refusal
    from kiro_crew.dashboard import steer_retract
    from kiro_crew.dashboard.chat_delivery import (
        STEER_REQUEUED,
        STEER_STEERED,
        SteerUnavailable,
        steer_refusal_of,
    )

    turn = getattr(slot, "task", None)
    tries = int(wait_secs / step_secs) if pause is not None and step_secs > 0 else 0

    def _turn_is_over() -> bool:
        return not slot.running or getattr(slot, "task", None) is not turn

    while True:
        # The steer waits for an in-flight clear before it registers its text.
        # Wait for it here, with every entry still queued.
        await steer_retract.wait_for_clear(slot)
        if _turn_is_over() or _being_killed(slot):
            # A hard stop under way discarded what was queued when it was
            # pressed; what is queued now arrived after it and stays for the
            # next turn, so nothing is carried into the turn being killed.
            return SteerUnavailable(steer_refusal.TURN_ENDED)
        waits = carry.waits
        entry = next(
            (
                e
                for e in slot._queue
                if isinstance(e, dict) and (carry.mine(e) or (waits is not None and waits(e)))
            ),
            None,
        )
        if entry is not None and not carry.mine(entry):
            # The sender's requeued steer is the oldest of theirs here. It runs
            # as its own turn, so nothing of the sender's goes in ahead of it.
            return SteerUnavailable(steer_refusal.TURN_ENDED)
        if entry is None or entry.get("content") == ahead_of or not _may_go(state, slot, entry):
            return None
        queue_id = str(entry.get("id") or "")
        content = entry["content"]
        meta = entry.get("meta")
        relay = meta.get("relay") if isinstance(meta, dict) else None
        index = next(i for i, e in enumerate(slot._queue) if e is entry)
        ahead = [str(e.get("id") or "") for e in slot._queue[:index] if isinstance(e, dict)]
        stamps = getattr(slot, "_queued_at", None)
        stamp = stamps.get(queue_id) if isinstance(stamps, dict) else None
        # Off the queue for one attempt only. The steer does not suspend before
        # it has registered the text (the clear was waited for above), and a
        # registered steer is the teardown's to requeue, so a turn that ends
        # under the attempt finds the text in exactly one place.
        stop_mark = _hard_stop_mark(slot)
        slot.queue_pop(index)
        try:
            outcome = await steer(
                content, _baseline(meta), dict(relay) if isinstance(relay, dict) else None
            )
        except BaseException:
            if content in _retained(slot):
                # The steer registered it: the agent was handed it (the turn may
                # already have read it), or the teardown will requeue it. The
                # original must not also come back and run a second time.
                _left_the_queue(state, slot, queue_id)
            else:
                _put_back(slot, entry, ahead, stamp)
            raise
        if outcome == STEER_STEERED:
            carry.count += 1
            _left_the_queue(state, slot, queue_id)
            logger.info("queued entry %s went into %s's turn ahead of a steer", queue_id, slot.key)
            continue
        if outcome == STEER_REQUEUED:
            # The turn ended first and its teardown queued the text again, at the
            # head: it lives on as that entry, and the new note queues behind.
            _left_the_queue(state, slot, queue_id)
            return SteerUnavailable(steer_refusal.TURN_ENDED)
        if _hard_stopped(slot, stop_mark):
            # "Discard everything" was pressed under the attempt: the entry was
            # queued when it was pressed, so it must not come back.
            _discarded_by_hard_stop(state, slot, entry, queue_id)
            return SteerUnavailable(steer_refusal.TURN_ENDED)
        _put_back(slot, entry, ahead, stamp)
        refusal = steer_refusal_of(outcome) or steer_refusal.REFUSED
        if refusal == steer_refusal.BETWEEN_STEPS and tries > 0 and pause is not None:
            # The turn is starting or winding up. The entry waits on the queue,
            # where a drain finds it first if this turn ends.
            tries -= 1
            await pause(step_secs)
            continue
        logger.info(
            "queued entry %s stays queued on %s: its steer was refused (%s)",
            queue_id,
            slot.key,
            refusal,
        )
        return None if refusal == steer_refusal.SAME_PENDING else SteerUnavailable(refusal)
