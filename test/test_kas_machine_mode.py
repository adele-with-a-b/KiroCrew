"""KAS machine mode: the engine reads a host-issued token file, behind ``crew_client``.

Off (the preview off, or ``agent.kas_auth`` left at ``acp-callback``) a spawn is
the one ``main`` makes. On, the bundle argv carries the machine flags, Crew answers
no credential callback, the token variable is dropped from the engine's env, and a
missing file or cell refuses before any install. Crew never opens the token file.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from kiro_crew import crew_client_preview
from kiro_crew.acp import kas_engine_auth
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_transport import KAS_BUNDLE_AUTH_ARG
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.agent_sdk import kas_machine
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.config.sections_agent_fields import KAS_MACHINE_TOKEN_PATH_DEFAULT

_ENDPOINT = "https://runtime.example.test"
_CONTROL = "https://control.example.test"
_REGION = "example-region-1"


def _agent(**over):
    base = dict(
        acp_backend=ACP_BACKEND_KAS,
        kas_auth="machine",
        kas_token_path="",
        kas_endpoint=_ENDPOINT,
        kas_control_plane_endpoint=_CONTROL,
        kas_region=_REGION,
        kas_machine_token_env="EXAMPLE_MACHINE_TOKEN",
        kas_machine_env_drop=["EXAMPLE_DROP"],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _cfg(agent, on=True):
    previews = {"crew_client": True} if on else {}
    return SimpleNamespace(agent=agent, dashboard=SimpleNamespace(feature_previews=previews))


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "machine-token"
    path.write_text("x", encoding="utf-8")
    return str(path)


@pytest.fixture
def configured(monkeypatch):
    """Point the config reads at *cfg* (preview and agent section)."""

    def set_cfg(cfg):
        monkeypatch.setattr(crew_client_preview, "_config", lambda c=None: c or cfg)
        monkeypatch.setattr(
            kas_machine, "configured_machine_auth", lambda: kas_machine.machine_auth_in(cfg)
        )

    return set_cfg


def test_the_token_path_has_no_default():
    assert KAS_MACHINE_TOKEN_PATH_DEFAULT == ""


@pytest.mark.parametrize(
    "agent,on,expect",
    [
        (_agent(), True, True),
        (_agent(), False, False),  # the preview off: callback mode whatever kas_auth says
        (_agent(kas_auth="acp-callback"), True, False),
        (_agent(acp_backend="kiro"), True, False),
    ],
)
def test_machine_mode_needs_the_preview_the_kas_backend_and_machine(agent, on, expect):
    assert (kas_machine.machine_auth_in(_cfg(agent, on)) is not None) is expect


@pytest.mark.parametrize(
    "over,refusal",
    [
        (dict(kas_token_path=""), kas_machine.MACHINE_TOKEN_MISSING),
        (dict(kas_endpoint="http://runtime.example.test"), kas_machine.MACHINE_CELL_MISSING),
        (dict(kas_control_plane_endpoint=""), kas_machine.MACHINE_CELL_MISSING),
        (dict(kas_region=""), kas_machine.MACHINE_CELL_MISSING),
    ],
)
def test_a_spawn_without_a_file_or_a_cell_is_refused(over, refusal, token_file):
    over.setdefault("kas_token_path", token_file)
    machine = kas_machine.machine_auth(_agent(**over))
    with pytest.raises(kas_machine.MachineAuthRefused) as caught:
        kas_machine.check_spawnable(machine)
    assert caught.value.refusal == refusal


def test_the_file_is_checked_without_being_opened(token_file, monkeypatch):
    import builtins

    monkeypatch.setattr(builtins, "open", lambda *a, **k: pytest.fail("token file opened"))
    assert kas_machine.token_file_present(token_file)
    assert not kas_machine.token_file_present("relative/path")


def _ctx(tmp_path):
    return SpawnContext(agent="kirocrew", work_dir=tmp_path, model=None, environ={}, home=tmp_path)


@pytest.fixture
def bundle_installed(monkeypatch, tmp_path):
    installed = SimpleNamespace(
        version="0.0.0",
        node_bin=str(tmp_path / "node"),
        acp_server=str(tmp_path / "acp-server.js"),
        node_version="0",
    )
    monkeypatch.setattr("kiro_crew.acp.kas_bundle.installed_bundle", lambda: installed)
    monkeypatch.setattr("kiro_crew.acp.harness.kas.ensure_engine_bundle_cache", lambda argv: None)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._chat_sharing_preview_on", lambda: False)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: True)


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.asyncio
async def test_the_bundle_spawn(configured, bundle_installed, token_file, tmp_path, on):
    configured(_cfg(_agent(kas_token_path=token_file), on))
    harness = KasHarness()
    plan = await harness.resolve_spawn(_ctx(tmp_path))
    env = {"EXAMPLE_MACHINE_TOKEN": "x", "EXAMPLE_DROP": "y", "KEEP": "z"}
    harness.apply_spawn_env(env, spawned_binary=None)
    if not on:
        assert KAS_BUNDLE_AUTH_ARG in plan.argv and plan.host_auth is True
        assert "--auth=machine" not in plan.argv and "EXAMPLE_MACHINE_TOKEN" in env
        return
    assert KAS_BUNDLE_AUTH_ARG not in plan.argv and plan.host_auth is False
    at = plan.argv.index("--auth=machine")
    assert plan.argv[at : at + 5] == [
        "--auth=machine",
        f"--token-path={token_file}",
        f"--endpoint={_ENDPOINT}",
        f"--control-plane-endpoint={_CONTROL}",
        f"--region={_REGION}",
    ]
    assert "EXAMPLE_MACHINE_TOKEN" not in env and "EXAMPLE_DROP" not in env
    assert env["KEEP"] == "z"


@pytest.mark.asyncio
async def test_a_missing_file_refuses_before_any_install(configured, monkeypatch, tmp_path):
    configured(_cfg(_agent()))
    monkeypatch.setattr("kiro_crew.acp.harness.kas._chat_sharing_preview_on", lambda: False)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: True)
    monkeypatch.setattr(
        "kiro_crew.acp.kas_bundle.installed_bundle", lambda: pytest.fail("install asked")
    )
    with pytest.raises(AcpRuntimeError, match=kas_machine.MACHINE_TOKEN_MISSING):
        await KasHarness().resolve_spawn(_ctx(tmp_path))


@pytest.mark.asyncio
async def test_the_relay_route_refuses(configured, monkeypatch, token_file, tmp_path):
    configured(_cfg(_agent(kas_token_path=token_file)))
    monkeypatch.setattr("kiro_crew.acp.harness.kas._chat_sharing_preview_on", lambda: False)
    monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: False)
    with pytest.raises(AcpRuntimeError, match=kas_machine.MACHINE_ROUTE_NOT_BUNDLE):
        await KasHarness().resolve_spawn(_ctx(tmp_path))


def test_an_edition_provider_wins_over_machine_mode(configured, token_file, monkeypatch):
    configured(_cfg(_agent(kas_token_path=token_file)))
    edition = object()
    kas_engine_auth.register_engine_auth(lambda: edition)
    assert kas_engine_auth.active_engine_auth() is edition
    kas_engine_auth.register_engine_auth(None)
    assert isinstance(kas_engine_auth.active_engine_auth(), kas_machine.MachineEngineAuth)


def test_a_refused_start_names_the_engine_class():
    auth = kas_machine.MachineEngineAuth(kas_machine.machine_auth(_agent()))
    said = auth.start_refusal("engine said TokenInvalidError", -32603)
    assert said == "Kiro refused the machine token: TokenInvalidError (-32603)"
    assert auth.start_refusal("something else", -32603) == ""


@pytest.mark.parametrize("present", [True, False])
def test_whoami_in_machine_mode(configured, token_file, capsys, present):
    from kiro_crew import cli_kas_login

    configured(_cfg(_agent(kas_token_path=token_file if present else "")))
    code = cli_kas_login._whoami(argparse.Namespace(format="json"))
    out = capsys.readouterr()
    if present:
        assert code == 0 and json.loads(out.out) == {"accountType": "machine"}
    else:
        assert code == 1 and kas_machine.MACHINE_TOKEN_MISSING in out.err


def test_the_catalog_is_keyed_by_the_token_file(configured, token_file):
    configured(_cfg(_agent(kas_token_path=token_file)))
    assert kas_machine.catalog_identity() == "machine:" + token_file
    configured(_cfg(_agent(kas_token_path=token_file), on=False))
    assert kas_machine.catalog_identity() == ""


def test_the_gateway_line_names_the_variable_only():
    machine = kas_machine.machine_auth(_agent())
    line = kas_machine.gateway_env_line(machine, {"EXAMPLE_MACHINE_TOKEN": "value"})
    assert line == "machine auth: EXAMPLE_MACHINE_TOKEN in gateway env present=yes"
    assert "value" not in line
