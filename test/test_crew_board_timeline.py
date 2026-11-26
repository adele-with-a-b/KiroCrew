"""The Crew board's Timeline read (``GET /api/crew-board/timeline``).

The board read sends each item's newest 20 events; the Timeline needs every line of
the window it draws, each with its time, and never a line's text (a ``bind`` line's
text is the worker's session key). The clocks are pinned: the store's stamp and the
route's own read time.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import work_ledger as wl
from kiro_crew import work_ledger_store, work_ledger_writes
from kiro_crew.dashboard.handlers import crew_board_timeline as timeline

CONDUCTOR = "chat-1-conductor"
OWNER = "owner-subject"
WORKER = "chat-2-worker-secret-key"
NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


class _Clock:
    """The store's stamp, moved by hand: each write lands at ``at``."""

    def __init__(self) -> None:
        self.at = NOW - timedelta(hours=30)

    def stamp(self) -> str:
        return self.at.isoformat(timespec="seconds")


@pytest.fixture
def clock(monkeypatch) -> Iterator[_Clock]:
    c = _Clock()
    # Each module's own binding: the store stamps events, the writes stamp closed_at.
    monkeypatch.setattr(work_ledger_store, "_now_iso", c.stamp)
    monkeypatch.setattr(work_ledger_writes, "_now_iso", c.stamp)
    monkeypatch.setattr(timeline, "_now", lambda: NOW)
    yield c


def _state():
    return SimpleNamespace(_slots={}, get_slot=lambda _key: None, owner_id=OWNER)


async def _call(query: str, user: str = OWNER):
    request = make_mocked_request(
        "GET", f"/api/crew-board/timeline?{query}", app={"state": _state()}
    )
    request["user"] = user
    request["app"] = ""
    response = await timeline.api_crew_board_timeline(request)
    return json.loads(response.body.decode()), response


def _item(clock: _Clock, title: str, *, hours_ago: float) -> str:
    clock.at = NOW - timedelta(hours=hours_ago)
    wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    return wl.apply_conductor_action(
        CONDUCTOR, "create", title=title, acceptance={"kind": "human_approval"}
    )["item"].item_id


def _at(clock: _Clock, hours_ago: float) -> None:
    clock.at = NOW - timedelta(hours=hours_ago)


@pytest.mark.asyncio
async def test_every_line_of_the_window_with_its_time_and_never_its_text(clock):
    item_id = _item(clock, "port the gate", hours_ago=11)
    _at(clock, 10.5)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    # More lines than the board read's 20-event tail.
    for n in range(24):
        _at(clock, 10 - n * 0.25)
        wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision=f"step {n}")
    _at(clock, 3)
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="opened PR 7")
    _at(clock, 2)
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pending")

    body, response = await _call(f"conductor={CONDUCTOR}")
    assert response.status == 200
    assert body["hours"] == 12
    assert body["read_at"] == NOW.isoformat(timespec="seconds")
    assert body["since"] == (NOW - timedelta(hours=12)).isoformat(timespec="seconds")
    lines = body["events"][item_id]
    kinds = [line["kind"] for line in lines]
    assert kinds == ["create", "bind", *["decision"] * 24, "report", "verdict"]
    stamps = [datetime.fromisoformat(line["ts"]) for line in lines]
    assert stamps == sorted(stamps)
    assert lines[-2]["status"] == "done"
    assert lines[-1]["verdict"] == "pending"
    raw = response.body.decode()
    # No line's text reaches the page: not the worker's key, its summary or a decision.
    assert WORKER not in raw
    assert "opened PR 7" not in raw
    assert "step 3" not in raw
    assert all("text" not in line for line in lines)


@pytest.mark.asyncio
async def test_the_window_keeps_one_line_from_before_it_and_drops_closed_items(clock):
    early = _item(clock, "long running", hours_ago=20)
    _at(clock, 18)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=early, worker_session_key=WORKER)
    _at(clock, 8)
    wl.apply_worker_report(CONDUCTOR, early, status="question", summary="which?")
    gone = _item(clock, "finished yesterday", hours_ago=29)
    _at(clock, 26)
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=gone, state="abandoned")

    body, _ = await _call(f"conductor={CONDUCTOR}&hours=6")
    # Nothing happened to it in the last 6 hours: its newest earlier line says
    # what it was doing when the window opened.
    assert [(line["kind"], line["status"]) for line in body["events"][early]] == [
        ("report", "question")
    ]
    assert gone not in body["events"]

    body, _ = await _call(f"conductor={CONDUCTOR}&hours=24")
    assert [line["kind"] for line in body["events"][early]] == ["create", "bind", "report"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", ["0", "48", "12.5", "-6", "six"])
async def test_only_the_offered_windows_are_read(clock, hours):
    _item(clock, "any", hours_ago=1)
    body, response = await _call(f"conductor={CONDUCTOR}&hours={hours}")
    assert response.status == 400
    assert body["code"] == "invalid_hours"


@pytest.mark.asyncio
async def test_refusals_carry_a_code(clock):
    body, response = await _call("")
    assert (response.status, body["code"]) == (400, "missing_conductor")
    body, response = await _call("conductor=chat-9-never-a-conductor")
    assert (response.status, body["code"]) == (404, "no_ledger")
    _item(clock, "any", hours_ago=1)
    body, response = await _call(f"conductor={CONDUCTOR}", user="someone-else")
    assert response.status == 403
    assert body["code"] == "owner_only"
