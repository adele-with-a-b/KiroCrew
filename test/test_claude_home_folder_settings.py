"""A claude session whose work folder is the home directory keeps its MCP servers.

There the folder's ``.claude`` is the user's Claude config directory, so the
project ``settings.json`` the exclusion examines is the user's own file. That file
loads through the ``user`` source whatever the exclusion does, so its hooks must
not refuse the exclusion the way a checked-in project file's hooks do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _no_ambient_pin_setting  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _by_name, _write_spec

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

_HOOKS = {
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./g.sh"}]}]
    }
}


def _home_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A work folder whose ``.claude`` is the user's config dir, as ``$HOME`` is.

    Its ``settings.json`` sets hooks (a user's own hooks), and its
    ``settings.local.json`` is the user's, not Crew's.
    """
    config = tmp_path / ".claude"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps(_HOOKS), encoding="utf-8")
    (config / "settings.local.json").write_text(
        json.dumps({"spinnerTipsEnabled": False}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return tmp_path


class TestHomeFolderSession:
    @pytest.mark.parametrize("requested", [None, "default", "bypassPermissions"])
    def test_the_users_own_hooks_do_not_withhold_the_array(
        self, tmp_path, agents_dir, monkeypatch, requested
    ):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        home = _home_folder(tmp_path, monkeypatch)
        client = AcpClient(
            work_dir=home,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CLAUDE,
            permission_mode=requested,
        )
        client._write_claude_local_settings()

        assert client._claude_local_settings_excluded is True
        assert "foo" in _by_name(client._session_mcp_servers())
        options = client._claude_session_meta()["claudeCode"]["options"]
        # The user's file still loads, as the user tier; neither project tier does.
        assert options["settingSources"] == ["user"]

    def test_the_home_folders_local_file_is_still_examined(self, tmp_path, agents_dir, monkeypatch):
        """Only the user's ``settings.json`` is exempt: hooks in the folder's local file refuse."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        home = _home_folder(tmp_path, monkeypatch)
        (home / ".claude" / "settings.local.json").write_text(json.dumps(_HOOKS), encoding="utf-8")
        client = AcpClient(work_dir=home, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()

        assert client._claude_local_settings_excluded is False
        assert client._session_mcp_servers() == []

    def test_another_folders_settings_json_with_hooks_still_refuses(self, tmp_path, monkeypatch):
        user = tmp_path / "user-config"
        user.mkdir()
        (user / "settings.json").write_text(json.dumps(_HOOKS), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user))
        project = tmp_path / "repo" / ".claude"
        project.mkdir(parents=True)
        (project / "settings.json").write_text(json.dumps(_HOOKS), encoding="utf-8")

        assert client_mod._project_settings_restrictions(user / "settings.json") == []
        assert client_mod._project_settings_restrictions(project / "settings.json") is None
