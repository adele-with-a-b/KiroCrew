"""Second live views of a session's own tabs: two tabs side by side, for a comparison.

A view is a :class:`~kiro_crew.browser_harness.live_hub.LiveSession` under its own key
with ``view_of`` set to its parent's: its own stream and frame acks, so a card bound to
the view's key shows the picture as on any session. A view owns no tab (the agent's
tab binding and every ownership check never see it) and no turn (the parent's turn
ending leaves it live). It is for looking only: the live routes refuse a takeover,
input or a reopen on a view's key (``dashboard/handlers/browser_live.py``), because
the person would be driving the parent's tab outside the parent's own lock.

:func:`stop_view` ends it, and the parent chat's close stops every view of its tabs
(``dashboard/handlers/browser_ops._close_tabs_of``). An app page opens one through
``BrowserView``'s ``tabId`` (the ``/api/browser/live/views/*`` routes).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from kiro_crew.browser_harness.live_hub import STATE_DONE, STATE_LIVE, tab_alive

if TYPE_CHECKING:
    from kiro_crew.browser_harness.live_hub import LiveHub, LiveSession
    from kiro_crew.browser_harness.screencast import LiveStream, Relay

#: The most second views one session may hold open.
VIEW_CAP = 8
#: A view nobody has looked at (visibility or a frame ack) for this long is ended at
#: the parent's next :func:`free_view_key`: a page that reloads or closes runs no
#: cleanup, and its view would otherwise hold a key until the chat closes. The page
#: of a mounted view says it still looks well inside this (``useBrowserSecondView``).
VIEW_IDLE_S = 120.0


def reap_idle_views(hub: LiveHub, parent_key: str) -> list[str]:
    """Fold *parent_key*'s views nobody has looked at for :data:`VIEW_IDLE_S`; their keys."""
    now = hub._clock()
    gone = [k for k in views_of(hub, parent_key) if now - hub._sessions[k].seen_at >= VIEW_IDLE_S]
    for key in gone:
        hub._fold(hub._sessions[key])
    return gone


def free_view_key(hub: LiveHub, parent_key: str) -> str:
    """The first ``<parent>.view<n>`` no live view of *parent_key* holds, or "" at the cap.

    Idle views are reaped first (:func:`reap_idle_views`).
    """
    reap_idle_views(hub, parent_key)
    taken = set(views_of(hub, parent_key))
    for n in range(1, VIEW_CAP + 1):
        key = f"{parent_key}.view{n}"
        if key not in taken:
            return key
    return ""


async def start_view(
    hub: LiveHub,
    parent_key: str,
    view_key: str,
    target_id: str,
    *,
    stream_factory: Callable[..., LiveStream] | None = None,
) -> LiveStream | None:
    """Show one of *parent_key*'s own tabs in a second live picture, keyed *view_key*.

    Raises ``LookupError`` for a parent with no session (or one that is itself a
    view), a tab the parent does not own or the relay does not list, or a key
    another session holds.
    """
    parent = hub._sessions.get(parent_key)
    if parent is None or parent.view_of or not view_key or view_key == parent_key:
        raise LookupError("unknown_session")
    relay = parent.relay
    if target_id not in parent.targets or not tab_alive(relay, target_id):
        raise LookupError("not_agent_tab")
    held = hub._sessions.get(view_key)
    if held is not None and (held.view_of != parent_key or held.state != STATE_DONE):
        # Another session's key, or a view another open reserved (still starting).
        raise LookupError("key_taken")
    assert relay is not None  # tab_alive is False without one
    sess = hub._session(view_key)
    # Reserved before the first await: from here ``views_of`` lists the key, so a
    # second open that runs while this stream attaches is given another one.
    sess.view_of = parent_key
    sess.turn_id = uuid.uuid4().hex
    sess.started_ms = int(time.time() * 1000)
    sess.ended = False
    sess.state = STATE_LIVE
    sess.seen_at = hub._clock()
    same_tab = parent.target_id == target_id
    try:
        stream = await hub.start_stream(
            view_key,
            relay,
            target_id,
            browser=parent.browser,
            headless=parent.headless,
            url=parent.url if same_tab else "",
            title=parent.title if same_tab else "",
            stream_factory=stream_factory,
        )
    except BaseException:
        hub._fold(sess)
        raise
    if stream is None:
        hub._fold(sess)
        return None
    if not same_tab:
        await _seed_page(hub, sess, relay, target_id)
    return stream


async def _seed_page(hub: LiveHub, sess: LiveSession, relay: Relay, target_id: str) -> None:
    """The address and title of a tab the agent is not on, from the browser itself.

    The agent's page reports name only its current tab; for any other, ask CDP once
    (``Target.getTargetInfo``). Best effort: a view with no address still shows.
    """
    try:
        reply = await relay.send("Target.getTargetInfo", {"targetId": target_id})
    except Exception:  # noqa: BLE001 - the picture matters, the label is a nicety
        return
    info = reply.get("targetInfo") if isinstance(reply, dict) else None
    if isinstance(info, dict) and sess.state != STATE_DONE:
        hub._update_page(sess, url=str(info.get("url") or ""), title=str(info.get("title") or ""))
        hub._emit(sess)


async def stop_view(hub: LiveHub, view_key: str) -> bool:
    """End a live view :func:`start_view` opened: stop its stream and fold it.

    The folded view stays in the hub as every folded card does, so its key is free
    for the parent's next :func:`start_view`. ``False`` when *view_key* is not a
    live view.
    """
    sess = hub._sessions.get(view_key)
    if sess is None or not sess.view_of or sess.state == STATE_DONE:
        return False
    hub._fold(sess)
    return True


def views_of(hub: LiveHub, parent_key: str) -> list[str]:
    """The keys of every live (not folded) view of *parent_key*'s tabs."""
    return [
        k
        for k, s in hub._sessions.items()
        if s.view_of and s.view_of == parent_key and s.state != STATE_DONE
    ]


def parent_of(hub: LiveHub, key: str) -> str:
    """The parent session of the view *key*, or "" when *key* is not a view."""
    sess = hub._sessions.get(key)
    return sess.view_of if sess is not None else ""


__all__ = [
    "VIEW_CAP",
    "VIEW_IDLE_S",
    "free_view_key",
    "parent_of",
    "reap_idle_views",
    "start_view",
    "stop_view",
    "views_of",
]
