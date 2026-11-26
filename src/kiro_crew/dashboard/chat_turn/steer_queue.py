"""Steer settlement, queue entry classification, the drain's admission sweep and the stream's
steer arm (``_on_steer_consumed``)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_delivery import (
    STEER_STATE_CONSUMED,
    STEER_STATE_REQUEUED,
    STEER_STATE_UNCONFIRMED,
    attachment_meta,
    find_written_steer_row,
    knowledge_meta,
    queued_text_for_display,
    quote_meta,
    start_queue_persist,
)
from kiro_crew.dashboard.chat_delivery_owner_steer import DELIVERED_AGAIN_KEY
from kiro_crew.dashboard.chat_delivery_owner_steer import forget as forget_owner_steer
from kiro_crew.dashboard.chat_utils import (
    AUTOMATION_NOTICE_KIND,
    COMPOSER_SEND_META,
    CRON_NOTIFICATION_KIND,
    MCP_APP_MESSAGE_KIND,
    STEER_POSSIBLY_DELIVERED_META,
    SUBAGENT_COMPLETION_KIND,
    _remove_queued_by_id,
    _stamped_turn_actor,
    is_synthetic_payload_item,
    is_system_injection_item,
)
from kiro_crew.dashboard.chat_utils_injected_rows import (
    AGENT_RUN_RETRY_KEY,
    AUTOMATION_STEER_META,
    REQUEUED_STEER_ROW_MID_META,
    _is_agent_row,
)
from kiro_crew.dashboard.session_deliver import WAKE_GENERATION
from kiro_crew.dashboard.slot_queue_repository import (
    RESTORED_BOOT_ACCEPTED_KEY,
    RESTORED_HELD_AGAIN_KEY,
    RESTORED_QUEUE_KEY,
    RESTORED_SEND_CHOSEN_KEY,
    after_owner_front,
    broadcast_queue_order,
    is_owner_queue_entry,
    restored_card_waits,
    retire_held_parked_work,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        COMMANDS_OFF_META_KEY,
        DashboardState,
        _ChatSlot,
        crew_log_emit,
        logger,
        possibly_consumed_steers,
        settle_consumed_steers,
    )
    from kiro_crew.dashboard.chat_turn.queue_drain import _RECOVERED_WAKE_KEY
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn


def _steered_wake(nudge: "dict | None") -> "tuple[str, int] | None":
    """The ``(loop id, config generation)`` a steered wake's automation meta names.

    ``None`` for an automation steer that carries no generation.
    """
    if not nudge or nudge.get(WAKE_GENERATION) is None:
        return None
    return nudge["loop_id"], nudge[WAKE_GENERATION]


def _mark_steer_row_state(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    new_state: str,
    siblings: list[str] | None = None,
) -> str:
    """Move *message*'s persisted steer row to *new_state* and tell open clients.

    Returns the patched row's ``mid``, or "" when no row was patched (none found,
    no ``mid``, or the update missed). The requeue hands that id to the queue
    entry so the drain, after it appends this turn's own row, can mark this row
    ``superseded`` and the client draws the text once.
    One writer for both lifecycle transitions, so `consumed` and `requeued` can
    never disagree about how a row is patched. Best-effort by design: a row that
    cannot be found or updated must never stop the settle or the requeue, because
    losing the message is worse than a row left in `written`.

    Reuses the existing `chat_message_update` patch rather than inventing a
    steer-specific event -- the client already applies that to a rendered row --
    and keys it on the row's `mid` where it has one, because `ts` is not an
    identity: two rows minted in the same clock tick share it, so a ts-only lookup
    takes whichever came first.
    """
    row = find_written_steer_row(slot, message, siblings)
    if row is None:
        return ""
    ts = str(row.get("ts") or "")
    new_meta = dict(row.get("meta") or {})
    new_meta["steerState"] = new_state
    _mid_raw = new_meta.get("mid")
    _mid = _mid_raw if isinstance(_mid_raw, str) and _mid_raw else None
    if not ts and not _mid:
        return ""
    # Patch by `mid` where the row has one: `ts` is not an identity, so a ts-only
    # lookup takes the FIRST row carrying it and could patch a same-tick twin.
    if slot.update_message(ts, meta=new_meta, mid=_mid) is None:
        return ""
    payload: dict[str, object] = {"slot": slot.key, "ts": ts, "meta": new_meta}
    if _mid:
        # Carried so the client resolves the same row this did; omitted when the
        # row has no mid, keeping the payload shape unchanged for legacy rows.
        payload["mid"] = _mid
    try:
        state.broadcast_ws("chat_message_update", payload)
    except Exception:
        # The row on the slot is already correct; clients reconcile from slot
        # detail on the next fetch.
        logger.warning(
            "steer state broadcast failed for slot %s (state %s)",
            slot.key,
            new_state,
            exc_info=True,
        )
    # The patch above landed, so the row is real even when the broadcast failed;
    # the update path only reaches here with a ts or a mid, and only a mid is an
    # identity the drain can resolve.
    return _mid or ""


def _settle_consumed_steers(
    slot: "_ChatSlot",
    snapshot: str,
    state: "DashboardState | None" = None,
) -> "tuple[str, int] | None":
    """Settle pending steers covered by a ``steering_consumed`` echo.

    Returns the loop wake (:func:`_steered_wake`) of the newest automation steer
    this echo settled, so the turn that took it can carry that loop's cycle.

    The parse-and-match rules live in ``steer_settle.settle_consumed_steers``,
    shared with the ``/side`` sidecar, which hands kiro-cli the same
    fire-and-forget steers and needs the same answer.

    ``state`` is optional only so existing direct callers keep working; the
    production call site passes it, and without it the settled rows keep the
    ``written`` state they were persisted with rather than being promoted to
    ``consumed``.

    """
    if not slot._pending_steers:
        return None
    # An empty echo is no evidence of consumption (``steer_settle`` says so in
    # as many words), so nothing settles and every entry stays pending. ``_requeue_unconsumed_steers`` -- wired into
    # ``_run_chat``'s outer finally, so it runs on every turn-exit path --
    # degrades a pending entry to a queue card at the head of the queue. The
    # cost is a duplicate: on a backend that injected the steer but echoed no
    # text, the steer runs again -- usually immediately, since the turn-exit
    # drain starts the next queued turn -- but it runs VISIBLY, as its own turn
    # in the transcript (and holds as a cancellable card when the drain is
    # withheld, e.g. sign-in required), unlike the silent loss that sweeping
    # the list on no evidence produces. Every sibling call site (the
    # ``_refusal_notices`` settle in the same event branch, and the ``/side``
    # sidecar) settles nothing on an empty echo too.
    previous = list(slot._pending_steers)
    from kiro_crew.dashboard import responsive_chat

    possibly_delivered = responsive_chat.settle_steers(
        slot, previous, snapshot, possibly_consumed_steers
    )
    newly_possibly_delivered = set(possibly_delivered) - slot._steer_possibly_delivered
    slot._steer_possibly_delivered.update(possibly_delivered)
    remaining = responsive_chat.settle_steers(slot, previous, snapshot, settle_consumed_steers)
    settled_count = len(previous) - len(remaining)
    logger.debug(
        "Steer consumed for slot %s (%d settled, %d still pending)",
        slot.key,
        settled_count,
        len(remaining),
    )
    if state is not None and snapshot.strip():
        # Promote ONLY on an echo that carried evidence. An empty echo settles
        # nothing, so the multiset difference below is empty and promotes
        # nothing even without this guard -- it stays as an explicit
        # fail-closed gate, mirroring ``chat_delivery``'s positive-evidence
        # discipline, so a future change to the settle rules cannot make an
        # evidence-free frame promote a row.
        # Promote exactly the entries this echo accounted for. Computed as a
        # multiset difference against `remaining` so a duplicate identical steer
        # that stayed pending does not get its row promoted by its twin's echo.
        _still = list(remaining)
        for _msg in slot._pending_steers:
            if _msg in _still:
                _still.remove(_msg)
                continue
            # Record the evidence before promoting. `chat_delivery` decides a row's
            # INITIAL state by whether its entry is still registered, and an entry
            # removed by the empty-echo sweep is indistinguishable from one removed
            # by a matched echo at that point -- so it read an empty frame as a
            # confirmed injection. Keyed on the delivery id so a later identical
            # steer cannot inherit this one's evidence.
            _cdid = getattr(slot, "_steer_delivery_ids", {}).get(_msg, "")
            if _cdid:
                _confirmed_ids = getattr(slot, "_steer_confirmed", None)
                if _confirmed_ids is None:
                    _confirmed_ids = set()
                    slot._steer_confirmed = _confirmed_ids
                _confirmed_ids.add(_cdid)
            # No ledger entry from here, and none from the delivery either: the
            # session vocabulary carries no steer type, because its POSITION needs
            # two coroutines to agree and neither of them can.
            #
            # This echo is the only positive evidence that a turn consumed the text
            # and the only site that knows which turn did, so this is where such an
            # entry would have to be written. But the text the steer INTERRUPTED
            # reaches the log later, from the handler's segment cut, which runs when
            # `client.steer()` returns. Under stdin backpressure that RPC is still
            # in `drain()` when this echo arrives, so the steer entry would take a
            # lower seq than the assistant text it cut, and a fold reads that text
            # as the reply TO the steer rather than the reply it interrupted.
            #
            # Cutting the segment from here instead trades the defect for a worse
            # one: post-steer text arriving in the same window would be flushed
            # above the steer row in the transcript. Recording it at all therefore
            # waits on a resolver that owns both facts. A reader sees the steer as a
            # `message/received` on the next turn, which is what the transcript
            # shows too.
            # `remaining + [_msg]` is the live-steer list, NOT `_pending_steers`:
            # the resolver refuses to patch when two live steers share the
            # sanitized content, and `_pending_steers` still holds this whole
            # echo's entries until the assignment below. So a redaction collision
            # whose members the echo ALL accounted for would look ambiguous and
            # both rows would keep `written` forever, understating a confirmed
            # injection. Ambiguity is about
            # attribution, and only a steer that is still PENDING can also claim
            # this row; ``steer_settle`` already draws the line this way for its
            # own keys. When a twin stayed pending the count is 2 again and the
            # refusal stands, because then which row is which is unknowable.
            _mark_steer_row_state(state, slot, _msg, STEER_STATE_CONSUMED, remaining + [_msg])
        if settled_count:
            # A steer row is persisted as soon as the RPC accepts it, while this
            # echo is the later authority that the running turn actually consumed
            # the user's message. The agent can post an ``ask_question`` card in
            # between. In that order the earlier row append found no card to
            # retire, so finish the same next-user-message lifecycle here.
            #
            # Keep the filter narrow: an unmatched/empty echo proves nothing,
            # and a legacy blocking ask owns a parked wait that only its
            # round-trip may resolve. ``clear_question_pending`` also broadcasts
            # the card ids and pushes the slot status, so reconnecting clients
            # cannot rehydrate the stale card.
            state.clear_question_pending(slot.key, blocking=False)
    slot._pending_steers[:] = remaining
    from kiro_crew.dashboard.chat_handlers import crew_steer_consumed

    consumed_wake = None
    settled_msgs = [m for m in dict.fromkeys(previous) if m not in remaining]
    # An owner's unread steer is on disk with the queue (``chat_delivery_owner_steer``):
    # read now, it leaves that copy at once, so a restart cannot hand it back.
    owner_read = any(slot._steer_owner_origin.get(m) for m in settled_msgs)
    # Send order, so the newest settled wake is the one kept.
    for settled_msg in settled_msgs:
        # A person's steer into a Crew Mode crew member lands on its ledger now.
        crew_steer_consumed(state, slot, settled_msg)
        responsive_chat.forget_steer(slot, settled_msg)
        slot._steer_attachment_meta.pop(settled_msg, None)
        slot._steer_act_now.discard(settled_msg)
        wake = _steered_wake(slot._steer_automation.pop(settled_msg, None))
        consumed_wake = wake or consumed_wake
        # Consumed by this turn, so no requeue will read them. The successful steer
        # RPC leaves the admission and both origin marks in place for that requeue.
        slot._steer_admissions.pop(settled_msg, None)
        slot._steer_user_origin.pop(settled_msg, None)
        slot._steer_channel_origin.pop(settled_msg, None)
        slot._steer_owner_origin.pop(settled_msg, None)
        slot._steer_decision_strips.pop(settled_msg, None)
        slot._steer_possibly_delivered.discard(settled_msg)
    if settled_count or newly_possibly_delivered:
        from kiro_crew.dashboard import steer_retract

        steer_retract.on_settled(slot)
    if owner_read and state is not None:
        start_queue_persist(state, slot)
    return consumed_wake


def _mark_held_steers_possibly_delivered(slot: "_ChatSlot", client: object) -> None:
    """Mark each pending steer whose held write *client* reports unanswered.

    A steer the client held while a tool ran is written later, off the steer's
    own RPC, so a stalled write never reaches ``steer_into_running_turn``'s
    ambiguous-delivery branch. Read before the turn drops its client, so
    :func:`_requeue_unconsumed_steers` stamps such an entry as possibly
    delivered. The client knows the text it was handed, so each raw entry is
    compared through its wire form; a client without the accessor marks nothing.
    The steers the client took into the turn without confirming them are read
    here too (:func:`_mark_taken_steers`).
    """
    _mark_taken_steers(slot, client)
    unanswered = getattr(client, "held_steers_possibly_delivered", None)
    if not unanswered or not isinstance(unanswered, (set, frozenset)):
        return
    from kiro_crew.dashboard import responsive_chat

    wires = responsive_chat.turn_state(slot).steers
    for message in slot._pending_steers:
        if (wires.get(message, message) or "").strip() in unanswered:
            slot._steer_possibly_delivered.add(message)


def _mark_taken_steers(slot: "_ChatSlot", client: object) -> None:
    """Record each pending steer *client* took into the ending turn unconfirmed.

    claude's ``injected`` put the text on the running turn's input, and a turn that
    then fails reports nothing consumed (``steers_taken_unconfirmed``). Read before
    the turn drops its client, through each entry's wire form, so
    :func:`_requeue_unconsumed_steers` settles a peer's such steer instead of
    queueing it to run twice. A client without the accessor marks nothing, so every
    other harness keeps the requeue.
    """
    taken = getattr(client, "steers_taken_unconfirmed", None)
    if not taken or not isinstance(taken, (set, frozenset)):
        slot._steer_taken_unconfirmed = set()
        return
    from kiro_crew.dashboard import responsive_chat

    wires = responsive_chat.turn_state(slot).steers
    slot._steer_taken_unconfirmed = {
        message
        for message in slot._pending_steers
        if (wires.get(message, message) or "").strip() in taken
    }


def _settles_taken(slot: "_ChatSlot", steer_msg: str, taken: set[str]) -> bool:
    """Whether the teardown tries to settle *steer_msg* as taken but unconfirmed.

    Only another session's steer whose sender can be told (a send-origin stamp,
    which ``notify_send_origin_discarded`` writes to; the settle itself still
    requeues one whose notice does not land): the person's own steer, an
    automation's, and one with nobody to tell (a hub conductor's, an app's) keep
    the requeue, which is how they learn of a message the turn may have dropped.
    A steer whose RPC has not returned is left to that RPC's own reconcile, as the
    requeue leaves it.
    """
    from kiro_crew.dashboard import responsive_chat
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        send_origin_slot,
    )

    return (
        steer_msg in taken
        and bool(send_origin_slot(slot._steer_admissions.get(steer_msg)))
        and not slot._steer_user_origin.get(steer_msg, False)
        and steer_msg not in slot._steer_automation
        and steer_msg not in slot._steer_rpc_in_flight
        and responsive_chat.turn_state(slot).steer_entries.get(steer_msg) is None
    )


def _push_unconfirmed_wake(slot: "_ChatSlot", admission: Any) -> None:
    """Pull the sender's work-ledger tick forward when it is this worker's conductor
    (``conductor_wake.wake_for_unconfirmed_note``), off the event loop: the push
    reads the worker's binding file. Best-effort: the sender's scheduled tick reads
    the same record."""
    from kiro_crew import conductor_wake
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        send_origin_slot,
    )

    origin = send_origin_slot(admission)
    if not origin:
        return
    try:
        asyncio.get_running_loop().run_in_executor(
            None, conductor_wake.wake_for_unconfirmed_note, str(slot.key), origin
        )
    except Exception:  # noqa: BLE001 - the teardown must finish
        logger.debug("unconfirmed-note wake not scheduled (slot %s)", slot.key)


def _settle_taken_peer_steer(
    state: "DashboardState", slot: "_ChatSlot", steer_msg: str, live: list[str]
) -> bool:
    """Settle a peer's steer the ending turn took in and never confirmed, or say
    it cannot be.

    The text was on the turn's input, so it is not queued to run a second time.
    Its sender is told first that it may not have run; only once that notice
    landed does its row say so (``unconfirmed``), the sender's work-ledger watch
    get its wake (:func:`queue_age.note_unconfirmed`), and every per-text record
    of it go, as a consumed steer's does. A notice that did not land (a closed
    sender, a key another tab now holds, a write that failed) settles nothing and
    answers False, so the caller requeues the steer as before: a steer is never
    dropped with nobody told.
    """
    from kiro_crew.dashboard import queue_age
    from kiro_crew.dashboard.session_control import (
        SEND_DISCARD_UNCONFIRMED_STEER,
        notify_send_origin_discarded,
    )

    admission = slot._steer_admissions.get(steer_msg)
    try:
        told = notify_send_origin_discarded(
            state,
            entry_meta=admission,
            target_slot=slot,
            text=steer_msg,
            cause=SEND_DISCARD_UNCONFIRMED_STEER,
        )
    except Exception:  # noqa: BLE001 - the teardown must finish; the requeue still covers it
        logger.warning("unconfirmed-steer notice failed (slot %s)", slot.key, exc_info=True)
        told = False
    if told is not True:
        return False
    _mark_steer_row_state(state, slot, steer_msg, STEER_STATE_UNCONFIRMED, live)
    queue_age.note_unconfirmed(slot, steer_msg, admission)
    _push_unconfirmed_wake(slot, admission)
    for records in (
        slot._steer_admissions,
        slot._steer_delivery_ids,
        slot._steer_send_ids,
        slot._steer_attachment_meta,
        slot._steer_decision_strips,
        slot._steer_user_origin,
        slot._steer_channel_origin,
        slot._steer_owner_origin,
    ):
        records.pop(steer_msg, None)
    slot._steer_act_now.discard(steer_msg)
    slot._steer_possibly_delivered.discard(steer_msg)
    stamps = getattr(slot, "_steered_at", None)
    if isinstance(stamps, dict):
        stamps.pop(steer_msg, None)  # its send instant, kept only for a requeue
    logger.info("a peer steer went into %s's failed turn unconfirmed; not requeued", slot.key)
    return True


def _requeue_unconsumed_steers(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Degrade unconsumed mid-turn steers into ordinary queue cards.

    The first step of :func:`_hand_off_queue`, so it runs once on every turn
    exit. A steer that kiro-cli never confirmed via ``steering_consumed`` died
    with the turn (stall-cancel, soft STOP, error, or a steer racing the
    turn's natural end); without this it would vanish silently.

    Requeues at the HEAD of the slot queue — steers were meant to be injected
    before any queued item ran — preserving their relative order, and
    broadcasts a ``queue_push`` per message so open clients render the card.
    The owner's messages run in the order he sent them: his own unread steer
    goes back behind the entries of his already waiting at the front, and any
    other (a peer's note, a loop wake) behind all of his
    (``after_owner_front``), so text no person sent never runs ahead of the
    owner's waiting message. One ``queue_reorder`` frame then tells open tabs,
    which draw each pushed card last, where the cards really are.
    The card is visible and individually cancellable: a user whose STOP meant
    "discard" dismisses it with one click; nothing is ever silently lost.
    A hard kill never reaches here with pending steers (the force-stop
    handler clears ``_pending_steers`` alongside ``_queue``).

    A steer the person is taking back is settled first
    (``steer_retract.on_turn_ending``): an Unsend or an Edit never reached the
    turn, so it is not requeued, and a Queue instead is left to this requeue.

    A peer's steer the turn's agent took in without confirming it
    (:func:`_mark_taken_steers`) is not requeued either: it went into the turn,
    so it is settled ``unconfirmed`` once its sender has been told
    (:func:`_settle_taken_peer_steer`), and it never runs twice. One whose sender
    could not be told is requeued like any other.
    """
    from kiro_crew.dashboard import steer_retract

    taken = set(slot._steer_taken_unconfirmed)
    slot._steer_taken_unconfirmed = set()
    steer_retract.on_turn_ending(state, slot)
    if not slot._pending_steers:
        return
    from kiro_crew.dashboard import queue_age, responsive_chat

    requeued = slot._pending_steers[:]
    slot._pending_steers.clear()
    # Past the owner's entries already waiting at the front: his requeued
    # steers go here, after the messages he sent before them.
    owner_at = after_owner_front(slot._queue)
    # Settled oldest first, so each sender's notices arrive in send order (the
    # requeue below walks newest first only because each insert goes in front).
    settled = {
        at
        for at, steer_msg in enumerate(requeued)
        if _settles_taken(slot, steer_msg, taken)
        and _settle_taken_peer_steer(state, slot, steer_msg, requeued)
    }
    for at, steer_msg in reversed(list(enumerate(requeued))):
        if at in settled:
            continue
        # The turn is over and never confirmed this steer, so the row persisted at
        # write time is now WRONG if it still reads as a successful injection.
        # Correct it before the queue card goes out, so the transcript and the card
        # tell the same story: this message did not redirect that turn, it runs as
        # its own. Done for every requeued entry, including the ones whose
        # delivery id below is still live -- the row is the user-facing claim and it
        # has to be corrected either way.
        # `requeued` is passed as the live-steer list because `_pending_steers` was
        # cleared above: the resolver needs to know how many steers in THIS batch
        # share the sanitized content before it trusts the newest matching row.
        _row_mid = _mark_steer_row_state(state, slot, steer_msg, STEER_STATE_REQUEUED, requeued)
        # Raw-at-rest by design: slot._queue is a DELIVERY payload (the drained
        # entry becomes the next turn's LLM input), matching every other queue
        # producer (queue_append in chat_handlers / messaging). The dashboard
        # egresses go through `queued_text_for_display`: the session's own
        # human's text is shown as typed, like an ordinary send's row, and every
        # other origin is display-redacted. Sanitizing at insert would corrupt the
        # delivered message relative to the normal queue path.
        #
        # Carry the delivery id the steer registered under. The drain unions every
        # consumed entry's meta onto the row it writes, so this reaches the row even
        # when several queued items are merged into one — which is the only way the
        # steer's caller can tell "already persisted by the drain" from "consumed by
        # the turn" after both bookkeeping lists have emptied.
        #
        # Stamp the containment snapshot too: a requeued steer is plain
        # user speech re-entering the queue, and this requeue is the last moment
        # its admission is re-affirmed — a link appearing between here and the
        # drain must drop it like any other queued prompt, while a session that
        # was ALREADY channel-born keeps its steers.
        #
        # The snapshot comes from the SEND's gate, never from reading the slot here:
        # this requeue runs in the teardown, past the steer RPC's suspension, so a
        # slot read would fold a mirror linked during that suspension into the
        # baseline and the drain would then read the widened audience as admitted.
        # `steer_into_running_turn` requires the stamp from every caller, so the
        # absent case is a steer registered by code that predates it; that entry
        # carries NO containment key and the drain checks it against every currently
        # held constraint, which is the documented fail-closed floor.
        _recorded = getattr(slot, "_steer_admissions", {}).pop(steer_msg, None)
        _meta: dict = dict(_recorded) if _recorded else {}
        _did = getattr(slot, "_steer_delivery_ids", {}).pop(steer_msg, "")
        if _did:
            _meta["steer_delivery_id"] = _did
        # Carry the client's `sendId` the same way, for the same reason one step
        # further on. The drain unions this meta onto the row it writes, so
        # this is what gives a REQUEUED steer's row the `meta.sendId` an ACCEPTED
        # steer's row already gets from `steer_into_running_turn` -- without it the
        # row is id-less, `mergePreservedThinking` has no id to resolve the
        # optimistic bubble against, and the pre-steer thinking chip strands at the
        # tail until a reload. Popped in lockstep with the delivery id above so the
        # two maps never disagree about what is still in flight. Additive: a steer
        # whose POST carried no id stores nothing here and its entry meta keeps the
        # exact prior shape.
        _sid = getattr(slot, "_steer_send_ids", {}).pop(steer_msg, "")
        if _sid:
            _meta["sendId"] = _sid
        _meta.update(getattr(slot, "_steer_attachment_meta", {}).pop(steer_msg, {}))
        # The composer's human asked this to act now; the turn ended before taking
        # it. The mark lets the sub-agent hold release it rather than park it
        # behind a wave its sender chose not to wait for.
        _act_now: set[str] = getattr(slot, "_steer_act_now", set())
        if steer_msg in _act_now:
            _act_now.discard(steer_msg)
            _meta[COMPOSER_SEND_META] = True
        # An automation steer (a work-ledger wake) keeps its origin on the entry,
        # so the drain writes it as a nudge row, alone, and never merges it into
        # the person's own queued text. Its loop wake leaves the meta, which is
        # persisted and becomes the row's `meta.nudge`, for the entry's
        # process-local `_RECOVERED_WAKE_KEY` below.
        _nudge = getattr(slot, "_steer_automation", {}).pop(steer_msg, None)
        _wake = _steered_wake(_nudge)
        if _nudge is not None:
            _meta[AUTOMATION_STEER_META] = {k: v for k, v in _nudge.items() if k != WAKE_GENERATION}
        # The text already has a row: the RPC accepted it, the persisting tail
        # wrote it, and the patch above just marked it `requeued` ("runs as its
        # own message"). Name that row on the entry so the drain, after it appends
        # this turn's own row, marks the old one `superseded` and the client stops
        # drawing it. Without this the user sees their message twice. "" means no
        # row was patched (the steer was requeued before any row existed), and then
        # the drain only appends.
        if _row_mid:
            _meta[REQUEUED_STEER_ROW_MID_META] = _row_mid
        # The decision receipt rides the entry for the same reason the two ids do:
        # the drain unions a consumed entry's meta onto the row it writes, so this is
        # the only writer a requeued steer has. Both outcomes a `message.steer`
        # decision can CHOOSE already stamp it -- the steer path on its persisted row,
        # `queue_for_next_turn` on its entry -- and this is the third path, the race
        # where the turn ended while the RPC was suspended. Popped in lockstep with the
        # maps above. Additive: a manual steer recorded nothing and its entry meta
        # keeps the exact prior shape.
        _strip = getattr(slot, "_steer_decision_strips", {}).pop(steer_msg, None)
        if _strip:
            _meta["decisions_strip"] = _strip
        # Provenance is REPORTED by the steer's caller, not derived from the slot.
        # `steer_into_running_turn`'s callers differ on exactly this point: the
        # api_chat composer branch, whose text its session's own human typed, and
        # `session_send`, whose text a peer sent (a channel conversation resumed
        # into the session reports as the composer does -- its owner gate makes the
        # author the session's own human). The slot cannot tell them apart, and the
        # difference is the whole point of the flag:
        # `directive_user_origin` exempts the entry from the drain's LINKED drop
        # because "the author typed into the session's own surface", which is true
        # of the composer and false of a peer. Deriving it would hand a peer the
        # human's exemption, so a link appearing while the steer RPC was suspended
        # would let the peer's text run and mirror to an audience
        # `authorize_target` refuses outright.
        #
        # Absent means NOT the session's own human: an unrecorded steer fails closed
        # into the ordinary drop rather than inheriting the exemption. An app slot's
        # steers stay unexempted as before.
        _origin = bool(getattr(slot, "_steer_user_origin", {}).pop(steer_msg, False))
        _requeue_user_origin = _origin and not bool(getattr(slot, "_app", ""))
        # The channel mark rides the same way, from its own lockstep map: a
        # requeued steer runs as its own turn, so a channel human's text keeps the
        # narrower channel authority a queued channel message carries. Absent means
        # not through a channel.
        _channel = bool(getattr(slot, "_steer_channel_origin", {}).pop(steer_msg, False))
        # The owner mark rides the same way, from its own lockstep map: the
        # requeued steer's turn may read a peer crew's sessions only if the
        # owner sent it. Absent means not the owner.
        _owner = bool(getattr(slot, "_steer_owner_origin", {}).pop(steer_msg, False))
        if _channel:
            # A channel steer that re-enters the queue runs as its own turn, so it
            # takes the mark the hand-off's queued entry carries: the text is the
            # sender's words, never a dashboard command to run on drain.
            _meta[COMMANDS_OFF_META_KEY] = True
        _maybe_delivered: set[str] = getattr(slot, "_steer_possibly_delivered", set())
        # An RPC still in flight counts too: its frame may already be in the
        # pipe, and its verdict lands after this entry may have drained.
        if steer_msg in _maybe_delivered or steer_msg in getattr(
            slot, "_steer_rpc_in_flight", set()
        ):
            _maybe_delivered.discard(steer_msg)
            _meta[STEER_POSSIBLY_DELIVERED_META] = True
        responsive_meta = responsive_chat.turn_state(slot).steer_entries.get(steer_msg)
        if responsive_meta is not None:
            _meta.update(responsive_meta)
            _requeue_user_origin = True
        # A peer's steer wanted this turn and got the queue: its sender is woken if
        # it sits, counted from when it was sent (``queue_age``).
        _meta.update(queue_age.requeued_steer_meta(_recorded))
        qid = slot.queue_insert(
            0,
            steer_msg,
            meta=_meta,
            directive_user_origin=_requeue_user_origin,
            directive_channel_origin=_channel,
            directive_owner_origin=_owner and _requeue_user_origin,
        )
        # The owner's steer taken without their rights comes back here, unread, as
        # their own turn, so the turn's end does not queue it again. One the agent
        # took into the failed turn unconfirmed went in, and its turn says so.
        if forget_owner_steer(slot, steer_msg) and steer_msg in taken:
            slot._queue[0][DELIVERED_AGAIN_KEY] = True
        # The loop runs newest first, so each lands ahead of the ones already
        # put back and they keep their order: the owner's after his waiting
        # entries, any other after all of his.
        _entry = slot._queue.pop(0)
        slot._queue.insert(
            owner_at if is_owner_queue_entry(_entry) else after_owner_front(slot._queue), _entry
        )
        queue_age.carry_steered(slot, steer_msg, qid)
        if _wake is not None:
            # The turn this entry drains into carries the loop's wake, so its fault
            # is charged to that loop; not a durable queue key, so never persisted.
            next(item for item in slot._queue if item["id"] == qid)[_RECOVERED_WAKE_KEY] = _wake
        from kiro_crew.dashboard.chat_handlers import crew_steer_requeued

        # A person's unread steer into a crew member is recorded when the drain
        # delivers this entry, never if the person cancels it first.
        crew_steer_requeued(
            slot, steer_msg, next((item for item in slot._queue if item.get("id") == qid), None)
        )
        if responsive_chat.forget_steer(slot, steer_msg):
            entry = next(item for item in slot._queue if item["id"] == qid)
            entry[responsive_chat.RESPONSIVE_KEY] = True
            entry["_responsive_protected"] = True
            responsive_chat.remember_row(
                slot,
                entry,
                next(
                    (
                        row
                        for row in reversed(slot.messages)
                        if (row.get("meta") or {}).get("steer_delivery_id")
                        == _meta.get("steer_delivery_id")
                    ),
                    None,
                ),
            )
        try:
            _push: dict = {
                "slot": slot.key,
                # As typed only for the session's own human -- the rule the queue view
                # applies to the entry (``queue_entry_is_user_origin``), which reads
                # the channel stamp too.
                "content": queued_text_for_display(
                    steer_msg, user_origin=_requeue_user_origin and not _channel
                ),
                "ts": datetime.now(timezone.utc).isoformat(),
                "queue_id": qid,
            }
            # The requeued steer's attachment lists and quote were folded into
            # `_meta` above; the card drawn from this frame is what a cancel
            # restores, and a frame without the quote would show the raw
            # blockquote as text until a reload.
            _push_meta: dict[str, object] = {
                **attachment_meta(_meta),
                # Same rule the entry's text follows (`queue_entry_is_user_origin`):
                # the human's own words stay as typed, anyone else's are redacted.
                **quote_meta(_meta, user_origin=_requeue_user_origin and not _channel),
                **knowledge_meta(_meta),
            }
            from kiro_crew.dashboard.chat_delivery import normalize_send_id

            _push_send_id = normalize_send_id(_meta.get("sendId"))
            if _push_send_id:
                _push_meta["sendId"] = _push_send_id
            # Says this card is a requeued steer, so the client's drain of THIS
            # card (matched by `queue_id`) can tie its row to the steer row. A
            # card only: the entry and the row it drains into never carry it.
            _push_meta["requeuedSteer"] = True
            _push["meta"] = _push_meta
            from kiro_crew.dashboard import responsive_chat as _rc

            _rc.mark_written_on_push(slot, qid, _push)
            state.broadcast_ws("queue_push", _push)
        except Exception:
            # Broadcast is best-effort — the message is already safely in the
            # queue; clients reconcile from slot detail on next fetch.
            logger.warning(
                "queue_push broadcast failed for requeued steer (slot %s)",
                slot.key,
                exc_info=True,
            )
    if len(slot._queue) > 1:
        # Each `queue_push` above draws its card last; these cards sit at the
        # front, so open tabs are told the real order, as a server move does.
        broadcast_queue_order(state, slot, [item["id"] for item in slot._queue])
    logger.info(
        "Requeued %d unconsumed steer(s) for slot %s (turn ended before consumption)",
        len(requeued),
        slot.key,
    )


def _queue_entry_is_orchestration(item: dict) -> bool:
    """True when a queue entry is runner/system orchestration, not user speech.

    Used only by the promise-only guards (via `_has_user_queued_followup`) to
    decide "did the USER intervene". A background cron notification or sub-agent
    completion queued mid-turn is orchestration, not a user "don't do that", so it
    must NOT block or purge a pending recovery.

    Classification is PURELY STRUCTURAL — the `kind` tag stamped at enqueue, never
    the message text. `is_system_injection_item` covers the three orchestration
    kinds (`CRON_NOTIFICATION_KIND`, `SUBAGENT_COMPLETION_KIND`,
    `SYNTHETIC_RECOVERY_KIND`); `is_synthetic_payload_item` additionally covers a
    recovery entry that replays runner-authored text. There is deliberately NO
    content match: a `CRON_NOTIFY_RE.match` / prefix test is prefix-anchored and
    therefore spoofable — a user could queue
    `[Cron notification from "x"]\ndon't delete it` during a promise-only turn and
    have their intervention silently ignored while the announced action dispatches
    anyway. A user message carries no enqueue tag, so it correctly counts as a
    user follow-up and aborts the pending recovery; real cron / sub-agent events
    are tagged at their injection sites and stay excluded."""
    return is_synthetic_payload_item(item) or is_system_injection_item(item)


#: Which turn actor an enqueue-time queue ``kind`` names. The tag is stamped by
#: the producer at ``queue_append`` and is not derivable from the entry's text,
#: which is the point: the banners these two injections wrap their text in
#: (``CRON_NOTIFY_PREFIX``, ``SUBAGENT_COMPLETION_PREFIXES``) are strings a user
#: can type, and attributing a turn in the session's log is a claim a reader
#: takes as fact. Same source, same reason as ``is_system_injection_item``.
#:
#: ``SYNTHETIC_RECOVERY_KIND`` is deliberately absent: a recovery's actor is
#: whoever caused the ORIGINAL turn, which no kind can name, so it travels in the
#: entry's meta instead (:data:`TURN_ACTOR_META_KEY`).
_QUEUE_KIND_ACTORS: dict[str, str] = {
    CRON_NOTIFICATION_KIND: "cron",
    SUBAGENT_COMPLETION_KIND: "subagent",
    MCP_APP_MESSAGE_KIND: "app",
    # A re-routed automation refusal is the monitor subsystem talking, so the
    # turn it rides is attributed to the autonudge actor, never the person.
    AUTOMATION_NOTICE_KIND: "autonudge",
}


def _actor_for_queue_items(items: "list[dict]") -> str:
    """The turn actor the consumed queue entries name, or ``""`` for none.

    First mapped kind wins, then a stamped actor. A merge run never mixes them --
    it stops AT a system injection (``_dequeue_next_message``), so a merged batch
    is either all plain user messages or one injection alone.
    """
    for item in items:
        actor = _QUEUE_KIND_ACTORS.get(item.get("kind", ""))
        if actor:
            return actor
    for item in items:
        stamped = _stamped_turn_actor(item)
        if isinstance(stamped, str) and stamped in crew_log_emit.ACTORS:
            return stamped
    return ""


def _has_user_queued_followup(slot: "_ChatSlot") -> bool:
    """True when the slot queue holds a USER-authored follow-up message.

    The promise-only guards use this to decide "did the USER intervene". Every
    entry that is runner/system orchestration (`_queue_entry_is_orchestration`) is
    excluded; anything left is user speech, which must block or purge a pending
    recovery so the user's intent wins.

    A restored card the user has not sent is excluded too: no drain runs it, so
    it is not a follow-up that takes over from a retry, continuation or replay.
    Counting it would switch those off for as long as the card waits. A restored
    card the user chose to run counts like any other entry, unless a limit held
    it again (`restored_card_waits`).

    While a failed agent run's retry is queued or its turn has not yet read it
    (``AGENT_RUN_RETRY_KEY``, a process-local key only ``_requeue_agent_run`` sets;
    the turn's ``_TurnExit.unread_run``), no agent's row (``_is_agent_row``) is a
    follow-up: the run's own entries are that retry, and another session's note or
    a wake queued since waits behind it for the next turn, so neither cancels the
    retry and the notes it carries."""
    queue = getattr(slot, "_queue", [])
    starting = getattr(getattr(slot, "_preparing_turn", None), "unread_run", None)
    behind_run_retry = any(q.get(AGENT_RUN_RETRY_KEY) for q in [*queue, *(starting or ())])
    return any(
        not _queue_entry_is_orchestration(q)
        and not restored_card_waits(q)
        and not (behind_run_retry and _is_agent_row(q))
        for q in queue
    )


def _entry_drop_constraints(now: dict[str, Any], q: dict[str, Any]) -> list[str]:
    """The constraints for which :func:`_drop_stale_admissions` drops *q* under the
    drain-time snapshot *now*; empty when it keeps the entry.

    Exempt ONLY cron notifications and sub-agent completions: both are minted
    fresh by trusted internal producers for THIS slot's own turn lifecycle, and
    channel-born sessions receive them by design. A synthetic-recovery entry is
    deliberately NOT exempt — it replays externally admitted content verbatim
    under a fresh queue id, so an exemption would let the retry ride past a link
    that appeared during the recovery window. Every recovery producer stamps
    admission context at requeue (`_queue_recovery`, the manual continue), so a
    recovery in a channel-born session still drains: its stamp records
    linked=True.
    """
    if q.get("kind") in (CRON_NOTIFICATION_KIND, SUBAGENT_COMPLETION_KIND):
        return []
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard import session_control as _sc

    return _sc.newly_held_constraints(
        now,
        q.get("meta"),
        directive_user_origin=q.get("_directive_user_origin") is True,
    )


#: What a send button answers for a card :func:`drain_would_drop` names: the card
#: stays queued (a restored one stays held) and nothing is sent.
CONTAINED_CODE = "queue_item_contained"
CONTAINED_ERROR = "This chat's channel link or limits don't allow it. The words stay in the queue."


def drain_would_drop(state: DashboardState, slot: _ChatSlot, entries: list[dict]) -> set[str]:
    """The ids of *entries* (cards in *slot*'s queue) the drain would drop if it
    sent them now: the drain's own containment test, asked before a send.

    A send button asks it before it lets a card go (Send now, Send all held), so
    a card the drain would stop at its turn is refused at the press: it stays
    held, words and all, and no turn is stopped for it. A restored card carries
    no admission snapshot, so in a chat linked or mirrored to a channel every one
    of them is such a card.
    """
    if not entries:
        return set()
    from kiro_crew.dashboard import session_control as _sc

    now = _sc.containment_snapshot(state, slot, on_probe_failure=True)
    return {q["id"] for q in entries if _entry_drop_constraints(now, q)}


def _drop_stale_admissions(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    selected_id: str | None = None,
    selected_ids: frozenset[str] = frozenset(),
) -> set[str]:
    """Drop queued entries whose admission-time containment has lapsed.

    A restored card the user has not sent is left out: no drain runs it, and
    the restore path strips its containment snapshot, so judging it here would
    delete a card the user is meant to send, edit or delete. It is judged when
    the user sends it: a card marked chosen, or the one ``selected_id`` names.
    *selected_ids* are the startup drain's accepted cards, judged together so
    one notice covers every card this pass holds again.

    A restored card of the owner's this check stops is never removed, whenever
    its limit came (after Send all or Send now, or a mirror-store read that
    failed): it is the person's words and the restore kept no other copy. It
    loses its "send" marks and gains ``RESTORED_HELD_AGAIN_KEY``, so no later
    pass judges or picks it until a person sends it again; one notice and one
    audit row cover the cards a pass keeps. Returns the ids so kept. A restored
    peer's note (it carries its sender's address) is dropped like any entry.

    Authorization is decided when a prompt is ADMITTED (`authorize_target` for
    `session_send`, the authenticated composer for a human typing into a busy
    session), but delivery happens later, at this drain — and the target-side
    containment those decisions rest on can change in between: a target
    authorized while unlinked can be given a channel or mirror link before its
    queue drains, and the queued prompt would then execute and republish to an
    audience its admission never contemplated.

    Producers of plain (user-speech) entries stamp the containment snapshot at
    enqueue (`session_control.containment_meta`); this sweep recomputes the same
    constraints and drops any entry for which a constraint holds NOW that did
    not hold at admission — including a WORKSPACE change, which swaps the
    memory/lessons/project context under a waiting prompt. An unmarked plain
    entry fails closed against the boolean constraint set, so an untagged
    producer can never ride a queued prompt past a boundary the tagged paths
    respect.

    Entries carrying `_directive_user_origin` (authenticated-human provenance)
    are exempt from the LINKED constraint only: the author typed into the
    session's own surface and linking it is that owner's deliberate act, so
    composer input into a just-linked session is designed behaviour. A NEW
    outbound mirror still drops them — the author does not control mirror
    links — as do all other constraints (see
    `session_control.newly_held_constraints`).

    Structural exemption is narrow: cron notifications and sub-agent
    completions only (`CRON_NOTIFICATION_KIND` / `SUBAGENT_COMPLETION_KIND`) —
    runner machinery minted fresh by trusted internal producers, which
    channel-born sessions receive by design. Synthetic-recovery entries are
    NOT exempt: a recovery replays externally admitted content verbatim, so it
    is re-validated like any plain entry against the admission stamp its
    requeue recorded (`_queue_recovery`), failing closed when unmarked.

    Runs at the top of the drain with no suspension point between the snapshot
    and the dequeue (everything below is synchronous on the event loop), so the
    decision cannot go stale before the surviving entry becomes a turn. A drop
    is never silent: the queue card is retracted, a visible notice naming the
    changed constraint lands in the transcript, and the drop is written to the
    SEL.

    A CROSS-SESSION delivery is reported in both directions. The entry carries
    the sending session as a slot key plus that slot's tab identity
    (`session_control.send_origin_meta`, stamped at admission beside the
    containment snapshot), so the sender gets its own notice naming
    the target and the changed constraint
    (`session_control.notify_send_origin_dropped`) and the SEL row names it as
    the drop's origin. Without that the sender's last word on the message is the
    `started: False` receipt it got when the target queued it, and the outcome it
    most needs — the message will never run — would reach only the target's
    transcript. A human-typed entry carries no stamp and is unaffected.
    """
    if not slot._queue:
        return set()
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard import gateway_notice as gn
    from kiro_crew.dashboard import session_control as _sc

    now = _sc.containment_snapshot(state, slot, on_probe_failure=True)
    _mirror_unverified = bool(now.get("mirror_unverified"))
    doomed: list[tuple[dict, list[str]]] = []
    kept: list[tuple[dict, list[str]]] = []
    for q in slot._queue:
        # Judged once: a card held again waits for a person's send, whatever drain comes.
        if q.get(RESTORED_HELD_AGAIN_KEY):
            continue
        if (
            q.get(RESTORED_QUEUE_KEY)
            and not q.get(RESTORED_SEND_CHOSEN_KEY)
            and (selected_id is None or q.get("id") != selected_id)
            and q.get("id") not in selected_ids
        ):
            continue
        changed = _entry_drop_constraints(now, q)
        if changed:
            # A peer's note proven by its receipt carries its sender's address: it
            # is dropped and its sender told, as any entry. The owner's card is kept.
            owners = q.get(RESTORED_QUEUE_KEY) and not _sc.send_origin_slot(q.get("meta"))
            (kept if owners else doomed).append((q, changed))
    if kept:
        _keep_restored_held(state, slot, kept, mirror_unverified=_mirror_unverified)
    for q, changed in doomed:
        slot.queue_remove_by_id(q["id"])
        # The broadcast is unconditional: the frontend's queue card was created
        # by the producer's queue_push, not by a transcript placeholder row, so
        # gating retraction on the (rare) placeholder existing would leave a
        # card on screen for a message the server discarded. The placeholder
        # removal is the separate, best-effort half.
        _remove_queued_by_id(slot.messages, q["id"])
        state.broadcast_ws("queue_pop", {"slot": slot.key, "content": "", "queue_id": q["id"]})
        _change = _sc.describe_containment_change(changed, mirror_unverified=_mirror_unverified)
        slot.append(
            "notice",
            f"⚠️ Queued message dropped: {_change} after it was queued, so the "
            "authorization that admitted it no longer holds.",
            "msg msg-info",
            meta=gn.gateway_notice_meta(gn.GATEWAY_NOTICE_QUEUED_MESSAGE_DROPPED, change=_change),
        )
        # A cross-session delivery has a SENDER waiting on it, and the notice
        # above is on the target's transcript, which that sender does not read.
        # It was told at admission that the message was queued, so without this
        # the one outcome it most needs — the message will never run — is the one
        # it is never told. Read off the entry's own stamp, which is empty for a
        # human-typed entry and for an entry restored after a restart: the
        # restore path strips the stamp deliberately, because it names a write
        # target and the metadata line is editable, so a delivery that outlives a
        # restart is dropped without a report rather than reported to whoever an
        # edited stamp named.
        #
        # The stamp's TAB identity rides along so the notice can only reach the
        # slot object that sent the message. Named slot keys are reused, and the
        # notifier refuses a key whose current occupant is a different tab.
        _meta = q.get("meta")
        _origin = _sc.send_origin_slot(_meta)
        _sc.notify_send_origin_dropped(
            state,
            origin=_origin,
            origin_tab=_sc.send_origin_tab(_meta),
            target_slot=slot,
            text=q.get("content") or "",
            constraints=changed,
            mirror_unverified=_mirror_unverified,
        )
        # A message a CHANNEL conversation queued here (a resumed dashboard
        # session's Discord DM) has the same reader problem one step further
        # out: it was told "queued" in the channel and reads neither this
        # transcript nor the SEL. Same stamp discipline -- read off the entry,
        # stripped on restore -- and the notice re-runs the outbound recipient
        # check before anything is sent.
        _sc.notify_channel_recipient_dropped(
            state,
            entry_meta=_meta,
            target_slot=slot,
            text=q.get("content") or "",
            constraints=changed,
            mirror_unverified=_mirror_unverified,
        )
        _sc.audit_queued_drop(slot, q["id"], changed, origin=_origin)
        _log = logger.warning if _mirror_unverified and "mirrored" in changed else logger.info
        _log(
            "Dropped queued entry %s for slot %s at drain re-validation " "(newly held: %s%s)",
            q["id"],
            slot.key,
            ",".join(changed),
            (
                "; mirror probe FAILED — refusal is fail-closed, not an observed link"
                if _mirror_unverified and "mirrored" in changed
                else ""
            ),
        )
    return {q["id"] for q, _changed in kept}


def _keep_restored_held(
    state: DashboardState,
    slot: _ChatSlot,
    kept: list[tuple[dict, list[str]]],
    *,
    mirror_unverified: bool,
) -> None:
    """Hold each restored card in *kept* again, in its place, with one notice.

    The sweep would delete them otherwise. A restored card has no admission
    record, so a limit that comes after any send button (or a mirror store that
    does not answer) stops it. It stays queued for the person to send, edit or
    delete, and the notice names each reason once. One audit row records the
    cards kept. Its parked work row, if any, is retired, so no later start sends
    it on its own; the mark keeps this process's drains (the startup drain's
    accepted ids among them) off it until the person sends it again.
    """
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard import gateway_notice as gn
    from kiro_crew.dashboard import session_control as _sc
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        audit_queued_keep,
    )

    reasons: list[str] = []
    for q, changed in kept:
        q.pop(RESTORED_SEND_CHOSEN_KEY, None)
        q.pop(RESTORED_BOOT_ACCEPTED_KEY, None)
        q[RESTORED_HELD_AGAIN_KEY] = True
        for reason in changed:
            if reason not in reasons:
                reasons.append(reason)
    audit_queued_keep(slot, [q["id"] for q, _changed in kept], reasons)
    retire_held_parked_work(state, [q for q, _changed in kept])
    change = _sc.describe_containment_change(reasons, mirror_unverified=mirror_unverified)
    slot.append(
        "notice",
        f"⚠️ Not sent, kept held: {change}. The words stay in the queue for you to "
        "send, edit or delete.",
        "msg msg-info",
        meta=gn.gateway_notice_meta(gn.GATEWAY_NOTICE_HELD_MESSAGE_KEPT, change=change),
    )
    logger.info(
        "Kept %d restored queued entr(ies) held for slot %s at drain re-validation (%s)",
        len(kept),
        slot.key,
        ",".join(reasons),
    )
    # The held count moves, which tells open tabs to read the queue again.
    state.push_slots_update()


async def _on_steer_dropped(turn: _ChatTurn, event) -> None:
    """kiro-cli dropped the steers it held, unread, on a take-back's clear.

    A copy fanned out to every session on the process names no owner, so it
    settles nothing here.
    """
    if getattr(event, "runtime_global", False):
        return
    from kiro_crew.dashboard import steer_retract

    await steer_retract.on_dropped(turn.state, turn.slot)


async def _on_steer_consumed(turn: _ChatTurn, event) -> None:
    """A steer the session took in mid-turn."""
    _person_steered_turn = turn._person_steered_turn
    _refusal_notices = turn._refusal_notices
    _refusal_notices_settled = turn._refusal_notices_settled
    slot = turn.slot
    state = turn.state
    _had_person_steer = bool(getattr(slot, "_pending_steers", None))
    _consumed_wake = _settle_consumed_steers(slot, event.text or "", state)
    if _consumed_wake is not None:
        # This turn took a loop's steered wake, so it carries that loop's cycle: a
        # fault that ends it is charged to the loop. It gains no wake authority. The
        # steer names the slot's live loop at its current generation.
        turn._wake_cycle = _consumed_wake
    if _had_person_steer:
        # A person's steer is in this turn (kiro-cli echoes
        # consumption BEFORE the model's reply streams). Whether
        # the quiet end has already applied or comes later, the
        # post-terminal text drop must not eat their answer. Read
        # before the settle, which clears the entry.
        _person_steered_turn = True
        turn._person_steered_turn = _person_steered_turn
    if _refusal_notices:
        # Same echo, same parser as the user-steer ledger: an
        # empty echo is no evidence, and treating it as delivery
        # would drop the fallback continuation and leave the model
        # holding kiro-cli's "User denied tool execution"
        # uncorrected.
        _still_pending = settle_consumed_steers(_refusal_notices, event.text or "")
        _refusal_notices_settled += len(_refusal_notices) - len(_still_pending)
        turn._refusal_notices_settled = _refusal_notices_settled
        _refusal_notices[:] = _still_pending
