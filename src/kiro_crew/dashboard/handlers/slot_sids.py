"""``GET /api/chat/slots/{slot}/sids``: one chat's title and every native session id it ran on.

A chat moves to a new native session on a context-cap trim, a reset or a provider
switch, so an app that keyed data on a session id needs the chat's whole list to
read it as one conversation (``session_map_sid_history``). Who may call it is the
``SLOT_SCOPE`` row in ``slot_ownership.SLOT_ROUTE_POLICIES``: the person and internal
callers any live slot, an app the live slots its declared slot scope shows it.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.slot_ownership import checkpoint_slot_replaced, slot_not_found


async def api_chat_slot_sids(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/sids — ``{key, title, sid, provider, history, cap, full}``.

    ``history`` is oldest first and never holds ``sid``; ``full`` says the cap
    dropped older ids. ``title`` is the sidebar's title (``NEW_SESSION_TITLE`` while
    untitled). A slot that is not live is the uniform 404.
    """
    state = request.app["state"]
    slot = state._slots.get(request.match_info["slot"])
    if slot is None or checkpoint_slot_replaced(request, slot):
        return slot_not_found()
    key = effective_session_key(slot)
    record = state.sessions._session_map.sid_record(key)
    return web.json_response({"key": key, "title": slot.display_title, **record})
