"""The end of a model reply that no tool call followed: where the agent stopped.

claude-agent-acp ends a model message with a ``usage_update`` (its
``message_delta`` reading) and opens a tool call on the tool block's
``content_block_start``, so in one message a tool call always arrives BEFORE
the message's closing ``usage_update``. Text with a ``usage_update`` after it
and no tool call in between is therefore a message that ended on text: Claude
meant to end its turn there. When the turn goes on anyway -- a Claude Code Stop
hook sent it back, or a queued message or a background task resumed it -- the
adapter sends nothing else that says so (it drops ``hook_response`` and the
hook's feedback message), so this is the one signal a consumer gets.

Two tools open no ``tool_call``: ``TodoWrite`` is drawn as a ``plan`` and a
``Task*`` call is suppressed until its result, whose ``plan`` arrives AFTER the
closing ``usage_update`` (0.109.0 ``sdk-to-acp.js``, the ``tool_use`` case). So
the closing reading only makes the mark pending; the next message's first frame
(its ``usage_update``, text or tool call) sends it, and a ``plan`` before that
drops it. A turn that ends there never sends it, which costs nothing: its last
message is the turn's conclusion anyway.

shortcut: a ``TaskGet``, or a ``TaskList``/``TaskUpdate`` that changes no entry,
sends no frame at all (no ``plan``), so the note before it is marked and stays
open; the miss only ever shows more, never hides an answer. Upgrade when the
adapter reports a message's ``stop_reason``.

The dashboard marks the segment such a reply flushes into (``meta.reply_end``)
so the transcript keeps it open when a later message of the same turn becomes
the conclusion. A progress note written in the same message as its tool call
never qualifies.

Claude only: the ordering above is claude-agent-acp's, read from its
``v1/acp-agent.js`` (``stream_event`` usage on ``message_start`` and
``message_delta``); another harness may report usage at other points, so it
gets no event (a positive ``is_claude_backend_name`` gate, H5).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.types import (
    EVENT_REPLY_END,
    EVENT_TEXT_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_PLAN,
    UPDATE_TOOL_CALL,
    UPDATE_USAGE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name

#: The frames that open the next model message: a pending mark is sent before them.
_NEXT_MESSAGE_FRAMES = frozenset(
    {UPDATE_USAGE, UPDATE_AGENT_MESSAGE_CHUNK, UPDATE_AGENT_THOUGHT_CHUNK, UPDATE_TOOL_CALL}
)


def reset(owner: Any) -> None:
    """Forget the previous prompt's reply state (called when a prompt starts)."""
    owner._reply_text_open = False
    owner._reply_end_pending = False


def note_event(owner: Any, ev: AcpEvent) -> None:
    """Track whether text has been yielded since the last tool call or checklist."""
    if ev.kind == EVENT_TEXT_CHUNK and ev.text and not ev.control_notice:
        owner._reply_text_open = True
    elif ev.kind in (EVENT_TOOL_CALL, EVENT_TODO_UPDATE):
        owner._reply_text_open = False


def on_frame(owner: Any, msg: JsonRpcMessage, backend: str | None) -> AcpEvent | None:
    """The reply-end event a top-level ``session/update`` frame releases, or None.

    Call it once per frame of the session's own, child frames excluded.
    """
    if not is_claude_backend_name(backend):
        return None
    update = (msg.params or {}).get("update")
    if not isinstance(update, dict):
        return None
    kind = update.get("sessionUpdate")
    if kind == UPDATE_PLAN:
        reset(owner)
        return None
    if kind not in _NEXT_MESSAGE_FRAMES:
        return None
    released = getattr(owner, "_reply_end_pending", False) is True
    owner._reply_end_pending = False
    if kind == UPDATE_USAGE and getattr(owner, "_reply_text_open", False):
        owner._reply_text_open = False
        owner._reply_end_pending = True
    return AcpEvent(kind=EVENT_REPLY_END) if released else None
