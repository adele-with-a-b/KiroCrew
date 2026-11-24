"""A claude model message that ended on its text is marked, and the transcript row keeps it.

The shape that hid an answer: the agent answered and ended its turn, a Claude Code
Stop hook sent the turn back (claude-agent-acp sends no frame for that), and the
agent wrote a correction. The frames the handle sees are the claude adapter's real
order: a tool call opens on ``content_block_start``, so it arrives BEFORE its
message's closing ``usage_update``; a message that ends on text has a
``usage_update`` after the text and no tool call between them. A checklist call
(``TodoWrite``, ``Task*``) opens no tool call, only a ``plan``, which for ``Task*``
comes after that closing reading -- so the mark waits for the next message's first
frame and a ``plan`` drops it.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import reply_end
from kiro_crew.acp import session_handle as sh
from kiro_crew.acp.types import (
    EVENT_REPLY_END,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    METHOD_SESSION_UPDATE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_turn.segment_flush import REPLY_END_META_KEY
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


def _handle(backend: str) -> sh.AcpSessionHandle:
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.acp_backend = backend
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    rt.send_request = AsyncMock(return_value=1)
    return sh.AcpSessionHandle("sA", asyncio.Queue(), rt)


def _frame(update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE, params={"sessionId": "sA", "update": update}
    )


def _text(text: str) -> dict:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def _tool(call_id: str) -> dict:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": "Read config.json",
        "kind": "read",
        "status": "pending",
    }


_USAGE = {"sessionUpdate": "usage_update", "used": 1200, "size": 200000}
_PLAN = {
    "sessionUpdate": "plan",
    "entries": [{"content": "Read the scheduler", "status": "pending", "priority": "medium"}],
}


def _kinds(handle: sh.AcpSessionHandle, updates: list[dict]) -> list[str]:
    out: list[str] = []
    for update in updates:
        out.extend(ev.kind for ev in handle._handle_update(_frame(update)))
    return out


def _marked(updates: list[dict], backend: str = ACP_BACKEND_CLAUDE) -> list[str]:
    """The text, tool and reply-end events the handle makes of *updates*, in order."""
    kinds = _kinds(_handle(backend), updates)
    return [k for k in kinds if k in (EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, EVENT_REPLY_END)]


#: question -> progress note + tool -> answer (turn ends) -> Stop hook -> tool -> correction
_SENT_BACK_TURN = [
    _USAGE,  # message 1 message_start
    _text("Let me read the scheduler first."),
    _tool("c1"),
    _USAGE,  # message 1 message_delta: closed after its tool call
    _USAGE,  # message 2 message_start
    _text("The job runs at 09:00 local time."),
    _USAGE,  # message 2 message_delta: ended on text -- the answer
    # (the Stop hook blocks here; the adapter sends nothing)
    _USAGE,  # message 3 message_start: releases the answer's mark
    _tool("c2"),
    _USAGE,  # message 3 message_delta
    _USAGE,  # message 4 message_start
    _text("Correction: it runs at 09:00 UTC."),
    _USAGE,  # message 4 message_delta: the turn's last message, its conclusion
]


def test_the_sent_back_turn_marks_the_answer_never_the_progress_note():
    assert _marked(_SENT_BACK_TURN) == [
        EVENT_TEXT_CHUNK,  # progress note
        EVENT_TOOL_CALL,
        EVENT_TEXT_CHUNK,  # answer
        EVENT_REPLY_END,  # ... marked before the next message's tool call
        EVENT_TOOL_CALL,
        EVENT_TEXT_CHUNK,  # correction
    ]


def test_a_note_before_a_task_call_is_not_marked():
    """A ``Task*`` call is suppressed until its result: its ``plan`` follows the closing reading."""
    marked = _marked([_text("Let me track this."), _USAGE, _PLAN, _USAGE, _tool("c1"), _USAGE])
    assert EVENT_REPLY_END not in marked


def test_a_note_before_a_todowrite_plan_is_not_marked():
    marked = _marked([_text("Let me track this."), _PLAN, _USAGE, _USAGE, _tool("c1")])
    assert EVENT_REPLY_END not in marked


@pytest.mark.parametrize("next_frame", [_USAGE, _text("Also,"), _tool("c9")])
def test_the_next_messages_first_frame_releases_the_mark(next_frame):
    assert _marked([_text("Done."), _USAGE, next_frame]).count(EVENT_REPLY_END) == 1


@pytest.mark.parametrize("backend", ["kiro", "kas", "codex", ""])
def test_a_harness_other_than_claude_gets_no_reply_end(backend):
    assert EVENT_REPLY_END not in _marked(_SENT_BACK_TURN, backend)


def test_the_mark_fires_once_per_message():
    assert _marked([_text("Done."), _USAGE, _USAGE, _USAGE]).count(EVENT_REPLY_END) == 1


def test_a_child_sessions_frame_releases_nothing():
    handle = _handle(ACP_BACKEND_CLAUDE)
    _kinds(handle, [_text("Done."), _USAGE])
    child = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE, params={"sessionId": "child-1", "update": _USAGE}
    )
    assert EVENT_REPLY_END not in [ev.kind for ev in handle._handle_update(child)]
    assert EVENT_REPLY_END in _kinds(handle, [_USAGE])


def test_a_new_prompt_drops_the_last_prompts_pending_mark():
    """A turn cut off after its text (a cancel) must not mark the next prompt's first frame."""
    handle = _handle(ACP_BACKEND_CLAUDE)
    _kinds(handle, [_text("The job runs at"), _USAGE])
    reply_end.reset(handle)  # what both prompt starts call
    assert EVENT_REPLY_END not in _kinds(handle, [_USAGE, _text("Hi."), _tool("c1")])


def test_both_prompt_starts_reset_the_reply_state():
    import inspect

    from kiro_crew.acp import client_turn, session_handle_turn

    assert "reply_end.reset(self)" in inspect.getsource(client_turn._dispatch_events)
    assert "reply_end.reset(self)" in inspect.getsource(session_handle_turn)


def test_a_control_notice_is_not_reply_text():
    owner = SimpleNamespace(_reply_text_open=False, _reply_end_pending=False)
    reply_end.note_event(owner, AcpEvent(kind=EVENT_TEXT_CHUNK, text="x", control_notice=True))
    msg = _frame(_USAGE)
    assert reply_end.on_frame(owner, msg, ACP_BACKEND_CLAUDE) is None
    assert reply_end.on_frame(owner, msg, ACP_BACKEND_CLAUDE) is None


def test_the_turn_marks_the_open_segment_and_its_row_carries_the_flag_once():
    from kiro_crew.dashboard.chat_turn.stream_chunks import _on_reply_end

    slot = _ChatSlot("reply-end-meta")
    _on_reply_end(SimpleNamespace(has_assistant_text=False, slot=slot))
    assert slot.segment_reply_end is False  # no open text: the segment already flushed
    _on_reply_end(SimpleNamespace(has_assistant_text=True, slot=slot))
    meta = chat_runner._segment_row_meta(slot, text="The job runs at 09:00 local time.")
    assert meta is not None and meta[REPLY_END_META_KEY] is True
    assert slot.segment_reply_end is False
    later = chat_runner._segment_row_meta(slot, text="Let me read the scheduler first.")
    assert not (later or {}).get(REPLY_END_META_KEY)


@pytest.mark.asyncio
async def test_the_streaming_client_path_marks_the_same_messages():
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.types import AcpPromptStats

    updates = [*_SENT_BACK_TURN[:9], _PLAN, _text("Tracking it."), _USAGE, _PLAN, _USAGE]

    async def fake_prompt_loop(req_id, timeout):
        for update in updates:
            yield "update", JsonRpcMessage(method=METHOD_SESSION_UPDATE, params={"update": update})
        yield "complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

    client = AcpClient()
    client._acp_backend = ACP_BACKEND_CLAUDE
    client._prompt_loop = fake_prompt_loop
    client.last_prompt_stats = AcpPromptStats()
    client._tool_call_inputs = {}
    client._stale_eligible = False
    client._turn_done = asyncio.Event()
    client._last_stop_reason = ""
    client._read_new_tool_results_sync = lambda: []

    kinds = [
        ev.kind async for ev in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)
    ]
    assert [k for k in kinds if k in (EVENT_TOOL_CALL, EVENT_REPLY_END)] == [
        EVENT_TOOL_CALL,
        EVENT_REPLY_END,
        EVENT_TOOL_CALL,
    ]
