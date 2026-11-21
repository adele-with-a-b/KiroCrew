"""<crew home>/extra_sensitive_paths.txt adds the operator's own secret folders to the floor."""

from __future__ import annotations

from kiro_crew.security.operator_sensitive import FILE_NAME, operator_sensitive_paths


def test_literal_entries_are_kept_even_when_missing(tmp_path):
    raw = "# my secrets\n.svc/env\n.tool/auth   # helper\n"
    assert operator_sensitive_paths(raw, home=str(tmp_path)) == [".svc/env", ".tool/auth"]


def test_a_star_expands_to_the_folders_that_exist(tmp_path):
    for name in ("a", "b"):
        (tmp_path / ".oc" / name / "auth").mkdir(parents=True)
    (tmp_path / ".oc" / "c").mkdir()
    assert operator_sensitive_paths(".oc/*/auth", home=str(tmp_path)) == [
        ".oc/a/auth",
        ".oc/b/auth",
    ]


def test_absolute_parent_and_empty_entries_are_dropped(tmp_path):
    raw = "/etc\n../up\na/../b\na//b\n.\n\n .ok \n"
    assert operator_sensitive_paths(raw, home=str(tmp_path)) == [".ok"]


def test_the_file_is_read_from_the_crew_home(tmp_path, monkeypatch):
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    assert operator_sensitive_paths(home=str(tmp_path)) == []
    (crew / FILE_NAME).write_text(".svc/env\n", encoding="utf-8")
    assert operator_sensitive_paths(home=str(tmp_path)) == [".svc/env"]


def test_the_declaration_file_itself_is_on_the_floor(tmp_path, monkeypatch):
    from kiro_crew.security.paths import is_sensitive_path

    crew = tmp_path / ".kiro" / "crew"
    crew.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    assert is_sensitive_path(str(crew / FILE_NAME))
