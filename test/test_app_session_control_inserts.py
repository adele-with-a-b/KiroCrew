"""A docked session control's ``composerInsert``: the owner approves it like a composer action.

A control declares ``composerInsert: true`` with ``dock: true``
(``apps/session_control_insert_grants.py``); the gateway publishes the owner's answer
as the row's ``sessionControlInsertGrant``, which the dashboard reads before it hands
the docked box ``useInsertIntoComposer``. These cases install a real app into a
private home, so the row comes from the same manifest, approved-grants set and record
``/api/apps`` reads: an install approves what it declares, an update that adds it
waits for the owner, a set stored before the key existed approves nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps import protected_app_state
from kiro_crew.apps import session_control_insert_grants as sci
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    enable_app,
    get_app,
    install_app,
    update_app,
)
from kiro_crew.apps.point_grants import (
    explicit_only_grant_families,
    grant_source,
    point_grant_families,
)

APP = "inbox-box"
FAMILY = sci.SESSION_CONTROL_INSERT_FAMILY


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    """A private app home that admits a local third-party app."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )


def _control(control_id: str, insert: bool) -> dict:
    ctl = {"id": control_id, "entryPoint": "dist/box.mjs", "label": control_id, "dock": True}
    if insert:
        ctl["composerInsert"] = True
    return ctl


def _source(tmp_path: Path, sub: str, controls: list[dict], version: str = "1.0.0") -> Path:
    src = tmp_path / sub / APP
    (src / "ui" / "dist").mkdir(parents=True)
    (src / "ui" / "dist" / "box.mjs").write_text("export default () => null\n", encoding="utf-8")
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Inbox Box",
        "description": "a docked box",
        "author": "tester",
        "contributes": {"sessionControls": controls},
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _install(tmp_path: Path, controls: list[dict]) -> None:
    result = install_app(_source(tmp_path, "v1", controls))
    assert result.ok, result.error
    assert enable_app(APP).ok


def _update(tmp_path: Path, controls: list[dict]) -> None:
    result = update_app(_source(tmp_path, "v2", controls, version="2.0.0"))
    assert result.ok, result.error


def _grant() -> dict | None:
    row = get_app(APP)
    assert row is not None
    return row.get("sessionControlInsertGrant")


def test_the_declaration_is_an_explicit_only_staged_grant_family():
    assert FAMILY in point_grant_families()
    assert FAMILY in explicit_only_grant_families()
    declared = {
        "contributes": {"sessionControls": [_control("inbox", True), _control("log", False)]}
    }
    assert grant_source(declared)[FAMILY] == ["inbox:insert"]
    # Only the JSON true declares it.
    loose = {
        "contributes": {"sessionControls": [{**_control("inbox", False), "composerInsert": "yes"}]}
    }
    assert grant_source(loose)[FAMILY] == []


def test_an_app_that_declares_nothing_has_no_grant_row(tmp_path):
    _install(tmp_path, [_control("inbox", False)])
    assert _grant() is None


def test_an_install_approves_what_it_declares(tmp_path):
    _install(tmp_path, [_control("inbox", True)])
    assert _grant() == {"approved": ["inbox"], "staged": []}


def test_an_update_that_adds_it_waits_for_the_owner(tmp_path):
    _install(tmp_path, [_control("inbox", False)])
    _update(tmp_path, [_control("inbox", True)])
    assert _grant() == {"approved": [], "staged": ["inbox"]}

    approved = enable_app(APP, grants_consent={FAMILY: ["inbox:insert"]})
    assert approved.ok, approved.error
    assert _grant() == {"approved": ["inbox"], "staged": []}


def test_an_update_that_adds_a_second_control_holds_back_only_that_one(tmp_path):
    _install(tmp_path, [_control("inbox", True)])
    _update(tmp_path, [_control("inbox", True), _control("log", True)])
    assert _grant() == {"approved": ["inbox"], "staged": ["log"]}


def test_an_update_that_drops_it_has_no_grant_row(tmp_path):
    _install(tmp_path, [_control("inbox", True)])
    _update(tmp_path, [_control("inbox", False)])
    assert _grant() is None


def test_a_set_stored_before_the_key_existed_approves_nothing(tmp_path):
    """An older host ignored the field, so its owner was never shown it."""
    _install(tmp_path, [_control("inbox", True)])
    stored = json.loads(protected_app_state.read_grants_text(APP) or "{}")
    stored.pop(FAMILY)
    protected_app_state.write_grants_text(APP, json.dumps(stored))

    assert _grant() == {"approved": [], "staged": ["inbox"]}
    assert enable_app(APP, grants_consent={FAMILY: ["inbox:insert"]}).ok
    assert _grant() == {"approved": ["inbox"], "staged": []}
