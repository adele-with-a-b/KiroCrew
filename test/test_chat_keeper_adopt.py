"""The client side of a chat keeper: who may run under one, the adopt handshake, detach.

The handshake runs against a stand-in client that records what it sends and
answers from a script; the keeper side is ``test_chat_keeper_process.py``. Design:
runtime-ownership.md, "Chats that outlive the gateway".
"""

from __future__ import annotations

import asyncio
import collections
import json
import weakref
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import chat_keeper
from kiro_crew.acp import client_keeper
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.chat_keeper.transport import AttachState

SID = "11111111-2222-3333-4444-555555555555"


def _state(**over: Any) -> AttachState:
    answer = {"id": 4, "result": {"sessionId": SID, "modes": {"currentModeId": "default"}}}
    mode = {
        "method": "session/update",
        "params": {
            "sessionId": SID,
            "update": {"sessionUpdate": "current_mode_update", "currentModeId": "plan"},
        },
    }
    body = {
        "session_key": "dashboard:chat-1",
        "keeper_pid": 4242,
        "adapter_pid": 4243,
        "returncode": None,
        "max_client_id": 17,
        "last_ack": 3,
        "seq": 9,
        "gap": False,
        "open_agent_requests": 0,
        "session_answers": {SID: json.dumps(answer)},
        "session_updates": {SID: {"current_mode_update": json.dumps(mode)}},
        **over,
    }
    return AttachState.from_json(body)


class _Process:
    """The members of ``KeeperProcess`` the client code reads."""

    def __init__(self, state: AttachState) -> None:
        self.state = state
        self.record = SimpleNamespace(instance="abc123def456")
        self.resumed = 0
        self.detached = False
        self.held = True
        self.returncode = None
        self.stdout = SimpleNamespace(consumed_seq=7)
        self.acked: list[int] = []

    def resume(self) -> bool:
        self.resumed += 1
        self.held = False
        return True

    def ack(self, seq: int) -> bool:
        self.acked.append(seq)
        return True

    async def detach(self, ack: int) -> None:
        self.detached = True
        self.acked.append(ack)


class _Client:
    """A stand-in for the ``AcpClient`` members the adopt handshake reads and writes."""

    backend = "claude"
    _reads_back_session_mode = False
    _pins_starting_mode = True

    def __init__(self, process: _Process, answers: dict[str, dict]) -> None:
        self._process = process
        self._session_key = "dashboard:chat-1"
        self.memory_mode = "persistent"
        self._resume_session_id: str | None = SID
        self._session_id: str | None = None
        self._resumed = False
        self._next_id = 1
        self._buffer: collections.deque = collections.deque(maxlen=100)
        self._answers = answers
        self.sent: list[tuple[int, str, dict]] = []
        self.captured: list[dict] = []
        self.pinned: list[dict] = []
        self.last_prompt_stats = SimpleNamespace(restore_cost_session=lambda sid: None)
        self.on_agent_turn: Any = object()

    def _initialize_params(self) -> dict:
        return {"protocolVersion": 1}

    async def _send_request(self, method: str, params: dict) -> int:
        rid = self._next_id
        self._next_id += 1
        self.sent.append((rid, method, params))
        return rid

    async def _wait_for_response(self, rid: int, timeout: float) -> dict:
        method = next(m for i, m, _ in self.sent if i == rid)
        return self._answers[method]

    def _note_harness_adapter_version(self) -> None:
        return None

    def _capture_available_models(self, resp: dict) -> None:
        self.captured.append(resp)

    def _store_session_config(self, resp: dict) -> None:
        return None

    def _verify_session_mode(self, resp: dict) -> None:
        return None

    async def _pin_starting_mode(self, resp: dict) -> None:
        self.pinned.append(resp)

    async def _stop_idle_reader(self) -> None:
        return None


_INIT = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, "agentInfo": {}}


@pytest.fixture(autouse=True)
def _a_keeper_process(monkeypatch):
    # The stand-in process stands for a KeeperProcess wherever the module asks.
    monkeypatch.setattr(client_keeper, "keeper_process", lambda client: client._process)


@pytest.mark.asyncio
async def test_the_adopt_handshake_initializes_asks_the_status_and_never_loads() -> None:
    process = _Process(_state())
    client = _Client(process, {"initialize": _INIT, "session/status": {"status": "open"}})
    await client_keeper.adopt_session(client)  # type: ignore[arg-type]
    assert [m for _, m, _ in client.sent] == ["initialize", "session/status"]
    # Every new id is above the highest the previous gateway used.
    assert [i for i, _, _ in client.sent] == [18, 19]
    assert client.sent[1][2] == {"sessionId": SID}
    assert client._session_id == SID and client._resumed is True
    # The session's state is the cached answer with the later mode applied.
    assert client.captured[0]["modes"]["currentModeId"] == "plan"
    assert client.pinned and client.pinned[0]["sessionId"] == SID
    assert process.resumed == 1
    assert [msg.method for msg in client._buffer] == ["session/update"]


@pytest.mark.asyncio
async def test_a_chat_whose_first_turn_never_landed_adopts_the_keepers_one_session() -> None:
    process = _Process(_state())
    client = _Client(process, {"initialize": _INIT, "session/status": {"status": "open"}})
    client._resume_session_id = None
    await client_keeper.adopt_session(client)  # type: ignore[arg-type]
    assert client._session_id == SID


@pytest.mark.asyncio
async def test_a_session_the_adapter_does_not_hold_refuses_and_keeps_the_resume_id() -> None:
    process = _Process(_state())
    client = _Client(process, {"initialize": _INIT, "session/status": {"status": "saved"}})
    with pytest.raises(client_keeper.KeeperAdoptRefused):
        await client_keeper.adopt_session(client)  # type: ignore[arg-type]
    assert client._resume_session_id == SID
    assert client._session_id is None and client._resumed is False
    assert process.resumed == 0


def test_a_refusal_is_named_for_a_gap_and_for_an_exited_adapter() -> None:
    assert client_keeper._refusal(_Process(_state())) == ""  # type: ignore[arg-type]
    assert "journal" in client_keeper._refusal(_Process(_state(gap=True)))  # type: ignore[arg-type]
    assert "exited" in client_keeper._refusal(_Process(_state(returncode=1)))  # type: ignore[arg-type]


def _tagged(seq: int | None) -> JsonRpcMessage:
    msg = JsonRpcMessage(method="session/update", params={})
    if seq is not None:
        setattr(msg, client_keeper.KEEPER_SEQ_ATTR, seq)
    return msg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held", "expected"), [([], 7), ([5, 6], 4), ([None, 6], 5)], ids=["empty", "held", "untagged"]
)
async def test_a_detach_acks_what_was_consumed_less_what_is_still_held(
    monkeypatch, held: list, expected: int
) -> None:
    process = _Process(_state())
    client = _Client(process, {})
    client._session_id = SID
    client._buffer.extend(_tagged(seq) for seq in held)
    monkeypatch.setattr(client_keeper, "detachable", lambda c: True)
    assert await client_keeper.detach(client) is True  # type: ignore[arg-type]
    assert process.acked == [expected]
    assert process.detached and client.on_agent_turn is None


@pytest.mark.parametrize(
    ("key", "backend", "memory", "expected"),
    [
        ("dashboard:chat-1", "claude", "persistent", True),
        ("_bg", "claude", "persistent", False),
        ("slack:123", "claude", "persistent", False),
        ("dashboard:chat-1", "kiro", "persistent", False),
        ("dashboard:chat-1", "claude", "incognito", False),
    ],
)
def test_only_a_persistent_dashboard_claude_chat_is_eligible(
    key, backend, memory, expected
) -> None:
    client = SimpleNamespace(_session_key=key, backend=backend, memory_mode=memory)
    assert client_keeper.eligible(client) is expected  # type: ignore[arg-type]


def test_the_safe_point_ack_waits_for_an_empty_buffer_and_a_released_hold() -> None:
    process = _Process(_state())
    client = _Client(process, {})
    client_keeper.ack_safe_point(client)  # type: ignore[arg-type]
    assert process.acked == []  # still held
    process.held = False
    client._buffer.append(_tagged(8))
    client_keeper.ack_safe_point(client)  # type: ignore[arg-type]
    assert process.acked == []  # a frame is still held
    client._buffer.clear()
    client_keeper.ack_safe_point(client)  # type: ignore[arg-type]
    assert process.acked == [7]


def test_a_chat_a_keeper_serves_is_not_marked_interrupted_at_restore(monkeypatch) -> None:
    from kiro_crew.dashboard import chat_keeper_adopt

    slot = SimpleNamespace(key="chat-1", linked_session_key="")
    assert chat_keeper_adopt.kept_by_a_keeper(slot) is False
    monkeypatch.setattr(chat_keeper_adopt, "_KEPT_SESSION_KEYS", frozenset({"dashboard:chat-1"}))
    assert chat_keeper_adopt.kept_by_a_keeper(slot) is True
    assert chat_keeper_adopt.kept_by_a_keeper(SimpleNamespace(key="chat-2")) is False


def _record(**launch: Any) -> Any:
    from kiro_crew.chat_keeper import records

    return records.KeeperRecord(
        instance="abc123def456",
        session_key="dashboard:chat-1",
        backend="claude",
        keeper_pid=4242,
        keeper_start="start",
        adapter_pid=4243,
        socket="/x.s",
        wire=1,
        created=1.0,
        launch=launch,
    )


@pytest.mark.asyncio
async def test_a_keeper_launched_under_another_sandbox_is_ended_not_adopted(monkeypatch) -> None:
    from kiro_crew.chat_keeper import records

    ended: list[str] = []

    async def _end(record: Any, why: str, **_: Any) -> None:
        ended.append(why)

    async def _on() -> bool:
        return True

    async def _no_attach(*_: Any, **__: Any) -> None:
        raise AssertionError("a keeper under another sandbox must not be attached")

    monkeypatch.setattr(records, "live_records", lambda: [_record(sandbox="old")])
    monkeypatch.setattr(client_keeper, "eligible", lambda c: True)
    monkeypatch.setattr(client_keeper, "_config_async", _on)
    monkeypatch.setattr(client_keeper, "_end_unadopted", _end)
    monkeypatch.setattr("kiro_crew.chat_keeper.transport.attach", _no_attach)
    client = SimpleNamespace(_session_key="dashboard:chat-1", backend="claude")
    assert await client_keeper.adopt_for(client, lambda: "new") is None  # type: ignore[arg-type]
    assert ended == ["its sandbox differs from this gateway's"]


@pytest.mark.asyncio
async def test_a_spawn_with_no_keeper_of_its_chat_computes_no_fingerprint(monkeypatch) -> None:
    from kiro_crew.chat_keeper import records

    def _fingerprint() -> str:
        raise AssertionError("the flag-off spawn reads only the keepers directory")

    monkeypatch.setattr(records, "live_records", lambda: [])
    client = SimpleNamespace(_session_key="dashboard:chat-1", backend="claude")
    assert await client_keeper.adopt_for(client, _fingerprint) is None  # type: ignore[arg-type]


def test_the_sandbox_fingerprint_follows_the_mode_the_masks_and_the_leaf_tables(
    monkeypatch,
) -> None:
    from kiro_crew import sandbox
    from kiro_crew.chat_keeper.spawn import sandbox_fingerprint

    base = sandbox_fingerprint("strict", ("/a",))
    assert sandbox_fingerprint("strict", ["/a"]) == base
    assert sandbox_fingerprint("off", ("/a",)) != base
    assert sandbox_fingerprint("strict", ("/a", "/b")) != base
    assert sandbox_fingerprint("strict", ("/a",), ("/e",)) != base, "an exposed file"
    assert sandbox_fingerprint("strict", ("/a",), (), True) != base, "the harness's own sandbox"
    monkeypatch.setattr(sandbox, "_CREW_HIDDEN_LEAVES", (*sandbox._CREW_HIDDEN_LEAVES, "new.key"))
    assert sandbox_fingerprint("strict", ("/a",)) != base, "an update's new HIDDEN leaf"


def test_the_sandbox_fingerprint_follows_the_floor_and_the_read_only_layer(monkeypatch) -> None:
    from kiro_crew import sandbox
    from kiro_crew.chat_keeper.spawn import sandbox_fingerprint

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "configured_readonly_layer", lambda: ((), ()))
    base = sandbox_fingerprint("standard", ())
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "strict")
    assert sandbox_fingerprint("standard", ()) != base, "a raised min_level floor"
    assert sandbox_fingerprint("standard", ()) == sandbox_fingerprint(
        "strict", ()
    ), "the tier the wrap applies, not the one asked for"
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "configured_readonly_layer", lambda: (("/ro",), ()))
    assert sandbox_fingerprint("standard", ()) != base, "a new read-only path"


@pytest.mark.asyncio
async def test_the_rules_a_kept_session_shipped_reach_the_gateway_that_adopts_it(
    tmp_path, monkeypatch
) -> None:
    from dataclasses import replace

    from kiro_crew.chat_keeper import records

    record = replace(_record(sandbox="s"), socket=str(tmp_path / "abc123def456.s"))
    process = SimpleNamespace(detached=False, record=record)
    monkeypatch.setattr(client_keeper, "keeper_process", lambda client: process)
    shipped = frozenset({("deny", "Bash(rm:*)"), ("ask", "Write")})
    creator = SimpleNamespace(_claude_shipped_restrictions=shipped)
    await client_keeper.record_session_restrictions(creator)  # type: ignore[arg-type]
    written = records.KeeperRecord.from_json(
        json.loads((tmp_path / "abc123def456.json").read_text(encoding="utf-8"))
    )
    assert written is not None and written.launch["sandbox"] == "s"
    adopter = SimpleNamespace()
    client_keeper.restore_launch_state(adopter, written)  # type: ignore[arg-type]
    assert (
        adopter._claude_shipped_restrictions == shipped
    ), "without it every project deny reads as gained and the kept chat is stopped"


def test_an_adopted_client_gets_the_launch_state_its_launcher_set(tmp_path) -> None:
    from kiro_crew.chat_keeper.spawn import launch_state

    launcher = SimpleNamespace(
        _scratch_dir=tmp_path,
        _sandbox_wrapped_by_crew=True,
        _sandbox_hidden_dirs=("/h",),
        _child_env_names=frozenset({"A", "B"}),
        _ssh_auth_sock_forwarded=True,
    )
    record = _record(**json.loads(json.dumps(launch_state(launcher, "fp"))))
    client = SimpleNamespace()
    client_keeper.restore_launch_state(client, record)  # type: ignore[arg-type]
    assert client._scratch_dir == tmp_path
    assert client._sandbox_wrapped_by_crew is True and client._ssh_auth_sock_forwarded is True
    assert client._sandbox_hidden_dirs == ("/h",)
    assert client._child_env_names == frozenset({"A", "B"})
    swept = _record(scratch_dir=str(tmp_path / "gone"))
    client_keeper.restore_launch_state(client, swept)  # type: ignore[arg-type]
    assert client._scratch_dir is None


@pytest.mark.asyncio
@pytest.mark.skipif(not chat_keeper.supported(), reason="the chat keeper is POSIX only")
async def test_a_kept_turn_that_does_not_come_back_gets_its_interrupted_row(monkeypatch) -> None:
    from kiro_crew.dashboard import chat_keeper_adopt
    from kiro_crew.dashboard.slot_persistence.turn_marker import _reconcile_local_turn_marker
    from kiro_crew.dashboard.state import _ChatSlot

    def _errors(slot: Any) -> list[dict]:
        return [row for row in slot.messages if row.get("role") == "error"]

    slot = _ChatSlot("chat-1")
    slot.append("user", "explain the plan", "msg msg-u", broadcast=False)
    slot.append("assistant", "Part one is ", "msg msg-a", broadcast=False)
    monkeypatch.setattr(chat_keeper_adopt, "_KEPT_SESSION_KEYS", frozenset({"dashboard:chat-1"}))
    assert _reconcile_local_turn_marker(slot, 1)
    assert _errors(slot) == [], "the keeper may still be running the turn"

    async def _not_attached(*_: Any) -> bool:
        return False

    ended: list[str] = []

    async def _end(record: Any, why: str, **_: Any) -> None:
        ended.append(why)

    monkeypatch.setattr(chat_keeper_adopt, "_adopt_one", _not_attached)
    monkeypatch.setattr(client_keeper, "_end_unadopted", _end)
    state = SimpleNamespace(_slots={"chat-1": slot})
    cfg = SimpleNamespace(agent=SimpleNamespace(chat_keeper=True, agent_started_turns=True))
    assert await chat_keeper_adopt.adopt_kept_chats(state, cfg, [_record()]) == 0
    assert len(_errors(slot)) == 1, "the turn did not come back: it was interrupted"
    assert ended == [
        "the eager adopt did not attach"
    ], "ended, so the first message cannot adopt it and read the old turn as its own"
    assert not chat_keeper_adopt.kept_by_a_keeper(slot), "a slot built later owes its row"


#: The two chats the restore rebuilds, as ``kept_by_a_keeper`` reads a slot.
_CHAT_1_AND_2 = [SimpleNamespace(key=f"chat-{n}", linked_session_key="") for n in (1, 2)]


async def _boot_with_kept_chats(
    monkeypatch, tmp_path, adopt_one: Any, *, restore_raises: bool = False
) -> None:
    """Run the startup restore over live keepers of chat-1, chat-2 and chat-3.

    The restore rebuilds chat-1 and chat-2; chat-3's keeper names no restored chat,
    so the pass ends it last. *adopt_one* stands in for each attach.
    """
    from dataclasses import replace
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.chat_keeper import records
    from kiro_crew.dashboard import chat_keeper_adopt, restart_receipt
    from kiro_crew.dashboard import server as srv
    from kiro_crew.dashboard.state import DashboardState

    live = [
        replace(_record(), created=float(n), session_key=f"dashboard:chat-{n}", instance=f"i{n}")
        for n in (3, 1, 2)
    ]
    monkeypatch.setattr(chat_keeper_adopt, "_KEPT_SESSION_KEYS", frozenset())
    monkeypatch.setattr(chat_keeper, "supported", lambda: True)
    monkeypatch.setattr(records, "prune_dead", lambda: None)
    monkeypatch.setattr(records, "live_records", lambda: live)

    async def _end(record: Any, why: str, **_: Any) -> None:
        pass

    monkeypatch.setattr(chat_keeper_adopt, "_adopt_one", adopt_one)
    monkeypatch.setattr(client_keeper, "_end_unadopted", _end)
    monkeypatch.setattr(restart_receipt, "_append", lambda _path, _record: None)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

    async def _open(st: Any) -> int:
        for slot in _CHAT_1_AND_2:
            st.get_or_create_slot(slot.key)
        assert all(map(chat_keeper_adopt.kept_by_a_keeper, _CHAT_1_AND_2)), "armed"
        return 0

    recent = AsyncMock(side_effect=RuntimeError("restore")) if restore_raises else None
    monkeypatch.setattr(srv.cautious_boot, "pause_before", AsyncMock(return_value=None))
    monkeypatch.setattr(srv.chat, "restore_open_slots_async", _open)
    monkeypatch.setattr(
        srv.chat, "restore_recent_sessions_async", recent or AsyncMock(return_value=0)
    )
    state = DashboardState(
        sessions=MagicMock(count=0), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    cfg = SimpleNamespace(
        agent=SimpleNamespace(chat_keeper=True, agent_started_turns=True),
        dashboard=SimpleNamespace(
            restore_window_minutes=30,
            restore_sessions=True,
            idle_slot_sweep_days=0,
            surface_channel_sessions=False,
        ),
    )
    await srv._restore_dashboard_sessions(state, cfg)


@pytest.mark.asyncio
async def test_the_kept_set_is_empty_once_the_pass_ends(monkeypatch, tmp_path) -> None:
    from kiro_crew.dashboard import chat_keeper_adopt

    seen: list[str] = []

    async def _attached(_state: Any, slot: Any, *_: Any) -> bool:
        if chat_keeper_adopt.kept_by_a_keeper(_CHAT_1_AND_2[1]):
            seen.append(slot.key)
        return True

    await _boot_with_kept_chats(monkeypatch, tmp_path, _attached)
    assert seen == ["chat-1", "chat-2"], "chat-2's keeper is still live while the pass runs"
    assert not any(map(chat_keeper_adopt.kept_by_a_keeper, _CHAT_1_AND_2))


@pytest.mark.asyncio
async def test_a_pass_whose_attach_raises_leaves_no_chat_kept(monkeypatch, tmp_path) -> None:
    from kiro_crew.dashboard import chat_keeper_adopt

    async def _raises(*_: Any) -> bool:
        raise RuntimeError("attach")

    with pytest.raises(RuntimeError, match="attach"):
        await _boot_with_kept_chats(monkeypatch, tmp_path, _raises)
    # Their keepers end after ``ADOPT_SECS``: "kept, do not message it" must not outlive them.
    assert not any(map(chat_keeper_adopt.kept_by_a_keeper, _CHAT_1_AND_2))


@pytest.mark.asyncio
async def test_a_restore_that_raises_after_arming_leaves_no_chat_kept(
    monkeypatch, tmp_path
) -> None:
    from kiro_crew.dashboard import chat_keeper_adopt

    async def _not_reached(*_: Any) -> bool:
        raise AssertionError("no attach runs after a failed restore")

    with pytest.raises(RuntimeError, match="restore"):
        await _boot_with_kept_chats(monkeypatch, tmp_path, _not_reached, restore_raises=True)
    assert not any(map(chat_keeper_adopt.kept_by_a_keeper, _CHAT_1_AND_2))


def _six_keepers(slots: dict[str, Any]) -> tuple[Any, list[Any]]:
    """A gateway holding *slots*, and keepers for chat-1..chat-6 created in that order."""
    from dataclasses import replace

    live = [
        replace(_record(), created=float(n), session_key=f"dashboard:chat-{n}", instance=f"i{n}")
        for n in range(1, 7)
    ]
    # Handed over out of creation order, as a directory listing may give them.
    return SimpleNamespace(_slots=slots), live[3:] + live[:3]


def _restored_slots() -> dict[str, Any]:
    """chat-1..chat-5 as the restore left them; chat-6's slot did not come back."""
    from kiro_crew.dashboard.state import _ChatSlot

    slots = {f"chat-{n}": _ChatSlot(f"chat-{n}") for n in range(1, 6)}
    # A turn in flight whose "interrupted" row the restore holds back for the keeper.
    for name in ("chat-2", "chat-5"):
        slots[name]._restart_interruption_owed = True
        slots[name]._restored_turn_marker = {"generation": 3, "cut": True}
    # In flight too, on a transcript that already read as cut: the marker alone.
    slots["chat-4"]._restored_turn_marker = {"generation": 2, "cut": True}
    # The person's own Stop ended this turn before the restart.
    slots["chat-3"]._restored_turn_marker = {"generation": 2, "cut": False}
    return slots


@pytest.mark.asyncio
@pytest.mark.skipif(not chat_keeper.supported(), reason="the chat keeper is POSIX only")
async def test_the_chats_that_were_running_a_turn_are_attached_before_the_idle_ones(
    monkeypatch,
) -> None:
    from kiro_crew.dashboard import chat_keeper_adopt

    attached: list[str] = []

    async def _attaches(_state: Any, slot: Any, *_: Any) -> bool:
        attached.append(slot.key)
        return True

    async def _end(record: Any, why: str, **_: Any) -> None:
        attached.append(f"ended {record.session_key}")

    monkeypatch.setattr(chat_keeper_adopt, "_adopt_one", _attaches)
    monkeypatch.setattr(client_keeper, "_end_unadopted", _end)
    state, live = _six_keepers(_restored_slots())
    cfg = SimpleNamespace(agent=SimpleNamespace(chat_keeper=True, agent_started_turns=True))
    assert await chat_keeper_adopt.adopt_kept_chats(state, cfg, live) == 5
    assert attached == [
        # Running a turn, by keeper creation time.
        "chat-2",
        "chat-4",
        "chat-5",
        # Idle, by keeper creation time; the keeper of no restored chat keeps its place.
        "chat-1",
        "chat-3",
        "ended dashboard:chat-6",
    ]


@pytest.mark.asyncio
async def test_with_the_flag_off_the_keepers_are_ended_in_creation_order(monkeypatch) -> None:
    from kiro_crew.dashboard import chat_keeper_adopt

    ended: list[str] = []

    async def _end(record: Any, why: str, **_: Any) -> None:
        ended.append(record.session_key)

    monkeypatch.setattr(client_keeper, "_end_unadopted", _end)
    state, live = _six_keepers(_restored_slots())
    cfg = SimpleNamespace(agent=SimpleNamespace(chat_keeper=False, agent_started_turns=True))
    assert await chat_keeper_adopt.adopt_kept_chats(state, cfg, live) == 0
    assert ended == [f"dashboard:chat-{n}" for n in range(1, 7)]


@pytest.mark.asyncio
@pytest.mark.parametrize("spent, eligible", [(True, True), (False, False)])
async def test_a_keeper_a_spawn_does_not_adopt_is_ended_before_it_spawns(
    monkeypatch, spent: bool, eligible: bool
) -> None:
    from kiro_crew.chat_keeper import records

    ended: list[str] = []

    async def _end(record: Any, why: str, **_: Any) -> None:
        ended.append(record.instance)

    async def _no_attach(*_: Any, **__: Any) -> None:
        raise AssertionError("a keeper this spawn does not adopt must not be attached")

    other = records.KeeperRecord(**{**_record().__dict__, "instance": "fff", "session_key": "x"})
    monkeypatch.setattr(chat_keeper, "supported", lambda: True)
    monkeypatch.setattr(records, "live_records", lambda: [_record(), other])
    monkeypatch.setattr(client_keeper, "eligible", lambda c: eligible)
    monkeypatch.setattr(client_keeper, "_end_unadopted", _end)
    monkeypatch.setattr("kiro_crew.chat_keeper.transport.attach", _no_attach)
    client = SimpleNamespace(
        _session_key="dashboard:chat-1", backend="claude", _keeper_adopt_spent=spent
    )
    assert await client_keeper.adopt_for(client, lambda: "s") is None  # type: ignore[arg-type]
    assert ended == ["abc123def456"], "this chat's keeper only, ended before the spawn"


def test_a_keeper_whose_start_cannot_be_read_stays_live_and_keeps_its_files(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew import platform_compat
    from kiro_crew.chat_keeper import records

    record = _record()
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: True)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: None)
    monkeypatch.setattr(records, "load_records", lambda home=None: [record])
    removed: list[str] = []
    monkeypatch.setattr(records, "remove_files", lambda i, home=None: removed.append(i))
    assert records.is_live(record)
    assert records.live_keeper_pids() == {4242}, "the sweeps still skip it"
    assert records.prune_dead() == 0 and removed == []
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "another")
    assert not records.is_live(record), "a recycled pid is a dead keeper"
    assert records.prune_dead() == 1 and removed == ["abc123def456"]


@pytest.mark.parametrize(
    ("keeper", "agent_started", "keeps"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_the_keeper_flag_needs_agent_started_turns(keeper, agent_started, keeps) -> None:
    agent = SimpleNamespace(chat_keeper=keeper, agent_started_turns=agent_started)
    assert client_keeper._keeps(agent) is keeps


@pytest.mark.asyncio
async def test_a_detached_client_starts_nothing_for_a_late_prompt(monkeypatch) -> None:
    from kiro_crew.acp import client_start
    from kiro_crew.acp.transport_errors import AcpError

    process = _Process(_state())
    client = _Client(process, {})
    client._session_id = SID
    monkeypatch.setattr(client_keeper, "detachable", lambda c: True)
    assert await client_keeper.detach(client) is True  # type: ignore[arg-type]
    client._process = None  # what the detached client's shutdown leaves

    async def _no_spawn() -> None:
        raise AssertionError("a detached chat's keeper would be ended by the spawn's adopt")

    client._spawn = _no_spawn  # type: ignore[attr-defined]
    with pytest.raises(AcpError, match="chat keeper"):
        await client_start.ensure_ready(client)  # type: ignore[arg-type]


def test_a_record_past_64_kib_is_still_read(tmp_path) -> None:
    from dataclasses import replace

    from kiro_crew.chat_keeper import records

    rules = [["deny", f"Bash(tool-{n}:*)"] for n in range(4000)]
    record = replace(_record(shipped_restrictions=rules), socket=str(tmp_path / "x.s"))
    records.ensure_dir(tmp_path)
    records.write_record(record, tmp_path)
    assert (records.keepers_dir(tmp_path) / f"{record.instance}.json").stat().st_size > 64 * 1024
    (loaded,) = records.load_records(tmp_path)
    assert loaded.launch["shipped_restrictions"] == rules


@pytest.mark.asyncio
async def test_an_adopted_session_gets_no_initial_effort_request() -> None:
    from kiro_crew.providers import acp_model_effort

    process = _Process(_state())
    client = _Client(process, {"initialize": _INIT, "session/status": {"status": "open"}})
    await client_keeper.adopt_session(client)  # type: ignore[arg-type]
    assert client_keeper.adopted(client)  # type: ignore[arg-type]
    sent: list[str] = []

    async def _set(level: str) -> None:
        sent.append(level)

    provider = SimpleNamespace(
        capabilities=SimpleNamespace(effort_via_config_option=True),
        _resolve_effort=lambda: "high",
        _set_effort_config_option=_set,
        _client=client,
    )
    await acp_model_effort._apply_initial_effort(provider)  # type: ignore[arg-type]
    assert sent == [], "its answer would be read through the replay the adopted turn is owed"
    client._process = _Process(_state())
    client._model = "auto"  # type: ignore[attr-defined]
    await acp_model_effort._apply_initial_effort(provider)  # type: ignore[arg-type]
    assert sent == ["high"], "a session made on a later process gets its level"


@pytest.mark.asyncio
async def test_the_owed_adopt_handshake_names_its_process() -> None:
    process = _Process(_state())
    client = _Client(process, {})
    client._keeper_adopting = process
    assert client_keeper.adopting(client)  # type: ignore[arg-type]
    client._process = _Process(_state())
    assert not client_keeper.adopting(client), "a later process owes no adopt"  # type: ignore[arg-type]
    client._process = None
    with pytest.raises(AssertionError):
        await client_keeper.adopt_session(client)  # type: ignore[arg-type]
    assert client._keeper_adopting is None


def test_a_keeper_that_did_not_come_up_fails_as_a_spawn_does() -> None:
    from kiro_crew.chat_keeper.spawn import KeeperStartError

    assert issubclass(KeeperStartError, OSError), "ensure_ready retries and classifies an OSError"


class _Start:
    """A client one start of a chat builds (weakly referable, as ``AcpClient`` is)."""

    def __init__(self) -> None:
        self._session_key = "dashboard:chat-1"
        self.backend = "claude"
        self._process: Any = None
        self._keeper_adopting: Any = None
        self._keeper_adopt_spent = False


def _attached(record: Any) -> _Process:
    process = _Process(_state(last_ack=9))
    process.record = record
    process.pid = record.keeper_pid  # type: ignore[attr-defined]
    return process


def _one_keeper(monkeypatch, attach: Any) -> list[str]:
    from kiro_crew.chat_keeper import records

    ended: list[str] = []

    async def _end(record: Any, why: str, **_: Any) -> None:
        ended.append(why)

    async def _on() -> bool:
        return True

    monkeypatch.setattr(chat_keeper, "supported", lambda: True)
    monkeypatch.setattr(records, "live_records", lambda: [_record(sandbox="s")])
    monkeypatch.setattr(records, "read_key", lambda instance: b"k")
    monkeypatch.setattr(client_keeper, "eligible", lambda c: True)
    monkeypatch.setattr(client_keeper, "_config_async", _on)
    monkeypatch.setattr(client_keeper, "_end_unadopted", _end)
    monkeypatch.setattr("kiro_crew.chat_keeper.transport.attach", attach)
    return ended


@pytest.fixture(autouse=True)
def _no_holds(monkeypatch):
    monkeypatch.setattr(client_keeper, "_HOLDS", {})


@pytest.mark.asyncio
async def test_a_second_start_of_a_kept_chat_neither_takes_nor_ends_its_keeper(
    monkeypatch,
) -> None:
    # A page open across the restart starts the chat beside the adopt pass. The
    # keeper hands its attachment to the last attach, so a second attach would
    # break the first start, and a fresh spawn would run two adapters on one session.
    from kiro_crew.testing.wait import await_until

    attaching, go = asyncio.Event(), asyncio.Event()
    attaches: list[str] = []

    async def _attach(record: Any, secret: bytes, **_: Any) -> _Process:
        attaches.append(record.instance)
        attaching.set()
        await go.wait()
        return _attached(record)

    ended = _one_keeper(monkeypatch, _attach)
    first, second = _Start(), _Start()
    adopting = asyncio.ensure_future(client_keeper.adopt_for(first, lambda: "s"))  # type: ignore[arg-type]
    await await_until(attaching, what="the first start's attach")
    late = asyncio.ensure_future(client_keeper.adopt_for(second, lambda: "s"))  # type: ignore[arg-type]
    try:
        with pytest.raises(client_keeper.KeeperHeldElsewhere):
            await await_until(late, what="the second start's adopt")
    finally:
        go.set()
    process = await await_until(adopting, what="the first start's adopt")
    assert process is not None and first._keeper_adopting is process
    assert attaches == ["abc123def456"], "attached once, by the first start"
    # Its retry (a spawn already spent) is held off too, never a fresh adapter.
    with pytest.raises(client_keeper.KeeperHeldElsewhere):
        await client_keeper.adopt_for(second, lambda: "s")  # type: ignore[arg-type]
    assert ended == []
    assert client_keeper.holder_of("abc123def456") is first


@pytest.mark.asyncio
async def test_a_hold_ends_with_its_start_and_the_keeper_can_be_adopted_again(
    monkeypatch,
) -> None:
    async def _attach(record: Any, secret: bytes, **_: Any) -> _Process:
        return _attached(record)

    _one_keeper(monkeypatch, _attach)
    first, second = _Start(), _Start()
    process = await client_keeper.adopt_for(first, lambda: "s")  # type: ignore[arg-type]
    assert process is not None
    first._keeper_adopting = None
    first._process = None  # its start failed and dropped the process
    assert client_keeper.holder_of("abc123def456") is None
    assert await client_keeper.adopt_for(second, lambda: "s") is not None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_keeper_a_start_here_holds_is_not_ended_under_it(monkeypatch) -> None:
    from kiro_crew import process_identity

    def _no_signal(*_: Any) -> None:
        raise AssertionError("a keeper a start here holds must not be signalled")

    monkeypatch.setattr(process_identity, "isolated_group_of", _no_signal)
    holder = _Start()
    process = _Process(_state(last_ack=9))
    holder._process = process
    client_keeper._HOLDS["abc123def456"] = (weakref.ref(holder), process)
    await client_keeper._end_unadopted(_record(), "the eager adopt did not attach")


@pytest.mark.asyncio
async def test_the_adopt_pass_waits_on_the_start_holding_the_keeper(monkeypatch) -> None:
    # The page's start took the keeper first: the pass's own start stands down, and
    # the pass waits for that start to register instead of ending the keeper.

    from kiro_crew.dashboard import chat_keeper_adopt, chat_runner
    from kiro_crew.testing.wait import await_until

    holder = _Start()
    process = _Process(_state(last_ack=9))
    holder._process = process
    client_keeper._HOLDS["abc123def456"] = (weakref.ref(holder), process)
    provider = SimpleNamespace(_client=holder)
    asked, registered = asyncio.Event(), False
    watched: list[Any] = []

    def _get_provider(key: str) -> Any:
        asked.set()
        return provider if registered else None

    async def _stands_down(*_: Any, **__: Any) -> None:
        pass

    monkeypatch.setattr(chat_runner, "_eager_spawn", _stands_down)
    monkeypatch.setattr(chat_runner, "effective_session_key", lambda slot: "dashboard:chat-1")
    monkeypatch.setattr(chat_runner, "_watch_agent_turns", lambda *a: watched.append(a[2]))
    monkeypatch.setattr(chat_keeper_adopt, "_HELD_START_POLL_SECS", 0.0)
    state = SimpleNamespace(sessions=SimpleNamespace(get_provider=_get_provider))
    slot = SimpleNamespace(running=False, _eager_spawn_task=None, _prefetch_ttl_task=None)
    adopt = asyncio.ensure_future(
        chat_keeper_adopt._adopt_one(state, slot, SimpleNamespace(), _record())
    )
    await await_until(asked, what="the pass reading the provider")
    registered = True
    assert await await_until(adopt, what="the pass") is True
    assert watched == [provider], "the pass watches the held start's turn"


@pytest.mark.asyncio
async def test_the_pass_disarms_the_prefetch_ttl_the_holding_start_arms_last(monkeypatch) -> None:
    # The page's start registers its provider first, then arms its prefetch TTL and
    # cap: the pass waits for that start to end before it disarms them, or the kept
    # chat would count as an unclaimed pre-warm and be ended at the TTL.
    from kiro_crew.dashboard import chat_keeper_adopt, chat_runner
    from kiro_crew.testing.wait import await_until

    holder = _Start()
    process = _Process(_state(last_ack=9))
    holder._process = process
    client_keeper._HOLDS["abc123def456"] = (weakref.ref(holder), process)
    provider = SimpleNamespace(_client=holder)
    registered, arm = asyncio.Event(), asyncio.Event()
    armed: dict[str, Any] = {}
    slot = SimpleNamespace(running=False, _eager_spawn_task=None, _prefetch_ttl_task=None)
    never = asyncio.Event()

    async def _page_start() -> None:
        registered.set()
        await arm.wait()
        slot._prefetch_ttl_task = asyncio.ensure_future(never.wait())
        armed["dashboard:chat-1"] = 1

    def _get_provider(key: str) -> Any:
        if registered.is_set():
            arm.set()  # the pass saw the provider: the page's tail runs now
            return provider
        return None

    async def _stands_down(*_: Any, **__: Any) -> None:
        # The page's start began while the pass's own was starting, and took the hold.
        slot._eager_spawn_task = asyncio.ensure_future(_page_start())
        await await_until(registered, what="the page's start registering")

    monkeypatch.setattr(chat_runner, "_eager_spawn", _stands_down)
    monkeypatch.setattr(chat_runner, "_armed_prefetches", armed)
    monkeypatch.setattr(chat_runner, "effective_session_key", lambda slot: "dashboard:chat-1")
    monkeypatch.setattr(chat_runner, "_watch_agent_turns", lambda *a: None)
    monkeypatch.setattr(chat_keeper_adopt, "_HELD_START_POLL_SECS", 0.0)
    state = SimpleNamespace(sessions=SimpleNamespace(get_provider=_get_provider))
    adopted = await await_until(
        asyncio.ensure_future(
            chat_keeper_adopt._adopt_one(state, slot, SimpleNamespace(), _record())
        ),
        what="the pass",
    )
    assert adopted is True
    ttl = slot._prefetch_ttl_task
    assert ttl is not None
    await await_until(asyncio.ensure_future(asyncio.wait({ttl})), what="the TTL's cancel")
    assert ttl.cancelled(), "the TTL the page armed after registering is cancelled"
    assert armed == {}, "and the chat is no unclaimed pre-warm"
