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

from kiro_crew.acp import client_claude_session as client_session_mod
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

    def test_the_default_config_dir_under_home_is_recognised(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """No ``CLAUDE_CONFIG_DIR``: the user's file is ``~/.claude/settings.json``."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        home = _home_folder(tmp_path, monkeypatch)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        client = AcpClient(work_dir=home, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()

        assert client._claude_local_settings_excluded is True
        assert "foo" in _by_name(client._session_mcp_servers())

    @pytest.mark.parametrize(
        "name", ["CLAUDE_CONFIG_DIR", "CLAUDE_HOME", "HOME", "USERPROFILE", "claude_config_dir"]
    )
    def test_a_child_pointed_at_another_user_tier_gets_no_exemption(
        self, tmp_path, agents_dir, monkeypatch, name
    ):
        """The child would load another ``settings.json`` as its user tier, so this one's
        hooks would load nowhere: it refuses, as a project file does."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        home = _home_folder(tmp_path, monkeypatch)
        client = AcpClient(
            work_dir=home,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CLAUDE,
            extra_env={name: str(tmp_path / "elsewhere")},
        )
        client._write_claude_local_settings()

        assert client._claude_local_settings_excluded is False
        assert client._session_mcp_servers() == []

    def test_a_spec_that_moves_the_childs_config_dir_gets_no_exemption(
        self, tmp_path, agents_dir, monkeypatch
    ):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        home = _home_folder(tmp_path, monkeypatch)
        client = AcpClient(work_dir=home, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        monkeypatch.setattr(
            client_session_mod,
            "apply_spec_claude_options",
            lambda options, agent: options.update(env={"CLAUDE_CONFIG_DIR": "/elsewhere"}),
        )

        assert client_session_mod._claude_user_settings_file(client) is None


class TestProjectSettingsRestrictions:
    def test_another_folders_settings_json_with_hooks_still_refuses(self, tmp_path):
        user = tmp_path / "user-config"
        user.mkdir()
        (user / "settings.json").write_text(json.dumps(_HOOKS), encoding="utf-8")
        project = tmp_path / "repo" / ".claude"
        project.mkdir(parents=True)
        (project / "settings.json").write_text(json.dumps(_HOOKS), encoding="utf-8")
        restrictions = client_session_mod._project_settings_restrictions

        assert restrictions(user / "settings.json", user / "settings.json") == []
        assert restrictions(project / "settings.json", user / "settings.json") is None
        # With no user file known, the user's own path is examined like any other.
        assert restrictions(user / "settings.json") is None

    def test_a_link_to_the_users_file_is_not_the_users_file(self, tmp_path):
        """Recognised by spelling, never through a link a repository controls."""
        user = tmp_path / "user-config"
        user.mkdir()
        (user / "settings.json").write_text("{}", encoding="utf-8")
        project = tmp_path / "repo" / ".claude"
        project.mkdir(parents=True)
        try:
            (project / "settings.json").symlink_to(user / "settings.json")
        except OSError:
            pytest.skip("this host cannot create a symlink")

        assert (
            client_session_mod._project_settings_restrictions(
                project / "settings.json", user / "settings.json"
            )
            is None
        )

    def test_a_path_spelled_through_dot_dot_is_not_the_users_file(self, tmp_path):
        user = tmp_path / "user-config"
        user.mkdir()
        (user / "settings.json").write_text(json.dumps(_HOOKS), encoding="utf-8")
        (tmp_path / "repo").mkdir()
        spelled = tmp_path / "repo" / ".." / "user-config" / "settings.json"

        assert (
            client_session_mod._project_settings_restrictions(spelled, user / "settings.json")
            is None
        )
