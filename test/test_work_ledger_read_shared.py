"""``GET /api/work-ledger``: reads with the same arguments share one build.

The tool behind the route gives up after ten seconds and its caller asks again, so the
same read arrives while one is still being built. Pinned here: reads that arrive during
a build share the ONE next build, which started after each of them arrived (it shows a
write made while the first build was held); other arguments get a build of their own;
every read still makes its own remote pull and writes its own audit line; a refusal is
never handed to a read it was not built for; nothing is kept after a build.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.handlers import crew_board_flight as flight
from kiro_crew.dashboard.handlers import remote_ingest, remote_puller
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.dashboard.handlers import work_ledger_read

CONDUCTOR = "chat-a-conductor"

#: Longest any signal or task in these tests may take before the test fails instead of hanging.
_BOUND_S = 60.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield
    assert flight._waiting == {} and flight._running == {}, "a build was left behind"


@pytest.fixture(autouse=True)
def _open_route(monkeypatch):
    """Session recognition, restriction and the channel probe have their own suites."""

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)


class _Arrivals:
    """Reads that made their remote pull, the last thing a read does before its build.

    The pull returns without yielding, so a read counted here has asked for its build
    (or queued for the board) by the time a waiter on :meth:`reached` runs again.
    """

    def __init__(self) -> None:
        self.count = 0
        self._changed = asyncio.Event()

    async def pull(self, state: Any, key: str) -> None:
        self.count += 1
        self._changed.set()

    async def reached(self, count: int) -> None:
        while self.count < count:
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), _BOUND_S)


@pytest.fixture
def arrivals(monkeypatch) -> _Arrivals:
    """Remote dispatch on, with the pull replaced by a counter."""
    seen = _Arrivals()
    monkeypatch.setattr(work_ledger_read, "_remote_dispatch_on", lambda: True)
    monkeypatch.setattr(remote_puller, "ensure_running", lambda state: None)
    monkeypatch.setattr(remote_ingest, "ingest_board", seen.pull)
    return seen


class _Builds:
    """Every listing of the board's items by a read: one per build. The first can be held.

    A read lists in an executor thread; the store's own listings, made by the writes a
    test does on the loop's thread, are not counted.
    """

    def __init__(self) -> None:
        self.count = 0
        #: The loop to tell when the first listing is held; set to hold it.
        self.hold_first: asyncio.AbstractEventLoop | None = None
        self.first_listed = asyncio.Event()
        self.release = threading.Event()
        self._real = wl.list_work_items
        self._loop_thread = threading.get_ident()

    def list_work_items(self, key: str) -> list[Any]:
        items = self._real(key)
        if threading.get_ident() == self._loop_thread:
            return items
        self.count += 1
        if self.hold_first is not None and self.count == 1:
            # Held AFTER the listing: what this build answers is already fixed.
            self.hold_first.call_soon_threadsafe(self.first_listed.set)
            assert self.release.wait(_BOUND_S), "the held build was never released"
        return items


@pytest.fixture
def builds(monkeypatch):
    counter = _Builds()
    monkeypatch.setattr(wl, "list_work_items", counter.list_work_items)
    yield counter
    counter.release.set()


@pytest.fixture
def audits(monkeypatch) -> list[tuple[str, str]]:
    """Each read's own audit line: (outcome, resources)."""
    lines: list[tuple[str, str]] = []

    def record(caller: str, operation: str, outcome: str, *, resources: str = "") -> None:
        lines.append((outcome, resources))

    monkeypatch.setattr(work_ledger_read, "_audit", record)
    return lines


def _item(title: str) -> str:
    wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    made = wl.apply_conductor_action(
        CONDUCTOR, "create", title=title, acceptance={"kind": "human_approval"}
    )
    return made["item"].item_id


def _read(query: str = "") -> asyncio.Future[web.Response]:
    """One read of the conductor's board, started."""
    app = web.Application()
    state = MagicMock()
    # No slot is open: a mock would answer every lookup with a truthy object.
    state.get_slot = MagicMock(side_effect=lambda key: None)
    app["state"] = state
    path = "/api/work-ledger" + (f"?{query}" if query else "")
    request = make_mocked_request("GET", path, app=app, headers={"X-Session-Key": CONDUCTOR})
    request["internal_auth"] = True
    return asyncio.ensure_future(routes.api_work_ledger_get(request))


async def _answers(*reads: asyncio.Future[web.Response]) -> list[web.Response]:
    return list(await asyncio.wait_for(asyncio.gather(*reads), _BOUND_S))


def _titles(response: web.Response) -> list[str]:
    """The answer's item titles, sorted: items made within one second list in id order."""
    assert response.status == 200, response.text
    return sorted(row["title"] for row in json.loads(response.text)["items"])


async def _held_read(builds: _Builds, arrivals: _Arrivals) -> asyncio.Future[web.Response]:
    """A read whose build has listed the board and is held there."""
    builds.hold_first = asyncio.get_running_loop()
    first = _read()
    await asyncio.wait_for(builds.first_listed.wait(), _BOUND_S)
    assert arrivals.count == 1
    return first


@pytest.mark.asyncio
async def test_reads_that_arrive_during_a_build_share_the_one_next_build(builds, arrivals, audits):
    _item("there before the first read")
    first = await _held_read(builds, arrivals)
    _item("written while the first build was held")
    later = [_read() for _ in range(3)]
    await arrivals.reached(4)
    builds.release.set()
    old, *new = await _answers(first, *later)
    assert builds.count == 2
    # The held build had listed the board already; the shared one began after every
    # later read arrived, so each of them sees the write.
    assert _titles(old) == ["there before the first read"]
    for response in new:
        assert _titles(response) == [
            "there before the first read",
            "written while the first build was held",
        ]
    assert len({response.body for response in new}) == 1
    assert {response.content_type for response in (old, *new)} == {"application/json"}
    assert {response.charset for response in (old, *new)} == {"utf-8"}
    # Its own remote pull and its own audit line, for every read.
    assert arrivals.count == 4
    assert sorted(audits) == [("ok", "1 item(s)")] + [("ok", "2 item(s)")] * 3


@pytest.mark.asyncio
async def test_other_arguments_get_a_build_of_their_own(builds, arrivals):
    item_id = _item("one")
    _item("two")
    first = await _held_read(builds, arrivals)
    full = [_read(), _read()]
    compact = _read("compact=true")
    one_item = _read(f"item_id={item_id}")
    await arrivals.reached(5)
    builds.release.set()
    _held, full_a, full_b, compact_answer, one_answer = await _answers(
        first, *full, compact, one_item
    )
    # The held build, one the two whole-board reads share, and one each for the others.
    assert builds.count == 4
    assert full_a.body == full_b.body
    assert "compact" not in json.loads(full_a.text)
    assert json.loads(compact_answer.text)["compact"] is True
    assert _titles(full_a) == _titles(compact_answer) == ["one", "two"]
    assert _titles(one_answer) == ["one"]


@pytest.mark.asyncio
async def test_a_refusal_is_not_handed_to_a_read_it_was_not_built_for(
    builds, arrivals, audits, monkeypatch
):
    """The second build is refused: the read that started it gets the refusal, and the
    one that had joined it builds again for itself."""
    _item("one")
    checks: list[int] = []

    async def dirty_on_the_second(*a: Any) -> web.Response | None:
        checks.append(1)
        if len(checks) != 2:
            return None
        return web.json_response({"error": "rebuild first", "code": "cache_dirty"}, status=409)

    monkeypatch.setattr(work_ledger_read, "_refuse_if_dirty", dirty_on_the_second)
    first = await _held_read(builds, arrivals)
    starter = _read()
    await arrivals.reached(2)
    joiner = _read()
    await arrivals.reached(3)
    builds.release.set()
    held, refused, rebuilt = await _answers(first, starter, joiner)
    assert held.status == 200
    assert refused.status == 409
    assert json.loads(refused.text)["code"] == "cache_dirty"
    assert _titles(rebuilt) == ["one"]
    assert len(checks) == 3
    assert audits == [("ok", "1 item(s)")] * 2


@pytest.mark.asyncio
async def test_nothing_is_kept_after_a_build(builds, arrivals):
    _item("one")
    assert _titles((await _answers(_read()))[0]) == ["one"]
    _item("two")
    assert _titles((await _answers(_read()))[0]) == ["one", "two"]
    assert builds.count == 2
