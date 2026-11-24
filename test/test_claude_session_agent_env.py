"""A claude session names its agent to its ``claude`` child as ``KIROCREW_SESSION_AGENT``.

The value is the client's ``_agent``, which Crew sets from the session's own
record. It rides the ``session/new`` envelope in ``_meta.claudeCode.options.env``
(spread over the adapter's process env for that session's child only) and in the
flag tier's ``settings.env``, which Claude Code applies after every settings
file's ``env``, so a settings file a chat writes cannot rename the agent. It is
always set, so a value inherited from a parent process is overwritten. Both
claude paths build that envelope through ``_harness_session_meta``: the
process-per-session client and a session on the shared runtime.
``KIROCREW_RUNTIME_AGENT``, the runtime process's own agent, is left as it was.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import test_acp_session_mcp_client_seam as client_seam
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _write_spec

from kiro_crew.acp import client_claude_session
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import claude as claude_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.constants import KIROCREW_RUNTIME_AGENT_ENV, KIROCREW_SESSION_AGENT_ENV

_VAR = "KIROCREW_SESSION_AGENT"


def _named(options: dict) -> tuple[str | None, str | None]:
    """The agent in the process-env tier and in the flag tier."""
    return (
        (options.get("env") or {}).get(_VAR),
        ((options.get("settings") or {}).get("env") or {}).get(_VAR),
    )


def _options(client: Any) -> dict:
    return client._harness_session_meta([])["claudeCode"]["options"]


def test_the_variable_is_its_own_not_the_runtimes():
    assert KIROCREW_SESSION_AGENT_ENV == _VAR
    assert KIROCREW_RUNTIME_AGENT_ENV != _VAR


def test_the_envelope_leaves_the_runtime_variable_alone(tmp_path):
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    options = _options(client)
    assert KIROCREW_RUNTIME_AGENT_ENV not in options["env"]
    assert KIROCREW_RUNTIME_AGENT_ENV not in options["settings"]["env"]


def test_a_session_names_its_own_agent_in_both_tiers(tmp_path):
    client = AcpClient(
        work_dir=tmp_path, agent="kirocrew-conductor", acp_backend=ACP_BACKEND_CLAUDE
    )
    assert _named(_options(client)) == ("kirocrew-conductor", "kirocrew-conductor")


def test_a_deprecated_alias_is_named_by_the_agent_it_emits(tmp_path):
    """``kirocrew-ledger-conductor`` runs the Conductor's spec, so it is named as one."""
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    client._agent = "kirocrew-ledger-conductor"
    assert _named(_options(client)) == ("kirocrew-conductor", "kirocrew-conductor")


def test_a_session_with_no_agent_carries_it_empty_never_absent(tmp_path):
    """A client whose ``_agent`` is empty: absent would let a value inherited from a
    parent process through, had the spawn not dropped it."""
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    client._agent = ""
    assert _named(_options(client)) == ("", "")


def test_the_bare_options_builder_is_unchanged(tmp_path):
    """Only the wire envelope names the agent; the options builder stays as it was."""
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    assert client._claude_session_meta()["claudeCode"]["options"] == {}


def test_a_settings_tier_cannot_rename_the_agent(tmp_path):
    """Inline settings (a declined project file's carried tier) naming another agent
    are overridden in the flag tier, and the shared inline dict is not mutated."""
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    inline = {"env": {_VAR: "kirocrew-conductor", "AWS_REGION": "us-west-2"}}
    client._claude_local_settings_excluded = True
    client._claude_inline_settings = inline
    options = _options(client)
    assert options["settings"]["env"] == {_VAR: "kirocrew", "AWS_REGION": "us-west-2"}
    assert options["env"] == {_VAR: "kirocrew"}
    assert inline["env"][_VAR] == "kirocrew-conductor"


def test_the_agent_specs_own_env_cannot_rename_the_agent(tmp_path, monkeypatch):
    """The spec's ``claudeCode.env`` block lands before the name is written."""

    def _spec_env(options: dict, _agent: Any) -> None:
        options["env"] = {_VAR: "kirocrew-conductor", "FOO": "1"}

    monkeypatch.setattr(client_claude_session, "apply_spec_claude_options", _spec_env)
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    options = _options(client)
    assert options["env"] == {_VAR: "kirocrew", "FOO": "1"}
    assert _named(options) == ("kirocrew", "kirocrew")


@pytest.mark.asyncio
async def test_the_session_new_request_names_the_agent(tmp_path, agents_dir, monkeypatch):
    """On the wire. That Claude Code applies the flag tier over a chat-written
    settings file is its documented order, not run here."""
    _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    client._write_claude_local_settings()
    sent, killed = client_seam.TestClientSeam._adapter_transport(
        client, monkeypatch, started_mode="default"
    )

    await client._initialize_session()

    assert killed == []
    options = dict(sent)["session/new"]["_meta"]["claudeCode"]["options"]
    assert _named(options) == ("kirocrew", "kirocrew")


# ── The shared runtime ───────────────────────────────────────────────────────


@pytest.fixture
def harness(monkeypatch):
    async def _argv() -> list[str]:
        return ["/opt/bin/claude-agent-acp"]

    monkeypatch.setattr(claude_mod, "_resolve_claude_argv", _argv)
    monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", lambda _argv: "0.94.0")
    return harness_for(ACP_BACKEND_CLAUDE)


async def _shared_state(harness: Any, work_dir: Path, agent: str | None) -> Any:
    work_dir.mkdir()
    state = await harness.prepare_session(
        work_dir=work_dir,
        agent=agent,
        model="",
        session_key=f"dashboard:{work_dir.name}",
        channel_id="",
        memory_mode="persistent",
    )
    await harness.adopt_session_spec(state, None)
    return state


def _child_env(process_env: dict[str, str], options: dict) -> dict[str, str]:
    """The env a session's ``claude`` child runs with: claude-agent-acp spreads the
    envelope's ``env`` over its own process env (``{...process.env, ...options.env}``,
    0.109.0 ``v1/acp-agent.js`` and ``v2/agent.js``), and Claude Code copies the
    flag tier's ``settings.env`` over that last."""
    return {
        **process_env,
        **(options.get("env") or {}),
        **((options.get("settings") or {}).get("env") or {}),
    }


def test_a_worker_on_a_runtime_a_conductor_founded_gets_its_own_agent(
    harness, tmp_path, agents_dir
):
    """Even with ``kirocrew-conductor`` in the process env (the spawn builders drop
    it; this holds without them), a worker session's child runs as the worker."""
    founder_env = {_VAR: "kirocrew-conductor", "PATH": "/usr/bin"}
    _write_spec(agents_dir, servers={}, tools=None, name="my-worker")
    state = asyncio.run(_shared_state(harness, tmp_path / "w", "my-worker"))
    try:
        meta = harness.session_meta(state, [], zero_tools=False, stub_token="tok-w")
        assert _child_env(founder_env, meta["claudeCode"]["options"])[_VAR] == "my-worker"
    finally:
        asyncio.run(harness.end_session(state))


def test_a_chat_with_no_agent_replaces_the_inherited_value(tmp_path):
    """A client whose ``_agent`` is empty: an inherited ``kirocrew-conductor`` is
    replaced by an empty value, the only removal ``options.env`` can express."""
    client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
    client._agent = ""
    child = _child_env({_VAR: "kirocrew-conductor"}, _options(client))
    assert child[_VAR] == ""


@pytest.mark.asyncio
async def test_a_gateway_started_in_a_conductor_chat_gives_its_chats_their_own_agent(
    tmp_path, agents_dir, monkeypatch
):
    """The gateway's own env holds ``kirocrew-conductor`` (it was started from a
    Conductor chat's shell); the worker chat's ``session/new`` still names the
    worker, even over that env."""
    monkeypatch.setenv(_VAR, "kirocrew-conductor")
    _write_spec(
        agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"], name="my-worker"
    )
    client = AcpClient(work_dir=tmp_path, agent="my-worker", acp_backend=ACP_BACKEND_CLAUDE)
    client._write_claude_local_settings()
    sent, killed = client_seam.TestClientSeam._adapter_transport(
        client, monkeypatch, started_mode="default"
    )

    await client._initialize_session()

    assert killed == []
    options = dict(sent)["session/new"]["_meta"]["claudeCode"]["options"]
    assert _child_env(dict(os.environ), options)[_VAR] == "my-worker"


def test_two_sessions_on_one_process_each_name_their_own_agent(harness, tmp_path, monkeypatch):
    """A Conductor chat and a worker chat through the same harness, under a process
    env that holds ``kirocrew-conductor``: each envelope names its own agent, beside
    the identity the harness adds."""
    monkeypatch.setenv(_VAR, "kirocrew-conductor")

    async def _run() -> tuple[Any, Any]:
        return (
            await _shared_state(harness, tmp_path / "a", "kirocrew-conductor"),
            await _shared_state(harness, tmp_path / "b", "kirocrew"),
        )

    conductor, plain = asyncio.run(_run())
    try:
        one = harness.session_meta(conductor, [], zero_tools=False, stub_token="tok-1")
        two = harness.session_meta(plain, [], zero_tools=False, stub_token="tok-2")
        one_opts, two_opts = one["claudeCode"]["options"], two["claudeCode"]["options"]
        assert _named(one_opts) == ("kirocrew-conductor", "kirocrew-conductor")
        assert _named(two_opts) == ("kirocrew", "kirocrew")
        assert two_opts["env"]["KIROCREW_SESSION_KEY"] == "dashboard:b"
    finally:
        asyncio.run(harness.end_session(conductor))
        asyncio.run(harness.end_session(plain))


def test_a_shared_session_with_no_agent_runs_and_names_the_default(harness, tmp_path):
    """``ClaudeSharedSession`` runs a session with no agent as ``kirocrew``, as the
    runtime does, so that is the name it carries."""
    state = asyncio.run(_shared_state(harness, tmp_path / "a", None))
    try:
        meta = harness.session_meta(state, [], zero_tools=False, stub_token="")
        assert _named(meta["claudeCode"]["options"]) == ("kirocrew", "kirocrew")
    finally:
        asyncio.run(harness.end_session(state))


# ── No process names one session's agent: the spawn builders drop an inherited one ──


def test_a_session_child_never_inherits_another_sessions_agent():
    """``AcpClient``'s spawn env (every backend it serves): the gateway's own
    ``kirocrew-conductor`` is dropped, and a claude session names its own on the
    envelope after the spawn."""
    client = object.__new__(AcpClient)
    client._session_key = "dashboard:w"
    client._stub_session_token = "tok"
    env = {_VAR: "kirocrew-conductor", "PATH": "/usr/bin"}
    client._apply_session_identity_env(env)
    assert _VAR not in env
    assert env["KIROCREW_SESSION_KEY"] == "dashboard:w"


def test_a_runtime_process_never_inherits_another_sessions_agent(tmp_path):
    """The shared runtime's real launch, under a parent env that holds
    ``kirocrew-conductor``: the value reaches the env builder and is gone from the
    env the child is spawned with, beside the runtime's own agent."""
    import dataclasses
    from unittest.mock import patch

    import acp_launch_capture as capture_mod

    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX

    seen: list[tuple[str | None, str | None, str | None]] = []
    real_launch = runtime_mod.launch

    async def _observed_launch(host, request, tools):
        def _mark(env):
            before = env.get(_VAR)
            request.env_after_marker(env)
            seen.append((before, env.get(_VAR), env.get(KIROCREW_RUNTIME_AGENT_ENV)))

        observed = dataclasses.replace(request, env_after_marker=_mark)
        return await real_launch(host, observed, tools)

    capture_mod.capture(
        ACP_BACKEND_CODEX,
        tmp_path,
        extra_patches=(
            patch.dict(os.environ, {_VAR: "kirocrew-conductor"}),
            patch.object(runtime_mod, "launch", new=_observed_launch),
        ),
    )
    assert len(seen) == 1
    before, after, runtime_agent = seen[0]
    assert before == "kirocrew-conductor"
    assert after is None
    assert runtime_agent


def test_a_worker_session_on_a_conductor_runtime_names_the_worker(harness, tmp_path, agents_dir):
    """``AcpRuntime.create_session`` on a process a Conductor founded: the worker
    session's ``session/new`` names the worker, not the process's agent."""
    from test_claude_shared_runtime import _claude_runtime, _Plane

    _write_spec(agents_dir, servers={}, tools=None, name="my-worker")
    plane = _Plane({"sessionId": "s-w", "modes": {"currentModeId": "default"}})
    rt = _claude_runtime(plane)
    rt._agent = "kirocrew-conductor"

    async def _run() -> None:
        await rt.create_session(cwd=str(tmp_path), agent="my-worker")
        await rt.terminate_session("s-w")

    asyncio.run(_run())
    new = [p for m, p in plane.sent if m == "session/new"]
    assert len(new) == 1
    assert _named(new[0]["_meta"]["claudeCode"]["options"]) == ("my-worker", "my-worker")


def test_a_resumed_session_names_the_agent_its_skill_view_was_built_from(
    harness, tmp_path, agents_dir, monkeypatch
):
    """``AcpRuntime.load_session`` of a shared-runtime session stored under a skill
    view: its ``session/load`` names the source agent, as ``session/new`` would."""
    from test_claude_shared_runtime import _claude_runtime, _LoadPlane

    from kiro_crew.acp import skill_projection

    async def _resolve(agent: str) -> str:
        return "my-worker" if agent == "my-worker-view" else agent

    monkeypatch.setattr(skill_projection, "resolve_source_agent", _resolve)
    _write_spec(agents_dir, servers={}, tools=None, name="my-worker")
    plane = _LoadPlane()
    rt = _claude_runtime(plane)
    rt._agent = "kirocrew-conductor"
    rt._can_load_session = True

    async def _run() -> None:
        await rt.load_session("", "s-5", cwd=str(tmp_path), agent="my-worker-view")
        await rt.terminate_session("s-5")

    asyncio.run(_run())
    loads = [p for m, p in plane.sent if m == "session/load"]
    assert len(loads) == 1
    assert _named(loads[0]["_meta"]["claudeCode"]["options"]) == ("my-worker", "my-worker")
