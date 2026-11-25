"""The patrol-wake setting: ``work_ledger.remote_dispatch_patrol_wake``.

Rule 8 of the remote dispatch gate admits ``crew=`` only on a turn the owner
sent. With the setting on (default off) it also admits one automated turn: a
wake of the shipped work-ledger patrol in the owner's own conductor mode chat.
Pinned here, through the real runner, the real delivery seam, the real loop fire
and the real gate:

* the turn's ``_turn_patrol_wake`` mark is adopted only from the loop fire's
  ``_directive_patrol_wake``, never beside a user-sent or channel opener, and is
  cleared at the turn's end and by any steer;
* the loop fire declares it from ``conductor_patrol.is_fixed_patrol_payload``
  alone, so a loop carrying any other text or watch never holds it;
* the gate admits a marked turn only with the setting on, and still refuses every
  other turn the owner did not send, a worker, and a chat that is not in
  conductor mode.
"""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
)
from test_autonudge_dashboard_fire import _loop, _orchestrator
from test_autonudge_dashboard_fire import _slot as _fire_slot
from test_session_control_crew import _REAL_OWNER_CHECK, _call, _create, env  # noqa: F401

from kiro_crew import conductor_patrol
from kiro_crew.config.section_builders import _build_work_ledger_config
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import remote_dispatch_headroom as headroom
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew
from kiro_crew.slack import gateway as gw

# ── the setting ──────────────────────────────────────────────────────────────


def test_the_setting_defaults_off_and_reads_only_a_real_bool():
    assert _build_work_ledger_config({}).remote_dispatch_patrol_wake is False
    assert (
        _build_work_ledger_config(
            {"remote_dispatch_patrol_wake": "true"}
        ).remote_dispatch_patrol_wake
        is False
    )
    assert (
        _build_work_ledger_config({"remote_dispatch_patrol_wake": True}).remote_dispatch_patrol_wake
        is True
    )


def _settings(monkeypatch, *, enabled: bool = True, patrol_wake: bool) -> None:
    value = dataclasses.replace(headroom._DISABLED, enabled=enabled, patrol_wake=patrol_wake)
    monkeypatch.setattr(headroom, "remote_dispatch_settings", lambda: value)


# ── the runner: adopt at entry, clear at exit ────────────────────────────────


def _one_turn(state, slot, seen: list[bool]) -> None:
    """Wire *state* to run one bare turn whose model records the patrol mark."""
    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

    async def _stream(_msg):
        seen.append(slot._turn_patrol_wake)
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield AcpEvent(kind=EVENT_COMPLETE)

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "user", "channel", "expected"),
    [
        (True, False, False, True),
        (True, True, False, False),
        (True, False, True, False),
        (False, False, False, False),
    ],
)
async def test_the_turn_holds_the_patrol_mark_only_for_a_declared_automated_opener(
    tmp_path, declared: bool, user: bool, channel: bool, expected: bool
) -> None:
    from test_acp_tool_identity import _stub_state

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    seen: list[bool] = []
    _one_turn(state, slot, seen)

    await cr._run_chat(
        state,
        slot,
        "patrol",
        _directive_user_origin=user,
        _directive_channel_origin=channel,
        _directive_patrol_wake=declared,
    )
    assert seen == [expected]
    assert slot._turn_patrol_wake is False, "the mark ends with the turn"


@pytest.mark.asyncio
async def test_a_turn_that_declares_nothing_holds_no_mark(tmp_path) -> None:
    """The default is the refusing one: a cron, a session_send, a completion."""
    from test_acp_tool_identity import _stub_state

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    seen: list[bool] = []
    _one_turn(state, slot, seen)
    await cr._run_chat(state, slot, "a note from another chat", _directive_user_origin=False)
    assert seen == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_origin", [False, True])
async def test_any_steer_ends_the_patrol_mark(owner_origin: bool) -> None:
    """A steer adds text the gateway did not write, whoever sent it."""
    from kiro_crew.dashboard import chat_delivery
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1")
    slot._turn_patrol_wake = True
    client = MagicMock(supports_steer=True, steer_needs_loss_recovery=False)
    client.steer = AsyncMock(return_value=None)
    slot._acp_client = client
    await chat_delivery.steer_into_running_turn(
        MagicMock(), slot, "also do this", user_origin=owner_origin, owner_origin=owner_origin
    )
    assert slot._turn_patrol_wake is False


# ── the loop fire: declared for the fixed patrol alone ───────────────────────


def _running_spawn(spawned: list[asyncio.Task]):
    def _spawn(_state, _slot, coro, **_kw):
        task = asyncio.create_task(coro)
        spawned.append(task)
        return task

    return _spawn


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [False, True])
async def test_the_fire_declares_the_mark_from_the_fixed_patrol_check(fixed: bool) -> None:
    orch = _orchestrator()
    orch.dashboard_state.get_slot = MagicMock(return_value=_fire_slot())
    loop = _loop()
    spawned: list[asyncio.Task] = []
    run_chat = AsyncMock()
    seen: list[object] = []

    def _check(row: object) -> bool:
        seen.append(row)
        return fixed

    with (
        patch.object(gw, "spawn_guarded_turn", _running_spawn(spawned)),
        patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
        patch.object(conductor_patrol, "is_fixed_patrol_payload", _check),
    ):
        assert await orch._fire_dashboard_nudge(loop) is True
        await asyncio.gather(*spawned)
    assert loop in seen
    assert run_chat.await_args.kwargs["_directive_patrol_wake"] is fixed


@pytest.mark.asyncio
async def test_a_loop_the_agent_armed_itself_declares_no_mark() -> None:
    """The real check: an ordinary loop is not the shipped patrol."""
    orch = _orchestrator()
    orch.dashboard_state.get_slot = MagicMock(return_value=_fire_slot())
    spawned: list[asyncio.Task] = []
    run_chat = AsyncMock()
    with (
        patch.object(gw, "spawn_guarded_turn", _running_spawn(spawned)),
        patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
    ):
        assert await orch._fire_dashboard_nudge(_loop()) is True
        await asyncio.gather(*spawned)
    assert run_chat.await_args.kwargs["_directive_patrol_wake"] is False


# ── the gate ─────────────────────────────────────────────────────────────────


@pytest.fixture
def real_gate(env, monkeypatch):  # noqa: F811 -- the imported fixture
    """The crew harness with the real owner check and a turn nobody sent."""
    monkeypatch.setattr(crew, "owner_unverifiable_reason", _REAL_OWNER_CHECK)
    env.caller._turn_owner_sent = False
    env.caller._turn_patrol_wake = False
    audits: list[tuple[str, str]] = []
    monkeypatch.setattr(crew, "_audit_patrol_wake", lambda key, verb: audits.append((key, verb)))
    env.audits = audits
    return env


def test_a_patrol_wake_is_refused_with_the_setting_off(real_gate, monkeypatch):
    _settings(monkeypatch, patrol_wake=False)
    real_gate.caller._turn_patrol_wake = True
    status, body = _call(real_gate, "create", crew="lab")
    assert (status, body["code"]) == (403, crew.OWNER_UNVERIFIABLE)
    assert real_gate.peer.calls == [] and real_gate.audits == []


def test_a_patrol_wake_is_admitted_with_the_setting_on_and_audited(real_gate, monkeypatch):
    _settings(monkeypatch, patrol_wake=True)
    real_gate.caller._turn_patrol_wake = True
    assert _create(real_gate) == "chat-77"
    assert [verb for _key, verb in real_gate.audits] == ["create"]


def test_the_setting_admits_no_other_turn_the_owner_did_not_send(real_gate, monkeypatch):
    """A cron, a note from another chat and a completion hold no patrol mark."""
    _settings(monkeypatch, patrol_wake=True)
    status, body = _call(real_gate, "create", crew="lab")
    assert (status, body["code"]) == (403, crew.OWNER_UNVERIFIABLE)
    assert real_gate.peer.calls == [] and real_gate.audits == []


def test_the_setting_needs_the_preview_switch(real_gate, monkeypatch):
    _settings(monkeypatch, enabled=False, patrol_wake=True)
    real_gate.caller._turn_patrol_wake = True
    status, body = _call(real_gate, "create", crew="lab")
    assert (status, body["code"]) == (403, crew.OWNER_UNVERIFIABLE)
    assert real_gate.peer.calls == []


@pytest.mark.parametrize("shape", ["worker", "not_conductor_mode"])
def test_the_setting_never_admits_a_worker_or_a_plain_chat(real_gate, monkeypatch, shape: str):
    """Rules 1-7 run first: the mark alone reaches no crew."""
    _settings(monkeypatch, patrol_wake=True)
    real_gate.caller._turn_patrol_wake = True
    if shape == "worker":
        real_gate.caller._created_by = "dashboard:chat-9"
    else:
        real_gate.caller.agent = ""
        real_gate.caller.agent_kind = ""
    status, body = _call(real_gate, "create", crew="lab")
    assert status == 403, body
    assert real_gate.peer.calls == [] and real_gate.audits == []
