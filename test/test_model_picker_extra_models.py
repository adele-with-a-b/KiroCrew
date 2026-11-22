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
from kiro_crew.config.picker_extra_models import extra_ids_for
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


def _key(model: str) -> str:
    from kiro_crew.dashboard.handlers.agents import _normalize_model_key

    return _normalize_model_key(model)


def test_extra_ids_append_after_the_advertised_rows_once():
    rows = [{"model_name": "auto"}, {"model_name": "opus"}]
    out = with_extra_models(rows, ["opus", _EXTRA, _EXTRA], _key)
    assert [r["model_name"] for r in out] == ["auto", "opus", _EXTRA]
    assert out[-1]["display_name"] == _EXTRA
    assert out[-1]["context_window"] > 0


def test_an_auto_spelling_adds_nothing():
    rows = [{"model_name": "auto"}]
    assert with_extra_models(rows, ["default", "Auto"], _key) == rows


def test_an_alias_and_its_versioned_model_both_keep_a_row_in_any_order():
    versioned = "global.anthropic.claude-opus-4-8[1m]"
    assert _key(versioned) == _key("opus")
    for rows, extras in (
        ([{"model_name": "opus"}], [versioned]),
        ([{"model_name": versioned}], ["opus"]),
        ([], ["opus", versioned]),
        ([], [versioned, "opus"]),
    ):
        names = [r["model_name"] for r in with_extra_models(rows, extras, _key)]
        assert sorted(names) == sorted(["opus", versioned])


def test_only_the_claude_backend_reads_its_extras():
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(model_picker_extra_models={"claude": [_EXTRA], "codex": [_EXTRA]})
    )
    assert extra_ids_for(cfg, "claude") == [_EXTRA]
    assert extra_ids_for(cfg, "codex") == []
    assert extra_ids_for(SimpleNamespace(agent=None), "claude") == []


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
