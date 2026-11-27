"""The "Not now" control: the owner sets one reply ask aside until the agent asks again.

A chat whose newest finished reply asks the owner something (an ``[OPTIONS:]``
line or a line opening with a question label, the ``options`` and ``labelled``
kinds of ``needs_you``) lights the owner's Needs you list. Pressing "Not now"
under that reply names the reply's row (``meta.mid``); while the newest reply
ask is still that row, the projection leaves it out of ``needs_you``, so every
reader of the field (the sidebar entry, the counts, the badges) follows with no
change of its own. A newer reply that asks again is another row, so it lights
the chat again; the same reply never does.

Every row carries :data:`FIELD`: ``None`` when the newest reply asks the owner
nothing (or asks someone else), else ``{row_id, first_words, set_aside}`` for
that ask, which is where the chat draws the control and the sidebar its calm
line. Approvals and question cards block the agent, so they are never here and
can never be set aside.

The mark is one string on the slot (:data:`ATTR`), saved on the metadata line
(:data:`META_KEY`) so a reload and a restart keep it. Only the dashboard user
sets it, through :func:`api_chat_slot_set_aside` (the mute toggles' user-only
fences); no agent route or MCP tool reaches it. Nothing is sent to the agent and
no transcript row is written.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.needs_you import Ask
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The row field.
FIELD = "reply_ask"
#: The slot attribute holding the set-aside reply's ``meta.mid``, ``""`` for none.
ATTR = "_needs_you_set_aside"
#: Its key on the metadata line.
META_KEY = "needs_you_set_aside"
#: The longest row id the route accepts; a ``meta.mid`` is far shorter.
ROW_ID_MAX = 200

_OPERATION = "chat.slot_set_aside"


def split_set_aside(slot: Any, asks: list[Ask]) -> tuple[list[Ask], dict[str, Any] | None]:
    """The reply *asks* that still count, and the row's :data:`FIELD` value.

    *asks* are ``needs_you.reply_asks``' answer: none, or the newest reply's one
    ask. An ask with no row cannot be named, so it is never set aside.
    """
    if not asks or not asks[0].row_id:
        return asks, None
    ask = asks[0]
    aside = getattr(slot, ATTR, "") == ask.row_id
    value = {"row_id": ask.row_id, "first_words": ask.words, "set_aside": aside}
    return ([] if aside else asks), value


def _refuse(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message, "code": code}, status=status)


async def api_chat_slot_set_aside(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/set-aside -- "Not now", or "Bring back".

    Body ``{"row_id": str, "set_aside": bool}``. Setting aside lands only while
    *row_id* is still the chat's open reply ask, judged under the slot metadata
    lock; otherwise ``409 not_waiting`` (as the Board's Skip refuses a stale
    press), so an ask that arrived after the page drew is never hidden unseen.
    Bringing back clears the mark when it names that row. Persisted before the
    answer; every open window gets the new row from the slots frame.
    """
    # Deferred: chat_folders imports the state facade, which imports the projection.
    from kiro_crew.dashboard import chat_folders
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.handlers._shared import read_bounded_json

    owned = chat_folders.user_owned_slot(request, _OPERATION, "set_aside")
    if isinstance(owned, web.Response):
        return owned
    state, slot, request_app, authorized_history_key = owned
    name = request.match_info["slot"]
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    row_id = body.get("row_id")
    wanted = body.get("set_aside")
    if not isinstance(row_id, str) or not row_id or len(row_id) > ROW_ID_MAX:
        return _refuse("row_id_invalid", "row_id must name the reply", 400)
    if not isinstance(wanted, bool):
        return _refuse("set_aside_not_bool", "set_aside must be a boolean", 400)
    source, caller = chat_folders._audit_origin(request)

    def audit(outcome: str, error: str = "") -> None:
        sel().log_api_access(
            caller=caller,
            operation=_OPERATION,
            outcome=outcome,
            source=source,
            resources=name,
            **({"error": error} if error else {}),
        )

    async with chat_folders._slot_meta_txn_lock(state):
        if (
            state._slots.get(name) is not slot
            or slot_history_key(slot) != authorized_history_key
            or not chat_folders.app_owns_transcript(
                state._slots, request_app, authorized_history_key
            )
        ):
            audit("denied", "session was deleted or rebound")
            return _refuse("session_gone", "session was deleted or rebound", 409)
        prior = getattr(slot, ATTR, "")
        if wanted:
            shown = slot.to_dict().get(FIELD)
            if not isinstance(shown, dict) or shown.get("row_id") != row_id:
                audit("denied", "not the open reply ask")
                return _refuse(
                    "not_waiting",
                    "Nothing was set aside: this ask changed or was answered since it was shown.",
                    409,
                )
            new = row_id
        else:
            new = "" if prior == row_id else prior
        if new == prior:
            audit("allowed")
            return web.json_response({"ok": True, "set_aside": wanted, "changed": False})
        # shortcut: the live mark is set before the save rather than staged
        # through the save's folds as the mute toggles do, so a slots frame sent
        # during the save can show it early; a failed save puts it back and
        # sends the row again. Stage it like ``muted_override`` if that window
        # ever matters.
        setattr(slot, ATTR, new)
        committed = False

        def _committed() -> None:
            nonlocal committed
            committed = True

        try:
            saved = await save_slot_off_loop(
                state,
                slot,
                force=True,
                best_effort=False,
                expected_history_key=authorized_history_key,
                after_commit_under_lock=_committed,
            )
        except Exception:
            setattr(slot, ATTR, prior)
            slot._dirty = True
            state.push_slots_update()
            audit("denied", "could not persist")
            logger.warning("set-aside of slot=%s not saved", slot.key, exc_info=True)
            return _refuse("set_aside_save_failed", "could not save; please retry", 503)
        if not saved or not committed:
            setattr(slot, ATTR, prior)
            state.push_slots_update()
            audit("denied", "session has no durable record")
            return _refuse("session_gone", "this session cannot be saved yet", 409)
    state.push_slots_update()
    audit("allowed")
    return web.json_response({"ok": True, "set_aside": wanted, "changed": True})
