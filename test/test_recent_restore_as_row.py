"""The startup recent-session restore builds a person's tab as a sidebar row.

Only the in-memory window waits: the transcript on disk is never touched, and
the window that loads on first read is the one an eager restore builds, so older
history still pages in from disk in full.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.dashboard import needs_you, slot_projection
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.history_schema import HUMAN_TURN_META_KEY
from kiro_crew.testing.clock import ManualClock

ROWS = 600


def _make_state(tmp_path: Path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _write_session(tmp_path: Path, name: str, rows: int, meta: dict | None = None) -> Path:
    path = tmp_path / f"dashboard_{name}.jsonl"
    head = {"_type": "metadata", "created_at": "2026-03-23T10:00:00", "title": name}
    head.update(meta or {})
    lines = [json.dumps(head)]
    for i in range(rows):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(
            json.dumps(
                {
                    "role": role,
                    "content": f"m{i}",
                    "ts": f"2026-03-23T10:{i // 60:02d}:{i % 60:02d}",
                }
            )
        )
    # newline="\n": Windows text mode would write \r\n, which a transcript never holds.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(cp, "live_loop_slot_keys", lambda: set())
    return tmp_path


@pytest.mark.asyncio
async def test_a_recent_person_tab_is_restored_as_a_row_and_loads_in_full(home):
    path = _write_session(home, "long", ROWS)
    # Only the message rows: the restore backfills a tab id into the metadata line.
    before = path.read_bytes().split(b"\n", 1)[1]
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1

    slot = state._slots["long"]
    assert slot.window_pending is True
    assert slot._messages == []
    assert len(slot._lazy_tail) == cp.LAZY_TAIL_ROWS
    assert slot._lazy_tail[-1]["content"] == f"m{ROWS - 1}"
    assert path.read_bytes().split(b"\n", 1)[1] == before, "a row restore touched the rows"

    window = slot.messages
    assert slot.window_pending is False
    assert [m["content"] for m in window] == [f"m{i}" for i in range(ROWS - 500, ROWS)]
    assert slot._disk_older_count == ROWS - 500
    # The rows older than the window are still all on disk, for the older-page read.
    on_disk = state.conversation_log.read_messages_chained("dashboard:long")
    assert [m["content"] for m in on_disk] == [f"m{i}" for i in range(ROWS)]
    assert path.read_bytes().split(b"\n", 1)[1] == before


@pytest.mark.asyncio
async def test_a_tab_a_loop_drives_keeps_its_window(home, monkeypatch):
    monkeypatch.setattr(cp, "live_loop_slot_keys", lambda: {"looped"})
    _write_session(home, "looped", 20)
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1

    slot = state._slots["looped"]
    assert slot.window_pending is False
    assert len(slot._messages) == 20


@pytest.mark.asyncio
async def test_an_unknown_loop_set_keeps_every_window(home, monkeypatch):
    monkeypatch.setattr(cp, "live_loop_slot_keys", lambda: None)
    monkeypatch.setattr(cp, "stored_loop_slot_keys", lambda: None)
    _write_session(home, "unknown", 20)
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1
    assert state._slots["unknown"].window_pending is False


@pytest.mark.asyncio
async def test_a_tab_something_else_drives_keeps_its_window(home):
    _write_session(home, "linked", 20, meta={"linked_session_key": "cron:abc"})
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1
    assert state._slots["linked"].window_pending is False


@pytest.mark.asyncio
async def test_a_foldered_row_saves_back_byte_for_byte_after_it_loads(home):
    path = _write_session(home, "kept", ROWS, meta={"folder_id": "f1"})
    state = _make_state(home)
    await cp.restore_recent_sessions_async(state, window_minutes=0)
    slot = state._slots["kept"]
    assert slot.window_pending is True

    slot.append("user", "new", "msg msg-u", ts="2026-03-24T00:00:00")
    slot.drain()
    cp._save_slot_to_history(state, slot, force=True)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[1:]]
    assert [r["content"] for r in rows] == [f"m{i}" for i in range(ROWS)] + ["new"]


#: The frozen "now" of the needs_you restore test: 2027-01-15T08:00:00Z.
_NOW = 1_800_000_000.0


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "Built.\n\nShip it now, or wait?\n[OPTIONS: Ship it | Wait]",
        "Built.\n\n[Q1a] Ship it now? I would.",
    ],
    ids=["options", "labelled"],
)
async def test_a_row_restored_without_its_window_keeps_what_it_asks_the_owner(
    home, monkeypatch, reply
):
    # ``needs_you`` reads who started the reply's turn (``meta.human``) and the
    # reply's id (``meta.mid``) off the restored tail, so the tail keeps both.
    ManualClock(_NOW).install(monkeypatch, slot_projection)
    rows = [
        {
            "role": "user",
            "content": "Build it, then ask me.",
            "ts": _stamp(_NOW - 120),
            "meta": {HUMAN_TURN_META_KEY: True},
        },
        {"role": "assistant", "content": reply, "ts": _stamp(_NOW - 60), "meta": {"mid": "m-a"}},
    ]
    live = _ChatSlot("asks")
    live.messages.extend(dict(row) for row in rows)
    expected = live.to_dict()["needs_you"]
    assert expected is not None and expected["row_id"] == "m-a"
    head = {"_type": "metadata", "created_at": _stamp(_NOW - 900), "title": "asks"}
    (home / "dashboard_asks.jsonl").write_text(
        "\n".join(json.dumps(line) for line in (head, *rows)) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1

    slot = state._slots["asks"]
    assert slot.window_pending is True
    assert slot.to_dict()["needs_you"] == expected
    assert slot.window_pending is True, "the row read its window to answer"


#: Tool calls in the long turn below: a step line and a tool row each, so the
#: person's message is older than the restored tail and inside the rows
#: ``needs_you`` looks back over.
_LONG_TURN_CALLS = 80


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("starter", "asks"),
    [
        ({"content": "Check every file, then ask me.", "meta": {HUMAN_TURN_META_KEY: True}}, True),
        # Another session's send began the turn: the person's older message
        # must not be the row kept in front.
        ({"content": "[From chat-2] Check every file."}, False),
    ],
    ids=["a-person", "another-session"],
)
async def test_a_long_turn_restored_without_its_window_keeps_who_started_it(
    home, monkeypatch, starter, asks
):
    ManualClock(_NOW).install(monkeypatch, slot_projection)
    rows = [
        {
            "role": "user",
            "content": "Earlier request.",
            "ts": _stamp(_NOW - 1000),
            "meta": {HUMAN_TURN_META_KEY: True},
        },
        {"role": "assistant", "content": "Done.", "ts": _stamp(_NOW - 990)},
        {"role": "user", "ts": _stamp(_NOW - 900), **starter},
    ]
    for call in range(_LONG_TURN_CALLS):
        at = _stamp(_NOW - 800 + call)
        rows.append({"role": "assistant", "content": f"Reading file {call}.", "ts": at})
        rows.append({"role": "tool", "content": "🔧 Read", "ts": at})
    reply = "All files read.\n\n[Q1a] Fix the three that fail? I would."
    rows.append(
        {"role": "assistant", "content": reply, "ts": _stamp(_NOW - 60), "meta": {"mid": "m-a"}}
    )
    assert cp.LAZY_TAIL_ROWS < len(rows) - 3 < needs_you.STARTER_SCAN_ROWS
    live = _ChatSlot("long-turn")
    live.messages.extend(dict(row) for row in rows)
    expected = live.to_dict()
    assert (expected["needs_you"] is not None) is asks
    head = {"_type": "metadata", "created_at": _stamp(_NOW - 2000), "title": "long-turn"}
    (home / "dashboard_long-turn.jsonl").write_text(
        "\n".join(json.dumps(line) for line in (head, *rows)) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1

    slot = state._slots["long-turn"]
    assert slot.window_pending is True
    restored = slot.to_dict()
    for field in ("needs_you", "last_message", "interrupted", "waiting_for_input"):
        assert restored[field] == expected[field], field
    assert slot.window_pending is True, "the row read its window to answer"
