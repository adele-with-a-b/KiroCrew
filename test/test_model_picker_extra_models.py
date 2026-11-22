"""``dashboard.model_picker_extra_models``: ids a backend runs but does not advertise."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.agents_cc_model_rows import with_extra_models

_EXTRA = "vendor.model-unlisted-9[1m]"


@pytest.fixture()
def cfg_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=path):
        yield path


def test_default_is_empty():
    assert KiroCrewConfig().dashboard.model_picker_extra_models == {}


def test_loader_keeps_valid_ids_per_backend(cfg_file):
    cfg_file.write_text(
        json.dumps(
            {
                "dashboard": {
                    "model_picker_extra_models": {
                        "claude": [f" {_EXTRA} ", _EXTRA, "auto", "", "has space", 7],
                        "codex": "not-a-list",
                        "pi": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert KiroCrewConfig.load().dashboard.model_picker_extra_models == {"claude": [_EXTRA]}


def test_loader_ignores_a_non_object(cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"model_picker_extra_models": [_EXTRA]}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().dashboard.model_picker_extra_models == {}


def test_extra_ids_append_after_the_advertised_rows_once():
    rows = [{"model_name": "auto"}, {"model_name": "opus"}]
    dashboard = SimpleNamespace(model_picker_extra_models={"claude": ["opus", _EXTRA]})
    out = with_extra_models(rows, dashboard, "claude")
    assert [r["model_name"] for r in out] == ["auto", "opus", _EXTRA]
    assert out[-1]["display_name"] == _EXTRA
    assert out[-1]["context_window"] > 0


def test_another_backends_extras_are_not_added():
    rows = [{"model_name": "auto"}]
    dashboard = SimpleNamespace(model_picker_extra_models={"codex": [_EXTRA]})
    assert with_extra_models(rows, dashboard, "claude") == rows


@pytest.mark.asyncio
async def test_api_models_lists_the_extra_id_on_the_claude_backend(cfg_file):
    from kiro_crew.dashboard.handlers import agents

    cfg_file.write_text(
        json.dumps(
            {
                "agent": {"acp_backend": "claude"},
                "dashboard": {"model_picker_extra_models": {"claude": [_EXTRA]}},
            }
        ),
        encoding="utf-8",
    )
    app = web.Application()
    app.router.add_get("/api/models", agents.api_models)
    advertised = [{"model_name": "auto", "display_name": "Auto", "description": ""}]
    with (
        patch.object(agents, "_cc_models", return_value=advertised),
        patch.object(agents, "_scoped_default", return_value=""),
    ):
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.get("/api/models")
            assert resp.status == 200
            names = [r["model_name"] for r in await resp.json()]
    assert names == ["auto", _EXTRA]
