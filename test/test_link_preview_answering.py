"""A link-preview card's answering mode: its grant, its bootstrap calls, and the row fold.

* ``answering: true`` (with ``sessionKey: true``) is its own explicit-only grant
  family (``apps/link_preview_answering_grants.py``): an install approves it, an
  update that adds it waits for the owner, a set stored before the family existed
  approves nothing, and the row says which (``linkPreviewGrant.answering``).
* The manifest refuses a non-boolean ``answering`` and one without ``sessionKey``.
* The frame's ``card.answering`` only shapes messages: ``enter`` / ``update`` /
  ``hand`` are numbered and answered, ``hand`` names the line text it was built
  from, and the dashboard's events reach the provider. Nothing in it sends.
* ``meta.answers`` (the "N answers sent" fold) is kept only on a person's own
  send, and only in its one shape (``dashboard/chat_api/answers_meta.py``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps import app_frame as lpf
from kiro_crew.apps import link_preview_answering_grants as lpa
from kiro_crew.apps import protected_app_state
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    enable_app,
    get_app,
    install_app,
    update_app,
)
from kiro_crew.apps.manifest_contributes import LinkPreviewConfig
from kiro_crew.apps.point_grants import explicit_only_grant_families, grant_source
from kiro_crew.dashboard.chat_api.answers_meta import answers_meta
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

APP = "answer-cards"
FAMILY = lpa.LINK_PREVIEW_ANSWERING_FAMILY


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    """A private app home that admits a local third-party app."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )


def _provider(answering: bool) -> dict:
    p = {
        "id": "cards",
        "entry": "dist/cards.mjs",
        "textPatterns": [{"id": "q", "pattern": "\\[Q\\d+[a-z]\\]"}],
        "sessionKey": True,
    }
    if answering:
        p["answering"] = True
    return p


def _source(tmp_path: Path, sub: str, answering: bool, version: str = "1.0.0") -> Path:
    src = tmp_path / sub / APP
    (src / "ui" / "dist").mkdir(parents=True)
    (src / "ui" / "dist" / "cards.mjs").write_text(
        "export function activate() {}\n", encoding="utf-8"
    )
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Answer Cards",
        "description": "cards that collect answers",
        "author": "tester",
        "contributes": {"linkPreviews": [_provider(answering)]},
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _install(tmp_path: Path, answering: bool) -> None:
    result = install_app(_source(tmp_path, "v1", answering))
    assert result.ok, result.error
    assert enable_app(APP).ok


def _update(tmp_path: Path, answering: bool) -> None:
    result = update_app(_source(tmp_path, "v2", answering, version="2.0.0"))
    assert result.ok, result.error


def _grant() -> tuple[bool, bool]:
    row = get_app(APP)
    assert row is not None
    g = row.get("linkPreviewGrant") or {}
    return g.get("answering"), g.get("stagedAnswering")


def test_the_declaration_is_an_explicit_only_family_that_needs_the_session_key():
    assert FAMILY in explicit_only_grant_families()
    assert grant_source({"contributes": {"linkPreviews": [_provider(True)]}})[FAMILY] == [
        "answering"
    ]
    assert grant_source({"contributes": {"linkPreviews": [_provider(False)]}})[FAMILY] == []
    # Without the chat's session key the mode has no chat to act in.
    alone = {**_provider(True), "sessionKey": False}
    assert grant_source({"contributes": {"linkPreviews": [alone]}})[FAMILY] == []


def test_the_manifest_refuses_a_loose_or_chatless_answering():
    loose = LinkPreviewConfig.from_dict({**_provider(False), "answering": "yes"})
    assert any("answering must be true or false" in e for e in loose.validate())
    chatless = LinkPreviewConfig.from_dict({**_provider(True), "sessionKey": False})
    assert any("answering needs sessionKey" in e for e in chatless.validate())
    ok = LinkPreviewConfig.from_dict(_provider(True))
    assert ok.validate() == []
    assert ok.to_dict()["answering"] is True


def test_an_install_approves_it_and_an_update_that_adds_it_waits(tmp_path):
    _install(tmp_path, False)
    assert _grant() == (False, False)
    _update(tmp_path, True)
    assert _grant() == (False, True)
    assert enable_app(APP, grants_consent={FAMILY: ["answering"]}).ok
    assert _grant() == (True, False)


def test_a_set_stored_before_the_family_existed_approves_nothing(tmp_path):
    _install(tmp_path, True)
    assert _grant() == (True, False)
    stored = json.loads(protected_app_state.read_grants_text(APP) or "{}")
    stored.pop(FAMILY)
    protected_app_state.write_grants_text(APP, json.dumps(stored))
    assert _grant() == (False, True)


@pytest.mark.parametrize(
    ("raw", "person", "kept"),
    [
        ({"count": 2, "app": "Answer Cards"}, True, {"count": 2, "app": "Answer Cards"}),
        ({"count": 2, "app": "Answer Cards"}, False, None),  # an app token's or agent's send
        ({"count": 0, "app": "Answer Cards"}, True, None),
        ({"count": 1000, "app": "Answer Cards"}, True, None),
        ({"count": True, "app": "Answer Cards"}, True, None),
        ({"count": 2.0, "app": "Answer Cards"}, True, None),
        ({"count": 2, "app": ""}, True, None),
        ({"count": 2, "app": "x" * 65}, True, None),
        ({"count": 2, "app": "A​B\x07"}, True, {"count": 2, "app": "AB"}),
        ("2 answers", True, None),
        (None, True, None),
    ],
)
def test_the_fold_is_kept_only_on_a_persons_send_in_its_one_shape(raw, person, kept):
    assert answers_meta(raw, person_send=person) == kept


_HARNESS = r"""
const posted = [];
const listeners = {};
const parent = { postMessage: (m) => posted.push(m) };
globalThis.window = { parent, addEventListener: (t, f) => { (listeners[t] ||= []).push(f); } };
globalThis.location = { pathname: '/app-frame/a/linkPreviews/c/f/t', href: 'http://gw/app-frame/a/linkPreviews/c/f/t' };
const provider = 'export function activate(ctx) { return { show(link) { globalThis.shows.push(link); }, answering(e) { globalThis.events.push(e); } }; }';
globalThis.shows = [];
globalThis.events = [];
globalThis.document = {
  activeElement: null,
  body: {},
  getElementById: () => ({ textContent: JSON.stringify({
    app: 'a', entry: 'data:text/javascript,' + encodeURIComponent(provider),
    fetchBase: '/app-frame-fetch/a/', theme: 'light',
  }) }),
};
"""

_DRIVER = r"""
const deliver = (data) => (listeners.message || []).forEach((f) => f({ source: parent, data: { 'kirocrew-app-frame': 1, ...data } }));
deliver({ type: 'init', capability: 'c' });
for (let i = 0; i < 50 && !posted.some((m) => m.type === 'ready'); i++) await new Promise((r) => setTimeout(r, 10));
const rect = { left: 0, top: 0, width: 1, height: 1 };
deliver({ type: 'show', invocation: 5, text: '[Q4a]', rect, theme: 'dark', sessionKey: 'dashboard:s', message: { ts: 't' } });
const [a] = shows;
const entered = a.card.answering.enter({ place: 'Q4a · 2 of 3', hint: 'go', keys: [{ key: 'Tab', label: 'go' }], keepAt: 1 });
deliver({ type: 'answering', event: { kind: 'draft', text: 'yes, after lunch', rev: 7 } });
const handed = a.card.answering.hand({ text: 'Q1a go', count: 1 });
const pinned = a.card.answering.hand({ text: 'Q1a go', count: 1, rev: 3 });
const odd = a.card.answering.enter(() => 1);
a.card.answering.leave();
deliver({ type: 'answering', event: { kind: 'ended', why: 'sent' } });
deliver({ type: 'answering', event: 'not an event' });
const asked = posted.filter((m) => m.type === 'answering');
deliver({ type: 'answered', request: asked[0].request, ok: true });
deliver({ type: 'answered', request: asked[1].request, ok: false, reason: 'stale' });
deliver({ type: 'answered', request: asked[2].request, ok: true });
const results = await Promise.all([entered, handed, pinned, odd]);
console.log(JSON.stringify({
  sent: asked.map(({ 'kirocrew-app-frame': _t, ...m }) => m),
  results,
  events,
  frozen: Object.isFrozen(a.card.answering),
}));
"""


def test_the_cards_answering_calls_shape_messages_and_events_reach_the_provider():
    """``enter`` / ``hand`` are numbered and answered; ``hand`` names the latest line
    text Crew told the card about unless the app names its own; ``leave`` is unnumbered;
    events from the dashboard reach ``answering(event)``; a non-JSON setup is refused
    unsent. No call here sends a message: the dashboard alone does, on a press."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", _HARNESS + lpf._BOOTSTRAP + _DRIVER],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["sent"] == [
        {
            "type": "answering",
            "op": "enter",
            "invocation": 5,
            "request": 1,
            "place": "Q4a · 2 of 3",
            "hint": "go",
            "keys": [{"key": "Tab", "label": "go"}],
            "keepAt": 1,
        },
        {
            "type": "answering",
            "op": "hand",
            "invocation": 5,
            "request": 2,
            "text": "Q1a go",
            "count": 1,
            "rev": 7,
        },
        {
            "type": "answering",
            "op": "hand",
            "invocation": 5,
            "request": 3,
            "text": "Q1a go",
            "count": 1,
            "rev": 3,
        },
        {"type": "answering", "op": "leave", "invocation": 5},
    ]
    assert result["results"] == [
        {"ok": True},
        {"ok": False, "reason": "stale"},
        {"ok": True},
        {"ok": False, "reason": "not_json"},
    ]
    assert result["events"] == [
        {"kind": "draft", "text": "yes, after lunch", "rev": 7},
        {"kind": "ended", "why": "sent"},
    ]
    assert result["frozen"] is True
