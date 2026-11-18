"""Discarding a Bot chat's session with its allocation ending fence held.

``bot_chat_app_tools.stop_app_bot_chat_servers`` stops a Bot chat that mounts a
withdrawn app's servers. A discard alone cannot tell "nothing runs" from "a start
is inside ``provider.start()``, recorded as mounted but not registered yet": both
find no registered session. The ending fence of the session allocator
(``SessionAllocationService.begin_ending`` / ``spawn_in_flight`` / ``end_ending``)
is what can, so the discard runs inside it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("kiro_crew.bot_chat_app_tools")


async def discard_with_ending_fence(
    sessions: Any, session_key: str, unconfirmed_out: list | None
) -> tuple[bool, str | None]:
    """Discard *session_key* with its ending fence held throughout.

    Returns ``(confirmed, in_flight)``. The fence invalidates any start already in
    flight and holds new ones at the door while the discard runs; ``spawn_in_flight``
    is read before it lifts, so a start inside ``provider.start()`` or one refused
    during the discard is named. A named start, or a fence that could not be raised,
    means the stop is not confirmed: no registered session is not evidence that
    nothing runs.
    """
    try:
        alloc = sessions._allocation_boundary()
        alloc.begin_ending(session_key)
    except Exception:  # noqa: BLE001
        logger.warning(
            "bot chat app tools: could not raise the ending fence for %s",
            session_key,
            exc_info=True,
        )
        return False, "ending fence unavailable"
    try:
        kwargs: dict[str, Any] = {"replay": False, "confirm_teardown": True}
        if unconfirmed_out is not None:
            kwargs["unconfirmed_handle_out"] = unconfirmed_out
        confirmed = await sessions.discard_conversation(session_key, **kwargs)
        in_flight = alloc.spawn_in_flight(session_key)
    finally:
        alloc.end_ending(session_key)
    return bool(confirmed), in_flight if isinstance(in_flight, str) and in_flight else None


class _UnobservedKill:
    """The retry handle of a hard kill nothing reads back: it never confirms.

    The close stays pending, so the Bot's stop keeps reporting incomplete, until the
    gateway restarts and every chat starts on a freshly built spec.
    """

    async def evict_session(self, session_id: str) -> bool:
        return False


def note_unobserved_kill(session_key: str, token: str) -> None:
    """Keep a Bot chat's close unconfirmed after a hard kill whose outcome is unknown."""
    from kiro_crew.bot_chat_app_tools import note_unconfirmed_close

    note_unconfirmed_close(session_key, _UnobservedKill(), token)


async def stop_tracked_bot_chats(state: Any, app_name: str) -> list[str] | None:
    """Stop the Bot chats the tracking tables tie to *app_name*, from those tables alone.

    For a lifecycle operation that finds no installed record for the app: an app
    that deleted its own ``installed.json`` is still mounted, pending delivery or
    pending close in ``bot_chat_app_tools``, and its Bot chats must stop all the
    same. Returns ``None`` when nothing is tracked for the app, else the slugs (or
    session keys) whose stop is not confirmed; an empty list means every one
    stopped. Runs inside the app's stop fence.
    """
    from kiro_crew import bot_chat_app_tools as tools

    if not tools.app_has_active_bot_sessions(app_name):
        return None
    async with tools.app_stop_fence(app_name):
        tools.revoke_app_generation(app_name)
        try:
            _stopped, failed = await tools.stop_app_bot_chat_servers(state, app_name)
        except Exception:  # noqa: BLE001 - reported as incomplete, never swallowed
            logger.warning("stopping tracked Bot chats for %r failed", app_name, exc_info=True)
            failed = ["unknown"]
    return list(failed)
