"""A newer broadcast note replaces the same sender's older unread one.

A conductor broadcasts "main moved" after each merge. A worker that stays in one
turn for hours would otherwise find five of them stacked in its queue, each
starting its own turn later to say it is stale. So when a broadcast is about to
QUEUE on a target that already holds an unread broadcast entry from the same
sender, the new one takes the oldest one's place and the others leave the queue.
It keeps that place's position, its wait (``queue_age.carry_wait``: the instant
and the identity the unread wake is keyed on) and its wish for the running turn,
so a newer note never resets a sender's unread clock, never wakes the sender
again about the same wait, never moves the sender's news behind messages that
came after it, and its text says how many notes it replaced.

Only an entry the sender's own broadcast queued is replaced: the sender stamp
must name the caller's slot AND tab (``session_control_queue``'s ``yours``), the
entry must carry the relay verb :data:`BROADCAST_VIA`, and it must still be
unread -- not a person's message, not one a person has open in its editor, not
one whose row the transcript already holds. A ``session_send`` by name, a
requeued steer and anything restored after a restart (the restore drops the
sender stamp) never match, so they are never replaced.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Slot attribute, keyed by queue-entry id: how many earlier notes that entry
#: already replaced, so a chain of replacements still reports the whole count.
#: Kept BESIDE the queue like ``queue_age``'s stamps, never in the entry's meta,
#: which the drain unions onto the transcript row it writes. In-process only: a
#: restored entry has lost its sender stamp, so it is never replaced anyway.
REPLACED_ATTR = "_broadcast_replaced"


def _replaced_by(slot: Any, entry: dict[str, Any]) -> int:
    counts = getattr(slot, REPLACED_ATTR, None)
    count = counts.get(str(entry.get("id") or "")) if isinstance(counts, dict) else None
    return count if isinstance(count, int) and count > 0 else 0


def _note_replaced(slot: Any, queue_id: str, count: int) -> None:
    """Record that *queue_id* stands for *count* earlier notes, keeping only the
    records of entries still queued."""
    live = {str(e.get("id") or "") for e in slot._queue}
    counts = getattr(slot, REPLACED_ATTR, None)
    kept = {k: v for k, v in counts.items() if k in live} if isinstance(counts, dict) else {}
    kept[queue_id] = count
    try:
        setattr(slot, REPLACED_ATTR, kept)
    except AttributeError:
        return


def superseded_entries(
    state: "DashboardState", slot: Any, caller_key: str, *, via: str
) -> list[dict[str, Any]]:
    """The entries of *slot*'s queue a new broadcast from *caller_key* replaces.

    *via* is the broadcast's relay verb, passed in by its one caller so the verb
    keeps a single spelling (``session_messaging.BROADCAST_VIA``).
    """
    # Deferred: both import the session_control facade, which imports this module.
    from kiro_crew.dashboard import responsive_chat
    from kiro_crew.dashboard.queue_edit_hold import edit_held
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        send_origin_meta,
        send_origin_tab,
    )
    from kiro_crew.dashboard.session_control_queue import (
        QUEUE_FROM_CALLER,
        _queue_entry_from,
    )

    caller_tab = send_origin_tab(send_origin_meta(state, caller_key))
    if not caller_tab:
        return []
    out = []
    for entry in list(getattr(slot, "_queue", ())):
        if not isinstance(entry, dict):
            continue
        meta = entry.get("meta")
        relay = meta.get("relay") if isinstance(meta, dict) else None
        if not isinstance(relay, dict) or relay.get("via") != via:
            continue
        if _queue_entry_from(entry, caller_key, caller_tab) != QUEUE_FROM_CALLER:
            continue
        if edit_held(entry) or responsive_chat.row_already_written(slot, entry):
            continue
        out.append(entry)
    return out


def replaced_count(slot: Any, entries: list[dict[str, Any]]) -> int:
    """How many notes *entries* stand for, each counting those it replaced."""
    return sum(1 + _replaced_by(slot, entry) for entry in entries)


def replaced_line(count: int) -> str:
    """The line a replacing note carries, read by the target's model and person."""
    noun = "note" if count == 1 else "notes"
    return (
        f"\n\n(This replaces {count} earlier unread broadcast {noun} from this session; "
        "only this one is current.)"
    )


def _take_place(slot: Any, entries: list[dict[str, Any]], queued_before: set[str]) -> bool:
    """Give the entry just queued the place, wait and turn wish of the oldest of
    *entries*. Returns whether it moved.

    The new entry is the one whose id was not queued before the delivery; with
    none (the delivery started a turn instead) there is nothing to carry.
    """
    from kiro_crew.dashboard import queue_age

    queue = slot._queue
    new = [e for e in queue if str(e.get("id") or "") not in queued_before]
    replaced_ids = {str(e.get("id") or "") for e in entries}
    if not new or not replaced_ids:
        return False
    entry = new[-1]
    new_id = str(entry.get("id") or "")
    _note_replaced(slot, new_id, replaced_count(slot, entries))
    # Its wait is the oldest replaced one's, instant and identity: the sender's
    # news has sat unread since then, a fresh stamp would keep pushing the unread
    # wake back, and a fresh identity would wake the sender again on each note.
    queue_age.carry_wait(slot, new_id, sorted(replaced_ids))
    # A replaced note that wanted the running turn hands that wish on.
    meta = entry.get("meta")
    if isinstance(meta, dict) and any(
        isinstance(e.get("meta"), dict) and e["meta"].get(queue_age.STEER_WANTED_META)
        for e in entries
    ):
        meta[queue_age.STEER_WANTED_META] = True
    first = next((i for i, e in enumerate(queue) if str(e.get("id") or "") in replaced_ids), None)
    at = next((i for i, e in enumerate(queue) if e is entry), None)
    if first is None or at is None or first > at:
        return False
    del queue[at]
    queue.insert(first, entry)
    return True


def replace_superseded(
    state: "DashboardState",
    slot: Any,
    entries: list[dict[str, Any]],
    queued_before: set[str],
) -> None:
    """Put the entry just queued in the place of *entries*, then take them off
    *slot*'s queue and off every open dashboard.

    *queued_before* is the queue's ids before the delivery, which is how the new
    entry is told from the rest. No sender notice: the sender is the one replacing
    them, and the reply it gets says how many went. The ``queue_cancel`` frame
    carries no content, so no composer takes the text back (the cancel route's
    frame does, for the person's own words). The rest is what the cancel and
    reorder routes do to the slot: any waiting placeholder row goes with the entry,
    the rows follow the new order, and the session list is pushed once. The new
    note is already queued, so a failing push is logged, never raised: the
    sender must not read a delivery that landed as a failure.
    """
    # Deferred like the imports above: this module loads inside the session_control
    # facade, so it adds no dashboard module to that facade's import time.
    from kiro_crew.dashboard import queue_age
    from kiro_crew.dashboard.chat_utils_injected_rows import (
        _remove_queued_by_id,
        _reorder_queued_rows,
    )

    moved = _take_place(slot, entries, queued_before)
    dropped: list[str] = []
    for entry in entries:
        queue_id = str(entry.get("id") or "")
        if not queue_id or slot.queue_remove_by_id(queue_id) is None:
            continue
        dropped.append(queue_id)
        if _remove_queued_by_id(slot.messages, queue_id):
            slot.invalidate_source_links()
    if not dropped:
        return
    # Their side records go with them, so every wait or count a reader sees names
    # an entry still queued.
    queue_age.forget(slot, dropped)
    counts = getattr(slot, REPLACED_ATTR, None)
    if isinstance(counts, dict):
        for queue_id in dropped:
            counts.pop(queue_id, None)
    order = [str(e.get("id") or "") for e in slot._queue]
    if moved:
        _reorder_queued_rows(slot.messages, order)
        slot.invalidate_source_links()
    logger.info(
        "session_broadcast: a newer broadcast replaced queued entries %s on %s",
        ", ".join(dropped),
        slot.key,
    )
    pushes: list[tuple[Any, ...]] = [
        (state.broadcast_ws, "queue_cancel", {"slot": slot.key, "queue_id": queue_id})
        for queue_id in dropped
    ]
    if moved:
        pushes.append((state.broadcast_ws, "queue_reorder", {"slot": slot.key, "order": order}))
    pushes.append((state.push_slots_update,))
    # Each push on its own: one that fails must not leave the others' cards stale.
    for push, *args in pushes:
        try:
            push(*args)
        except Exception:
            logger.warning(
                "could not push a replaced broadcast note to %s", slot.key, exc_info=True
            )
