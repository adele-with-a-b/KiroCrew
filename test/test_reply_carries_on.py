"""A reply a gateway exit cut while its chat keeper kept the turn running.

The partial reply the dying turn saves carries ``meta.carries_on``, the dashboard's
cue to draw the adopted turn's rest in the same bubble (``carriedOnReply.ts``). A
turn whose process simply died carries no such marker, so its reply and a later
agent-started turn's stay two bubbles.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp import client_keeper
from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_turn.agent_started import REPLY_CARRIES_ON_META_KEY


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _state_and_slot(tmp_path: Path):
    from kiro_crew.acp.client import AcpProcessDied

    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = "acp-carry-0001"
    client.client._session_id = "acp-carry-0001"
    client.shutdown = AsyncMock()

    async def _stream(*_a, **_kw):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="For landscapes and nature,")
        raise AcpProcessDied("detached at the gateway exit")

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("carry-slot")
    slot.append("user", "compare the film stocks", "msg msg-u")
    return state, slot, client


def _cut_rows(slot) -> list[dict]:
    return [
        m
        for m in slot.messages
        if m.get("role") == "assistant" and m.get("content") == "For landscapes and nature,"
    ]


def _error_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "error"]


@pytest.mark.asyncio
async def test_a_reply_cut_while_its_keeper_kept_the_turn_says_it_carries_on(tmp_path, monkeypatch):
    state, slot, client = _state_and_slot(tmp_path)
    monkeypatch.setattr(client_keeper, "is_detached", lambda c: c is client._client)

    await _run_chat(state, slot, "compare the film stocks")

    (row,) = _cut_rows(slot)
    assert (row.get("meta") or {}).get(REPLY_CARRIES_ON_META_KEY) is True
    assert _error_rows(slot) == [], "a kept turn is not a lost connection"
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reply_whose_process_died_does_not_carry_on(tmp_path, monkeypatch):
    state, slot, client = _state_and_slot(tmp_path)
    monkeypatch.setattr(client_keeper, "is_detached", lambda c: False)

    await _run_chat(state, slot, "compare the film stocks")

    rows = _cut_rows(slot)
    assert rows, "the control: the partial reply was saved"
    assert _error_rows(slot), "the control: a dead process is announced"
    assert all(REPLY_CARRIES_ON_META_KEY not in (r.get("meta") or {}) for r in rows)
