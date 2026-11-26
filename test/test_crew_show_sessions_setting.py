"""A Bot's sessions stay in the person's own session list unless a switch leaves them out.

The sessions a live Bot thread opens are listed on its Sessions tab either way
(``crew_mode.list_view``, ``test_chat_slots_crew_view``). While
``dashboard.show_bot_sessions_in_list`` is on, every Bot's are listed in the
person's list too and the per-Bot switches do not apply; while it is off, each
Bot's own ``show_sessions_in_list`` decides for that Bot, on unless its config says
otherwise. These tests pin what the two switches rest on, in the shape of
``test_crew_card_send_setting``: the defaults and the config round trip, junk
reading as the default, the APIs refusing a non-bool, the stamp on the mate
thread's row, and the slot list re-sent when either switch flips.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import crew_mode
from kiro_crew.config.live import ConfigChange
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def test_defaults_to_on_and_round_trips():
    assert KiroCrewAgentConfig(kiro_agent="x").show_sessions_in_list is True
    cfg = KiroCrewConfig.load()
    cfg.agents["hidden"] = KiroCrewAgentConfig(kiro_agent="kirocrew", show_sessions_in_list=False)
    cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()
    again = KiroCrewConfig.load()
    assert again.agents["hidden"].show_sessions_in_list is False
    assert again.agents["plain"].show_sessions_in_list is True


def test_a_bot_whose_config_omits_the_switch_reads_it_as_on():
    cfg = KiroCrewConfig.load()
    cfg.agents["ada"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="default")
    cfg.save()
    path = config_dir() / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agents"]["ada"].pop("show_sessions_in_list", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert KiroCrewConfig.load().agents["ada"].show_sessions_in_list is True


@pytest.mark.parametrize(
    ("stored", "want"), [(True, True), (False, False), ("yes", True), (0, True)]
)
def test_a_stored_per_bot_switch_loads_only_a_real_bool(stored, want):
    cfg = KiroCrewConfig.load()
    cfg.agents["weird"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()
    path = config_dir() / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agents"]["weird"]["show_sessions_in_list"] = stored
    path.write_text(json.dumps(data), encoding="utf-8")
    assert KiroCrewConfig.load().agents["weird"].show_sessions_in_list is want


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.fixture()
def seeded_agent():
    cfg = KiroCrewConfig.load()
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    return "existing"


@pytest.mark.asyncio
async def test_the_switch_persists_off_and_on(seeded_agent):
    async with TestClient(TestServer(_crud_app())) as client:
        off = await client.put(f"/api/agents/{seeded_agent}", json={"show_sessions_in_list": False})
        assert off.status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].show_sessions_in_list is False
        on = await client.put(f"/api/agents/{seeded_agent}", json={"show_sessions_in_list": True})
        assert on.status == 200
    assert KiroCrewConfig.load().agents[seeded_agent].show_sessions_in_list is True


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", ["true", 1, None])
async def test_non_bool_is_refused_and_writes_nothing(seeded_agent, junk):
    async with TestClient(TestServer(_crud_app())) as client:
        resp = await client.put(
            f"/api/agents/{seeded_agent}",
            json={"workspace": "other", "show_sessions_in_list": junk},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_show_sessions_in_list"
    stored = KiroCrewConfig.load().agents[seeded_agent]
    assert stored.show_sessions_in_list is True
    assert stored.workspace == "default"


def _rows(listed: object) -> list[dict]:
    mate = {"key": "dm-ada", "mode": "member", "agent": "ada"}
    if listed is not None:
        mate[crew_mode.MATE_LISTED_FIELD] = listed
    return [
        mate,
        {"key": "chat-1", crew_mode.MATE_ROOT_FIELD: "dm-ada"},
        {"key": "chat-2", crew_mode.MATE_ROOT_FIELD: "dm-gone"},
        {"key": "chat-3"},
    ]


@pytest.mark.parametrize("listed", [None, False, "yes"])
def test_a_live_mate_threads_sessions_leave_a_persons_list_unless_listed(listed) -> None:
    assert [r["key"] for r in crew_mode.list_view(_rows(listed), "", agent=False)] == [
        "dm-ada",
        "chat-2",
        "chat-3",
    ]


def test_a_listed_mate_thread_keeps_its_sessions_in_a_persons_list() -> None:
    assert [r["key"] for r in crew_mode.list_view(_rows(True), "", agent=False)] == [
        "dm-ada",
        "chat-1",
        "chat-2",
        "chat-3",
    ]


def test_a_stamp_under_another_name_changes_nothing() -> None:
    rows = _rows(None)
    rows[0]["show_sessions_in_list"] = True
    assert "chat-1" not in [r["key"] for r in crew_mode.list_view(rows, "", agent=False)]


@pytest.mark.parametrize(
    ("everyone", "own", "stamped"),
    [
        (True, False, True),
        (True, True, True),
        (False, True, True),
        (False, False, False),
        ("yes", "yes", False),
    ],
)
def test_stamp_rows_lists_a_bots_sessions_when_global_is_on_else_by_its_own_switch(
    monkeypatch: pytest.MonkeyPatch, everyone, own, stamped
) -> None:
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(show_bot_sessions_in_list=everyone),
        agents={"ada": SimpleNamespace(show_sessions_in_list=own)},
    )
    monkeypatch.setattr(crew_mode, "_known_mate_roots", set())
    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    rows = [
        {"key": "dm-ada", "mode": "member", "agent": "ada"},
        {"key": "chat-1", "created_by": "dm-ada", crew_mode.MATE_LISTED_FIELD: True},
        {"key": "chat-3"},
    ]
    crew_mode.stamp_rows(rows, parents={}, tree_slots=set())
    assert rows[0][crew_mode.MATE_LISTED_FIELD] is stamped
    assert rows[1][crew_mode.MATE_ROOT_FIELD] == "dm-ada"
    assert crew_mode.MATE_LISTED_FIELD not in rows[1]
    assert crew_mode.MATE_LISTED_FIELD not in rows[2]


def test_a_bot_with_no_record_is_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(dashboard=SimpleNamespace(show_bot_sessions_in_list=False), agents={})
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    assert crew_mode.mate_lists_sessions("ada") is False


def test_the_global_switch_defaults_off_and_loads_only_a_real_bool() -> None:
    assert KiroCrewConfig().dashboard.show_bot_sessions_in_list is False
    path = config_dir() / "config.json"
    for stored, want in [(True, True), ("yes", False), (1, False)]:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.setdefault("dashboard", {})["show_bot_sessions_in_list"] = stored
        path.write_text(json.dumps(data), encoding="utf-8")
        assert KiroCrewConfig.load().dashboard.show_bot_sessions_in_list is want


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("dashboard.show_bot_sessions_in_list", "dashboard.show_bot_sessions_in_list"),
        ("agents.show_sessions_in_list", "agents.ada.show_sessions_in_list"),
    ],
)
def test_a_flip_of_either_switch_resends_the_slot_list(name, path) -> None:
    from kiro_crew.dashboard.server import _register_config_watch

    app = web.Application()
    state = SimpleNamespace(
        workflow_service=None,
        channel_manager=None,
        set_dynamic_cards_enabled=MagicMock(),
        push_slots_update=MagicMock(),
        push_refresh=MagicMock(),
    )
    _register_config_watch(app, state, None)
    subs = {s.name: s for s in app["config_watch_subscriptions"]}
    apply = subs[name].callback()
    cfg = KiroCrewConfig()
    apply(ConfigChange(old=cfg, new=cfg, changed=frozenset({"agents.ada.display_name"})))
    state.push_slots_update.assert_not_called()
    state.push_refresh.assert_not_called()
    apply(ConfigChange(old=cfg, new=cfg, changed=frozenset({path})))
    state.push_slots_update.assert_called_once_with()
    state.push_refresh.assert_called_once_with("agents")
