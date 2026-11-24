"""The restart receipt: what a gateway start kept, cut and held, and the kept chat's row.

The adopt pass runs against the same stand-ins as ``test_chat_keeper_adopt.py``; what a
real keeper measures across a detach is ``test_chat_keeper_process.py``. Design:
runtime-ownership.md, "The restart receipt".
"""

from __future__ import annotations

import asyncio
import json
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp import client_keeper
from kiro_crew.chat_keeper.transport import AttachState
from kiro_crew.dashboard import restart_receipt

SESSION_KEY = "dashboard:chat-1"


def _state(**over: Any) -> AttachState:
    body = {
        "session_key": SESSION_KEY,
        "keeper_pid": 4242,
        "adapter_pid": 4243,
        "returncode": None,
        "max_client_id": 17,
        "last_ack": 9,
        "seq": 9,
        "gap": False,
        "open_agent_requests": 0,
        **over,
    }
    return AttachState.from_json(body)


def _record() -> Any:
    from kiro_crew.chat_keeper import records

    return records.KeeperRecord(
        instance="abc123def456",
        session_key=SESSION_KEY,
        backend="claude",
        keeper_pid=4242,
        keeper_start="start",
        adapter_pid=4243,
        socket="/x.s",
        wire=1,
        created=1.0,
    )


class _Client:
    def __init__(self, state: AttachState) -> None:
        self._session_key = SESSION_KEY
        self.backend = "claude"
        self._process = SimpleNamespace(state=state, record=_record())
        self._keeper_adopting: Any = None
        self._keeper_adopt_spent = False


def _slot(*, owed: bool, key: str = "chat-1-1") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        display_title="Fix the build",
        running=False,
        _eager_spawn_task=None,
        _prefetch_ttl_task=None,
        _restart_interruption_owed=owed,
        _restored_turn_marker={"generation": 3} if owed else None,
        _queue=[],
    )


@pytest.fixture(autouse=True)
def _fresh(monkeypatch) -> None:
    # The stand-in process stands for a KeeperProcess wherever a module asks.
    monkeypatch.setattr(client_keeper, "keeper_process", lambda client: client._process)
    monkeypatch.setattr(client_keeper, "_HOLDS", {})
    monkeypatch.setattr(restart_receipt, "_RECEIPT", None)


def _adopt(monkeypatch, slot: Any, state: AttachState) -> tuple[list[Any], Any]:
    """Run the pass's adopt of *slot* on a keeper reporting *state*; the events, in order."""
    from kiro_crew.dashboard import chat_keeper_adopt, chat_runner
    from kiro_crew.dashboard import state as dashboard_state

    events: list[Any] = []
    holder = _Client(state)
    client_keeper._HOLDS["abc123def456"] = (weakref.ref(holder), holder._process)
    provider = SimpleNamespace(_client=holder)

    async def _stands_down(*_: Any, **__: Any) -> None:
        pass

    def _append(_state: Any, _slot: Any, role: str, content: str, cls: str = "", **kw: Any) -> None:
        events.append(("row", role, content, kw["meta"]))

    monkeypatch.setattr(chat_runner, "_eager_spawn", _stands_down)
    monkeypatch.setattr(chat_runner, "effective_session_key", lambda slot: SESSION_KEY)
    monkeypatch.setattr(chat_runner, "_watch_agent_turns", lambda *a: events.append("watch"))
    monkeypatch.setattr(dashboard_state, "append_and_surface", _append)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda slot: SESSION_KEY
    )
    gateway = SimpleNamespace(sessions=SimpleNamespace(get_provider=lambda key: provider))
    return events, chat_keeper_adopt._adopt_one(gateway, slot, SimpleNamespace(), _record())


@pytest.mark.asyncio
async def test_a_kept_running_chat_gets_its_row_before_its_turn_is_watched(monkeypatch) -> None:
    restart_receipt.begin()
    slot = _slot(owed=True)
    state = _state(detached_secs=63.4, permission_waits=2, permission_wait_secs=61.2)
    events, adopt = _adopt(monkeypatch, slot, state)
    assert await asyncio.wait_for(adopt, 30) is True
    assert events[-1] == "watch" and len(events) == 2, "one row, then the turn"
    _, role, content, meta = events[0]
    assert role == "notice" and meta["kind"] == restart_receipt.ROW_KIND
    assert meta["notice"] == "restart_kept_running"
    assert meta["notice_args"] == {"down_secs": 63, "waits": 2, "wait_secs": 61}
    assert content == (
        "The app restarted. This chat kept running. The app's own tools were offline "
        "for 63 s. 2 tool calls waited up to 61 s for approval."
    )


@pytest.mark.asyncio
async def test_a_chat_no_approval_waited_in_says_nothing_about_waits(monkeypatch) -> None:
    # The pin off: claude never asks, so the keeper holds no permission request.
    restart_receipt.begin()
    events, adopt = _adopt(monkeypatch, _slot(owed=True), _state(detached_secs=40.0))
    assert await asyncio.wait_for(adopt, 30) is True
    _, _, content, meta = events[0]
    assert meta["notice_args"] == {"down_secs": 40}
    assert "waited" not in content


@pytest.mark.asyncio
async def test_a_turn_the_keeper_saw_running_gets_its_row_when_the_restore_owes_none(
    monkeypatch,
) -> None:
    # The transcript on disk already read as cut (an unanswered user row), so the
    # restore owed no "interrupted" row; the keeper still ran the turn on.
    restart_receipt.begin()
    slot = _slot(owed=False)
    events, adopt = _adopt(monkeypatch, slot, _state(detached_secs=31.0, turn_at_detach=True))
    assert await asyncio.wait_for(adopt, 30) is True
    assert events[0][3]["notice_args"] == {"down_secs": 31}
    record = restart_receipt.build(SimpleNamespace(_slots={slot.key: slot}))
    assert record is not None and record["kept"][0]["running"] is True


@pytest.mark.asyncio
async def test_a_keeper_too_old_to_measure_gives_a_row_with_no_numbers(monkeypatch) -> None:
    restart_receipt.begin()
    events, adopt = _adopt(monkeypatch, _slot(owed=True), _state())
    assert await asyncio.wait_for(adopt, 30) is True
    _, _, content, meta = events[0]
    assert "notice_args" not in meta
    assert content == "The app restarted. This chat kept running."


@pytest.mark.asyncio
async def test_an_idle_kept_chat_is_recorded_without_a_row(monkeypatch) -> None:
    restart_receipt.begin()
    slot = _slot(owed=False)
    events, adopt = _adopt(monkeypatch, slot, _state(detached_secs=12.0))
    assert await asyncio.wait_for(adopt, 30) is True
    assert events == ["watch"]
    record = restart_receipt.build(SimpleNamespace(_slots={slot.key: slot}))
    assert record is not None
    assert record["kept"] == [
        {
            "slot": slot.key,
            "title": "Fix the build",
            "session_key": SESSION_KEY,
            "running": False,
            "via": "adopt",
            "tools_down_secs": 12,
        }
    ]


@pytest.mark.asyncio
async def test_no_receipt_is_noted_outside_a_boot(monkeypatch) -> None:
    events, adopt = _adopt(monkeypatch, _slot(owed=True), _state(detached_secs=5.0))
    assert await asyncio.wait_for(adopt, 30) is True
    assert events == ["watch"]
    assert restart_receipt.build(SimpleNamespace(_slots={})) is None


@pytest.mark.asyncio
async def test_the_receipt_lists_cut_queued_and_ended_chats_and_is_appended(
    monkeypatch, tmp_path: Path
) -> None:
    from kiro_crew.config import paths
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    restart_receipt.begin()
    kept = _slot(owed=True, key="chat-1-1")
    cut = _slot(owed=True, key="chat-2-2")
    held = _slot(owed=False, key="chat-3-3")
    held._queue = [{"id": "q1", RESTORED_QUEUE_KEY: True}, {"id": "q2"}]
    restart_receipt.note_left(kept)
    restart_receipt.note_ended("dashboard:gone", None, "its chat was not restored")
    gateway = SimpleNamespace(_slots={s.key: s for s in (kept, cut, held)})
    record = await restart_receipt.finish(gateway)
    assert record is not None
    assert [k["slot"] for k in record["kept"]] == ["chat-1-1"]
    assert record["kept"][0]["via"] == "another start"
    assert record["cut"] == [{"slot": "chat-2-2", "title": "Fix the build"}]
    assert record["queued"] == [{"slot": "chat-3-3", "title": "Fix the build", "count": 1}]
    assert record["ended"] == [
        {"session_key": "dashboard:gone", "why": "its chat was not restored"}
    ]
    assert record["summary"] == "1 kept (1 running), 1 cut, 1 queued, 1 keeper(s) ended"
    lines = restart_receipt.receipts_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [record]
    assert restart_receipt.build(gateway) is None, "the receipt ends with the boot"


@pytest.mark.parametrize(
    "over, expected",
    [
        ({}, (None, False, 0, 0.0)),
        (
            {
                "detached_secs": 7.5,
                "turn_at_detach": True,
                "permission_waits": 1,
                "permission_wait_secs": 7.0,
            },
            (7.5, True, 1, 7.0),
        ),
        # Without the gap the fields describe, none of them is believed.
        (
            {"detached_secs": True, "turn_at_detach": True, "permission_waits": 3},
            (None, False, 0, 0.0),
        ),
        ({"detached_secs": float("nan"), "permission_wait_secs": 3}, (None, False, 0, 0.0)),
        ({"detached_secs": -1, "permission_waits": 3}, (None, False, 0, 0.0)),
        (
            {
                "detached_secs": 2,
                "turn_at_detach": 1,
                "permission_waits": "3",
                "permission_wait_secs": "x",
            },
            (2.0, False, 0, 0.0),
        ),
    ],
)
def test_the_welcome_gap_report_is_read_leniently(over: dict, expected: tuple) -> None:
    state = _state(**over)
    got = (
        state.detached_secs,
        state.turn_at_detach,
        state.permission_waits,
        state.permission_wait_secs,
    )
    assert got == expected
