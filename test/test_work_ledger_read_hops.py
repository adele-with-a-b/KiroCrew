"""``GET /api/work-ledger``: a read's executor hops do not grow with the board.

The handler waits for each ``asyncio.to_thread`` hop on the gateway's loop, so a
read that took one hop per item was as slow as the board is large times the loop is
busy. Pinned here: one item and many cost the same hops, full and compact, with
remote-bound rows on the board; and every row still carries what the store's own
per-item readers give for it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.dashboard.handlers import work_ledger_read

CONDUCTOR = "chat-a-conductor"
CREW = "crew-lab"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture(autouse=True)
def _open_route(monkeypatch):
    """Session recognition, restriction and the channel probe have their own suites."""

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)


def _req(query: str = "") -> web.Request:
    app = web.Application()
    state = MagicMock()
    # No slot is open: a mock would answer every lookup with a truthy object.
    state.get_slot = MagicMock(side_effect=lambda key: None)
    app["state"] = state
    path = "/api/work-ledger" + (f"?{query}" if query else "")
    req = make_mocked_request("GET", path, app=app, headers={"X-Session-Key": CONDUCTOR})
    req["internal_auth"] = True
    return req


async def _read(query: str = "") -> dict[str, Any]:
    resp = await routes.api_work_ledger_get(_req(query))
    body = json.loads(resp.text)
    assert resp.status == 200, body
    return body


def _create(n: int, *, remote: bool) -> str:
    """One reported item, bound to a worker here or on a crew."""
    wl.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    item_id = wl.apply_conductor_action(
        CONDUCTOR, "create", title=f"item {n}", acceptance={"kind": "human_approval"}
    )["item"].item_id
    worker = f"chat-worker-{n}"
    if remote:
        wl.apply_remote_bind(CONDUCTOR, item_id, worker_session_key=worker, worker_instance_id=CREW)
    else:
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=worker)
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary=f"report of item {n}")
    return item_id


@pytest.fixture
def hops(monkeypatch) -> list[object]:
    """Every executor hop awaited, by its target."""
    seen: list[object] = []
    real = asyncio.to_thread

    async def counting(func, /, *args, **kwargs):
        seen.append(func)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counting)
    return seen


async def _hops_of_a_read(hops: list[object], query: str) -> tuple[int, dict[str, Any]]:
    # Counted on a second read of the same board, so a once-only load on a first
    # read is never taken for a per-item cost.
    await _read(query)
    hops.clear()
    body = await _read(query)
    return len(hops), body


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "compact=true"])
async def test_a_read_takes_the_same_executor_hops_for_one_item_as_for_many(hops, query):
    """Six items, half of them bound to a worker on a crew, cost the hops one does."""
    _create(0, remote=True)
    one_item, _ = await _hops_of_a_read(hops, query)
    for n in range(1, 6):
        _create(n, remote=n % 2 == 0)
    six_items, body = await _hops_of_a_read(hops, query)
    assert len(body["items"]) == 6
    assert six_items == one_item


@pytest.mark.asyncio
async def test_each_row_carries_what_the_per_item_readers_give(hops):
    """Read together, each row is still its own item's: its event tail, its remote
    fields and its undelivered-call record are the ones a read of that item alone
    returns, and the header's depth cap is the one in force."""
    ids = [_create(n, remote=n % 2 == 0) for n in range(4)]
    wl.apply_worker_report(CONDUCTOR, ids[1], status="done", summary="finished item 1")
    body = await _read()
    assert [row["item_id"] for row in body["items"]] == ids
    assert body["conductor"]["max_depth"] == wl.max_depth()
    for row in body["items"]:
        item = wl.read_work_item(CONDUCTOR, row["item_id"])
        assert item is not None
        tail = wl.read_events(CONDUCTOR, item.item_id, limit=routes._MAX_EVENT_TAIL)
        assert row["events"] == [event.to_dict() for event in tail]
        assert row["report_undelivered"] == wl.read_undelivered(CONDUCTOR, item)
        remote = work_ledger_read._remote_row_fields(CONDUCTOR, item)
        assert bool(remote) == bool(item.worker_instance_id)
        assert {name: row[name] for name in remote} == remote
    compact = await _read("compact=true")
    for full_row, row in zip(body["items"], compact["items"]):
        assert "events" not in row
        assert row == {name: full_row[name] for name in row}


@pytest.mark.asyncio
async def test_a_read_of_one_item_reads_no_other_items_event_tail(monkeypatch):
    ids = [_create(n, remote=False) for n in range(3)]
    tails: list[str] = []
    real = work_ledger_read._tail_events

    def recording(key: str, item_id: str, limit: int):
        tails.append(item_id)
        return real(key, item_id, limit)

    monkeypatch.setattr(work_ledger_read, "_tail_events", recording)
    body = await _read(f"item_id={ids[1]}")
    assert [row["item_id"] for row in body["items"]] == [ids[1]]
    assert tails == [ids[1]]
    # The bars still come from the whole board: one item's evaluation reads them here.
    assert body["accept_batch"] == wl.accept_batch(wl.list_work_items(CONDUCTOR))
