"""Send every held card of a chat's queue with one press ("Send all held…").

After a restart the queue hands each unsent entry back as a held card
(:func:`~kiro_crew.dashboard.slot_queue_repository.is_held_queue_entry`): nothing
sends it until a person does. A card's own Send now is that person for one card.
This route is the same person for every held card the confirm box counted, with
one difference the owner ruled: it never stops a running turn.

Each named card that is still held gets the mark a card's "run this next" gives
(``RESTORED_SEND_CHOSEN_KEY``). The drain then sends them where they stand in the
queue, one per turn, and judges each against the slot's constraints when its turn
comes, as it does for a card Send now chose. A card that judgement would drop if
it ran now (a chat linked or mirrored to a channel: a restored card has no
admission record) is not marked: it stays held, words and all, and is named
back. On an idle slot the first one starts
now, through the start Send now uses (``turn_stop.start_queued_card``); a running
turn, the slot's own or a linked channel's, finishes first.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.queue_edit_hold import edit_held
from kiro_crew.dashboard.relay_archive import relay_archive_refusal
from kiro_crew.dashboard.slot_ownership import slot_not_found
from kiro_crew.dashboard.slot_queue_repository import (
    RESTORED_HELD_AGAIN_KEY,
    RESTORED_SEND_CHOSEN_KEY,
    is_held_queue_entry,
)

logger = logging.getLogger(__name__)

#: Why a held card named in the press stays held. The card's open editor holds
#: it (``queue_edit_hold``): sending it would send the words being changed.
EDITING_CODE = "queue_item_editing"
EDITING_ERROR = "It is open for editing."


def _choose_held(state: Any, slot: Any, wanted: set[str]) -> tuple[list[str], list[dict[str, str]]]:
    """Mark each entry of *slot*'s queue named in *wanted* that still waits for a
    person and that the drain would send.

    Returns the ids marked, in queue order, and one refusal per named card that
    stays held. A named entry that is gone, already chosen, or was never held is
    left as it is and named in neither list.
    """
    from kiro_crew.dashboard.chat_handlers import (
        CONTAINED_CODE,
        CONTAINED_ERROR,
        drain_would_drop,
    )

    named = [item for item in slot._queue if item.get("id") in wanted and is_held_queue_entry(item)]
    editing = {item["id"] for item in named if edit_held(item)}
    contained = drain_would_drop(state, slot, [i for i in named if i["id"] not in editing])
    sent: list[str] = []
    refused: list[dict[str, str]] = []
    for item in named:
        queue_id = item["id"]
        if queue_id in editing:
            refused.append({"queue_id": queue_id, "code": EDITING_CODE, "error": EDITING_ERROR})
        elif queue_id in contained:
            refused.append({"queue_id": queue_id, "code": CONTAINED_CODE, "error": CONTAINED_ERROR})
        else:
            item[RESTORED_SEND_CHOSEN_KEY] = True
            # This press is the person's send again of a card a limit held.
            item.pop(RESTORED_HELD_AGAIN_KEY, None)
            sent.append(queue_id)
    return sent, refused


async def api_chat_slot_queue_send_held(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/queue/send-held — send the held cards named.

    Body ``{"queue_ids": [...]}``: the held cards the confirm box counted, so a card
    held after the box opened stays held. Owner only. Answers ``sent`` (the ids
    marked, in queue order), ``refused`` (each named card that stays held, with
    its ``code`` and ``error``) and ``started`` (a turn began now).
    """
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.sel import sel

    state = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    # A person's own click is the only thing that lets a held card go: no app
    # token, agent tool or other signed-in person may.
    denied = await require_owner_dashboard_request(request, "chat.slot_queue_send_held")
    if denied is not None:
        return denied
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    raw = body.get("queue_ids")
    if not isinstance(raw, list) or not raw or not all(isinstance(i, str) and i for i in raw):
        return web.json_response(
            {"error": "queue_ids must be a list of queue ids", "code": "invalid_queue_ids"},
            status=400,
        )
    # No await from here to the idle check below: a turn that ends meanwhile
    # finds the marks, and one that has not ended drains them itself.
    sent, refused = _choose_held(state, slot, set(raw))
    if sent:
        try:
            state.broadcast_ws("queue_release", {"slot": name, "queue_ids": sent})
        except Exception:
            logger.warning("queue_release broadcast failed (slot %s)", name, exc_info=True)
        state.push_slots_update()
    sel().log_tool_invocation(
        session_key=chat_handlers._history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_send_held",
        tool_kind="command",
        outcome="chosen",
        metadata={"slot": name, "sent": len(sent), "refused": len(refused)},
    )
    started = False
    if sent and not slot.running and not slot._stopping and slot._stop_state == "idle":
        resp = await chat_handlers.start_queued_card(
            state, slot, name, request, None, tool_name="dashboard_send_held"
        )
        started = resp.status == 200
    return web.json_response({"ok": True, "sent": sent, "refused": refused, "started": started})
