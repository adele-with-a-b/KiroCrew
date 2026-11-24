"""The starting-mode pin on a claude session that is neither excluded nor a Side Chat.

With nothing requested such a session's seed names no ``defaultMode``, so the adapter
starts it in the mode the user's own settings name, or a resumed one in its
transcript's. By default the session keeps that mode. With
``KIROCREW_CC_PIN_INHERITED_MODE`` on, ``_pin_claude_starting_mode`` reads it back: it
moves ``auto``, ``bypassPermissions`` or an unreported mode to ``default``, keeps a mode
that asks and the mode Crew requested, and stops the session when the move fails. The
exclusion path, Side Chat and the adapter-version floor are in
``test_acp_session_mcp_client_seam.py``; both modules replay the recorded
claude-agent-acp 0.84.0 answers through ``acp_session_mcp_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _no_ambient_pin_setting  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import (
    _PIN_SETTING_ENV,
    _adapter_transport,
    _by_name,
    _pin_harness,
    _write_spec,
)

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk import backends


class TestUnrequestedModePin:
    """The pin with ``KIROCREW_CC_PIN_INHERITED_MODE`` on."""

    _pin_harness = staticmethod(_pin_harness)
    _adapter_transport = staticmethod(_adapter_transport)

    @pytest.fixture(autouse=True)
    def _setting_on(self, _floor_monkeypatch, no_ambient_pin_setting):
        _floor_monkeypatch.setenv(_PIN_SETTING_ENV, "1")

    def _seeded(self, tmp_path, **kw):
        """A client whose settings seed has already run, as the spawn path does."""
        client = AcpClient(work_dir=tmp_path, **kw)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        return client

    def _record_sent_array(self, client):
        """Record Crew's array as the start sent it, for a test that calls the pin directly."""
        client._mcp_mount_servers = client._claude_session_mcp_servers()
        assert "foo" in _by_name(client._mcp_mount_servers)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started_mode", ["auto", "bypassPermissions"])
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_crew_authored_session_is_pinned_off_an_approving_mode(
        self, tmp_path, agents_dir, monkeypatch, started_mode, path
    ):
        """A session that requested no mode is read back like the exclusion path.

        Its seed names no ``defaultMode``, so the adapter starts a
        ``session/new`` in the mode the user's own settings name and a
        ``session/load`` in the mode its transcript was left in. The transport
        reports that mode here. One that approves on its own is moved to
        ``default`` before the first prompt, and the session keeps Crew's array.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        seeded = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert "permissions" not in seeded
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode=started_mode)
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert client._resumed is (path == "load")
        params = dict(sent)["session/load" if path == "load" else "session/new"]
        assert "foo" in _by_name(params["mcpServers"])
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started_mode", ["default", "plan", "acceptEdits", "dontAsk"])
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_crew_authored_session_in_a_mode_that_asks_gains_no_call(
        self, tmp_path, agents_dir, monkeypatch, started_mode, path
    ):
        """Only an approving mode is moved: a stricter or narrower one is kept."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode=started_mode)
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    async def test_a_crew_authored_session_that_reports_no_mode_is_pinned(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A response that names no mode is pinned, as on the exclusion path."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        self._record_sent_array(client)
        sent, killed = self._pin_harness(client, monkeypatch)
        await client._pin_claude_starting_mode({"sessionId": "s-1"})
        assert sent == [("session/set_mode", {"sessionId": "s-1", "modeId": "default"})]
        assert killed == []

    @pytest.mark.asyncio
    async def test_a_failed_pin_stops_a_crew_authored_session(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The read-back fails closed on a session whose file Crew wrote too."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        self._record_sent_array(client)
        _sent, killed = self._pin_harness(client, monkeypatch, fail=True)
        with pytest.raises(client_mod.AcpError, match="pin the asking permission mode"):
            await client._pin_claude_starting_mode(
                {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
            )
        assert killed == [True]

    @pytest.mark.asyncio
    async def test_the_pin_reaches_the_session_only_through_its_driver_hooks(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The pin moves and stops the session through ``_claude_set_mode`` and
        ``_claude_stop_session`` and never touches the transport itself.

        A driver serving several sessions on one process overrides the two hooks;
        a pin that sent its own request or killed the process directly would
        bypass that driver and stop every session on the process.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        self._record_sent_array(client)
        moved: list[tuple[str, str]] = []
        stopped: list[bool] = []

        async def _set_mode(session_id: str, mode_id: str) -> None:
            moved.append((session_id, mode_id))
            raise client_mod.AcpError("set_mode refused")

        async def _stop() -> None:
            stopped.append(True)

        async def _no_transport(*_a, **_kw):
            raise AssertionError("the pin used the transport directly")

        monkeypatch.setattr(client, "_claude_set_mode", _set_mode)
        monkeypatch.setattr(client, "_claude_stop_session", _stop)
        for name in ("_send_request", "_wait_for_response", "_kill_process"):
            monkeypatch.setattr(client, name, _no_transport)
        with pytest.raises(client_mod.AcpError, match="pin the asking permission mode"):
            await client._pin_claude_starting_mode(
                {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
            )
        assert moved == [("s-1", "default")]
        assert stopped == [True]

    @pytest.mark.asyncio
    async def test_a_load_that_did_not_take_is_pinned_once_on_the_new_session(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A ``session/load`` with no ``modes`` falls through to ``session/new``.

        The load is not read back, because it did not take; the session that
        replaces it is, so the pin is sent once, on the new id.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        answer = client._wait_for_response

        async def _load_without_modes(rid, **kw):
            resp = await answer(rid, **kw)
            if sent[rid - 1][0] == "session/load":
                resp = {key: value for key, value in resp.items() if key != "modes"}
            return resp

        monkeypatch.setattr(client, "_wait_for_response", _load_without_modes)
        await client._initialize_session()
        assert client._resumed is False
        assert client._session_id == "s-new"
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": "s-new", "modeId": "default"}
        ]

    @pytest.mark.asyncio
    async def test_a_failed_pin_on_a_resumed_session_is_not_taken_for_a_failed_load(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The load-path pin sits outside the load's own error handling.

        A ``session/set_mode`` that fails after a successful ``session/load``
        stops the harness; it does not fall through to ``session/new``.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        answer = client._wait_for_response

        async def _refuse_set_mode(rid, **kw):
            if sent[rid - 1][0] == "session/set_mode":
                raise client_mod.AcpError("set_mode refused")
            return await answer(rid, **kw)

        monkeypatch.setattr(client, "_wait_for_response", _refuse_set_mode)
        with pytest.raises(client_mod.AcpError, match="pin the asking permission mode"):
            await client._initialize_session()
        assert killed == [True]
        assert "session/new" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    async def test_the_deny_re_read_stays_on_the_exclusion_path(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A checked-in project deny does not stop a session whose file Crew wrote.

        That session loads the project tier itself, so its deny rules apply
        natively. Only the exclusion path carries them inline and re-reads them
        before the first prompt.
        """
        project = tmp_path / ".claude"
        project.mkdir()
        (project / "settings.json").write_text(
            json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}}), encoding="utf-8"
        )
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert client._claude_session_excludes_local() is False
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": "s-new", "modeId": "default"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_an_opt_in_session_that_starts_or_resumes_in_bypass_is_pinned(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """The opt-in keeps ``auto`` only: a reported ``bypassPermissions`` is moved.

        ``auto`` is the only mode Crew requests, so this is also the case of a
        requested mode that differs from the approving mode the adapter reports.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, permission_mode="auto"
        )
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions"
        )
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_crew_opt_in_mode_is_not_overridden(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """The documented ``auto`` opt-in keeps the mode Crew asked for."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, permission_mode="auto"
        )
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_the_pin_does_not_project_the_array_again(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """The pin decides from the array the start sent, not from a new projection.

        The re-seed after the start clears the projection cache, so a projection
        made inside the pin would read the spec and the settings files on the
        event loop.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        await client._prepare_session_mcp()
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        inside_pin: list[bool] = []
        cache_cold_at_pin: list[bool] = []
        projected_inside_pin: list[bool] = []
        pin = client._pin_claude_starting_mode
        project = client._resolve_session_mcp_servers

        async def _recording_pin(resp):
            cache_cold_at_pin.append(client._session_mcp_cache is None)
            inside_pin.append(True)
            try:
                await pin(resp)
            finally:
                inside_pin.pop()

        def _recording_project():
            projected_inside_pin.append(bool(inside_pin))
            return project()

        monkeypatch.setattr(client, "_pin_claude_starting_mode", _recording_pin)
        monkeypatch.setattr(client, "_resolve_session_mcp_servers", _recording_project)
        await client._initialize_session()
        assert cache_cold_at_pin == [True]
        assert True not in projected_inside_pin
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_sent_array_is_pinned_though_a_new_projection_withholds_it(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """A session that started with Crew's servers is read back whatever comes later.

        A projection made after the start can withhold the array, for one when a
        settings read is refused or the file was replaced since the spawn. The
        session already holds Crew's servers, so its mode is still pinned.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        await client._prepare_session_mcp()
        monkeypatch.setattr(client, "_resolve_session_mcp_servers", lambda: [])
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        params = dict(sent)["session/load" if path == "load" else "session/new"]
        assert "foo" in _by_name(params["mcpServers"])
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]


class TestUnrequestedModeKeptByDefault:
    """With the setting off, the default, such a session keeps the mode it started in."""

    _adapter_transport = staticmethod(_adapter_transport)
    _seeded = TestUnrequestedModePin._seeded

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_user_auto_start_with_crews_array_is_not_pinned(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """A user ``defaultMode: auto``, Crew's array sent, no request: no pin, no stop."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"defaultMode": "auto"}}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        work = tmp_path / "work"
        work.mkdir()
        client = self._seeded(work, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert client._permission_mode is None
        assert "permissions" not in json.loads(
            (work / ".claude" / "settings.local.json").read_text()
        )
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        params = dict(sent)["session/load" if path == "load" else "session/new"]
        assert "foo" in _by_name(params["mcpServers"])
        assert "foo" in _by_name(client._mcp_mount_servers)
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "pinned"),
        [
            ("1", True),
            ("true", True),
            pytest.param(" On ", True, id="spaced-On-True"),
            ("0", False),
            ("off", False),
            ("2", False),
        ],
    )
    async def test_the_setting_reaches_the_pin_from_its_env_var(
        self, tmp_path, agents_dir, monkeypatch, value, pinned
    ):
        """The pin reads the env var at the start: a truthy spelling turns it on."""
        monkeypatch.setenv(_PIN_SETTING_ENV, value)
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []
        expected = [{"sessionId": "s-new", "modeId": "default"}] if pinned else []
        assert [p for m, p in sent if m == "session/set_mode"] == expected


class TestResolveCcPinInheritedMode:
    """The setting is off unless its env var holds a truthy spelling."""

    def test_the_setting_has_its_documented_name(self):
        assert backends.CC_PIN_INHERITED_MODE_ENV == _PIN_SETTING_ENV

    def test_unset_is_off_and_quiet(self, caplog):
        with caplog.at_level("WARNING"):
            assert backends.resolve_cc_pin_inherited_mode() is False
        assert caplog.text == ""

    @pytest.mark.parametrize(
        "value", ["1", "true", "TRUE", "yes", "on", pytest.param(" 1\n", id="spaced-1")]
    )
    def test_a_truthy_value_turns_it_on(self, monkeypatch, value):
        monkeypatch.setenv(_PIN_SETTING_ENV, value)
        assert backends.resolve_cc_pin_inherited_mode() is True

    @pytest.mark.parametrize("value", [pytest.param("", id="empty"), "0", "false", "no", "off"])
    def test_a_falsy_value_is_off_and_quiet(self, monkeypatch, caplog, value):
        monkeypatch.setenv(_PIN_SETTING_ENV, value)
        with caplog.at_level("WARNING"):
            assert backends.resolve_cc_pin_inherited_mode() is False
        assert caplog.text == ""

    @pytest.mark.parametrize("value", ["2", "enabled", "auto", "default"])
    def test_an_unrecognised_value_is_off_and_reported(self, monkeypatch, caplog, value):
        """A lever an operator believes is set is named, not silently dropped."""
        monkeypatch.setenv(_PIN_SETTING_ENV, value)
        with caplog.at_level("WARNING"):
            assert backends.resolve_cc_pin_inherited_mode() is False
        assert _PIN_SETTING_ENV in caplog.text
        assert "not recognised" in caplog.text


class TestClaudeGuardsSetting:
    """``agent.claude_guards``, the Settings switch, turns the same pin on.

    The env var stays unset in every test here (``no_ambient_pin_setting``), so a
    pin seen here came from the setting.
    """

    _adapter_transport = staticmethod(_adapter_transport)
    _seeded = TestUnrequestedModePin._seeded

    @pytest.fixture(autouse=True)
    def _no_snapshot(self, _floor_monkeypatch, no_ambient_pin_setting):
        """No adopted snapshot unless a test supplies one, so the disk file is read."""
        from kiro_crew.config import live

        _floor_monkeypatch.setattr(live, "snapshot", lambda: None)

    def _write_config(self, document: dict) -> None:
        from kiro_crew.config.paths import config_dir

        (config_dir() / "config.json").write_text(json.dumps(document), encoding="utf-8")

    async def _start(self, tmp_path, agents_dir, monkeypatch, started_mode="bypassPermissions"):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode=started_mode)
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []
        return [p for m, p in sent if m == "session/set_mode"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started_mode", ["auto", "bypassPermissions"])
    @pytest.mark.parametrize(("on", "pinned"), [(True, True), (False, False), (None, False)])
    async def test_the_setting_reaches_the_pin_from_the_config_file(
        self, tmp_path, agents_dir, monkeypatch, started_mode, on, pinned
    ):
        """On pins an approving start; off and absent leave it as today."""
        self._write_config({"agent": {} if on is None else {"claude_guards": on}})
        set_mode = await self._start(tmp_path, agents_dir, monkeypatch, started_mode)
        assert set_mode == ([{"sessionId": "s-new", "modeId": "default"}] if pinned else [])

    @pytest.mark.asyncio
    async def test_the_setting_keeps_a_mode_that_asks(self, tmp_path, agents_dir, monkeypatch):
        """The setting adds no move: a session that starts in ``default`` gains no call."""
        self._write_config({"agent": {"claude_guards": True}})
        assert await self._start(tmp_path, agents_dir, monkeypatch, "default") == []

    @pytest.mark.asyncio
    async def test_an_adopted_snapshot_is_read_without_the_disk(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """In a gateway the watcher's snapshot answers, so no file is read on the start."""
        from dataclasses import replace

        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        base = KiroCrewConfig()
        snap = replace(base, agent=replace(base.agent, claude_guards=True))
        monkeypatch.setattr(live, "snapshot", lambda: snap)

        def _no_disk():
            raise AssertionError("the snapshot should have answered")

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_no_disk))
        set_mode = await self._start(tmp_path, agents_dir, monkeypatch)
        assert set_mode == [{"sessionId": "s-new", "modeId": "default"}]

    @pytest.mark.asyncio
    async def test_a_discarded_agent_section_pins(self, tmp_path, agents_dir, monkeypatch, caplog):
        """A guard is not lost to a torn file: an unknown setting reads as on, and says so."""
        from dataclasses import replace

        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        torn = replace(KiroCrewConfig(), _degraded_sections=frozenset({"agent"}))
        monkeypatch.setattr(live, "snapshot", lambda: torn)
        with caplog.at_level("WARNING"):
            set_mode = await self._start(tmp_path, agents_dir, monkeypatch)
        assert set_mode == [{"sessionId": "s-new", "modeId": "default"}]
        assert "could not be read (agent), so agent.claude_guards" in caplog.text

    def _write_raw(self, text: str) -> None:
        from kiro_crew.config.paths import config_dir

        (config_dir() / "config.json").write_text(text, encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_config_file_that_does_not_parse_pins(
        self, tmp_path, agents_dir, monkeypatch, caplog
    ):
        """A torn file loads as defaults flagged whole-file degraded: the setting is unknown."""
        self._write_raw('{"agent": {"claude_guards": false')
        with caplog.at_level("WARNING"):
            set_mode = await self._start(tmp_path, agents_dir, monkeypatch)
        assert set_mode == [{"sessionId": "s-new", "modeId": "default"}]
        assert "the config could not be read (*" in caplog.text
        assert "is acting as on until restart" in caplog.text

    @pytest.mark.asyncio
    async def test_a_torn_file_keeps_it_on_after_the_repair_until_restart(
        self, tmp_path, agents_dir, monkeypatch, caplog
    ):
        """The loader's record of the tear outlives the repair, so the switch's off
        is not followed until the process restarts: never toward guards-off."""
        self._write_raw('{"agent": {"claude_guards": false')
        assert await self._start(tmp_path, agents_dir, monkeypatch) != []
        self._write_config({"agent": {"claude_guards": False}})
        caplog.clear()
        with caplog.at_level("WARNING"):
            set_mode = await self._start(tmp_path / "second", agents_dir, monkeypatch)
        assert set_mode == [{"sessionId": "s-new", "modeId": "default"}]
        assert "is acting as on until restart" in caplog.text

    @pytest.mark.asyncio
    async def test_a_loader_failure_pins(self, tmp_path, agents_dir, monkeypatch, caplog):
        """A failure the loader did not degrade around reads as on rather than failing the start."""
        from kiro_crew.config.loader import KiroCrewConfig

        def _raises():
            raise RuntimeError("an unforeseen loader failure")

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_raises))
        with caplog.at_level("WARNING"):
            set_mode = await self._start(tmp_path, agents_dir, monkeypatch)
        assert set_mode == [{"sessionId": "s-new", "modeId": "default"}]
        assert "could not load the config to check agent.claude_guards" in caplog.text

    @pytest.mark.asyncio
    async def test_a_torn_agent_section_pins_on_the_real_loader(
        self, tmp_path, agents_dir, monkeypatch, caplog
    ):
        """The loader drops a non-object ``agent`` section and flags it; that reads as on."""
        self._write_config({"agent": []})
        with caplog.at_level("WARNING"):
            set_mode = await self._start(tmp_path, agents_dir, monkeypatch)
        assert set_mode == [{"sessionId": "s-new", "modeId": "default"}]
        assert "could not be read (agent), so agent.claude_guards" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started_mode", ["default", "auto"])
    async def test_a_start_the_pin_would_not_act_on_reads_no_config(
        self, tmp_path, agents_dir, monkeypatch, started_mode
    ):
        """The setting is read only once a pin could follow.

        A start that already asks, and one whose mode is the one it requested, keep their
        mode whatever the setting says, so neither pays for a config read.
        """
        from kiro_crew.acp import client_claude_session

        async def _not_read():
            raise AssertionError("the setting was read for a start it cannot change")

        monkeypatch.setattr(client_claude_session, "claude_guards_setting_on", _not_read)
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(
            tmp_path,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CLAUDE,
            permission_mode=None if started_mode == "default" else started_mode,
        )
        sent, _ = self._adapter_transport(client, monkeypatch, started_mode=started_mode)
        await client._initialize_session()
        assert [p for m, p in sent if m == "session/set_mode"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["no_crew_tools", "below_floor"])
    async def test_a_start_the_pin_leaves_reads_no_config_though_it_approves(
        self, tmp_path, agents_dir, monkeypatch, case
    ):
        """A start with no Crew tools, or on an adapter below the floor, is left in its
        approving mode by both levers, so it returns before the setting is read."""
        from kiro_crew.acp import client_claude_session

        async def _not_read():
            raise AssertionError("the setting was read for a start it cannot change")

        monkeypatch.setattr(client_claude_session, "claude_guards_setting_on", _not_read)
        if case == "no_crew_tools":
            _write_spec(agents_dir, servers={}, tools=None)
        else:
            _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        version = "0.83.0" if case == "below_floor" else "0.84.0"
        sent, _ = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions", version=version
        )
        await client._initialize_session()
        assert [p for m, p in sent if m == "session/set_mode"] == []
        # Each case leaves by its own gate: no array sent, or Crew's array below the floor.
        sent_names = set(_by_name(client._mcp_mount_servers or []))
        assert sent_names == set() if case == "no_crew_tools" else "foo" in sent_names

    @pytest.mark.asyncio
    async def test_another_section_torn_leaves_the_setting_as_written(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Only the agent section holds the setting, so only its loss reads as on."""
        from dataclasses import replace

        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        torn = replace(KiroCrewConfig(), _degraded_sections=frozenset({"slack"}))
        monkeypatch.setattr(live, "snapshot", lambda: torn)
        assert await self._start(tmp_path, agents_dir, monkeypatch) == []

    def test_the_settings_page_may_write_it(self):
        from kiro_crew.dashboard.handlers.core_config_schema import _EDITABLE_CONFIG

        assert _EDITABLE_CONFIG["agent.claude_guards"] == {"type": "bool"}
