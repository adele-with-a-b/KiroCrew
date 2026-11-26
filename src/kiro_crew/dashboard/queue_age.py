"""How long a session's queued messages have waited, and whose they are.

A count alone ("13 message(s) queued") does not tell a sender whether its note
went in a minute ago or has sat behind a turn for two hours, and only the second
needs it to act. Each queue entry's enqueue instant is kept BESIDE the queue, in
``slot._queued_at`` keyed by entry id, never inside the entry: queue dicts are
compared wholesale on the wire and in persistence, so a clock in them would make
their shape time-dependent (``slot_queue_repository.note_enqueue`` keeps its own
stamp beside the entry for the same reason).

In-process only. An entry restored after a restart has no stamp, so it is counted
but gives no age, and nothing here says it is fresh.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew.session_ledger import ledger_key

#: The one clock both the stamp and every age read use, so a test freezes it here.
_clock = time.time

#: Queue-entry ``meta`` key for a message whose sender wanted it in the running
#: turn (an asked-for steer, or the default's) and got the queue instead. Only
#: these wake the sender when they sit unread: a message queued by name is one the
#: sender chose to have wait.
STEER_WANTED_META = "steer_wanted"


@dataclass(frozen=True)
class QueueWait:
    """The waiting entries a read selected: how many, and the oldest one's wait."""

    count: int
    #: Seconds the oldest stamped entry has waited, or ``None`` when none carries a
    #: stamp (every one was restored after a restart).
    oldest_secs: float | None
    #: The identity of that oldest stamped wait, or ``""``: the entry's id, or, for
    #: an entry that took over older entries' wait (:func:`carry_wait`), the id of
    #: the first entry that wait began on.
    oldest_id: str


def note_queued(slot: Any, queue_id: str) -> None:
    """Stamp *queue_id* as appended to *slot*'s queue now.

    Every write keeps only the stamps of entries still queued, so the record never
    outgrows the queue it describes. Best-effort: a slot shape that cannot hold
    the record keeps its queue, unstamped.
    """
    if not queue_id:
        return
    live = {str(entry.get("id") or "") for entry in _entries(slot)}
    stamps = getattr(slot, "_queued_at", None)
    kept = {k: v for k, v in stamps.items() if k in live} if isinstance(stamps, dict) else {}
    kept[queue_id] = _clock()
    try:
        slot._queued_at = kept
    except AttributeError:
        return


def note_steered(slot: Any, message: str, *, read_reported: bool = True) -> None:
    """Stamp a steer *slot*'s running turn has accepted but not read yet.

    An accepted steer waits in ``slot._pending_steers`` until the model reaches
    its next step, which a turn blocked in one long tool call may not do for an
    hour. The stamp is ``(sent_at, counts)``: *read_reported* false keeps the
    send instant for a requeue (:func:`carry_steered`) but never counts it as
    unread (:func:`waits`), because a harness that can drop an accepted steer
    reports every steer consumed only when its turn ends, so its pending list
    holds steers the model has already read. Each write keeps only the stamps of
    steers still pending.
    """
    if not message:
        return
    live = set(getattr(slot, "_pending_steers", None) or ()) | {message}
    stamps = getattr(slot, "_steered_at", None)
    kept = {k: v for k, v in stamps.items() if k in live} if isinstance(stamps, dict) else {}
    kept[message] = (_clock(), read_reported)
    try:
        slot._steered_at = kept
    except AttributeError:
        return


#: Most unconfirmed notes one slot keeps a record of (:func:`note_unconfirmed`).
#: Matches the slot's own cap on the steers it holds, which bounds how many one
#: failed turn can settle; past it the oldest record goes.
_MAX_UNCONFIRMED = 32

#: Numbers each unconfirmed note's wait identity, so two settled at one clock
#: reading stay two waits.
_unconfirmed_serial = itertools.count(1)


def note_unconfirmed(slot: Any, message: str, admission: Any) -> None:
    """Record that *slot*'s failed turn took a session's steer in unconfirmed.

    The steer is not requeued (``steer_queue._settle_taken_peer_steer``), so no
    queue entry is left for :func:`waits` to count, and nothing runs it again.
    This record keeps it counted for its sender as a message the worker has not
    been seen to read, with an identity that starts
    :data:`~kiro_crew.ledger_wake.UNCONFIRMED_WAIT_PREFIX`, so the sender's
    work-ledger watch wakes at once (``reason=message_unconfirmed``) rather than
    after the unread threshold. It counts whether or not the worker runs another
    turn meanwhile (one queued behind the failed turn starts at once), until the
    sender next sends to the worker (:func:`forget_unconfirmed`), which is its
    answer. Settled oldest first, so past :data:`_MAX_UNCONFIRMED` the oldest
    record goes. Best-effort like :func:`note_queued`.
    """
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        send_origin_slot,
    )
    from kiro_crew.ledger_wake import UNCONFIRMED_WAIT_PREFIX

    origin = send_origin_slot(admission)
    if not origin or not message:
        return
    stamps = getattr(slot, "_steered_at", None)
    sent = _sent_at(stamps.get(message)) if isinstance(stamps, dict) else None
    now = _clock()
    records = getattr(slot, "_steer_unconfirmed_waits", None)
    kept = dict(records) if isinstance(records, dict) else {}
    token = f"{UNCONFIRMED_WAIT_PREFIX}{now!r}#{next(_unconfirmed_serial)}"
    kept[token] = (ledger_key(origin), sent if sent is not None else now)
    while len(kept) > _MAX_UNCONFIRMED:
        kept.pop(next(iter(kept)))
    try:
        slot._steer_unconfirmed_waits = kept
    except AttributeError:
        return


def forget_unconfirmed(slot: Any, sender: str) -> None:
    """Drop *sender*'s :func:`note_unconfirmed` records on *slot*.

    Called when *sender* sends to *slot* again: that send is its answer to the
    "may not have run" notices, so they stop counting as unread.
    """
    records = getattr(slot, "_steer_unconfirmed_waits", None)
    if not isinstance(records, dict) or not sender:
        return
    want = ledger_key(sender)
    for token in [t for t, (origin, _sent) in records.items() if origin == want]:
        records.pop(token, None)


def _unconfirmed(slot: Any, want: str) -> list[tuple[str, float]]:
    """``(identity, sent_at)`` of each unconfirmed note of *want*'s (every
    sender's for ``""``), oldest first."""
    records = getattr(slot, "_steer_unconfirmed_waits", None)
    if not isinstance(records, dict):
        return []
    return [
        (token, float(sent))
        for token, (origin, sent) in records.items()
        if not want or origin == want
    ]


def requeued_steer_meta(admission: Any) -> dict[str, bool]:
    """The mark a requeued steer's entry carries when a session sent it.

    A steer the turn ended before reading wanted that turn, so a peer's (its
    admission names a send origin) is marked :data:`STEER_WANTED_META` like a
    refused one; the person's own composer steer carries no send origin and keeps
    its entry's shape.
    """
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        send_origin_slot,
    )

    return {STEER_WANTED_META: True} if send_origin_slot(admission) else {}


def steer_read_unreported(slot: Any, message: Any) -> bool:
    """Whether *message* is a steer *slot*'s turn accepted on a harness that
    reports a steer read only when its turn ends cleanly.

    Such a harness drops a steer from the pending list only on that clean end,
    so one still pending when its turn ended may already have been read. True
    until :func:`carry_steered` takes the stamp.
    """
    stamps = getattr(slot, "_steered_at", None)
    stamp = stamps.get(message) if isinstance(stamps, dict) and isinstance(message, str) else None
    return isinstance(stamp, tuple) and len(stamp) == 2 and stamp[1] is False


def carry_steered(slot: Any, message: str, queue_id: str) -> None:
    """Give the queue entry a steer was requeued as the instant it was sent.

    The turn ended before reading it, so it has been waiting since then, not
    since the requeue.
    """
    stamps = getattr(slot, "_steered_at", None)
    at = _sent_at(stamps.pop(message, None)) if isinstance(stamps, dict) else None
    queued = getattr(slot, "_queued_at", None)
    if at is not None and isinstance(queued, dict) and queue_id in queued:
        queued[queue_id] = at


def carry_wait(slot: Any, queue_id: str, from_ids: list[str]) -> None:
    """Give *queue_id* the wait of the oldest of *from_ids*, the entries it replaces.

    Its stamp becomes that entry's instant, and the wait keeps that entry's
    identity in ``slot._queued_as``, which :func:`waits` reports as ``oldest_id``.
    The unread wake is keyed on that id, so a wait that changed hands stays one
    wait and wakes its sender once, not once per replacement. Best-effort like
    :func:`note_queued`.
    """
    stamps = getattr(slot, "_queued_at", None)
    if not isinstance(stamps, dict) or not isinstance(stamps.get(queue_id), (int, float)):
        return
    olds = [(float(stamps[i]), i) for i in from_ids if isinstance(stamps.get(i), (int, float))]
    if not olds:
        return
    at, oldest = min(olds)
    if at >= stamps[queue_id]:
        return
    stamps[queue_id] = at
    live = {str(entry.get("id") or "") for entry in _entries(slot)}
    origins = getattr(slot, "_queued_as", None)
    origins = origins if isinstance(origins, dict) else {}
    kept = {k: v for k, v in origins.items() if k in live}
    kept[queue_id] = origins.get(oldest, oldest)
    try:
        slot._queued_as = kept
    except AttributeError:
        return


def forget(slot: Any, queue_ids: list[str]) -> None:
    """Drop the stamps and wait identities of entries taken off *slot*'s queue."""
    for attr in ("_queued_at", "_queued_as"):
        records = getattr(slot, attr, None)
        if isinstance(records, dict):
            for queue_id in queue_ids:
                records.pop(queue_id, None)


def _sent_at(stamp: Any, *, counted_only: bool = False) -> float | None:
    """The send instant in a :func:`note_steered` stamp, or ``None``.

    *counted_only* answers ``None`` for a stamp whose harness reports no read.
    """
    if not (isinstance(stamp, tuple) and len(stamp) == 2):
        return None
    at, counts = stamp
    if not isinstance(at, (int, float)) or (counted_only and counts is not True):
        return None
    return float(at)


def waits(
    slot: Any, *, sender: str = "", steer_wanted: bool = False, steers: bool = False
) -> QueueWait | None:
    """The entries waiting in *slot*'s queue, or ``None`` when none is selected.

    *sender* keeps only entries a session-control send from that session queued
    (its stamped send origin, folded like a ledger key so ``dashboard_chat-X`` and
    ``chat-X`` match). *steer_wanted* keeps only those whose sender wanted the
    running turn (:data:`STEER_WANTED_META`). *steers* also counts the steers the
    running turn accepted and has not read (the ``_pending_steers`` whose
    :func:`note_steered` stamp counts, attributed by their admission's send
    origin) and the steers a failed turn took in unconfirmed
    (:func:`note_unconfirmed`); each of those wanted the turn.
    """
    if slot is None:
        return None
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        send_origin_slot,
    )

    want = ledger_key(sender) if sender else ""
    stamps = getattr(slot, "_queued_at", None)
    stamps = stamps if isinstance(stamps, dict) else {}
    origins = getattr(slot, "_queued_as", None)
    origins = origins if isinstance(origins, dict) else {}
    now = _clock()
    count = 0
    oldest_at: float | None = None
    oldest_id = ""
    for entry in _entries(slot):
        meta = entry.get("meta")
        if want and ledger_key(send_origin_slot(meta)) != want:
            continue
        if steer_wanted and not (isinstance(meta, dict) and meta.get(STEER_WANTED_META)):
            continue
        count += 1
        entry_id = str(entry.get("id") or "")
        at = stamps.get(entry_id)
        if isinstance(at, (int, float)) and (oldest_at is None or at < oldest_at):
            oldest_at, oldest_id = float(at), str(origins.get(entry_id) or entry_id)
    if steers:
        admissions = getattr(slot, "_steer_admissions", None)
        admissions = admissions if isinstance(admissions, dict) else {}
        steered = getattr(slot, "_steered_at", None)
        steered = steered if isinstance(steered, dict) else {}
        for message in list(getattr(slot, "_pending_steers", None) or ()):
            sent = _sent_at(steered.get(message), counted_only=True)
            if sent is None:
                continue
            if want and ledger_key(send_origin_slot(admissions.get(message))) != want:
                continue
            count += 1
            if oldest_at is None or sent < oldest_at:
                oldest_at, oldest_id = sent, f"steer@{sent!r}"
        # A note a failed turn took in unconfirmed (:func:`note_unconfirmed`). Its
        # identity names the wait even when an older one exists: it is the one
        # that needs the sender now, and each new failure is news.
        lost = _unconfirmed(slot, want)
        for _token, sent in lost:
            count += 1
            if oldest_at is None or sent < oldest_at:
                oldest_at = sent
        if lost:
            oldest_id = lost[-1][0]
    if not count:
        return None
    return QueueWait(count, None if oldest_at is None else max(0.0, now - oldest_at), oldest_id)


def sender_unread(
    slot_table: Any, worker_key: str, sender: str
) -> tuple[int, float | None, str] | None:
    """What *sender* asked to have read now and *worker_key* has not read yet.

    The work-ledger wake's resolver, bound by the gateway to its slot table: the
    entries *sender* queued wanting the running turn (:data:`STEER_WANTED_META`) and
    its steers the running turn accepted but has not read, as
    ``(count, oldest_secs, oldest_id)``, or ``None`` for none or an unreadable table.
    Both slot spellings are tried, as ``ledger_wake.worker_running`` tries them.
    """
    if slot_table is None or not worker_key or not sender:
        return None
    getter = getattr(slot_table, "get_slot", None)
    if not callable(getter):
        return None
    for candidate in (worker_key, f"dashboard_{worker_key}"):
        try:
            slot = getter(candidate)
        except Exception:  # noqa: BLE001 - a slot read must not fail a tick
            continue
        if slot is not None:
            found = waits(slot, sender=sender, steer_wanted=True, steers=True)
            return None if found is None else (found.count, found.oldest_secs, found.oldest_id)
    return None


def unread_row(slot: Any, sender: str) -> dict[str, int] | None:
    """A ledger row's ``messages_unread``: ``{"count", "oldest_secs"?}`` or ``None``.

    Every entry *sender* queued, whether or not it asked for the running turn, and
    every steer of its the running turn has not read yet: the read answers "has my
    worker seen what I sent", which a queued-by-name note is part of.
    ``oldest_secs`` is absent when nothing selected carries a stamp.
    """
    found = waits(slot, sender=sender, steers=True) if sender else None
    if found is None:
        return None
    row = {"count": found.count}
    if found.oldest_secs is not None:
        row["oldest_secs"] = int(found.oldest_secs)
    return row


def oldest_field(slot: Any) -> dict[str, int]:
    """``{"queue_oldest_secs": N}`` for a read payload, or ``{}`` with no age to give."""
    found = waits(slot)
    if found is None or found.oldest_secs is None:
        return {}
    return {"queue_oldest_secs": int(found.oldest_secs)}


def _entries(slot: Any) -> list[dict[str, Any]]:
    queue = getattr(slot, "_queue", None)
    if not isinstance(queue, list):
        return []
    return [entry for entry in queue if isinstance(entry, dict)]
