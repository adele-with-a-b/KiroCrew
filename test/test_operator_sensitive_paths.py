"""KIROCREW_EXTRA_SENSITIVE_PATHS adds the operator's own secret folders to the floor."""

from __future__ import annotations

from kiro_crew.security.operator_sensitive import operator_sensitive_paths


def test_literal_entries_are_kept_even_when_missing(tmp_path):
    assert operator_sensitive_paths(".svc/env:.tool/auth", home=str(tmp_path)) == [
        ".svc/env",
        ".tool/auth",
    ]


def test_a_star_expands_to_the_folders_that_exist(tmp_path):
    for name in ("a", "b"):
        (tmp_path / ".oc" / name / "auth").mkdir(parents=True)
    (tmp_path / ".oc" / "c").mkdir()
    assert operator_sensitive_paths(".oc/*/auth", home=str(tmp_path)) == [
        ".oc/a/auth",
        ".oc/b/auth",
    ]


def test_absolute_parent_and_empty_entries_are_dropped(tmp_path):
    raw = "/etc:../up:a/../b:a//b:.:: .ok "
    assert operator_sensitive_paths(raw, home=str(tmp_path)) == [".ok"]


def test_unset_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_EXTRA_SENSITIVE_PATHS", raising=False)
    assert operator_sensitive_paths(home=str(tmp_path)) == []
