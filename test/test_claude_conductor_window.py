"""A Conductor chat on the Claude backend runs with its own context window.

``session.conductor_context_window`` (600000 by default) rides a Conductor chat's
session envelope as ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``, in
``_meta.claudeCode.options.env`` and in the flag tier's ``settings.env``, the
tier Claude Code reads after every settings file's ``env``. Every other session
(a worker chat, a Bot's own thread, a Bot's worker, a chat on another agent)
carries nothing, so it keeps the window Claude Code's own settings name. The
number is read from config when the session's envelope is built, on both wire
paths (``session/new`` and ``session/load`` build it through
``_harness_session_meta``).

Invariant: the variable is in a session's envelope exactly when its agent is a
conductor (``crew_mode.conductor_agents``), its key is not a Bot's, its backend is
claude and the setting is above 0; and then both tiers carry the same number.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import members
from kiro_crew.acp import client_claude_session
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS
from kiro_crew.config import config_path
from kiro_crew.config.loader import KiroCrewConfig

_VAR = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
CONDUCTOR = "kirocrew-conductor"
#: A dashboard chat's session key, as it reaches the session client.
CHAT = "dashboard:chat-7"
#: A Bot's own thread: the ``member-<slug>`` slot.
BOT_THREAD = "dashboard:member-kirocrew-conductor"
#: The setting's default and its upper bound, spelled out: the numbers are the contract.
DEFAULT = 600_000
MAX = 2_000_000


def _tokens(agent: str, key: str | None, backend: str) -> int:
    """What the context cap asks: the window Crew gives the session, 0 for none.

    Imported here so that, with the feature reverted, each test fails on its own.
    """
    from kiro_crew.acp.claude_conductor_window import conductor_window_tokens

    return conductor_window_tokens(agent, key, backend)


def _set(value: Any) -> None:
    """Write ``session.conductor_context_window`` to this test's config file."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session": {"conductor_context_window": value}}), encoding="utf-8")


def _client(tmp_path, agent: str = CONDUCTOR, key: str | None = CHAT) -> AcpClient:
    return AcpClient(
        work_dir=tmp_path, agent=agent, session_key=key, acp_backend=ACP_BACKEND_CLAUDE
    )


def _window(options: dict) -> tuple[str | None, str | None]:
    """The window in the process-env tier and in the flag tier."""
    return (
        (options.get("env") or {}).get(_VAR),
        ((options.get("settings") or {}).get("env") or {}).get(_VAR),
    )


def _options(client: AcpClient) -> dict:
    return client._claude_session_meta()["claudeCode"]["options"]


# ── the setting ──────────────────────────────────────────────────────────────


def test_the_default_is_600k():
    from kiro_crew.config import sections

    assert KiroCrewConfig.load().session.conductor_context_window == DEFAULT
    assert sections.CONDUCTOR_CONTEXT_WINDOW_DEFAULT == DEFAULT
    assert sections.CONDUCTOR_CONTEXT_WINDOW_MAX == MAX


@pytest.mark.parametrize(
    ("written", "read"),
    [
        (450_000, 450_000),
        (0, 0),
        (-5, 0),
        (MAX + 1, MAX),
        ("many", DEFAULT),
    ],
)
def test_the_setting_is_read_within_its_bounds(written, read):
    _set(written)
    assert KiroCrewConfig.load().session.conductor_context_window == read


# ── who gets it ──────────────────────────────────────────────────────────────


def test_a_conductor_chat_carries_the_window_in_both_tiers(tmp_path):
    assert _options(_client(tmp_path)) == {
        "env": {_VAR: "600000"},
        "settings": {"env": {_VAR: "600000"}},
    }


def test_both_wire_paths_carry_it(tmp_path):
    """``session/new`` and ``session/load`` send what ``_harness_session_meta`` builds."""
    options = _client(tmp_path)._harness_session_meta([])["claudeCode"]["options"]
    assert _window(options) == ("600000", "600000")


@pytest.mark.parametrize(
    "agent",
    [
        "kirocrew-ledger-conductor",
        "kirocrew-pipeline-conductor",
        "kirocrew-security-conductor",
        "kirocrew-team-lead",
    ],
)
def test_every_shipped_conductor_carries_it(tmp_path, agent):
    assert _window(_options(_client(tmp_path, agent))) == ("600000", "600000")


def test_the_configured_crew_mode_agent_carries_it(tmp_path):
    """``agent.crew_mode_agent`` names a conductor too (``crew_mode.conductor_agents``)."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": {"crew_mode_agent": "my-lead"}}), encoding="utf-8")
    cfg = KiroCrewConfig.load()

    from kiro_crew import crew_mode

    assert "my-lead" in crew_mode.conductor_agents(cfg)
    try:
        assert _window(_options(_client(tmp_path, "my-lead"))) == ("600000", "600000")
    finally:
        path.unlink()
        crew_mode.remember(KiroCrewConfig.load())


@pytest.mark.parametrize(
    ("agent", "key"),
    [
        ("kirocrew-worker", CHAT),
        ("kirocrew", CHAT),
        ("kirocrew-mate-conductor", CHAT),
        ("kirocrew-mate-conductor", "dashboard:member-ada"),
        (CONDUCTOR, BOT_THREAD),
        (CONDUCTOR, "dashboard_member-ada"),
        ("", CHAT),
    ],
)
def test_no_other_session_carries_it(tmp_path, agent, key):
    """A worker chat, a chat on the default agent, a Bot's own conductor, a Bot's
    thread (even one carrying a conductor's name) and an agentless session."""
    client = _client(tmp_path, agent or "kirocrew", key)
    client._agent = agent
    assert _options(client) == {}


def test_a_bots_worker_does_not_carry_it(tmp_path):
    """A session recorded as a Bot's worker is that Bot's, whatever agent it names."""
    key = "dashboard:chat-bot-worker"
    members.note_crewmate_worker_session(key, "ada")
    try:
        assert _window(_options(_client(tmp_path, CONDUCTOR, key))) == (None, None)
    finally:
        members.forget_crewmate_worker_session(key)
    assert _window(_options(_client(tmp_path, CONDUCTOR, key))) == ("600000", "600000")


def test_the_window_crew_gives_a_session_is_the_one_its_envelope_carries():
    """The context cap reads this: the setting for a Conductor chat on claude, 0 for
    a chat on another backend, a Bot's thread and a chat on another agent."""
    assert _tokens(CONDUCTOR, CHAT, ACP_BACKEND_CLAUDE) == DEFAULT
    assert _tokens(CONDUCTOR, CHAT, ACP_BACKEND_KAS) == 0
    assert _tokens(CONDUCTOR, BOT_THREAD, ACP_BACKEND_CLAUDE) == 0
    assert _tokens("kirocrew-worker", CHAT, ACP_BACKEND_CLAUDE) == 0


# ── one setting, read at session start ───────────────────────────────────────


def test_a_changed_setting_reaches_the_next_session(tmp_path):
    """No build and no restart: each envelope reads the config in force."""
    _set(450_000)
    assert _window(_options(_client(tmp_path))) == ("450000", "450000")
    _set(500_000)
    assert _window(_options(_client(tmp_path))) == ("500000", "500000")


def test_the_adopted_config_is_read_before_the_file(tmp_path, monkeypatch):
    """In a gateway the watcher's snapshot answers, so no file is read at session start."""
    from kiro_crew.config import live

    _set(450_000)
    adopted = KiroCrewConfig.load()
    _set(500_000)
    monkeypatch.setattr(live, "snapshot", lambda: adopted)
    assert _window(_options(_client(tmp_path))) == ("450000", "450000")


def test_zero_sends_nothing(tmp_path):
    """Off: the Conductor chat follows Claude Code's own settings, like every other."""
    _set(0)
    assert _options(_client(tmp_path)) == {}
    assert _tokens(CONDUCTOR, CHAT, ACP_BACKEND_CLAUDE) == 0


def test_an_unreadable_config_sends_nothing(tmp_path, monkeypatch):
    """A session starts without the window rather than failing on it."""

    def _broken() -> Any:
        raise OSError("config unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_broken))
    assert _options(_client(tmp_path)) == {}


# ── nothing else in the envelope outranks it ─────────────────────────────────


def test_a_settings_tier_cannot_lower_the_window(tmp_path):
    """Claude Code copies each settings tier's ``env`` over the process env, so a
    tier naming another window (here carried inline on the exclusion path) is
    overridden in the flag tier, and the shared inline dict is not mutated."""
    client = _client(tmp_path)
    inline = {"env": {_VAR: "300000", "AWS_REGION": "us-west-2"}}
    client._claude_local_settings_excluded = True
    client._claude_inline_settings = inline
    options = _options(client)
    assert options["settings"]["env"] == {_VAR: "600000", "AWS_REGION": "us-west-2"}
    assert options["env"] == {_VAR: "600000"}
    assert inline["env"][_VAR] == "300000"


def _spec_names_a_window(monkeypatch) -> None:
    def _spec_env(options: dict, _agent: Any) -> None:
        options["env"] = {_VAR: "350000", "FOO": "1"}

    monkeypatch.setattr(client_claude_session, "apply_spec_claude_options", _spec_env)


def test_the_setting_replaces_a_window_the_agents_own_env_block_names(tmp_path, monkeypatch):
    """One number for every Conductor chat: the spec's ``claudeCode.env`` lands first."""
    _spec_names_a_window(monkeypatch)
    options = _options(_client(tmp_path))
    assert options["env"] == {_VAR: "600000", "FOO": "1"}
    assert options["settings"]["env"] == {_VAR: "600000"}


def test_at_zero_the_agents_own_env_block_stands(tmp_path, monkeypatch):
    _spec_names_a_window(monkeypatch)
    _set(0)
    assert _options(_client(tmp_path)) == {"env": {_VAR: "350000", "FOO": "1"}}
