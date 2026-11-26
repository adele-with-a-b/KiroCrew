"""``GET /api/crew-board/capacity``: this computer's room for the all-projects Board.

The page counts worker chats itself from the boards; this route answers only the
probe behind ``resource_status`` and the name a local story's ``runs_on`` carries.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import resource_status
from kiro_crew.dashboard.handlers import crew_board_capacity as capacity
from kiro_crew.relay_client import lease

OWNER = "owner-subject"


def _status(available_gb: float, posture: str, pressure_level: int | None = None):
    return resource_status.ResourceStatus(
        available_gb=available_gb,
        cpu_count=8,
        load_per_cpu=0.1,
        posture=posture,
        pressure_gb=4.0,
        critical_gb=2.0,
        memory_pressure_level=pressure_level,
    )


async def _get(monkeypatch, status, *, user: str = OWNER, app: str = ""):
    calls: list[int] = []

    def probe(cfg=None):
        calls.append(1)
        return status

    monkeypatch.setattr(resource_status, "probe", probe)
    monkeypatch.setattr(lease, "default_display_name", lambda: "this-computer")
    request = make_mocked_request(
        "GET", "/api/crew-board/capacity", app={"state": SimpleNamespace(owner_id=OWNER)}
    )
    request["user"] = user
    request["app"] = app
    response = await capacity.api_crew_board_capacity(request)
    return response, calls


@pytest.mark.asyncio
async def test_the_owner_reads_the_probe_and_this_computers_name(monkeypatch):
    response, calls = await _get(monkeypatch, _status(10.34, resource_status.POSTURE_AMPLE))
    assert response.status == 200
    assert json.loads(response.body.decode()) == {
        "name": "this-computer",
        "available_gb": 10.3,
        "posture": "ample",
        "tight_gb": 4.0,
        "critical_gb": 2.0,
        "pressure_held": False,
        "crews": [],
    }
    assert calls == [1]


class _State:
    """A gateway state the patrol's readings can be keyed by (weakly referenceable)."""

    owner_id = OWNER


def test_a_crews_room_is_the_patrols_last_fresh_reading_by_its_runs_on_name(monkeypatch):
    from kiro_crew.dashboard import patrol_seats
    from kiro_crew.dashboard.handlers import work_ledger_read

    state = _State()
    monkeypatch.setattr(patrol_seats, "_clock", lambda: 1000.0)
    monkeypatch.setitem(
        patrol_seats._READINGS,
        state,
        {
            "inst-b": ("", 990.0),
            "inst-a": ("memory", 995.0),
            "inst-old": ("", 1000.0 - patrol_seats.ROOM_FRESH_SECS - 1),
        },
    )
    monkeypatch.setattr(work_ledger_read, "_crew_names", lambda _s: {"inst-b": "demo-crew"})
    assert capacity.crew_rooms(state) == [
        {"name": "demo-crew", "room": ""},
        {"name": "inst-a", "room": "memory"},
    ]
    # Read only: asking again starts no crew read and stores nothing.
    assert patrol_seats._REFRESHING.get(state) is None
    assert len(patrol_seats._READINGS[state]) == 3


def test_no_patrol_reading_names_no_crew(monkeypatch):
    from kiro_crew.dashboard.handlers import work_ledger_read

    def names(_s):
        raise AssertionError("the registry is not read with nothing to name")

    monkeypatch.setattr(work_ledger_read, "_crew_names", names)
    assert capacity.crew_rooms(_State()) == []
    assert capacity.crew_rooms(SimpleNamespace(owner_id=OWNER)) == []


@pytest.mark.asyncio
async def test_unmeasured_memory_is_null_not_zero(monkeypatch):
    response, _calls = await _get(monkeypatch, _status(-1.0, resource_status.POSTURE_UNKNOWN))
    body = json.loads(response.body.decode())
    assert body["available_gb"] is None
    assert body["posture"] == "unknown"


def test_kernel_pressure_is_reported_beside_the_posture():
    from kiro_crew import platform_compat

    held = capacity.capacity_payload(
        _status(9.0, resource_status.POSTURE_AMPLE, platform_compat.MEMORY_PRESSURE_WARN), "x"
    )
    assert held["posture"] == "ample"
    assert held["pressure_held"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("user", "app"), [("someone-else", ""), (OWNER, "some-app")])
async def test_anyone_but_the_owner_is_refused_before_the_probe(monkeypatch, user, app):
    response, calls = await _get(
        monkeypatch, _status(10.0, resource_status.POSTURE_AMPLE), user=user, app=app
    )
    assert response.status in (401, 403)
    assert calls == []


@pytest.mark.asyncio
async def test_the_deferred_route_reaches_the_handler_on_cookie_auth(monkeypatch):
    from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
    from kiro_crew.dashboard.server_runtime import mcp_routes

    monkeypatch.setattr(resource_status, "probe", lambda cfg=None: _status(8.0, "ample"))
    monkeypatch.setattr(lease, "default_display_name", lambda: "this-computer")
    route = mcp_routes._deferred("crew_board_capacity", "api_crew_board_capacity")
    request = make_mocked_request(
        "GET", "/api/crew-board/capacity", app={"state": SimpleNamespace(owner_id=OWNER)}
    )
    request["user"] = OWNER
    request["app"] = ""
    response = await route(request)
    assert response.status == 200
    assert json.loads(response.body.decode())["name"] == "this-computer"
    path = "/api/crew-board/capacity"
    assert not any(path == p or path.startswith(p + "/") for p in _STRICT_INTERNAL_API_PATHS)
