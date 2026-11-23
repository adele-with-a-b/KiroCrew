"""Open files: an app's dashboard UI may open a local file on the person's click.

An app names it in ``permissions.openFiles`` (``apps/open_files.py``); the gateway
publishes the owner's answer as the row's ``openFilesGrant``, which the dashboard's
SDK host reads before it serves ``useOpenFile``. These cases install a real app into
a private home so the row comes from the same manifest, approved-grants set and
record ``/api/apps`` reads. They pin the parse, the disclosure, an install approving
what it declares, an update that adds it waiting for the owner, and a set stored
before the key existed approving nothing. The opening itself is ``/api/reveal``'s,
whose sensitive-path refusal ``test_reveal_path.test_reveal_path_sensitive_denied`` pins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps import open_files as of
from kiro_crew.apps import protected_app_state
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    approved_manifest_permissions,
    enable_app,
    get_app,
    install_app,
    update_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_sections import Permissions
from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest
from kiro_crew.apps.permissions import format_permissions_summary
from kiro_crew.apps.point_grants import (
    explicit_only_grant_families,
    grant_source,
    point_grant_families,
)

APP = "path-opener"
APPROVED = {"approved": True, "staged": False}
STAGED = {"approved": False, "staged": True}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    """A private app home that admits a local third-party app."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )


def _source(tmp_path: Path, sub: str, open_files: bool, version: str = "1.0.0") -> Path:
    src = tmp_path / sub / APP
    src.mkdir(parents=True)
    permissions: dict = {"storage": True}
    if open_files:
        permissions["openFiles"] = True
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Path Opener",
        "description": "shows file paths",
        "author": "tester",
        "permissions": permissions,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _install(tmp_path: Path, open_files: bool) -> None:
    result = install_app(_source(tmp_path, "v1", open_files))
    assert result.ok, result.error
    assert enable_app(APP).ok


def _update(tmp_path: Path, open_files: bool) -> None:
    result = update_app(_source(tmp_path, "v2", open_files, version="2.0.0"))
    assert result.ok, result.error


def _grant() -> dict | None:
    row = get_app(APP)
    assert row is not None
    return row.get("openFilesGrant")


# ── the declaration ─────────────────────────────────────────────────────────


def test_only_a_literal_true_declares_it():
    assert Permissions.from_dict({"openFiles": True}).openFiles is True
    assert Permissions.from_dict({"openFiles": True}).to_dict() == {"openFiles": True}
    for malformed in ("true", 1, ["open"], {"open": True}, None, False):
        assert Permissions.from_dict({"openFiles": malformed}).openFiles is False
    assert "openFiles" not in Permissions().to_dict()


def test_the_declaration_is_an_explicit_only_staged_grant_family():
    assert of.GRANT_FAMILY in point_grant_families()
    assert of.GRANT_FAMILY in explicit_only_grant_families()
    assert grant_source({"permissions": {"openFiles": True}})[of.GRANT_FAMILY] == ["open"]
    assert grant_source({"permissions": {"openFiles": "yes"}})[of.GRANT_FAMILY] == []


def test_the_catalog_and_the_install_summary_disclose_it():
    assert _disclosed_permissions_manifest({"permissions": {"openFiles": True}}) == {
        "permissions": {"openFiles": True}
    }
    assert _disclosed_permissions_manifest({"permissions": {"openFiles": "yes"}}) is None
    manifest = AppManifest.from_dict(
        {"name": APP, "version": "1.0.0", "permissions": {"openFiles": True}}
    )
    assert format_permissions_summary(manifest).splitlines() == [
        "  Files:     opens a file it picks in its default app after you click in it;"
        " a script or an app runs when it opens"
    ]


# ── the owner's approval on install and update ──────────────────────────────


def test_an_app_that_declares_nothing_has_no_grant_row(tmp_path):
    _install(tmp_path, False)
    assert _grant() is None


def test_an_install_approves_what_it_declares(tmp_path):
    _install(tmp_path, True)
    assert _grant() == APPROVED


def test_an_update_that_adds_it_waits_for_the_owner(tmp_path):
    _install(tmp_path, False)
    _update(tmp_path, True)
    assert _grant() == STAGED

    approved = enable_app(APP, grants_consent={of.GRANT_FAMILY: ["open"]})
    assert approved.ok, approved.error
    assert _grant() == APPROVED


def test_an_update_that_drops_it_has_no_grant_row(tmp_path):
    _install(tmp_path, True)
    _update(tmp_path, False)
    assert _grant() is None


def test_a_set_stored_before_the_key_existed_approves_nothing(tmp_path):
    """The permission could not have been shown to the owner before this host knew the key."""
    _install(tmp_path, True)
    stored = json.loads(protected_app_state.read_grants_text(APP) or "{}")
    stored.pop(of.GRANT_FAMILY)
    protected_app_state.write_grants_text(APP, json.dumps(stored))

    assert _grant() == STAGED
    assert enable_app(APP, grants_consent={of.GRANT_FAMILY: ["open"]}).ok
    assert _grant() == APPROVED


def test_the_hook_context_reads_the_flag_as_declared(tmp_path):
    """The hook-context builders (enable, gateway start, shutdown) read an app's
    permissions through ``approved_manifest_permissions``; a flag is not an entry list."""
    _install(tmp_path, True)
    row = get_app(APP)
    assert row is not None
    assert approved_manifest_permissions(row)["openFiles"] is True


# ── the opening is /api/reveal's, with its own path rules ───────────────────


@pytest.mark.asyncio
async def test_an_open_of_a_sensitive_path_is_refused_before_any_launch(tmp_path):
    """The SDK's open reaches the chat's own route; its sensitive-path owner refuses a
    real secret file (the data home's ``.env``) unpatched, and nothing is launched."""
    from unittest.mock import MagicMock, patch

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.files import api_reveal_path

    secret = tmp_path / "kirocrew-home" / ".env"
    secret.write_text("TOKEN=x", encoding="utf-8")
    app = web.Application()
    app.router.add_post("/api/reveal", api_reveal_path)
    launcher = MagicMock(return_value=True)
    with (
        patch("kiro_crew.sel.sel", return_value=MagicMock()),
        patch("kiro_crew.dashboard.handlers.files.is_direct_local_request", return_value=True),
        patch(
            "kiro_crew.dashboard.handlers.files.platform_compat.open_with_default_app",
            launcher,
        ),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/reveal", json={"path": str(secret), "action": "open"})
            assert resp.status == 403
            assert await resp.json() == {"error": "access denied"}
    launcher.assert_not_called()
