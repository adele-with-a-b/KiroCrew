"""A real chat keeper over a stand-in adapter: detach, journal, replay, exit, reaping.

Each test starts its own keeper because each asserts how one keeper's life ends
(an exit, the orphan timer, a terminate, a refusal), which a shared keeper could
not show twice. The adapter is ``fixtures/chat_keeper/fake_adapter.py``; every wait
is on a frame, a file the fake writes, or a file the keeper removes, under the
lost-run bound. Design: runtime-ownership.md, "Chats that outlive the gateway".
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import chat_keeper, platform_compat
from kiro_crew.chat_keeper import records
from kiro_crew.chat_keeper.spawn import KeeperSpawn
from kiro_crew.chat_keeper.transport import KeeperAttachError, KeeperProcess, attach
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = [
    pytest.mark.skipif(not chat_keeper.supported(), reason="the chat keeper is POSIX only"),
    pytest.mark.xdist_group(name="chat_keeper_process"),
]

FAKE_ADAPTER = Path(__file__).parent / "fixtures" / "chat_keeper" / "fake_adapter.py"
STDOUT_LIMIT = 10 * 1024 * 1024
SESSION_KEY = "dashboard:chat-keeper-test"


@pytest.fixture
def home(short_sock_dir: Path):
    yield short_sock_dir
    # End every keeper a test left running: each leads its own group.
    for record in records.live_records(short_sock_dir):
        try:
            os.killpg(record.keeper_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


async def _start(home: Path, *, adopt_secs: int = 60) -> KeeperProcess:
    spawn = KeeperSpawn(
        session_key=SESSION_KEY,
        backend="claude",
        adopt_secs=adopt_secs,
        stdout_limit=STDOUT_LIMIT,
        home=home,
    )
    assert await asyncio.to_thread(spawn.prepare)
    launcher = await asyncio.create_subprocess_exec(
        *spawn.wrap([sys.executable, str(FAKE_ADAPTER)]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    return await spawn.finish(launcher)


async def _send(process: KeeperProcess, body: dict[str, Any]) -> None:
    process.stdin.write(json.dumps({"jsonrpc": "2.0", **body}).encode("utf-8") + b"\n")
    await process.stdin.drain()


async def _read(process: KeeperProcess) -> dict[str, Any]:
    line = await await_until(asyncio.ensure_future(process.stdout.readline()), what="a frame")
    assert line, "the keeper connection ended"
    return json.loads(line)


def _text(msg: dict[str, Any]) -> str | None:
    update = (msg.get("params") or {}).get("update") or {}
    return (update.get("content") or {}).get("text")


async def _reattach(home: Path, *, hold: bool, secret: bytes | None = None) -> KeeperProcess:
    (record,) = records.live_records(home)
    key = secret if secret is not None else records.read_key(record.instance, home)
    assert key is not None
    return await attach(record, key, hold=hold, stdout_limit=STDOUT_LIMIT)


def _gate(home: Path, name: str) -> str:
    path = home / name
    os.mkfifo(path)
    return str(path)


async def _open_gate(gate: str) -> None:
    """Release the fake adapter waiting on *gate*; the write returns once it has read."""
    write = asyncio.to_thread(Path(gate).write_text, "go", encoding="utf-8")
    await await_until(asyncio.ensure_future(write), what=f"the fake adapter opening {gate}")


def _files(home: Path) -> list[str]:
    try:
        return sorted(os.listdir(records.keepers_dir(home)))
    except FileNotFoundError:
        return []


@pytest.mark.asyncio
async def test_a_detached_turn_is_replayed_exactly_once_to_a_held_attach(home: Path) -> None:
    first = await _start(home)
    record = first.record
    await _send(first, {"id": 1, "method": "session/new", "params": {}})
    assert (await _read(first))["result"]["sessionId"] == "S1"
    gate = _gate(home, "gate")
    await _send(
        first,
        {
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": "S1", "before": 2, "ask": True, "gate": gate, "after": 3},
        },
    )
    assert [_text(await _read(first)) for _ in range(2)] == ["b0", "b1"]
    ask = await _read(first)
    assert ask["method"] == "session/request_permission" and ask["id"] == 900
    # The old gateway saw the ask and never answered it, then went away.
    await first.detach(first.stdout.consumed_seq)

    second = await _reattach(home, hold=True)
    assert second.state.max_client_id == 2
    assert second.state.last_ack == 4
    assert second.state.session_answer("S1") == {
        "sessionId": "S1",
        "modes": {"currentModeId": "default"},
    }
    # While held, only an answer to this attachment's own request comes through.
    await _send(second, {"id": 10, "method": "echo", "params": {"held": True}})
    assert await _read(second) == {"jsonrpc": "2.0", "id": 10, "result": {"held": True}}
    # That answer is numbered above the backlog it was delivered ahead of.
    assert second.stdout.consumed_seq == 4, "an ack now would drop the unread backlog"
    assert second.resume()
    redelivered = await _read(second)
    assert redelivered["id"] == 900 and redelivered["method"] == "session/request_permission"
    await _send(second, {"id": 900, "result": {"outcome": "allow"}})
    assert _text(await _read(second)) == 'answered:{"outcome": "allow"}'
    await _open_gate(gate)
    assert [_text(await _read(second)) for _ in range(3)] == ["a0", "a1", "a2"]
    assert await _read(second) == {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}}
    assert second.pid == record.keeper_pid
    assert second.stdout.consumed_seq > 4, "past the backlog, the ack follows the reader"


@pytest.mark.asyncio
async def test_a_redelivered_permission_request_comes_after_its_tool_call(home: Path) -> None:
    first = await _start(home)
    await _send(first, {"id": 1, "method": "session/new", "params": {}})
    await _read(first)
    params = {"sessionId": "S1", "ask": True, "tool": True}
    await _send(first, {"id": 2, "method": "session/prompt", "params": params})
    tool_call = await _read(first)
    assert tool_call["params"]["update"]["sessionUpdate"] == "tool_call"
    assert (await _read(first))["id"] == 900
    await first.detach(first.stdout.consumed_seq)

    second = await _reattach(home, hold=True)
    assert second.resume()
    assert await _read(second) == tool_call, "the new gateway judges the ask with its call"
    redelivered = await _read(second)
    assert redelivered["id"] == 900 and redelivered["params"]["toolCall"]["toolCallId"] == "T1"
    await _send(second, {"id": 900, "result": {"outcome": "allow"}})
    assert _text(await _read(second)) == 'answered:{"outcome": "allow"}'


@pytest.mark.asyncio
async def test_the_welcome_reports_the_gap_and_the_approvals_that_waited_in_it(
    home: Path,
) -> None:
    # The restart receipt's numbers: no gap on a first attach, then the detached
    # time and the permission request left open through it.
    first = await _start(home)
    assert first.state.detached_secs is None and first.state.permission_waits == 0
    assert first.state.turn_at_detach is False
    await _send(first, {"id": 1, "method": "session/new", "params": {}})
    await _read(first)
    params = {"sessionId": "S1", "ask": True}
    await _send(first, {"id": 2, "method": "session/prompt", "params": params})
    assert (await _read(first))["id"] == 900
    await first.detach(first.stdout.consumed_seq)

    second = await _reattach(home, hold=True)
    gap = second.state.detached_secs
    assert gap is not None and gap >= 0
    assert second.state.turn_at_detach is True, "the prompt was unanswered at the detach"
    assert second.state.permission_waits == 1
    assert 0 <= second.state.permission_wait_secs <= gap
    assert second.resume()
    assert (await _read(second))["id"] == 900
    await _send(second, {"id": 900, "result": {"outcome": "allow"}})
    assert _text(await _read(second)) == 'answered:{"outcome": "allow"}'
    await second.detach(second.stdout.consumed_seq)

    third = await _reattach(home, hold=True)
    assert third.state.detached_secs is not None
    assert third.state.permission_waits == 0, "an answered request no longer waits"
    assert third.state.permission_wait_secs == 0.0


@pytest.mark.asyncio
async def test_a_journaled_permission_request_comes_after_its_acked_tool_call(home: Path) -> None:
    first = await _start(home)
    await _send(first, {"id": 1, "method": "session/new", "params": {}})
    await _read(first)
    params = {"sessionId": "S1", "ask": True, "tool": True}
    await _send(first, {"id": 2, "method": "session/prompt", "params": params})
    tool_call = await _read(first)
    assert tool_call["params"]["update"]["sessionUpdate"] == "tool_call"
    # The old gateway read the tool_call but not yet the request after it.
    await first.detach(first.stdout.consumed_seq)

    second = await _reattach(home, hold=True)
    assert second.resume()
    assert await _read(second) == tool_call, "the replayed ask must not arrive bare"
    redelivered = await _read(second)
    assert redelivered["id"] == 900 and redelivered["params"]["toolCall"]["toolCallId"] == "T1"


@pytest.mark.asyncio
async def test_a_line_a_detach_left_unread_is_replayed_and_not_streamed(home: Path) -> None:
    first = await _start(home)
    await _send(first, {"id": 1, "method": "session/new", "params": {}})
    await _read(first)
    await _send(
        first,
        {"id": 2, "method": "session/prompt", "params": {"sessionId": "S1", "before": 3}},
    )
    assert _text(await _read(first)) == "b0"
    await async_wait_until(
        lambda: b"b2" in bytes(first.stdout._buffer),  # type: ignore[attr-defined]
        describe=lambda: bytes(first.stdout._buffer),  # type: ignore[attr-defined]
    )
    await first.detach(first.stdout.consumed_seq)
    leftover = await await_until(asyncio.ensure_future(first.stdout.read()), what="the EOF")
    assert leftover == b"", "the old gateway would stream what the next one is replayed"
    second = await _reattach(home, hold=True)
    assert second.resume()
    assert [_text(await _read(second)) for _ in range(2)] == ["b1", "b2"]


@pytest.mark.asyncio
async def test_an_answer_to_a_previous_attachments_request_is_not_delivered(home: Path) -> None:
    first = await _start(home)
    gate = _gate(home, "echo-gate")
    await _send(first, {"id": 5, "method": "echo", "params": {"gate": gate}})
    await first.detach(first.stdout.consumed_seq)
    second = await _reattach(home, hold=False)
    await _open_gate(gate)
    await async_wait_until(lambda: Path(gate + ".done").exists(), describe=lambda: _files(home))
    await _send(second, {"id": 6, "method": "echo", "params": {"n": 6}})
    # Pipe order puts the answer to 5 first; it belonged to the attachment that is gone.
    assert await _read(second) == {"jsonrpc": "2.0", "id": 6, "result": {"n": 6}}


@pytest.mark.asyncio
async def test_an_exit_while_detached_reaches_the_next_attach_after_the_journal(
    home: Path,
) -> None:
    first = await _start(home)
    gate = _gate(home, "gate")
    await _send(first, {"id": 3, "method": "echo", "params": {"gate": gate}})
    await first.detach(first.stdout.consumed_seq)
    await _open_gate(gate)
    await async_wait_until(lambda: Path(gate + ".done").exists(), describe=lambda: _files(home))
    second = await _reattach(home, hold=True)
    await _send(second, {"id": 4, "method": "exit", "params": {"code": 7}})
    assert second.returncode is None
    second.resume()
    assert await await_until(asyncio.ensure_future(second.wait()), what="the exit") == 7
    # The keeper removes its own files once the exit is out.
    await async_wait_until(lambda: _files(home) == [], describe=lambda: _files(home))


@pytest.mark.asyncio
async def test_a_keeper_no_gateway_comes_back_for_ends_its_group(home: Path) -> None:
    first = await _start(home, adopt_secs=1)
    adapter_pid = first.state.adapter_pid
    await first.detach(first.stdout.consumed_seq)
    await async_wait_until(lambda: _files(home) == [], describe=lambda: _files(home))
    await async_wait_until(
        lambda: not platform_compat.pid_exists(adapter_pid), describe=lambda: adapter_pid
    )


@pytest.mark.asyncio
async def test_a_terminate_request_ends_the_adapter_and_reports_its_exit(home: Path) -> None:
    process = await _start(home)
    assert process.request_terminate()
    code = await await_until(asyncio.ensure_future(process.wait()), what="the exit")
    assert code == -signal.SIGTERM
    await async_wait_until(lambda: _files(home) == [], describe=lambda: _files(home))


@pytest.mark.asyncio
async def test_a_signal_to_the_group_ends_the_keeper_and_its_files(home: Path) -> None:
    process = await _start(home)
    os.killpg(process.pid, signal.SIGTERM)
    assert await await_until(asyncio.ensure_future(process.wait()), what="the exit") is not None
    await async_wait_until(lambda: _files(home) == [], describe=lambda: _files(home))


@pytest.mark.asyncio
async def test_a_wrong_secret_is_refused_and_the_attachment_stands(home: Path) -> None:
    first = await _start(home)
    with pytest.raises(KeeperAttachError) as refused:
        await _reattach(home, hold=False, secret=b"\x00" * 32)
    assert refused.value.reason == "refused"
    await _send(first, {"id": 1, "method": "echo", "params": {"still": "attached"}})
    assert (await _read(first))["result"] == {"still": "attached"}


@pytest.mark.asyncio
async def test_a_handshake_past_its_deadline_does_not_take_the_attachment(home: Path) -> None:
    import socket
    import time

    from kiro_crew.chat_keeper import transport

    first = await _start(home)
    record = first.record
    key = records.read_key(record.instance, home)
    assert key is not None
    # The attach gave up on this handshake before its thread ran.
    late = asyncio.to_thread(transport._handshake, record, key, False, time.monotonic() - 1.0)
    with pytest.raises(socket.timeout):
        await await_until(asyncio.ensure_future(late), what="the late handshake")
    await _send(first, {"id": 1, "method": "echo", "params": {"still": "attached"}})
    assert (await _read(first))["result"] == {"still": "attached"}


@pytest.mark.asyncio
async def test_a_gateway_gone_without_a_detach_leaves_the_keeper_to_its_timer(
    home: Path,
) -> None:
    first = await _start(home, adopt_secs=1)
    adapter_pid = first.state.adapter_pid
    transport = first._transport
    assert transport is not None
    # A gateway the shutdown cap cut off before its detach frame: the socket just ends.
    transport.abort()
    await async_wait_until(lambda: _files(home) == [], describe=lambda: _files(home))
    await async_wait_until(
        lambda: not platform_compat.pid_exists(adapter_pid), describe=lambda: adapter_pid
    )


@pytest.mark.asyncio
async def test_a_record_naming_another_chat_is_refused(home: Path) -> None:
    first = await _start(home)
    record = first.record
    forged = records.KeeperRecord(**{**record.__dict__, "session_key": "dashboard:other"})
    key = records.read_key(record.instance, home)
    assert key is not None
    with pytest.raises(KeeperAttachError) as refused:
        await attach(forged, key, hold=True, stdout_limit=STDOUT_LIMIT)
    assert refused.value.reason == "session"


@pytest.mark.asyncio
async def test_a_record_naming_another_pid_is_refused_before_any_byte(home: Path) -> None:
    first = await _start(home)
    record = first.record
    forged = records.KeeperRecord(**{**record.__dict__, "keeper_pid": os.getpid()})
    key = records.read_key(record.instance, home)
    assert key is not None
    with pytest.raises(KeeperAttachError) as refused:
        await attach(forged, key, hold=True, stdout_limit=STDOUT_LIMIT)
    assert refused.value.reason == "peer"


@pytest.mark.asyncio
async def test_a_gateway_on_another_home_does_not_take_a_keeper_for_an_orphan(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    """A keeper of home A, reparented by its gateway's exit, is no orphan to home B's sweep.

    Home B holds no record of it, so the only thing that spares it there is its
    command line: the adapter argv, sandbox launcher marker included, goes on the
    keeper's stdin. The control, a reparented child whose argv carries that marker,
    is a candidate in the same pass, so the pass does reach reparented pids.
    """
    from kiro_crew import session_pid
    from kiro_crew import session_pid_orphan_scan as scan

    keeper = await _start(home)
    control = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdin.read()",
        "/x/kirocrew_sandbox_probe",
        stdin=asyncio.subprocess.PIPE,
    )
    try:
        home_b = tmp_path / "home-b"
        home_b.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home_b))
        assert records.live_keeper_pids() == frozenset(), "home B knows no keeper"
        # What a gateway exit leaves: both reparented to init. The age floor is
        # lifted so it cannot be what spares the keeper.
        monkeypatch.setattr(scan, "_reparented_pids", lambda: [keeper.pid, control.pid])
        monkeypatch.setattr(session_pid, "_ORPHAN_MIN_AGE_SECONDS", 0)

        candidates = scan.find_orphan_mcp_candidates(set())

        assert control.pid in candidates, "the control: a marked orphan is a candidate"
        assert keeper.pid not in candidates
        assert records.live_keeper_pids(home) == {keeper.pid}
    finally:
        assert control.stdin is not None
        control.stdin.close()
        await await_until(asyncio.ensure_future(control.wait()), what="the control's exit")


@pytest.mark.asyncio
async def test_the_adapter_runs_with_the_gateways_umask_not_the_keepers(home: Path) -> None:
    """The keeper's 0o077 guards its own files; what the agent creates keeps the gateway's mask."""
    previous = os.umask(0o022)
    try:
        process = await _start(home)
    finally:
        os.umask(previous)
    await _send(process, {"id": 1, "method": "umask", "params": {}})
    assert (await _read(process))["result"] == {"umask": 0o022}


@pytest.mark.asyncio
async def test_a_keeper_not_adopted_is_gone_before_the_end_returns(home: Path) -> None:
    from kiro_crew.acp.client_keeper import _end_unadopted

    process = await _start(home)
    await _send(process, {"id": 1, "method": "ignore_term", "params": {}})
    assert (await _read(process))["result"] == {}
    (record,) = records.live_records(home)
    await await_until(
        asyncio.ensure_future(_end_unadopted(record, "a test")), what="the end of the keeper"
    )
    assert not platform_compat.pgroup_exists(record.keeper_pid), "a second adapter could start"
    assert _files(home) == [], "a keeper ended from outside leaves no files behind"


@pytest.mark.asyncio
async def test_the_files_of_a_killed_keeper_are_removed_by_the_end(home: Path) -> None:
    from kiro_crew.acp.client_keeper import _end_unadopted

    process = await _start(home)
    (record,) = records.live_records(home)
    os.killpg(process.pid, signal.SIGKILL)
    await async_wait_until(
        lambda: not platform_compat.pgroup_exists(record.keeper_pid),
        describe=lambda: record.keeper_pid,
    )
    assert _files(home), "a SIGKILL leaves the keeper no chance to remove its files"
    await await_until(
        asyncio.ensure_future(_end_unadopted(record, "a test")), what="the end of the keeper"
    )
    assert _files(home) == []


@pytest.mark.asyncio
async def test_a_teardown_that_killed_its_keeper_removes_the_files(home: Path) -> None:
    from types import SimpleNamespace

    from kiro_crew.acp.client_keeper import forget_if_ended

    process = await _start(home)
    client = SimpleNamespace(_process=process)
    await forget_if_ended(client)  # type: ignore[arg-type]
    assert _files(home), "a live keeper keeps its files"
    os.killpg(process.pid, signal.SIGKILL)
    await async_wait_until(
        lambda: records.is_dead(process.record), describe=lambda: process.record.keeper_pid
    )
    await forget_if_ended(client)  # type: ignore[arg-type]
    assert _files(home) == []


@pytest.mark.asyncio
async def test_a_model_config_option_the_old_gateway_set_reaches_the_next_one(home: Path) -> None:
    """claude moves its model with a ``model`` config option write. The adopting
    gateway reads ``models.currentModelId`` from the cached answer, so a keeper
    that cached only ``session/set_model`` made a switched chat come back on the
    model it was created on."""
    first = await _start(home)
    await _send(first, {"id": 1, "method": "session/new", "params": {"models": True}})
    await _read(first)
    # The adapter answers with the spelling it runs, which the keeper keeps
    # over the value the gateway wrote.
    write = {"sessionId": "S1", "configId": "model", "value": "m2", "answer_value": "m2-full"}
    await _send(first, {"id": 2, "method": "session/set_config_option", "params": write})
    await _read(first)
    await first.detach(first.stdout.consumed_seq)
    second = await _reattach(home, hold=True)
    assert second.state.session_answer("S1")["models"]["currentModelId"] == "m2-full"
    (options,) = second.state.cached_updates("S1")
    assert options["params"]["update"] == {
        "sessionUpdate": "config_option_update",
        "configOptions": [{"id": "model", "currentValue": "m2-full"}],
    }


@pytest.mark.asyncio
async def test_a_mode_and_model_the_old_gateway_set_reach_the_next_one(home: Path) -> None:
    first = await _start(home)
    await _send(first, {"id": 1, "method": "session/new", "params": {"models": True}})
    await _read(first)
    await _send(
        first,
        {"id": 2, "method": "session/set_mode", "params": {"sessionId": "S1", "modeId": "plan"}},
    )
    await _read(first)
    await _send(
        first,
        {"id": 3, "method": "session/set_model", "params": {"sessionId": "S1", "modelId": "m2"}},
    )
    await _read(first)
    await first.detach(first.stdout.consumed_seq)
    second = await _reattach(home, hold=True)
    assert second.state.session_answer("S1")["models"]["currentModelId"] == "m2"
    (mode,) = second.state.cached_updates("S1")
    assert mode["params"]["update"] == {
        "sessionUpdate": "current_mode_update",
        "currentModeId": "plan",
    }
