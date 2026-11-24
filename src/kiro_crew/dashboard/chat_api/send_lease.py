"""A send's shared-crew answers around the send claim: the composer's typing lease
(409 ``composer_locked``) and the commit of a claimed send once it was accepted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import normalize_send_id


def _send_id_of(user_meta: dict | None) -> str:
    return (normalize_send_id(user_meta.get("sendId")) or "") if user_meta else ""


def _committed_if_accepted(request: web.Request, resp: Any, user_meta: dict | None) -> Any:
    """Record the claimed send when *resp* accepted it; a refusal gives the claim back."""
    from kiro_crew.shared_crew import send_race

    if getattr(resp, "status", 500) < 400:
        send_race.commit_request(request, message_id=_send_id_of(user_meta))
    return resp


def _composer_locked_answer(
    request: web.Request, state: Any, slot: Any, steer: object
) -> web.StreamResponse | None:
    """409 ``composer_locked`` when someone else holds the session's typing lease.

    Only a dashboard user's send is held: an app or automation send has no
    composer and no lease. A steer into a running turn passes, so a locked
    collaborator can still steer or stop the turn that is running.
    """
    if not request.get("is_dashboard_user", False):
        return None
    if steer and getattr(slot, "turn_running", False):
        return None
    from kiro_crew.shared_crew import typing_lease
    from kiro_crew.shared_crew.identity import actor_of

    body = typing_lease.refuse_if_locked(state, slot.key, actor_of(request).principal)
    if body is None:
        return None
    return web.json_response(
        {
            "error": body["error"],
            "code": body["code"],
            "session_key": body["session_key"],
            "holder": body["holder"],
            "lease": body["lease"],
        },
        status=409,
    )
