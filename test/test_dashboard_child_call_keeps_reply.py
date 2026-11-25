"""A native sub-agent's step never cuts the parent's open reply.

The shape a background helper makes: the parent turn ran its own tools and wrote
its closing reply while a child it started earlier (an Agent run in the
background) went on calling tools and asking for approval. Each child call and
each child request reaches the turn's tool path after its activity event. A cut
there would leave the turn's final segment empty, so the empty-response guard
would read an answered turn as unanswered, post "The turn ended without a closing
reply" and queue a continuation whose reply takes the answer's place as the
transcript block's conclusion.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from dashboard_approval_helpers import _answer_approval, _context_builder, _drain
from dashboard_approval_helpers import _make_state as _make_approval_state
from dashboard_approval_helpers import _patch_stats, _set_stream
from dashboard_chat_helpers import _provider_mock

from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    LLMEvent,
)

ANSWER = "PR 64 is open and CI is green. Should I merge it now, or wait for the review?"
LATER = " The release notes are drafted too."
PROMPT = "Open the release PR and tell me when CI is green."


_REPLY_ROLES = ("assistant", "notice", "inject")


def _reply_rows(slot, roles=_REPLY_ROLES) -> list[tuple[str, str]]:
    return [(m["role"], m["content"]) for m in slot.messages if m.get("role") in roles]


async def _rows(tmp_path, monkeypatch, events, roles=_REPLY_ROLES):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    slot = state.get_or_create_slot("s1")
    client = _provider_mock()
    client.context_usage_pct = MagicMock(return_value=10.0)

    async def _stream(msg):
        for ev in events:
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    from kiro_crew.dashboard.chat import _run_chat

    await _run_chat(state, slot, PROMPT)
    return _reply_rows(slot, roles)


def _own_call(tcid: str) -> list[LLMEvent]:
    return [
        LLMEvent(
            kind=EVENT_TOOL_CALL, title="gh pr create", tool_kind="execute", tool_call_id=tcid
        ),
        LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id=tcid, tool_output="ok", tool_final=True),
    ]


def _child_call(tcid: str) -> list[LLMEvent]:
    return [
        LLMEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id="child-1", tool_call_id=tcid),
        LLMEvent(kind=EVENT_TOOL_CALL, title="Bash", tool_kind="execute", tool_call_id=tcid),
    ]


@pytest.mark.asyncio
async def test_a_child_call_after_the_reply_leaves_it_the_turns_final_text(tmp_path, monkeypatch):
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            *_own_call("own-1"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            *_child_call("child-call-1"),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
    )
    assert rows == [("assistant", ANSWER)]


@pytest.mark.asyncio
async def test_a_child_call_inside_the_reply_keeps_it_one_row(tmp_path, monkeypatch):
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            *_own_call("own-1"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            *_child_call("child-call-1"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=LATER),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
    )
    assert rows == [("assistant", ANSWER + LATER)]


@pytest.mark.asyncio
async def test_the_saved_child_row_sits_above_the_reply_it_did_not_cut(tmp_path, monkeypatch):
    # The reply flushes at the turn's end, so the child's row, appended when it
    # arrived, is saved above it: a reload shows the child's step first.
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            *_own_call("own-1"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            *_child_call("child-call-1"),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
        roles=("tool", "assistant"),
    )
    assert [role for role, _ in rows] == ["tool", "tool", "assistant"]
    assert "Bash" in rows[1][1]
    assert rows[2] == ("assistant", ANSWER)


@pytest.mark.asyncio
async def test_the_parents_own_call_after_a_child_call_still_cuts_there(tmp_path, monkeypatch):
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            *_child_call("child-call-1"),
            *_own_call("own-3"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=LATER.strip()),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
        roles=("tool", "assistant"),
    )
    assert [role for role, _ in rows] == ["tool", "assistant", "tool", "assistant"]
    assert rows[1] == ("assistant", ANSWER)
    assert rows[3] == ("assistant", LATER.strip())


_CHILD_ASKS = {
    # A claude or kiro-cli child: its activity event names the call first.
    "named-by-activity": [
        LLMEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id="child-1", tool_call_id="child-ask"),
        LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id="req-1",
            title="fs_write",
            tool_kind="edit",
            tool_call_id="child-ask",
        ),
    ],
    # A KAS child under the parent's own session id, naming no call: the
    # transport's mark is the only sign.
    "transport-marked": [
        LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id="req-1",
            title="fs_write",
            tool_kind="edit",
            native_child_call=True,
            child_calls_marked=True,
        ),
    ],
}


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize("ask", sorted(_CHILD_ASKS))
async def test_a_childs_answered_request_after_the_reply_leaves_it_the_final_text(tmp_path, ask):
    # The approval card's row is where a parent's own request pays the cut it
    # owes; a child's request owes none.
    state, client = _make_approval_state(tmp_path, context_builder=_context_builder())
    slot = _ChatSlot("chat-1-test")
    _set_stream(
        client,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            *_CHILD_ASKS[ask],
            LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ],
    )

    from kiro_crew.dashboard.chat import _run_chat

    approver = asyncio.get_running_loop().create_task(_answer_approval(slot, "req-1", "approved"))
    with _patch_stats():
        await _run_chat(state, slot, PROMPT)
    await _drain(approver)

    assert any(m.get("role") == "permission" for m in slot.messages)
    client.approve_tool.assert_called_once()
    assert _reply_rows(slot) == [("assistant", ANSWER)]


@pytest.mark.asyncio
async def test_the_parents_own_call_after_its_text_still_cuts_and_counts_as_no_reply(
    tmp_path, monkeypatch
):
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            *_own_call("own-2"),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
    )
    assert rows[0] == ("assistant", ANSWER)
    assert any(role == "notice" for role, _ in rows[1:])
