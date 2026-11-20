"""A user-level agent spec's ``claudeCode`` block reaches the claude session envelope."""

from __future__ import annotations

import json

from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401

from kiro_crew.acp.claude_agent_options import spec_claude_options
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE


def _spec(agents_dir, name: str, block) -> None:
    spec = {"name": name, "mcpServers": {}, "claudeCode": block}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _options(tmp_path, agent: str, mode: str = "persistent") -> dict:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, agent=agent)
    client.memory_mode = mode
    return client._claude_session_meta()["claudeCode"]["options"]


def test_memory_dir_env_and_strict_mcp_reach_the_envelope(tmp_path, agents_dir):
    mem = str(tmp_path / "mem")
    _spec(
        agents_dir,
        "persona",
        {"autoMemoryDirectory": mem, "env": {"OPENCLAW_AGENT": "persona"}, "strictMcpConfig": True},
    )
    options = _options(tmp_path, "persona")
    assert options["settings"]["autoMemoryDirectory"] == mem
    assert options["env"] == {"OPENCLAW_AGENT": "persona"}
    assert options["strictMcpConfig"] is True


def test_the_auto_memory_off_switch_still_wins(tmp_path, agents_dir):
    _spec(
        agents_dir,
        "persona",
        {"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}, "autoMemoryDirectory": str(tmp_path)},
    )
    options = _options(tmp_path, "persona", mode="incognito")
    assert options["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert options["settings"]["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_invalid_entries_are_dropped(tmp_path, agents_dir):
    _spec(
        agents_dir,
        "persona",
        {
            "autoMemoryDirectory": "relative/dir",
            "env": {"lower": "x", "OK": 1},
            "strictMcpConfig": False,
        },
    )
    assert spec_claude_options("persona") == {}


def test_no_block_leaves_the_envelope_alone(tmp_path, agents_dir):
    (agents_dir / "plain.json").write_text(
        json.dumps({"name": "plain", "mcpServers": {}}), encoding="utf-8"
    )
    options = _options(tmp_path, "plain")
    assert "settings" not in options and "env" not in options and "strictMcpConfig" not in options


def test_tools_narrow_the_builtins_and_never_widen(tmp_path, agents_dir):
    _spec(agents_dir, "reader", {"tools": ["Read", "Bash", "Read"]})
    assert _options(tmp_path, "reader")["tools"] == ["Read", "Bash"]
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, agent="reader")
    client._side_read_only = True
    client._claude_side_read_denies = ("Read(//secret/**)",)
    assert client._claude_session_meta()["claudeCode"]["options"]["tools"] == ["Read"]
