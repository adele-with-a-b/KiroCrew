"""The chat keeper's pure parts: framing, proofs, its command line, records, and the
consumed-line accounting the gateway acks from. No process is started here; the
real keeper is ``test_chat_keeper_process.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.chat_keeper import records, wire
from kiro_crew.chat_keeper.keeper import Keeper, KeeperArgs, parse_args
from kiro_crew.chat_keeper.transport import AttachState, SeqStreamReader


def test_frames_round_trip_through_a_byte_at_a_time_feed() -> None:
    data = (
        wire.frame(wire.K_STDIN, b"abc")
        + wire.seq_frame(wire.K_STDOUT, 7, b'{"x":1}\n')
        + wire.json_frame(wire.K_EXIT, {"returncode": 3})
    )
    decoder = wire.FrameDecoder()
    frames: list[tuple[int, bytes]] = []
    for byte in data:
        frames.extend(decoder.feed(bytes([byte])))
    assert frames[0] == (wire.K_STDIN, b"abc")
    assert wire.split_seq(frames[1][1]) == (7, b'{"x":1}\n')
    assert wire.parse_json(frames[2][1]) == {"returncode": 3}
    assert decoder.pending == 0


def test_a_frame_length_out_of_bounds_breaks_the_wire() -> None:
    decoder = wire.FrameDecoder(max_frame=16)
    with pytest.raises(wire.WireError):
        decoder.feed(b"\x00\x00\x01\x00\x0a")
    with pytest.raises(wire.WireError):
        wire.FrameDecoder().feed(b"\x00\x00\x00\x00\x0a")


def test_a_proof_is_bound_to_role_instance_and_both_nonces() -> None:
    secret, k, g = b"s" * 32, b"k" * 32, b"g" * 32
    base = wire.proof(secret, wire.ROLE_GATEWAY, "abc", k, g)
    assert wire.proof_matches(base, base)
    assert base != wire.proof(secret, wire.ROLE_KEEPER, "abc", k, g)
    assert base != wire.proof(secret, wire.ROLE_GATEWAY, "abd", k, g)
    assert base != wire.proof(secret, wire.ROLE_GATEWAY, "abc", g, k)
    assert base != wire.proof(b"t" * 32, wire.ROLE_GATEWAY, "abc", k, g)
    assert not wire.proof_matches(base, None)
    assert not wire.proof_matches(base, base.encode())


def test_the_keeper_command_line_is_parsed_strictly_and_carries_no_adapter_argv() -> None:
    args = parse_args(
        ["--kirocrew-chat-keeper", "--instance", "abc123", "--dir", "/d", "--adopt-secs", "30"]
    )
    assert args.instance == "abc123" and args.adopt_secs == 30.0 and args.argv == []
    for bad in (
        [],
        ["--instance", "a"],
        ["--kirocrew-chat-keeper", "--instance", "a/b", "--dir", "/d", "--adopt-secs", "3"],
        ["--kirocrew-chat-keeper", "--instance", "a", "--dir", "/d", "--adopt-secs", "0"],
        ["--kirocrew-chat-keeper", "--instance", "a", "--dir", "/d"],
    ):
        with pytest.raises(SystemExit):
            parse_args(bad)


def test_the_launch_command_line_does_not_look_like_an_orphan_mcp() -> None:
    """Every gateway's orphan-MCP sweep reads a reparented python's argv for the
    sandbox launcher's marker; the keeper's must never carry it."""
    from kiro_crew.chat_keeper.spawn import KeeperSpawn
    from kiro_crew.session_pid_orphan_scan import _SANDBOX_LAUNCHER_MARKER, _is_orphan_mcp

    spawn = KeeperSpawn(session_key="dashboard:1", backend="claude", adopt_secs=60, stdout_limit=1)
    inner = ["/usr/bin/sandbox-exec", "-f", "/tmp/kirocrew_sandbox_x.sb", "node", "acp.js"]
    argv = spawn.wrap(inner)
    line = " ".join(argv).encode()
    assert _SANDBOX_LAUNCHER_MARKER not in line and not _is_orphan_mcp(line)
    assert _is_orphan_mcp(b"python3 -c x " + b" ".join(a.encode() for a in inner)), "control"
    assert spawn._inner == inner


def test_a_record_round_trips_and_a_malformed_one_is_dropped(tmp_path) -> None:
    record = records.KeeperRecord(
        instance="abc123def456",
        session_key="dashboard:1",
        backend="claude",
        keeper_pid=4242,
        keeper_start="start",
        adapter_pid=4243,
        socket="/x.s",
        wire=1,
        created=1.0,
        launch={"sandbox": "f00", "scratch_dir": "/s"},
    )
    records.ensure_dir(tmp_path)
    records.write_record(record, tmp_path)
    assert records.load_records(tmp_path) == [record]
    (records.keepers_dir(tmp_path) / "other.json").write_text("{}", encoding="utf-8")
    (records.keepers_dir(tmp_path) / "bad.json").write_text("not json", encoding="utf-8")
    assert records.load_records(tmp_path) == [record]
    assert records.KeeperRecord.from_json({**record.__dict__, "instance": "../x"}) is None
    assert records.KeeperRecord.from_json({**record.__dict__, "keeper_pid": 1}) is None


def test_the_keepers_directory_is_owner_only(tmp_path) -> None:
    path = records.ensure_dir(tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o700
    records.write_key("abc123def456", b"\x01" * 32, tmp_path)
    key = path / "abc123def456.key"
    assert (key.stat().st_mode & 0o777) == 0o600
    assert records.read_key("abc123def456", tmp_path) == b"\x01" * 32


def _keeper() -> Keeper:
    args = KeeperArgs(
        instance="abc",
        directory=__import__("pathlib").Path("/nonexistent"),
        adopt_secs=30.0,
        argv=["x"],
    )
    return Keeper(args, b"s" * 32, "dashboard:1")


def _line(body: dict) -> bytes:
    return json.dumps(body).encode() + b"\n"


def test_the_keeper_tracks_open_requests_ids_and_session_state() -> None:
    keeper = _keeper()
    from kiro_crew.chat_keeper.keeper import _Attachment

    att = _Attachment(writer=None, generation=1, held=False)  # type: ignore[arg-type]
    keeper._track_stdin(att, _line({"id": 4, "method": "session/new", "params": {}}))
    keeper._track_stdin(
        att, _line({"id": 5, "method": "session/prompt", "params": {"sessionId": "S1"}})
    )
    third = _line({"id": 6, "method": "session/set_mode", "params": {"sessionId": "S1"}})
    keeper._track_stdin(att, third[:10])
    assert keeper.max_client_id == 5  # the third line is still partial
    keeper._track_stdin(att, third[10:])
    assert keeper.max_client_id == 6
    # A session answer is cached; an answer to an attachment's own request is
    # owned by it, a prompt's answer by nobody.
    assert keeper._track_stdout(1, _line({"id": 4, "result": {"sessionId": "S1"}})) == 1
    assert keeper.session_answers["S1"]
    keeper._track_stdout(
        2,
        _line(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "S1",
                    "update": {"sessionUpdate": "usage_update", "used": 1},
                },
            }
        ),
    )
    keeper._track_stdout(
        3,
        _line(
            {
                "method": "session/update",
                "params": {"sessionId": "S1", "update": {"sessionUpdate": "agent_message_chunk"}},
            }
        ),
    )
    assert set(keeper.session_updates["S1"]) == {"usage_update"}
    keeper._track_stdout(4, _line({"id": 900, "method": "session/request_permission"}))
    assert set(keeper.open_agent_requests) == {"900"}
    keeper._track_stdin(att, _line({"id": 900, "result": {}}))
    assert keeper.open_agent_requests == {}
    assert keeper._track_stdout(5, _line({"id": 5, "result": {"stopReason": "end_turn"}})) is None


def test_an_ack_trims_the_journal_and_an_overflow_marks_a_gap(monkeypatch) -> None:
    from kiro_crew.chat_keeper import keeper as keeper_mod

    keeper = _keeper()
    monkeypatch.setattr(keeper_mod, "KEEPER_JOURNAL_MAX_BYTES", 30)
    for n in range(3):
        keeper._on_stdout_line(_line({"n": n}))
    assert [e.seq for e in keeper.journal] == [1, 2, 3]
    keeper._ack(2)
    assert [e.seq for e in keeper.journal] == [3]
    assert not keeper.gap
    for n in range(5):
        keeper._on_stdout_line(_line({"n": n}))
    assert keeper.gap
    keeper._ack(99)
    assert keeper.last_ack == keeper.seq
    assert not keeper.gap, "an ack past the dropped lines means a gateway read them live"


def test_a_gap_holds_until_a_gateway_acks_past_what_was_dropped(monkeypatch) -> None:
    from kiro_crew.chat_keeper import keeper as keeper_mod

    keeper = _keeper()
    monkeypatch.setattr(keeper_mod, "KEEPER_JOURNAL_MAX_BYTES", 30)
    for n in range(5):
        keeper._on_stdout_line(_line({"n": n}))
    dropped = keeper.dropped_through
    assert dropped and keeper.gap
    keeper._ack(dropped - 1)
    assert keeper.gap, "an ack short of the dropped line leaves it missing"
    keeper._ack(dropped)
    assert not keeper.gap


@pytest.mark.asyncio
async def test_consumed_seq_counts_whole_lines_taken_by_any_read() -> None:
    reader = SeqStreamReader(limit=1024, loop=asyncio.get_running_loop())
    reader.feed_line(1, b"aa\n")
    reader.feed_line(2, b"bbbb\n")
    assert reader.consumed_seq == 0
    assert await reader.readline() == b"aa\n"
    assert reader.consumed_seq == 1
    assert await reader.read(2) == b"bb"
    assert reader.consumed_seq == 1
    assert await reader.readline() == b"bb\n"
    assert reader.consumed_seq == 2


def test_the_attach_state_reads_the_cached_session_answer_and_updates() -> None:
    answer = json.dumps({"id": 4, "result": {"sessionId": "S1", "modes": {"currentModeId": "x"}}})
    update = json.dumps({"method": "session/update", "params": {"sessionId": "S1"}})
    state = AttachState.from_json(
        {
            "session_key": "k",
            "keeper_pid": 2,
            "adapter_pid": 3,
            "returncode": None,
            "max_client_id": 9,
            "last_ack": 1,
            "seq": 2,
            "gap": False,
            "open_agent_requests": 0,
            "session_answers": {"S1": answer},
            "session_updates": {"S1": {"usage_update": update}},
        }
    )
    assert state.session_answer("S1") == {"sessionId": "S1", "modes": {"currentModeId": "x"}}
    assert state.session_answer("S2") is None
    assert state.cached_updates("S1") == [json.loads(update)]


def test_the_transport_imports_where_signal_has_no_sigkill() -> None:
    """Windows has no ``signal.SIGKILL``; every client's shutdown imports the transport."""
    import subprocess
    import sys

    probe = (
        "import signal; del signal.SIGKILL; "
        "from kiro_crew.chat_keeper import transport; print(transport.LOST_RETURNCODE)"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert int(done.stdout.strip()) < 0


@pytest.mark.asyncio
async def test_a_held_reader_acks_no_higher_than_the_attach_until_the_backlog_is_read() -> None:

    reader = SeqStreamReader(1 << 20, asyncio.get_running_loop(), ceiling=3)
    reader.feed_line(10, b"early\n")
    assert await reader.readline() == b"early\n"
    assert reader.consumed_seq == 3 and reader.last_consumed_line == 10
    reader.feed_line(4, b"four\n")
    reader.feed_line(5, b"five\n")
    reader.mark_replayed()
    assert await reader.readline() == b"four\n"
    assert reader.consumed_seq == 3, "five is still unread"
    assert await reader.readline() == b"five\n"
    assert reader.consumed_seq == 10
    reader.feed_line(11, b"live\n")
    assert reader.consumed_seq == 10, "never above a line still in the buffer"


@pytest.mark.asyncio
async def test_a_reader_drops_what_is_unconsumed_and_keeps_its_ack() -> None:

    reader = SeqStreamReader(1 << 20, asyncio.get_running_loop())
    reader.feed_line(1, b"one\n")
    reader.feed_line(2, b"two\n")
    assert await reader.readline() == b"one\n"
    reader.discard_unconsumed()
    reader.feed_eof()
    assert await reader.read() == b"" and reader.consumed_seq == 1


class _Exited(BaseException):
    pass


@pytest.mark.asyncio
async def test_a_keeper_whose_teardown_cannot_signal_still_exits(tmp_path, monkeypatch) -> None:
    from kiro_crew.chat_keeper import keeper as keeper_mod

    def _gone(_sig: int) -> None:
        raise ImportError("the installed package was replaced")

    def _exit(code: int) -> None:
        raise _Exited(code)

    monkeypatch.setattr(keeper_mod, "_SIGNAL_OWN_GROUP", _gone)
    monkeypatch.setattr(keeper_mod.os, "_exit", _exit)
    kept = Keeper(KeeperArgs("abc123", tmp_path, 300.0, ["x"]), b"\0" * 32, "dashboard:k")
    with pytest.raises(_Exited):
        await kept.terminate("orphaned")
    assert kept.terminating


def test_an_oversize_line_counts_as_a_gap(monkeypatch) -> None:
    from kiro_crew.chat_keeper import wire

    keeper = _keeper()
    monkeypatch.setattr(wire, "MAX_FRAME_BYTES", 64)
    keeper._on_stdout_line(_line({"id": 9, "method": "session/request_permission", "n": "x" * 99}))
    assert keeper.gap, "a line no frame can carry reaches no gateway"
    assert not keeper.open_agent_requests, "a request kept from it could never be re-delivered"
    keeper._ack(keeper.seq)
    assert not keeper.gap


def test_a_finished_tool_call_is_forgotten_and_the_cache_is_bounded(monkeypatch) -> None:
    from kiro_crew.chat_keeper import keeper as keeper_mod

    keeper = _keeper()

    def _update(**update: object) -> bytes:
        return _line({"method": "session/update", "params": {"sessionId": "S1", "update": update}})

    keeper._on_stdout_line(_update(sessionUpdate="tool_call", toolCallId="T1"))
    assert "T1" in keeper.tool_calls
    keeper._on_stdout_line(
        _update(sessionUpdate="tool_call_update", toolCallId="T1", status="completed")
    )
    assert "T1" not in keeper.tool_calls and keeper.tool_call_bytes == 0
    monkeypatch.setattr(keeper_mod, "_TOOL_CALL_CACHE_MAX_BYTES", 300)
    for n in range(10):
        keeper._on_stdout_line(_update(sessionUpdate="tool_call", toolCallId=f"C{n}", pad="p" * 60))
    assert (
        keeper.tool_call_bytes <= 300
        and "C9" in keeper.tool_calls
        and "C0" not in keeper.tool_calls
    )
