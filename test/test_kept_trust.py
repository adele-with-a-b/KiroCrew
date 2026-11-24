"""Only an explicit stop clears the trust a restart keeps (``kiro_crew.kept_trust``).

The explicit stops (``kirocrew stop``, Ctrl+C, the desktop app's Quit) leave one
mark in the sealed ``kept-trust`` directory; the next boot reads it once, removes
every kept record, then the mark. Every other exit leaves no mark, so the next boot
keeps what was kept. These tests drive the mark's writer and reader on a temp data
home, the CLI stop through ``cli.main`` with the stop itself replaced, and the seals
through the sandbox and file-gate tables.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import cli, cli_server, kept_trust


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv(kept_trust.KEEP_TRUST_ENV, raising=False)
    kept_trust.reset_for_tests()
    return tmp_path


def _mark(home: Path) -> Path:
    return home / kept_trust.KEPT_DIR / kept_trust.OWNER_STOP_FILE


def _kept(home: Path) -> list[Path]:
    records = [home / kept_trust.KEPT_DIR / name for name in kept_trust._RECORDS]
    for record in records:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}", encoding="utf-8")
    return records


def _audits(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    from kiro_crew import sel as sel_mod

    calls: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    return calls


class TestTheMark:
    def test_a_stop_leaves_it_sealed_to_the_owner(self, home: Path) -> None:
        assert kept_trust.mark_owner_stop("kirocrew stop") is True
        mark = _mark(home)
        assert mark.read_text(encoding="ascii") == "kirocrew stop"
        if os.name == "posix":
            assert mark.stat().st_mode & 0o777 == 0o600
            assert mark.parent.stat().st_mode & 0o777 == 0o700

    def test_an_earlier_mark_is_left_and_not_claimed(self, home: Path) -> None:
        # Only the call that created a mark may take it back.
        assert kept_trust.mark_owner_stop("ctrl-c") is True
        assert kept_trust.mark_owner_stop("kirocrew stop") is False
        assert _mark(home).read_text(encoding="ascii") == "ctrl-c"

    @pytest.mark.skipif(sys.platform == "win32", reason="a directory symlink needs privilege")
    def test_a_linked_directory_is_not_written_through(
        self, home: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        (home / kept_trust.KEPT_DIR).symlink_to(elsewhere, target_is_directory=True)
        assert kept_trust.mark_owner_stop("kirocrew stop") is False
        assert list(elsewhere.iterdir()) == []

    def test_a_mark_that_cannot_be_written_never_fails_the_stop(self, home: Path) -> None:
        # A file where the directory must be: the stop still stops, and trust is kept.
        (home / kept_trust.KEPT_DIR).write_text("", encoding="utf-8")
        assert kept_trust.mark_owner_stop("kirocrew stop") is False


class TestTheNextBoot:
    def test_no_mark_keeps_every_record(self, home: Path) -> None:
        records = _kept(home)
        assert kept_trust.take_owner_stop() is False
        assert all(record.exists() for record in records)

    def test_a_mark_removes_every_record_then_itself_and_is_audited(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audits = _audits(monkeypatch)
        records = _kept(home)
        kept_trust.mark_owner_stop("desktop quit")
        assert kept_trust.take_owner_stop() is True
        assert not any(record.exists() for record in records)
        assert not _mark(home).exists()
        [audit] = audits
        assert audit["operation"] == "kept_trust:cleared_on_owner_stop"
        assert audit["outcome"] == "cleared"
        assert "stop:desktop quit" in audit["resources"]

    def test_the_verdict_is_read_once_per_process(self, home: Path) -> None:
        # Both readers (the YOLO restore and the chats' restore) see the same answer,
        # though the first one already removed the mark.
        kept_trust.mark_owner_stop("kirocrew stop")
        assert kept_trust.take_owner_stop() is True
        assert kept_trust.take_owner_stop() is True
        kept_trust.reset_for_tests()
        assert kept_trust.take_owner_stop() is False

    def test_a_record_that_cannot_be_removed_keeps_the_mark_for_the_next_boot(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audits = _audits(monkeypatch)
        _kept(home)
        kept_trust.mark_owner_stop("kirocrew stop")
        real_unlink = Path.unlink

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self.name == kept_trust.CHATS_FILE:
                raise PermissionError("held")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)
        # This boot restores nothing either way.
        assert kept_trust.take_owner_stop() is True
        assert _mark(home).exists()
        assert audits[0]["outcome"] == "failed"

    @pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs privilege")
    @pytest.mark.parametrize("shape", ["directory", "link"])
    def test_any_mark_but_a_clean_absence_counts(self, home: Path, shape: str) -> None:
        # A failure falls toward asking again, never toward keeping trust.
        mark = _mark(home)
        mark.parent.mkdir(parents=True)
        if shape == "directory":
            mark.mkdir()
        else:
            mark.symlink_to(home / "nowhere")
        assert kept_trust.take_owner_stop() is True


class TestTheCliStop:
    @pytest.fixture(autouse=True)
    def this_home(self, monkeypatch: pytest.MonkeyPatch) -> list:
        # Whether the stop targets this home's gateway; the tests below that need
        # another answer set it.
        answer = [True]
        monkeypatch.setattr(cli_server, "_stops_this_homes_gateway", lambda port: answer[0])
        return answer

    @pytest.fixture
    def stops(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> list:
        seen: list = []

        def _stop(port, expect_pid=None):
            seen.append((port, expect_pid, _mark(home).exists()))

        monkeypatch.setattr(cli_server, "_stop", _stop)
        return seen

    def test_a_plain_stop_marks_before_it_stops(
        self, home: Path, stops: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["kirocrew", "stop"])
        cli.main()
        # Marked already when the gateway was signalled, so a stop that lands
        # mid-shutdown still counts.
        assert stops == [(None, None, True)]
        assert _mark(home).read_text(encoding="ascii") == "kirocrew stop"

    def test_a_stop_that_stopped_nothing_takes_its_mark_back(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refused(port, expect_pid=None):
            raise SystemExit(1)

        monkeypatch.setattr(cli_server, "_stop", _refused)
        monkeypatch.setattr("sys.argv", ["kirocrew", "stop"])
        with pytest.raises(SystemExit):
            cli.main()
        assert not _mark(home).exists()

    def test_a_refused_stop_leaves_an_earlier_mark_alone(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kept_trust.mark_owner_stop("ctrl-c")

        def _refused(port, expect_pid=None):
            raise SystemExit(1)

        monkeypatch.setattr(cli_server, "_stop", _refused)
        monkeypatch.setattr("sys.argv", ["kirocrew", "stop"])
        with pytest.raises(SystemExit):
            cli.main()
        assert _mark(home).exists()

    def test_the_desktop_apps_pid_pinned_stop_does_not_mark(
        self, home: Path, stops: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["kirocrew", "stop", "--port", "5476", "--expect-pid", "4242"]
        )
        cli.main()
        assert stops == [(5476, 4242, False)]

    def test_a_stop_that_is_part_of_a_restart_does_not_mark(
        self, home: Path, stops: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(kept_trust.KEEP_TRUST_ENV, "1")
        monkeypatch.setattr("sys.argv", ["kirocrew", "stop", "--port", "5476"])
        cli.main()
        assert stops == [(5476, None, False)]

    def test_a_stop_of_another_homes_gateway_does_not_mark_this_home(
        self, home: Path, stops: list, this_home: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``--port`` on another home's gateway: this home's gateway keeps running,
        # and a mark here would clear its trust at its next crash.
        this_home[0] = False
        monkeypatch.setattr("sys.argv", ["kirocrew", "stop", "--port", "9999"])
        cli.main()
        assert stops == [(9999, None, False)]
        assert not _mark(home).exists()

    def test_a_stop_that_fails_on_an_error_takes_its_mark_back(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken(port, expect_pid=None):
            raise RuntimeError("audit log unavailable")

        monkeypatch.setattr(cli_server, "_stop", _broken)
        monkeypatch.setattr("sys.argv", ["kirocrew", "stop"])
        with pytest.raises(RuntimeError):
            cli.main()
        assert not _mark(home).exists()


class TestTheStopsTarget:
    """``_stops_this_homes_gateway``: the mark is for this home's gateway only."""

    @pytest.fixture
    def owner(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        from kiro_crew import platform_compat

        # Through the facade, which reaches every owner holding the name.
        found: dict = {"owner": (4242, False), "listening": [4242]}
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: port or 5476)
        monkeypatch.setattr(cli_server, "_config_dir_owner", lambda port: found["owner"])
        monkeypatch.setattr(platform_compat, "find_listening_pids", lambda port: found["listening"])
        return found

    def test_this_homes_running_gateway_is_the_target(self, owner: dict) -> None:
        from kiro_crew.cli_server_stop import _stops_this_homes_gateway

        assert _stops_this_homes_gateway(None) is True
        assert _stops_this_homes_gateway(5476) is True

    def test_no_gateway_in_this_home_is_no_target(self, owner: dict) -> None:
        from kiro_crew.cli_server_stop import _stops_this_homes_gateway

        owner["owner"] = None
        assert _stops_this_homes_gateway(None) is False

    def test_a_port_this_homes_gateway_does_not_hold_is_another_home(self, owner: dict) -> None:
        from kiro_crew.cli_server_stop import _stops_this_homes_gateway

        owner["listening"] = [777]
        assert _stops_this_homes_gateway(9999) is False

    def test_a_failed_probe_is_no_target(
        self, owner: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.cli_server_stop import _stops_this_homes_gateway

        def _boom(port):
            raise OSError("lsof")

        monkeypatch.setattr(cli_server, "_config_dir_owner", _boom)
        assert _stops_this_homes_gateway(None) is False


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX root only")
def test_a_root_stop_leaves_no_root_owned_directory(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert kept_trust.mark_owner_stop("kirocrew stop") is False
    assert not (home / kept_trust.KEPT_DIR).exists()


def test_the_directory_is_sealed_from_the_agent() -> None:
    from kiro_crew import sandbox, sandbox_precreate
    from kiro_crew.security import is_sensitive_write_path

    assert kept_trust.KEPT_DIR in sandbox._CREW_READONLY_LEAVES
    assert kept_trust.KEPT_DIR in sandbox_precreate._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    for name in (kept_trust.OWNER_STOP_FILE, kept_trust.CHATS_FILE, kept_trust.YOLO_FILE):
        assert is_sensitive_write_path(f"~/.kiro/crew/{kept_trust.KEPT_DIR}/{name}") is True
