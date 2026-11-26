"""``dashboard.file_links_open_default_app``: where a chat file link opens.

On by default: a click opens the file in the default app for its type, and off
it opens in the side panel. The PUT/GET wiring is covered by the boolean-field
tables in ``test_dashboard_files_coverage.py``; this file covers the default,
the load path and the generated schema the Settings page reads.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig

FIELD = "file_links_open_default_app"


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


def test_default_on():
    assert getattr(KiroCrewConfig().dashboard, FIELD) is True


def test_absent_key_loads_on(cfg_file):
    assert getattr(KiroCrewConfig.load().dashboard, FIELD) is True


def test_save_load_round_trip_off(cfg_file):
    cfg = KiroCrewConfig()
    setattr(cfg.dashboard, FIELD, False)
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"][FIELD] is False
    assert getattr(KiroCrewConfig.load().dashboard, FIELD) is False


def test_non_bool_falls_back_to_the_default(cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {FIELD: "no"}}), encoding="utf-8")
    assert getattr(KiroCrewConfig.load().dashboard, FIELD) is True


def test_in_generated_schema():
    from kiro_crew.config.schema import JSON_SCHEMA

    node = JSON_SCHEMA["properties"]["dashboard"]["properties"][FIELD]
    assert node["type"] == "boolean"
    assert node["default"] is True
    assert node["x-meta"]["label"]
    assert "do not save back" in node["x-meta"]["help"]
