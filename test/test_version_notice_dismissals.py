"""The untested-version notice a person dismissed for one release stays dismissed.

The dismissal lives on the gateway, per data home (``version_notice_dismissals``):
a new session of that harness at that release writes no notice, another release
writes one, and only the person's own browser may dismiss, so no agent or app can
hide the warning.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp.client_turn_control import (
    CLAUDE_ACP_TESTED_VERSION,
    TESTED_VERSION_NOTICE_HARNESSES,
    untested_version_harness,
)
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import version_notice_dismissals as vnd
from kiro_crew.providers.acp import AcpProvider

CLAUDE = sdk_backends.ACP_BACKEND_CLAUDE


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(vnd, "config_dir", lambda: tmp_path)
    vnd._clear_cache_for_tests()
    yield tmp_path
    vnd._clear_cache_for_tests()


# ── The store ──


def test_nothing_is_dismissed_before_any_write(tmp_path) -> None:
    assert vnd.load() == {}
    assert not vnd.is_dismissed("claude", "0.109.0")
    assert not vnd.store_path(tmp_path).exists()


def test_a_dismissal_is_stored_owner_only_and_read_back_after_a_restart(tmp_path) -> None:
    assert vnd.record("claude", "0.109.0") == {"claude": ["0.109.0"]}
    path = vnd.store_path(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"claude": ["0.109.0"]}
    assert os.stat(path).st_mode & 0o077 == 0
    assert os.stat(path.parent).st_mode & 0o077 == 0
    vnd._clear_cache_for_tests()
    assert vnd.is_dismissed("claude", "0.109.0")
    assert not vnd.is_dismissed("claude", "0.110.0")
    assert not vnd.is_dismissed("codex", "0.109.0")


def test_every_dismissed_release_is_kept_newest_last_up_to_the_cap() -> None:
    vnd.record("claude", "0.100.0")
    vnd.record("claude", "0.109.0")
    # Dismissing a release again moves it to the newest end, once.
    assert vnd.record("claude", "0.100.0") == {"claude": ["0.109.0", "0.100.0"]}
    for minor in range(vnd.MAX_VERSIONS_PER_HARNESS + 3):
        vnd.record("claude", f"1.{minor}.0")
    kept = vnd.load()["claude"]
    assert len(kept) == vnd.MAX_VERSIONS_PER_HARNESS
    assert kept[-1] == f"1.{vnd.MAX_VERSIONS_PER_HARNESS + 2}.0"
    assert "0.109.0" not in kept


def test_load_returns_a_copy() -> None:
    vnd.record("claude", "0.109.0")
    vnd.load()["claude"].append("9.9.9")
    assert vnd.load() == {"claude": ["0.109.0"]}


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"\xff\xfe",
        b'["claude"]',
        b'{"claude": "0.109.0"}',
    ],
    ids=["not-json", "not-utf8", "not-an-object", "not-a-list"],
)
def test_a_malformed_file_reads_as_no_dismissals(tmp_path, content) -> None:
    path = vnd.store_path(tmp_path)
    path.parent.mkdir()
    path.write_bytes(content)
    assert vnd.load() == {}


def test_unknown_harnesses_and_bad_versions_are_dropped_when_read(tmp_path) -> None:
    path = vnd.store_path(tmp_path)
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "claude": ["0.109.0", 7, "", "../x", "0.109.0", "x" * 65],
                "not-a-harness": ["1.0.0"],
            }
        ),
        encoding="utf-8",
    )
    assert vnd.load() == {"claude": ["0.109.0"]}


def test_a_link_or_an_oversized_file_is_ignored(tmp_path) -> None:
    path = vnd.store_path(tmp_path)
    path.parent.mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text('{"claude": ["0.109.0"]}', encoding="utf-8")
    path.symlink_to(target)
    assert vnd.load() == {}
    vnd._clear_cache_for_tests()
    path.unlink()
    path.write_bytes(b" " * (vnd.MAX_FILE_BYTES + 1))
    assert vnd.load() == {}


def test_a_failed_write_changes_nothing(tmp_path, monkeypatch) -> None:
    vnd.record("claude", "0.100.0")

    def refuse(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(vnd, "atomic_write", refuse)
    with pytest.raises(OSError):
        vnd.record("claude", "0.109.0")
    assert vnd.load() == {"claude": ["0.100.0"]}


def test_a_file_that_cannot_be_read_is_not_written_over(tmp_path) -> None:
    vnd.record("claude", "0.100.0")
    vnd._clear_cache_for_tests()
    path = vnd.store_path(tmp_path)
    path.chmod(0)
    try:
        # A display read shows the notice; the rewrite refuses rather than drop 0.100.0.
        assert vnd.load() == {}
        with pytest.raises(PermissionError):
            vnd.record("claude", "0.109.0")
    finally:
        path.chmod(0o600)
    assert json.loads(path.read_text(encoding="utf-8")) == {"claude": ["0.100.0"]}


# ── Which notices carry a release ──


def test_the_harnesses_are_the_backends_with_a_tested_range() -> None:
    assert TESTED_VERSION_NOTICE_HARNESSES == {"claude", "codex", "goose", "kiro-cli", "pi"}
    assert vnd.known_harnesses() == TESTED_VERSION_NOTICE_HARNESSES


def test_only_an_untested_release_names_a_harness() -> None:
    assert untested_version_harness(CLAUDE, "0.109.0") == "claude"
    assert untested_version_harness(CLAUDE, CLAUDE_ACP_TESTED_VERSION) == ""
    assert untested_version_harness(CLAUDE, "") == ""
    assert untested_version_harness("no-such-backend", "1.0.0") == ""


def _provider(backend, version):
    return SimpleNamespace(
        _client=SimpleNamespace(backend=backend, agent_version=version), agent_version=version
    )


def test_the_provider_names_the_notice_subject_only_when_it_warns() -> None:
    subject = AcpProvider.tested_version_notice_subject.fget
    assert subject(_provider(CLAUDE, "0.109.0")) == ("claude", "0.109.0")
    assert subject(_provider(CLAUDE, CLAUDE_ACP_TESTED_VERSION)) is None
    # No backend name (a KAS relay's notice): nothing to dismiss it by.
    assert subject(_provider(None, "0.109.0")) is None


def test_notice_meta_tags_only_a_named_harness_and_release() -> None:
    meta = vnd.notice_meta(SimpleNamespace(tested_version_notice_subject=("claude", "0.109.0")))
    assert meta == {"kind": vnd.NOTICE_KIND, "harness": "claude", "version": "0.109.0"}
    assert vnd.notice_meta(SimpleNamespace()) is None
    assert vnd.notice_meta(SimpleNamespace(tested_version_notice_subject=None)) is None
    assert vnd.notice_meta(SimpleNamespace(tested_version_notice_subject=("x", "1.0.0"))) is None
    assert vnd.notice_meta(SimpleNamespace(tested_version_notice_subject=("claude", "a b"))) is None


# ── The session-start notice ──


def _session_rows(monkeypatch, version):
    rows: list[tuple] = []
    monkeypatch.setattr(
        chat_runner, "append_and_surface", lambda *args, **kw: rows.append((args, kw))
    )
    provider = _provider(CLAUDE, version)
    client = SimpleNamespace(
        tested_version_notice=AcpProvider.tested_version_notice.fget(provider),
        tested_version_notice_subject=AcpProvider.tested_version_notice_subject.fget(provider),
    )
    chat_runner._notice_steer_upgrade(object(), object(), client)
    return rows


def test_a_new_session_tags_its_notice_with_the_harness_and_release(monkeypatch) -> None:
    rows = _session_rows(monkeypatch, "0.109.0")
    assert len(rows) == 1
    args, kw = rows[0]
    assert args[2] == "notice" and "0.109.0" in args[3]
    assert kw["meta"] == {"kind": vnd.NOTICE_KIND, "harness": "claude", "version": "0.109.0"}


def test_a_dismissed_release_writes_no_notice_and_another_release_does(monkeypatch) -> None:
    vnd.record("claude", "0.109.0")
    assert _session_rows(monkeypatch, "0.109.0") == []
    rows = _session_rows(monkeypatch, "0.110.0")
    assert len(rows) == 1
    assert rows[0][1]["meta"]["version"] == "0.110.0"


def test_other_session_notices_are_not_tagged(monkeypatch) -> None:
    vnd.record("claude", "0.109.0")
    rows: list[tuple] = []
    monkeypatch.setattr(
        chat_runner, "append_and_surface", lambda *args, **kw: rows.append((args, kw))
    )
    client = SimpleNamespace(
        steer_upgrade_notice="upgrade the adapter",
        tested_version_notice_subject=("claude", "0.109.0"),
    )
    chat_runner._notice_steer_upgrade(object(), object(), client)
    assert [kw["meta"] for _args, kw in rows] == [None]


# ── The routes ──


def _app(monkeypatch, owner=True) -> tuple[web.Application, MagicMock]:
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda _request: owner,
    )
    state = MagicMock()
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/version-notices", vnd.api_version_notices)
    app.router.add_post("/api/chat/version-notices/dismiss", vnd.api_version_notice_dismiss)
    return app, state


def _from_a_folder(monkeypatch, principal) -> None:
    monkeypatch.setattr(
        "kiro_crew.dashboard.token_auth.folder_principal", lambda _state, _req: principal
    )


@pytest.mark.asyncio
async def test_the_browser_dismisses_and_every_owner_page_is_told(monkeypatch, tmp_path) -> None:
    _from_a_folder(monkeypatch, "")
    app, state = _app(monkeypatch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/version-notices/dismiss", json={"harness": "claude", "version": "0.109.0"}
        )
        assert resp.status == 200
        assert await resp.json() == {"dismissed": {"claude": ["0.109.0"]}}
        state.broadcast_ws_owners.assert_called_once_with(
            vnd.WS_EVENT, {"harness": "claude", "version": "0.109.0"}
        )
        resp = await client.get("/api/chat/version-notices")
        assert resp.status == 200
        assert await resp.json() == {"dismissed": {"claude": ["0.109.0"]}}
    assert vnd.store_path(tmp_path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Internal-Secret": "s", "X-Internal-Caller": "kirocrew-dashboard"},
        {"X-Internal-Secret": "s"},
        {"X-Internal-Secret": "s", "X-Session-Key": "dashboard:a"},
    ],
    ids=["mcp-tool", "unnamed-internal", "from-a-session"],
)
async def test_an_agent_cannot_dismiss(monkeypatch, tmp_path, headers) -> None:
    _from_a_folder(monkeypatch, "")
    app, state = _app(monkeypatch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/version-notices/dismiss",
            json={"harness": "claude", "version": "0.109.0"},
            headers=headers,
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "version_notice_person_only"
    assert not vnd.store_path(tmp_path).exists()
    state.broadcast_ws_owners.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", ["some-app", "member:ops"])
async def test_an_app_or_member_cannot_dismiss(monkeypatch, tmp_path, principal) -> None:
    _from_a_folder(monkeypatch, principal)
    app, _state = _app(monkeypatch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/version-notices/dismiss", json={"harness": "claude", "version": "0.109.0"}
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "version_notice_person_only"
    assert not vnd.store_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_a_viewer_who_is_not_the_owner_can_neither_read_nor_dismiss(monkeypatch) -> None:
    _from_a_folder(monkeypatch, "")
    app, _state = _app(monkeypatch, owner=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/chat/version-notices")
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"
        resp = await client.post(
            "/api/chat/version-notices/dismiss", json={"harness": "claude", "version": "0.109.0"}
        )
        assert resp.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"harness": "nope", "version": "0.109.0"}, "harness_invalid"),
        ({"version": "0.109.0"}, "harness_invalid"),
        ({"harness": "claude"}, "version_invalid"),
        ({"harness": "claude", "version": "0.1 0"}, "version_invalid"),
        ({"harness": "claude", "version": "x" * 65}, "version_invalid"),
        ({"harness": "claude", "version": 109}, "version_invalid"),
    ],
)
async def test_a_bad_body_is_refused(monkeypatch, tmp_path, body, code) -> None:
    _from_a_folder(monkeypatch, "")
    app, _state = _app(monkeypatch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/version-notices/dismiss", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == code
    assert not vnd.store_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_a_dismissal_not_saved_is_said_and_not_broadcast(monkeypatch) -> None:
    _from_a_folder(monkeypatch, "")
    app, state = _app(monkeypatch)

    def refuse(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(vnd, "atomic_write", refuse)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/version-notices/dismiss", json={"harness": "claude", "version": "0.109.0"}
        )
        assert resp.status == 500
        assert (await resp.json())["code"] == "dismissal_not_saved"
    state.broadcast_ws_owners.assert_not_called()
    assert vnd.load() == {}


def test_the_gateway_serves_both_routes() -> None:
    from kiro_crew.dashboard.routes import chat as chat_routes

    app = web.Application()
    chat_routes.register(app)
    routes = {
        (route.method, route.resource.canonical): route.handler
        for route in app.router.routes()
        if route.resource is not None
    }
    assert routes[("GET", "/api/chat/version-notices")] is vnd.api_version_notices
    assert routes[("POST", "/api/chat/version-notices/dismiss")] is vnd.api_version_notice_dismiss
