"""A session-map row keeps every native session id that left its ``sid``, oldest first."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew import session_map_sid_history as sid_history
from kiro_crew.session_map import SessionMap

KEY = "dashboard:chat-1"
UNTIL = "2026-11-22T10:00:00Z"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(sid_history, "_now", lambda: UNTIL)
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield tmp_path


@pytest.fixture()
def smap(home):
    with patch("kiro_crew.session_map.config_dir", return_value=home):
        yield SessionMap()


def _sids(smap: SessionMap, key: str = KEY) -> list[str]:
    return [item["sid"] for item in smap.sid_history(key)]


def test_a_move_keeps_the_old_sid_with_its_provider(smap) -> None:
    smap.set(KEY, "s1", provider="claude_code")
    smap.set(KEY, "s2", provider="acp")
    assert smap.sid_history(KEY) == [{"sid": "s1", "provider": "claude_code", "until": UNTIL}]
    assert smap.conversation_sids(KEY) == ["s1", "s2"]


def test_writing_the_same_sid_again_records_nothing(smap) -> None:
    smap.set(KEY, "s1")
    smap.set(KEY, "s1", cwd="/tmp/x")
    assert smap.sid_history(KEY) == []
    assert smap.conversation_sids(KEY) == ["s1"]


def test_a_clear_then_a_new_session_keeps_the_cleared_sid(smap) -> None:
    """The context-cap and reset path: ``clear_sid``, then ``set`` from empty."""
    smap.set(KEY, "s1", provider="claude_code")
    assert smap.clear_sid(KEY) is True
    record = smap.sid_record(KEY)
    assert (record["sid"], _sids(smap)) == ("", ["s1"])
    smap.set(KEY, "s2", provider="claude_code")
    smap.clear_sid(KEY)
    smap.set(KEY, "s3", provider="claude_code")
    assert smap.conversation_sids(KEY) == ["s1", "s2", "s3"]
    assert smap.get_discarded_sid(KEY) == "s2"


def test_a_row_from_before_the_field_reads_its_discarded_sid(smap) -> None:
    smap._data[KEY] = {"sid": "s5", "discarded_sid": "s4"}
    assert smap.sid_history(KEY) == [{"sid": "s4", "provider": "", "until": ""}]
    smap.set(KEY, "s6", provider="acp")
    assert _sids(smap) == ["s4", "s5"]


def test_a_sid_already_listed_keeps_its_first_place_and_the_current_one_is_never_listed(
    smap,
) -> None:
    for sid in ("a", "b", "a", "c"):
        smap.set(KEY, sid)
    assert _sids(smap) == ["a", "b"]
    smap.set(KEY, "a")
    assert smap.conversation_sids(KEY) == ["b", "c", "a"]


def test_past_the_cap_the_oldest_is_dropped_and_the_row_says_so(smap) -> None:
    for n in range(sid_history.SID_HISTORY_CAP + 1):
        smap.set(KEY, f"s{n}")
    record = smap.sid_record(KEY)
    assert (len(record["history"]), record["full"]) == (sid_history.SID_HISTORY_CAP, False)
    smap.set(KEY, "last")
    record = smap.sid_record(KEY)
    assert len(record["history"]) == sid_history.SID_HISTORY_CAP
    assert record["history"][0]["sid"] == "s1"
    assert (record["full"], record["cap"]) == (True, sid_history.SID_HISTORY_CAP)


def test_bad_stored_entries_read_as_nothing(smap) -> None:
    smap._data[KEY] = {
        "sid": "cur",
        "sid_history": [
            "loose",
            {"sid": 7},
            {"sid": "x" * 10_000},
            {"sid": "ok", "provider": 3, "until": "y" * 100},
            {"sid": "ok"},
        ],
    }
    assert smap.sid_history(KEY) == [{"sid": "ok", "provider": "", "until": ""}]
    smap._data[KEY]["sid_history"] = "not a list"
    assert smap.sid_history(KEY) == []


def test_an_unknown_key_reads_empty_and_aliases_fold(smap) -> None:
    assert smap.sid_record("dashboard:nope") == {
        "sid": "",
        "provider": "",
        "history": [],
        "cap": sid_history.SID_HISTORY_CAP,
        "full": False,
    }
    smap.set(KEY, "s1")
    smap.set(KEY, "s2")
    assert smap.conversation_sids("dashboard:dashboard_chat-1") == ["s1", "s2"]


def test_the_list_survives_a_restart(home) -> None:
    with patch("kiro_crew.session_map.config_dir", return_value=home):
        first = SessionMap()
        first.set(KEY, "s1", provider="claude_code")
        first.set(KEY, "s2", provider="claude_code")
        first.flush()
        stored = json.loads((home / "session_map.json").read_text(encoding="utf-8"))
        assert stored[KEY]["sid_history"] == [
            {"sid": "s1", "provider": "claude_code", "until": UNTIL}
        ]
        assert SessionMap().conversation_sids(KEY) == ["s1", "s2"]
