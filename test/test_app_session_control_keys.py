"""A session control's ``keys``: parsed, served to the dashboard, refused at install when bad.

A key's ``keepOpen`` (level 19) is a JSON boolean: served and signed only when ``true``.

The dashboard reads the same fields (``website/src/lib/appSessionKeys.ts``), so the
grammar of ``default`` and the caps here are the ones it relies on.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manifest import (
    MAX_SESSION_CONTROL_KEYS,
    SESSION_CONTROL_KEY_ARG_MAX_BYTES,
    AppManifest,
)
from kiro_crew.apps.manifest_contributes import session_control_key_default_error

BASE = {"name": "my-app", "version": "1.0.0", "displayName": "My App", "description": "d"}


def _mf(keys) -> AppManifest:
    control = {"id": "questions", "entryPoint": "dist/c.mjs", "label": "Questions", "keys": keys}
    return AppManifest.from_dict({**BASE, "contributes": {"sessionControls": [control]}})


def _key_errors(keys) -> list[str]:
    return [e for e in _mf(keys).validate() if " key" in e]


def test_a_declared_key_round_trips_to_the_dashboard():
    keys = [
        {
            "id": "questions",
            "label": "Open Questions",
            "default": "Alt+Shift+Q",
            "arg": {"view": "questions"},
        },
        {"id": "references", "label": "Open References"},
    ]
    m = _mf(keys)
    assert _key_errors(keys) == []
    served = m.to_dict()["contributes"]["sessionControls"][0]["keys"]
    assert served == [
        {
            "id": "questions",
            "label": "Open Questions",
            "default": "Alt+Shift+Q",
            "arg": {"view": "questions"},
        },
        {"id": "references", "label": "Open References"},
    ]


def test_a_control_without_keys_serves_none():
    m = AppManifest.from_dict(
        {**BASE, "contributes": {"sessionControls": [{"id": "a", "entryPoint": "c.mjs"}]}}
    )
    assert "keys" not in m.to_dict()["contributes"]["sessionControls"][0]


@pytest.mark.parametrize(
    "raw",
    ["Alt+Shift+Q", "Mod+K", "ctrl+alt+1", "Mod+Shift+/", "Alt+ArrowUp", "Mod+Enter", "Alt+q"],
)
def test_good_defaults(raw):
    assert session_control_key_default_error(raw) == ""


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("Q", "Mod, Ctrl or Alt"),
        ("Shift+Q", "Mod, Ctrl or Alt"),
        ("Alt+Alt+Q", "each once"),
        ("Cmd+Q", "Mod, Ctrl, Alt or Shift"),
        ("Alt+Escape", "the key must be"),
        ("Alt+é", "the key must be"),
        ("Alt+", "the key must be"),
        ("", "the key must be"),
    ],
)
def test_bad_defaults_say_why(raw, why):
    assert why in session_control_key_default_error(raw)


def test_a_bad_default_is_refused_at_install():
    errors = _key_errors([{"id": "q", "label": "Q", "default": "Shift+Q"}])
    assert errors and "default 'Shift+Q'" in errors[0]
    errors = _key_errors([{"id": "q", "label": "Q", "default": 7}])
    assert errors and "must be a string" in errors[0]


def test_ids_labels_count_and_shape_are_checked():
    assert any("kebab-case" in e for e in _key_errors([{"id": "Bad Id", "label": "x"}]))
    assert any(
        "duplicated" in e
        for e in _key_errors([{"id": "a", "label": "x"}, {"id": "a", "label": "y"}])
    )
    assert any("missing required field: label" in e for e in _key_errors([{"id": "a"}]))
    assert any("missing required field: id" in e for e in _key_errors(["nope"]))
    too_many = [{"id": f"k{i}", "label": "x"} for i in range(MAX_SESSION_CONTROL_KEYS + 1)]
    assert any(f"at most {MAX_SESSION_CONTROL_KEYS} keys" in e for e in _key_errors(too_many))
    assert _key_errors("Alt+Q") == ["session control contribution questions keys must be an array"]


def test_keep_open_is_served_only_when_true_and_signed():
    """``keepOpen: true`` reaches the dashboard and the signed bytes; absent and
    ``false`` serve nothing, so a manifest that names neither keeps its payload."""
    plain = {"id": "next", "label": "Next question", "default": "Ctrl+Shift+N"}
    absent, off, on = (
        _mf([k]) for k in (plain, {**plain, "keepOpen": False}, {**plain, "keepOpen": True})
    )
    assert _key_errors([{**plain, "keepOpen": True}]) == []
    assert _key_errors([{**plain, "keepOpen": False}]) == []
    served = on.to_dict()["contributes"]["sessionControls"][0]["keys"][0]
    assert served == {**plain, "keepOpen": True}
    assert "keepOpen" not in absent.to_dict()["contributes"]["sessionControls"][0]["keys"][0]
    assert off.signing_payload() == absent.signing_payload()
    assert on.signing_payload() != absent.signing_payload()


@pytest.mark.parametrize("bad", ["true", 1, 0, None, [], {}])
def test_keep_open_that_is_not_a_boolean_is_refused(bad):
    errors = _key_errors([{"id": "next", "label": "Next", "keepOpen": bad}])
    assert errors == [
        "session control contribution questions key next keepOpen must be true or false"
    ]


def test_an_arg_over_the_cap_is_refused():
    big = {"text": "x" * SESSION_CONTROL_KEY_ARG_MAX_BYTES}
    assert any("bytes as JSON" in e for e in _key_errors([{"id": "a", "label": "x", "arg": big}]))
    assert _key_errors([{"id": "a", "label": "x", "arg": {"view": "questions"}}]) == []
