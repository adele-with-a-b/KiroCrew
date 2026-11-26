"""A Conductor holds no tool call open past one step's bound.

The owner's message is read when the tool call in flight ends, so a Conductor
that sits in one call for minutes leaves it unread. Two calls could hold one open
that long whatever comes in, and each is refused past
``conductor_step.CONDUCTOR_STEP_MAX_SECS`` with words that name the way out and say
to poll in steps of the bound or send the waiting to a worker:

* ``spawn_sub_agents``, whose collection waits up to two hours: each member is
  refused at ``/api/spawn``;
* a shell call that declares a longer wait (Claude Code ``Bash`` ``timeout``,
  Copilot ``initial_wait`` / ``delay``): refused at the tool gate, with the reason
  where every backend's model reads it.

``wait`` is the third long call and needs no refusal: the owner's message ends it
within one keepalive ping, steered in or queued, on every backend (pinned here
through the real composer route into a real running turn). Every other session
keeps every bound it had, and a Conductor keeps ``spawn_run`` and its short shell
calls. One identity rule names a Conductor for both gates.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_spawn_light_tier import _sessions
from test_wait_countdown_keepalive import SLOT, _ping

from kiro_crew import conductor_step
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_COPILOT
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

CONDUCTOR = "kirocrew-conductor"


def _slot(agent: str = CONDUCTOR, *, kind: str = "template") -> _ChatSlot:
    slot = _ChatSlot(SLOT)
    slot.agent = agent
    slot.agent_kind = kind
    return slot


# ── who is a Conductor ──


@pytest.mark.parametrize(
    "agent", ["kirocrew-conductor", "kirocrew-team-lead", "kirocrew-pipeline-conductor"]
)
def test_a_chat_on_a_conductor_template_is_a_conductor(agent: str) -> None:
    assert conductor_step.is_conductor_slot(_slot(agent), "dashboard:test")


def test_a_chat_its_crew_mode_stamp_names_is_a_conductor() -> None:
    slot = _slot("my-own-conductor")
    slot.crew_mode_agent = "my-own-conductor"
    assert conductor_step.is_conductor_slot(slot, "dashboard:test")


def test_a_bots_own_chat_on_the_mate_conductor_is_a_conductor() -> None:
    execution = ExecutionContext(
        "alpha", MemoryStoreRef("member-alpha", "alpha"), "member", MATE_CONDUCTOR_AGENT
    )
    with patch(
        "kiro_crew.execution_context.read_vouched_session_execution", return_value=execution
    ):
        assert conductor_step.is_conductor_slot(_slot("alpha", kind="member"), "dashboard:test")


@pytest.mark.parametrize("agent", ["kirocrew", "kirocrew-mate-worker", ""])
def test_any_other_chat_is_not(agent: str) -> None:
    with patch("kiro_crew.execution_context.read_vouched_session_execution", return_value=None):
        assert not conductor_step.is_conductor_slot(_slot(agent), "dashboard:test")


def test_an_unreadable_identity_is_not_a_conductor() -> None:
    with patch(
        "kiro_crew.execution_context.read_vouched_session_execution",
        side_effect=RuntimeError("store gone"),
    ):
        assert not conductor_step.is_conductor_slot(_slot("kirocrew", kind="member"), "k")


def test_a_chat_that_left_crew_mode_is_not_one_whatever_its_record_says() -> None:
    """The chat's own agent decides; a stale record naming a conductor does not."""
    record = ExecutionContext(None, MemoryStoreRef("default"), "template", CONDUCTOR)
    with patch("kiro_crew.execution_context.read_vouched_session_execution", return_value=record):
        assert not conductor_step.is_conductor_slot(_slot("kirocrew"), "dashboard:test")
        assert conductor_step.is_conductor_slot(None, "subagent:x"), "a chatless session"


# ── wait: no refusal, the owner's message ends it within one ping ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "reason"),
    [("steer", "steer"), ("steering_request", "steer"), ("no_steer", "queued")],
    ids=["kiro-cli steer", "a steer without a refusal steer (claude)", "a backend that cannot"],
)
async def test_the_owners_message_ends_a_conductors_wait_within_one_ping(
    tmp_path, monkeypatch, harness: str, reason: str
) -> None:
    """Through the real composer route into a real running turn the owner did not
    send: their typed message is steered in (or queued where the backend takes no
    steer), and the wait's next keepalive ping ends the sleep."""
    from test_typed_message_runs_alone import _Live

    live = _Live(tmp_path, monkeypatch)
    live.agent.supports_steer = harness != "no_steer"
    live.agent.supports_refusal_steer = harness == "steer"
    live.agent.steer_needs_loss_recovery = harness == "steering_request"
    live.agent.last_steer_monotonic = 0.0

    async def steer(_text: str) -> bool:
        live.agent.last_steer_monotonic += 1.0  # every steering client stamps its write
        return True

    live.agent.steer = steer
    await live.open_with("wake")
    live.slot.agent, live.slot.agent_kind = CONDUCTOR, "template"
    sleep = {"wait_id": "w1", "seconds": 1800, "remaining": 1800, "interval": 5}
    assert "end_wait" not in _ping(live.state, sleep, tab=live.slot.key, provider=live.agent)

    await live.type("Q1a Go.")
    reply = _ping(live.state, {**sleep, "remaining": 1795}, tab=live.slot.key, provider=live.agent)

    assert reply.get("end_wait") == "w1", "the next 5 s ping ends the sleep"
    assert reply.get("end_wait_reason") == reason
    live.slot._pending_steers.clear()
    live.slot._queue.clear()
    await live.finish()


# ── spawn_sub_agents ──


async def _spawn_as(template: str, body: dict[str, Any], *, stamp: str = "") -> Any:
    """POST /api/spawn for a parent chat on *template* (Crew Mode stamp *stamp*)."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key
    from kiro_crew.dashboard.handlers.messaging import api_spawn
    from kiro_crew.subagent import SubagentManager

    parent = ExecutionContext(None, MemoryStoreRef("default"), "template", template)
    sessions = _sessions()
    mgr = SubagentManager(sessions=sessions, ctx_builder=MagicMock())
    mgr._run = AsyncMock()  # type: ignore[method-assign]
    request = MagicMock()
    # The parent's chat, so the identity rule reads its own agent and stamp.
    tab = dashboard_slot_key("dashboard:c1")
    chat = _ChatSlot(tab)
    chat.agent, chat.agent_kind, chat.crew_mode_agent = template, "template", stamp
    request.app = {
        "state": SimpleNamespace(
            subagents=mgr,
            sessions=sessions,
            conversation_log=MagicMock(),
            _slots={tab: chat},
            get_slot=lambda key: chat if key == tab else None,
            _restricted_keys=set(),
            _ephemeral_keys=set(),
        )
    }
    request.headers = {}
    request.json = AsyncMock(return_value={"parent_session": "dashboard:c1", **body})
    with (
        patch(
            "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: KiroCrewConfig())
        ),
        patch("kiro_crew.subagent._validate_agent", lambda agent, cwd="": (agent, "", "")),
        patch("kiro_crew.execution_context.read_session_execution", lambda *a, **k: parent),
        patch(
            "kiro_crew.dashboard.handlers.messaging.parent_template_spawn_policy",
            lambda *a, **k: (template, (), ()),
        ),
    ):
        return await api_spawn(request)


@pytest.fixture
def _managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


@pytest.mark.asyncio
@pytest.mark.usefixtures("_managers", "healthy_host_memory")
async def test_a_conductors_collected_wave_member_is_refused() -> None:
    resp = await _spawn_as(CONDUCTOR, {"task": "x", "call_id": "abc123"})

    assert resp.status == 403
    data = json.loads(resp.text)
    assert data["code"] == conductor_step.NO_COLLECTION_CODE
    assert "`spawn_sub_agents` waits inside the call" in data["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_managers", "healthy_host_memory")
async def test_a_conductors_spawn_run_still_starts() -> None:
    resp = await _spawn_as(CONDUCTOR, {"task": "x"})

    assert resp.status == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("_managers", "healthy_host_memory")
async def test_anyone_elses_wave_still_starts() -> None:
    resp = await _spawn_as("kirocrew", {"task": "x", "call_id": "abc123"})

    assert resp.status == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("_managers", "healthy_host_memory")
async def test_a_custom_agent_in_conductor_mode_is_refused_the_wave_too() -> None:
    """The same identity rule as the shell gate: Crew Mode on a custom agent counts."""
    resp = await _spawn_as(
        "my-own-conductor", {"task": "x", "call_id": "abc123"}, stamp="my-own-conductor"
    )

    assert resp.status == 403
    assert json.loads(resp.text)["code"] == conductor_step.NO_COLLECTION_CODE


# ── shell ──


def _shell_event(params: dict | None) -> Any:
    """A shell permission request as the ACP client hands it to the gate."""
    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        is_shell=True,
        raw_tool_params=params,
        title="sleep 600",
        tool_kind="execute",
        request_id="r1",
        tool_call_id="t1",
    )


@pytest.mark.parametrize(
    "backend,params,secs",
    [
        (ACP_BACKEND_CLAUDE, {"command": "sleep 600", "timeout": 600000}, 600),
        (ACP_BACKEND_CLAUDE, {"command": "x", "timeout": 60000}, 60),
        (ACP_BACKEND_CLAUDE, {"command": "x", "timeout": 60001}, 61),
        (ACP_BACKEND_CLAUDE, {"command": "x"}, 0),
        (ACP_BACKEND_CLAUDE, {"command": "x", "timeout": 600000, "run_in_background": True}, 0),
        (ACP_BACKEND_COPILOT, {"command": "x", "initial_wait": 120}, 120),
        (ACP_BACKEND_COPILOT, {"sessionId": "s", "delay": 300}, 300),
        ("kiro", {"command": "sleep 600"}, 0),
        (ACP_BACKEND_CLAUDE, {"command": "x", "timeout": "600000"}, 0),
    ],
)
def test_the_declared_wait_is_read_per_harness(backend: str, params: dict, secs: int) -> None:
    assert conductor_step.shell_wait_secs(_shell_event(params), backend) == secs


def test_a_call_that_is_not_a_shell_declares_nothing() -> None:
    event = _shell_event({"timeout": 600000})
    event.is_shell = False
    assert conductor_step.shell_wait_secs(event, ACP_BACKEND_CLAUDE) == 0


def _turn(slot: _ChatSlot, backend: str) -> SimpleNamespace:
    client = MagicMock()
    client.reject_tool = AsyncMock()
    client.capabilities = SimpleNamespace(backend=backend)
    return SimpleNamespace(
        client=client,
        slot=slot,
        session_key="dashboard:test",
        state=MagicMock(),
        _refusal_notices=[],
        _refusal_reasons=[],
    )


async def _refuse(turn: SimpleNamespace, event: SimpleNamespace) -> bool:
    """The gate step, called as ``tool_approval`` calls it."""
    # The turn modules' functions run on the runner's namespace (``chat_turn.compose``).
    from kiro_crew.dashboard import chat_runner as cr

    return await cr._refuse_conductor_long_shell(
        turn.client,
        turn.slot,
        event,
        session_key=turn.session_key,
        backend=turn.client.capabilities.backend,
        state=turn.state,
        refusal_reasons=turn._refusal_reasons,
        refusal_notices=turn._refusal_notices,
    )


async def _gate(turn: SimpleNamespace, event: SimpleNamespace) -> bool:
    """The real gate step, with only the audit and the steer stubbed."""
    from kiro_crew.dashboard import chat_runner as cr

    with (
        patch.object(cr, "_steer_policy_notice", AsyncMock(return_value=True)),
        patch.object(cr, "sel", MagicMock()),
        patch("kiro_crew.execution_context.read_vouched_session_execution", return_value=None),
    ):
        return await _refuse(turn, event)


@pytest.mark.asyncio
async def test_a_conductors_long_shell_call_is_refused_with_the_reason() -> None:
    slot = _slot()
    turn = _turn(slot, ACP_BACKEND_CLAUDE)

    refused = await _gate(turn, _shell_event({"command": "sleep 600", "timeout": 600000}))

    assert refused is True
    turn.client.reject_tool.assert_awaited_once_with("r1")
    ((title, reason),) = turn._refusal_reasons
    assert "this shell call asks to wait up to 600 s" in reason
    assert any("waits at most 60 s" in m["content"] for m in slot.messages)


class _PastTheStep(Exception):
    """The first rung after the bound's step ran: the call reached the rungs."""


@pytest.mark.asyncio
async def test_the_gate_refuses_it_before_any_rung_could_approve_it() -> None:
    # Through the runner: its compose binds the gate step on the namespace every
    # owner function runs on, which a bare import of the owner module skips.
    from kiro_crew.dashboard import chat_runner as tool_approval
    from kiro_crew.dashboard.chat_turn.turn_locals import _Flow

    slot = _slot()
    slot._trust = True  # session trust: a rung below would approve a shell call
    turn = _turn(slot, ACP_BACKEND_CLAUDE)
    turn.client.approve_tool = AsyncMock()
    turn._deferred_cut_text, turn._native_tracker, turn._native_child_tc = None, {}, set()
    turn._wt_pending_coding, turn._card_origin = set(), False
    reached: list[bool] = []

    def first_rung(*_a: Any, **_k: Any) -> None:
        reached.append(True)
        raise _PastTheStep

    decide = tool_approval._decide_permission_request
    names = {
        "capabilities_of": lambda c: c.capabilities,
        "_steer_policy_notice": AsyncMock(return_value=True),
        "sel": MagicMock(),
    }
    with (
        patch.dict(decide.__globals__, names),
        patch("kiro_crew.mate_held_rules.held_for", first_rung),
        patch("kiro_crew.execution_context.read_vouched_session_execution", return_value=None),
    ):
        try:
            flow = await decide(turn, _shell_event({"command": "sleep 600", "timeout": 600000}))
        except _PastTheStep:
            flow = None

    assert reached == [], "the call reached the rungs below the bound's step"
    assert flow is _Flow.CONTINUE
    turn.client.reject_tool.assert_awaited_once_with("r1")
    turn.client.approve_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent,params,extra",
    [
        (CONDUCTOR, {"command": "make", "timeout": 60000}, {}),
        ("kirocrew", {"command": "sleep 600", "timeout": 600000}, {}),
        (CONDUCTOR, {"command": "sleep 600", "timeout": 600000}, {"sub_session_id": "child"}),
        (CONDUCTOR, {"command": "sleep 600", "timeout": 600000}, {"native_child_call": True}),
    ],
    ids=["within the bound", "not a conductor", "a sub-agent's call", "a native child's call"],
)
async def test_every_other_shell_call_goes_on(agent: str, params: dict, extra: dict) -> None:
    turn = _turn(_slot(agent), ACP_BACKEND_CLAUDE)
    event = _shell_event(params)
    for key, value in extra.items():
        setattr(event, key, value)

    assert await _gate(turn, event) is False
    turn.client.reject_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["ask_question", "mcp_server_wait", "browser", "web_fetch"])
async def test_a_conductors_other_long_calls_are_left_alone(tool: str) -> None:
    """Not a shell: whatever length it names, the gate step lets it go on."""
    turn = _turn(_slot(), ACP_BACKEND_CLAUDE)
    event = _shell_event({"timeout": 600000, "seconds": 600})
    event.is_shell = False
    event.title = tool

    assert await _gate(turn, event) is False
    turn.client.reject_tool.assert_not_awaited()


#: A long shell call in the argument of each harness whose shell declares a length.
_LONG_SHELL = {
    ACP_BACKEND_CLAUDE: {"command": "sleep 600", "timeout": 600000},
    ACP_BACKEND_COPILOT: {"command": "sleep 600", "initial_wait": 600},
}


def test_the_gate_refuses_only_where_the_reason_has_no_in_turn_channel() -> None:
    """Of every selectable harness, only Claude Code's and Copilot CLI's shell declares
    a length, and neither takes a refusal steer (``ACP_BACKENDS_STEER``: kiro-cli and
    KAS) or a rejection reason (KAS alone stamps ``takes_rejection_reason``)."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, BASELINE_SELECTABLE_BACKENDS
    from kiro_crew.agent_sdk.backends_turns import ACP_BACKENDS_STEER

    every_argument = {"command": "x", "timeout": 600000, "initial_wait": 600, "delay": 600}
    refusing = {
        backend
        for backend in BASELINE_SELECTABLE_BACKENDS
        if conductor_step.shell_wait_secs(_shell_event(every_argument), backend)
    }

    assert refusing == set(_LONG_SHELL)
    assert refusing.isdisjoint(ACP_BACKENDS_STEER | {ACP_BACKEND_KAS})


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(_LONG_SHELL))
async def test_the_reason_reaches_the_model_in_the_recovery_turn(tmp_path, backend: str) -> None:
    """Through the real turn, on each harness the gate refuses on: nothing is steered,
    the rejection carries no reason, and the turn queues its recovery turn with the
    refusal's own words. In the turn itself the model has its prompt's line."""
    from chat_runner_coverage_helpers import _complete, _permission, _quiet_sel, _runner_state

    from kiro_crew.agent_sdk.backends_turns import ACP_BACKENDS_STEER
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard import chat_runner as cr
    from kiro_crew.dashboard.state import REFUSAL_RECOVERY_PREFIX

    state, client = _runner_state(tmp_path)
    client.capabilities = capabilities_for(backend)
    client.supports_refusal_steer = backend in ACP_BACKENDS_STEER  # the client's own rule
    client.steer = AsyncMock(return_value=True)
    slot = _ChatSlot("chat-step-1")
    slot._titled = True
    params = _LONG_SHELL[backend]

    async def stream(*_a: Any, **_k: Any) -> Any:
        # Set once the turn is open, as the wait test above does: this harness has no
        # conductor template to open a turn on. The gate reads the chat's agent here.
        slot.agent, slot.agent_kind = CONDUCTOR, "template"
        yield _permission(title="sleep 600", tool_input=json.dumps(params), raw_tool_params=params)
        yield _complete()

    client.stream = MagicMock(side_effect=stream)
    client.stream_command = client.stream
    slot._empty_response_retries = 2
    with (
        _quiet_sel(),
        patch.object(cr, "_start_next_queued_turn", AsyncMock()),
        patch("kiro_crew.execution_context.read_vouched_session_execution", return_value=None),
    ):
        await cr._run_chat(state, slot, "hello")

    client.steer.assert_not_awaited()
    client.reject_tool.assert_awaited_once_with("req-cov-1")
    (recovery,) = [
        e["content"] for e in slot._queue if e["content"].startswith(REFUSAL_RECOVERY_PREFIX)
    ]
    assert "this shell call asks to wait up to 600 s" in recovery


def test_every_conductor_prompt_states_the_bound() -> None:
    """Where the refusal's own words cannot reach the model, the prompt already did."""
    from kiro_crew import agent as agent_mod
    from kiro_crew.agent_parts import spec_prompts
    from kiro_crew.context_assembly import member

    rule = spec_prompts._STEP_BOUND_RULE.strip()
    prompts = {
        "conductor": agent_mod._CONDUCTOR_SYSTEM_PROMPT,
        "pipeline and security conductors": agent_mod._CONDUCTOR_WORK_LEDGER_PROTOCOL,
        "team lead": agent_mod._SHARED_CONDUCTOR_RULES_BLOCK,
        "crewmate conductor": member.operating_mode_block("Cove", ledger=True),
    }
    assert {name for name, text in prompts.items() if rule not in text} == set()
    assert "`run_in_background`" in rule and "60000 ms" in rule


@pytest.mark.parametrize(
    ("backend", "ways"),
    [
        (ACP_BACKEND_CLAUDE, ("a `timeout` of 60000 ms or less", "`run_in_background`")),
        (ACP_BACKEND_COPILOT, ("an `initial_wait` and a `delay` of 60 s or less",)),
    ],
)
def test_the_shell_refusal_names_the_ways_out(backend: str, ways: tuple[str, ...]) -> None:
    text = conductor_step.shell_refusal(120, backend)
    assert all(way in text for way in ways), text


@pytest.mark.parametrize(
    "text",
    [
        conductor_step.collection_refusal(),
        conductor_step.shell_refusal(600, ACP_BACKEND_CLAUDE),
    ],
    ids=["spawn_sub_agents", "shell"],
)
def test_every_refusal_says_to_poll_in_short_steps_or_hand_it_to_a_worker(text: str) -> None:
    assert text.startswith("Refused: ")
    assert "Poll in steps of 60 s or less" in text
    assert "hand the waiting to a worker session" in text
    assert "work_report" in text
