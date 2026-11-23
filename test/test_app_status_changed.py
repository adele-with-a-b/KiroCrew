"""An app tells the dashboard its own status changed, and nobody else's.

``POST /api/apps/<app>/status-changed`` asks every owner dashboard window to re-read
``<app>``'s own status routes (its session-control chips and its top-bar items) at
once, instead of at the next turn end or poll. The path names the app; an app token
may only name its own; the owner's dashboard, where the app's page runs, may name any
installed app that declares a status route.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard.handlers import app_status_changed as mod

_MOD = "kiro_crew.dashboard.handlers.app_status_changed"


def _manifest(name: str, *, chip: bool = True, pill: bool = False) -> AppManifest:
    contributes: dict = {}
    if chip:
        contributes["sessionControls"] = [
            {
                "id": "count",
                "entryPoint": "dist/chip.mjs",
                "label": "Count",
                "statusPath": "count-status",
            }
        ]
    if pill:
        contributes["statusItems"] = [
            {"id": "total", "label": "Total", "statusPath": "total-status"}
        ]
    return AppManifest.from_dict(
        {
            "name": name,
            "version": "0.1.0",
            "displayName": name,
            "description": "x",
            "contributes": contributes,
        }
    )


_MANIFESTS = {
    "ledger": _manifest("ledger"),
    "other": _manifest("other"),
    "quiet": _manifest("quiet", chip=False),
    "pill-only": _manifest("pill-only", chip=False, pill=True),
}


class _FakeState:
    owner_id = "owner"

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def broadcast_ws_owners(self, event: str, data: dict) -> None:
        self.frames.append((event, data))


def _app(state: _FakeState, *, app: str, user: str = "") -> web.Application:
    @web.middleware
    async def fake_auth(request, handler):
        request["app"] = app
        if user:
            request["user"] = user
        return await handler(request)

    application = web.Application(middlewares=[fake_auth])
    application["state"] = state
    application.router.add_post("/api/apps/{name}/status-changed", mod.api_app_status_changed)
    return application


@pytest.fixture(autouse=True)
def _installed():
    with (
        patch(f"{_MOD}.is_app_enabled", side_effect=lambda n: n in _MANIFESTS),
        patch(f"{_MOD}.get_app_manifest", side_effect=_MANIFESTS.get),
    ):
        yield


@pytest.mark.asyncio
async def test_an_app_token_moves_only_its_own_status():
    state = _FakeState()
    async with TestClient(TestServer(_app(state, app="ledger"))) as client:
        resp = await client.post(
            "/api/apps/ledger/status-changed", json={"session_key": "dashboard:chat-7"}
        )
        assert resp.status == 200
        resp = await client.post("/api/apps/ledger/status-changed")
        assert resp.status == 200
        resp = await client.post(
            "/api/apps/other/status-changed", json={"session_key": "dashboard:chat-7"}
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "app_status_not_own"
    assert state.frames == [
        ("app_status_changed", {"app": "ledger", "session_key": "dashboard:chat-7"}),
        ("app_status_changed", {"app": "ledger"}),
    ]


@pytest.mark.asyncio
async def test_the_owner_dashboard_names_the_app_and_a_teammate_cannot():
    state = _FakeState()
    async with TestClient(TestServer(_app(state, app="", user="owner"))) as client:
        resp = await client.post(
            "/api/apps/other/status-changed", json={"session_key": "dashboard:chat-2"}
        )
        assert resp.status == 200
    async with TestClient(TestServer(_app(state, app="", user="teammate"))) as client:
        resp = await client.post("/api/apps/other/status-changed", json={})
        assert resp.status == 403
    assert state.frames == [
        ("app_status_changed", {"app": "other", "session_key": "dashboard:chat-2"})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["quiet", "missing"])
async def test_an_app_with_no_status_route_is_refused(name):
    state = _FakeState()
    async with TestClient(TestServer(_app(state, app="", user="owner"))) as client:
        resp = await client.post(f"/api/apps/{name}/status-changed", json={})
        assert resp.status == 404
        assert (await resp.json())["code"] == "app_status_not_declared"
    assert state.frames == []


@pytest.mark.asyncio
async def test_an_app_with_only_a_top_bar_item_may_refresh_it():
    state = _FakeState()
    async with TestClient(TestServer(_app(state, app="pill-only"))) as client:
        resp = await client.post("/api/apps/pill-only/status-changed")
        assert resp.status == 200
        assert await resp.json() == {"ok": True}
    assert state.frames == [("app_status_changed", {"app": "pill-only"})]


@pytest.mark.asyncio
async def test_a_disabled_app_is_refused():
    state = _FakeState()
    with patch(f"{_MOD}.is_app_enabled", return_value=False):
        async with TestClient(TestServer(_app(state, app="ledger"))) as client:
            resp = await client.post("/api/apps/ledger/status-changed")
            assert resp.status == 404
            assert (await resp.json())["code"] == "app_status_not_declared"
    assert state.frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "a b", "x" * 201, "dash\nboard", 7, ["dashboard:chat-1"]])
async def test_a_malformed_session_key_is_refused(bad):
    state = _FakeState()
    async with TestClient(TestServer(_app(state, app="ledger"))) as client:
        resp = await client.post("/api/apps/ledger/status-changed", json={"session_key": bad})
        assert resp.status == 400
    assert state.frames == []


@pytest.mark.asyncio
async def test_every_refusal_leaves_an_audit_record():
    state = _FakeState()
    with patch(f"{_MOD}.sel") as sel_mock:
        async with TestClient(TestServer(_app(state, app="ledger"))) as client:
            await client.post("/api/apps/other/status-changed", json={})
            await client.post("/api/apps/ledger/status-changed", json={"session_key": "a b"})
    outcomes = [c.kwargs["outcome"] for c in sel_mock.return_value.log_api_access.call_args_list]
    assert outcomes == ["denied", "denied"]


def test_the_route_segment_is_reserved_from_apps():
    from kiro_crew.apps.manifest import CORE_APP_ROUTE_SEGMENTS

    assert "status-changed" in CORE_APP_ROUTE_SEGMENTS


def test_the_frame_is_owner_only():
    from kiro_crew.dashboard import ws_event_scope

    assert "app_status_changed" in ws_event_scope._OWNER_ONLY_EVENTS
