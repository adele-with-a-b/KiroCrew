"""A newer broadcast note replaces the same sender's older unread one.

A conductor broadcasts "main moved" after each merge. A worker that stays in one
turn for hours would otherwise find five of them stacked in its queue, each
starting its own turn later to say it is stale. So when a broadcast is about to
QUEUE on a target that already holds an unread broadcast entry from the same
sender, those entries leave the queue and the new one says how many it replaced.

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

#: Queue-entry ``meta`` key: how many earlier notes this one already replaced, so
#: a chain of replacements still reports the whole count.
REPLACED_META = "broadcast_replaced"


def _replaced_by(entry: dict[str, Any]) -> int:
    meta = entry.get("meta")
    count = meta.get(REPLACED_META) if isinstance(meta, dict) else None
    return count if isinstance(count, int) and count > 0 else 0


def superseded_entries(
    state: "DashboardState", slot: Any, caller_key: str, *, via: str
) -> list[dict[str, Any]]:
    """The entries of *slot*'s queue a new broadcast from *caller_key* replaces.

    *via* is the broadcast's relay verb, passed in by its one caller so the verb
    keeps a single spelling (``session_messaging.BROADCAST_VIA``).
    """
    # Deferred: both import the session_control facade, which imports this package.
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


def replaced_count(entries: list[dict[str, Any]]) -> int:
    """How many notes *entries* stand for, each counting those it replaced."""
    return sum(1 + _replaced_by(entry) for entry in entries)


def replaced_line(count: int) -> str:
    """The line a replacing note carries, read by the target's model and person."""
    noun = "note" if count == 1 else "notes"
    return (
        f"(This replaces {count} earlier unread broadcast {noun} from this session; "
        "only this one is current.)\n\n"
    )


def drop_superseded(state: "DashboardState", slot: Any, entries: list[dict[str, Any]]) -> None:
    """Take *entries* off *slot*'s queue and off every open dashboard.

    No sender notice: the sender is the one replacing them, and the reply it gets
    says how many went. The frame carries no content, so no composer takes the
    text back (the cancel route's frame does, for the person's own words). The
    rest is what the cancel route does to the slot: any waiting placeholder row
    goes with the entry, and the session list's queue depth is pushed once.
    """
    # Deferred like the imports above: this module loads inside the session_control
    # facade, so it adds no dashboard module to that facade's import time.
    from kiro_crew.dashboard.chat_utils_injected_rows import _remove_queued_by_id

    dropped = False
    for entry in entries:
        queue_id = str(entry.get("id") or "")
        if not queue_id or slot.queue_remove_by_id(queue_id) is None:
            continue
        dropped = True
        if _remove_queued_by_id(slot.messages, queue_id):
            slot.invalidate_source_links()
        state.broadcast_ws("queue_cancel", {"slot": slot.key, "queue_id": queue_id})
        logger.info(
            "session_broadcast: a newer broadcast replaced queued entry %s on %s",
            queue_id,
            slot.key,
        )
    if dropped:
        state.push_slots_update()
