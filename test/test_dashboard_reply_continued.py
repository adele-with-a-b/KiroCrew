"""A reply the model ended on keeps its own row when the turn goes on with text.

The shape a Claude Code Stop hook makes: the agent answers and ends its turn,
the hook blocks the stop (claude-agent-acp sends no frame for it), and the agent
writes more text in a new model message with no tool call between. The stream
shows only ``reply_end`` between the two, so without a cut at that mark both
land in ONE row and the reader cannot see where the first reply ended.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.acp.types import EVENT_REPLY_END, EVENT_THINKING_CHUNK
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent

ANSWER = "The job runs at 09:00 local time."
MORE = "Checked, and it holds: config.json sets the same zone."


async def _rows(tmp_path, monkeypatch, events, roles=("assistant", "tool")):
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

    await _run_chat(state, slot, "When does the job run?")
    return [m for m in slot.messages if m.get("role") in roles]


@pytest.mark.asyncio
async def test_text_after_a_reply_end_starts_its_own_row(tmp_path, monkeypatch):
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            LLMEvent(kind=EVENT_REPLY_END),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=MORE),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
    )
    assert [(m["role"], m["content"]) for m in rows] == [("assistant", ANSWER), ("assistant", MORE)]
    assert rows[0].get("meta", {}).get("reply_end") is True
    assert "reply_end" not in rows[1].get("meta", {})


@pytest.mark.asyncio
async def test_without_the_mark_the_text_stays_one_row(tmp_path, monkeypatch):
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=MORE),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
    )
    assert [m["content"] for m in rows] == [ANSWER + MORE]


@pytest.mark.asyncio
async def test_a_tool_call_after_the_mark_cuts_once(tmp_path, monkeypatch):
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            LLMEvent(kind=EVENT_REPLY_END),
            LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=MORE),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
    )
    assert [m["role"] for m in rows] == ["assistant", "tool", "assistant"]
    assert rows[0]["content"] == ANSWER and rows[0].get("meta", {}).get("reply_end") is True
    assert rows[2]["content"] == MORE and "reply_end" not in rows[2].get("meta", {})


@pytest.mark.asyncio
async def test_thinking_before_the_next_text_still_cuts_at_the_mark(tmp_path, monkeypatch):
    # The shape the claude-agent-acp probe saw: the next message opens with a
    # thought before its text.
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            LLMEvent(kind=EVENT_REPLY_END),
            LLMEvent(kind=EVENT_THINKING_CHUNK, text="The hook wants one more line."),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=MORE),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
    )
    assert [(m["role"], m["content"]) for m in rows] == [("assistant", ANSWER), ("assistant", MORE)]
    assert rows[0].get("meta", {}).get("reply_end") is True


@pytest.mark.asyncio
async def test_a_reply_ending_in_an_open_fence_is_never_cut_inside_the_next(tmp_path, monkeypatch):
    # Decided once, at the mark: a later chunk that closes the fence must not
    # cut the next message's text in two.
    fenced = "Run this:\n```sh\ndate -u\n"
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=fenced),
            LLMEvent(kind=EVENT_REPLY_END),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="```\n"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=MORE),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
    )
    assert [m["content"] for m in rows] == [fenced + "```\n" + MORE]


@pytest.mark.asyncio
async def test_a_mark_at_the_turns_end_leaves_the_reply_as_its_final_text(tmp_path, monkeypatch):
    # claude-agent-acp 0.109.0 also releases the mark with the turn's closing
    # usage reading. Nothing follows, so nothing is cut: a cut there emptied the
    # turn's final text and the empty-turn guard sent a recovery prompt.
    rows = await _rows(
        tmp_path,
        monkeypatch,
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=ANSWER),
            LLMEvent(kind=EVENT_REPLY_END),
            LLMEvent(kind=EVENT_COMPLETE),
        ],
        roles=("assistant", "tool", "notice", "inject"),
    )
    assert [(m["role"], m["content"]) for m in rows] == [("assistant", ANSWER)]
