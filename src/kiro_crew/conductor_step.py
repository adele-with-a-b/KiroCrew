"""A Conductor holds no tool call open past one step's bound.

A step is one tool call. The owner's message into a running turn is read when the
call in flight ends, so a Conductor that sits in one call for minutes leaves that
message unread for minutes. The owner's rule: a Conductor polls in steps of the
bound at most, or has a worker wait and report when the thing is done.

This module owns that limit: which session is a Conductor, the bound, and the
words a refused call returns. Two calls read it: the blocking collection of
``spawn_sub_agents`` (refused at ``/api/spawn``), and a shell call that declares a
wait past the bound (refused at the tool gate, ``deny_chokepoints``, which sees only
a call the harness asks permission for: on Claude Code a call claude approves
itself raises no request, so it is not refused). ``wait`` is
not one of them: it already ends within one keepalive ping when a message for the
chat is steered in or queued (``live_turn._wait_end_reason``). A limit only, as the
conductors-stay-simple decision in ``docs/decisions/`` asks: nothing here gives a
Conductor a new ability.

A Conductor is named positively, by one rule (:func:`is_conductor_slot`) every gate
here reads: a chat whose own agent runs Crew Mode (a conductor template, or any agent
its Crew Mode stamp names, a custom one included: ``crew_mode.runs_crew_mode``); a
Bot's chat on the mate conductor, by its vouched execution record
(``mate_worker.is_conductor_execution``); and a session with no chat whose vouched
record names a conductor template. Anything else, including a session whose
identity cannot be read, is not limited here.

The shell bound reads what a call DECLARES, never how long it runs: a shell that
declares no length (kiro-cli's ``execute_bash``, a Claude Code ``Bash`` call with no
``timeout``, whose own default is longer than the bound) can still hold the call past
it, and a short command that declares a long ``timeout`` is refused. The refusal
names both ways out: a shorter ``timeout``, or ``run_in_background``.
"""

from __future__ import annotations

from typing import Any

#: The longest one tool call of a Conductor may stay open, in seconds.
CONDUCTOR_STEP_MAX_SECS = 60

#: The refusal code a refused ``spawn_sub_agents`` member carries.
NO_COLLECTION_CODE = "conductor_no_collection"


def is_conductor_slot(slot: Any, session_key: str = "") -> bool:
    """Whether the chat *slot* (session *session_key*) runs a Conductor.

    A chat whose own agent runs Crew Mode, as the dashboard reads it (so turning
    Crew Mode off ends the limit there); else this process's vouched execution
    record: a Bot's chat on the mate conductor (the chat names the Bot, not the
    conductor), or, for a session with no chat, a conductor template. In memory
    only; never raises.
    """
    from kiro_crew import crew_mode

    try:
        if slot is not None and crew_mode.runs_crew_mode(
            getattr(slot, "agent", ""),
            getattr(slot, "agent_kind", ""),
            getattr(slot, "mode", ""),
            getattr(slot, crew_mode.STAMP_FIELD, ""),
        ):
            return True
        if not session_key:
            return False
        from kiro_crew.execution_context import read_vouched_session_execution
        from kiro_crew.mate_worker import is_conductor_execution

        record = read_vouched_session_execution(session_key)
        if record is None:
            return False
        if is_conductor_execution(record):
            return True
        template = getattr(record, "template_id", "")
        return (
            slot is None and isinstance(template, str) and template in crew_mode.conductor_agents()
        )
    except Exception:  # noqa: BLE001 - an unreadable identity is not a Conductor's
        return False


def _whole_secs(value: object, per_sec: int = 1) -> int:
    """*value* as whole seconds (``per_sec`` units each), or 0 when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return 0
    return int(-(-value // per_sec))


def shell_wait_secs(event: Any, backend: str) -> int:
    """How long a shell call says it may wait, by its harness's own argument, else 0.

    Named per harness, positively: Claude Code's ``Bash`` takes ``timeout`` in
    milliseconds (and returns at once with ``run_in_background``), Copilot CLI's
    ``bash`` and ``read_bash`` take ``initial_wait`` and ``delay`` in seconds. A
    shell with no such argument (kiro-cli's ``execute_bash``) declares nothing, so
    0: nothing here cuts a command that is already running.
    """
    if getattr(event, "is_shell", False) is not True:
        return 0
    params = getattr(event, "raw_tool_params", None)
    if not isinstance(params, dict):
        return 0
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_COPILOT

    if backend == ACP_BACKEND_CLAUDE:
        if params.get("run_in_background") is True:
            return 0
        return _whole_secs(params.get("timeout"), per_sec=1000)
    if backend == ACP_BACKEND_COPILOT:
        return max(_whole_secs(params.get("initial_wait")), _whole_secs(params.get("delay")))
    return 0


def _refusal(what: str, *, way_out: str = "") -> str:
    return (
        f"Refused: {what} A Conductor holds no tool call open longer than "
        f"{CONDUCTOR_STEP_MAX_SECS} s, so the owner's message is read at your next "
        f"step. {way_out}Poll in steps of {CONDUCTOR_STEP_MAX_SECS} s or less, or hand the "
        "waiting to a worker session: say what to watch, to wait in steps of "
        f"{CONDUCTOR_STEP_MAX_SECS} s or less, and to report with work_report when it "
        "is done. Then end your turn; the report wakes you."
    )


def collection_refusal() -> str:
    """The text a Conductor's ``spawn_sub_agents`` member is refused with."""
    return _refusal(
        "`spawn_sub_agents` waits inside the call for its sub-agents, up to two hours.",
        way_out="Start each with `spawn_run`, which returns at once. ",
    )


def shell_refusal(seconds: int, backend: str = "") -> str:
    """The text a Conductor's shell call that may wait *seconds* is refused with.

    It names the ways out in *backend*'s own arguments (:func:`shell_wait_secs`).
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_COPILOT

    way_out = ""
    if backend == ACP_BACKEND_CLAUDE:
        way_out = (
            f"Run it again with a `timeout` of {CONDUCTOR_STEP_MAX_SECS * 1000} ms or less, "
            "or with `run_in_background` and check its output in later steps. "
        )
    elif backend == ACP_BACKEND_COPILOT:
        way_out = (
            f"Run it again with an `initial_wait` and a `delay` of {CONDUCTOR_STEP_MAX_SECS} s "
            "or less, and read its output in later steps. "
        )
    return _refusal(f"this shell call asks to wait up to {seconds} s.", way_out=way_out)
