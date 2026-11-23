"""``POST /api/apps/<app>/status-changed``: an app says its own status changed.

The dashboard reads an app's status routes on its own schedule: a session control's
``statusPath`` (the composer chip) when its popover closes and when a turn ends, a
status item's ``statusPath`` (the top-bar pill) about once a minute. A change the app
makes itself -- the person answers in the app's own tab, a background read finishes
-- starts no turn, so the chip kept its old value and the pill lagged. This route
lets the app say so: it sends every owner dashboard window one ``app_status_changed``
frame, and each window re-reads only that app's probes: all of its top-bar items,
and its chips for one chat when ``session_key`` is given, else for every chat.

Who may move which app's status:

- an app token only its own (the path names the app; the token's verified identity
  must equal it), the same rule ``/api/notifications/own`` keeps;
- the owner's dashboard, where an app's page runs, any app (the App SDK hook fills
  in the page's own app name);
- nobody an app that is not installed, is disabled, or declares no status route.

Every call is delivered: no rate limit and no dedupe, because an app may report two
real changes seconds apart and each costs only that app's own status reads.
"""

from __future__ import annotations

import asyncio
import re

from aiohttp import web

from kiro_crew.apps.manager import get_app_manifest, is_app_enabled
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.sel import sel

#: The WS frame type. Owner-only (``ws_event_scope._OWNER_ONLY_EVENTS``): the app
#: that caused it has no use for it, and a relayed teammate shows no app status.
APP_STATUS_CHANGED_FRAME = "app_status_changed"

#: A session key as the dashboard builds it (``dashboard:<slot>``) and as the other
#: surfaces do. Bounded so the frame stays small; the frontend matches it against its
#: own keys, so a key naming no session moves nothing.
_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9:._-]{1,200}$")

_OPERATION = "app_status_changed"


def _declares_status(app_name: str) -> bool:
    """Whether *app_name* is installed, enabled and declares a status route.

    Runs in a worker thread: ``get_app_manifest`` reads ``installed.json`` and
    ``app.json`` from disk.
    """
    if not is_app_enabled(app_name):
        return False
    manifest = get_app_manifest(app_name)
    if manifest is None:
        return False
    contributes = manifest.contributes
    return any(c.statusPath for c in contributes.sessionControls) or any(
        i.statusPath for i in contributes.statusItems
    )


def _refuse(caller: str, app_name: str, status: int, code: str, error: str) -> web.Response:
    sel().log_api_access(
        caller=caller,
        operation=_OPERATION,
        outcome="denied",
        source="app_status_api",
        resources=app_name,
        error=error,
    )
    return web.json_response({"error": error, "code": code}, status=status)


async def api_app_status_changed(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/status-changed -- re-read this app's status now."""
    app_name = request.match_info["name"]
    caller_app = str(request.get("app") or "")
    caller = f"app:{caller_app}" if caller_app else "dashboard"
    if caller_app:
        if caller_app != app_name:
            return _refuse(
                caller, app_name, 403, "app_status_not_own", "an app may only change its own status"
            )
    else:
        denied = await require_owner_dashboard_request(request, _OPERATION)
        if denied is not None:
            return denied

    body, cap_err = await read_bounded_json(request, allow_absent=True)
    if cap_err is not None:
        return cap_err
    body = body or {}
    frame: dict[str, str] = {"app": app_name}
    if "session_key" in body:
        session_key = body["session_key"]
        if not isinstance(session_key, str) or not _SESSION_KEY_RE.fullmatch(session_key):
            return _refuse(
                caller,
                app_name,
                400,
                "app_status_bad_session_key",
                "session_key is not a session key",
            )
        frame["session_key"] = session_key

    if not await asyncio.to_thread(_declares_status, app_name):
        return _refuse(
            caller,
            app_name,
            404,
            "app_status_not_declared",
            "app is not enabled or declares no status route",
        )

    request.app["state"].broadcast_ws_owners(APP_STATUS_CHANGED_FRAME, frame)
    sel().log_api_access(
        caller=caller,
        operation=_OPERATION,
        outcome="ok",
        source="app_status_api",
        resources=app_name,
    )
    return web.json_response({"ok": True})
