"""A loop that stood down for failing turns starts again when a turn completes.

In an outage every model call fails, so every loop reaches one of the two failure
stand-downs (``consecutive_failures``, ``session_start_failures``) and stops. The
evidence that clears the streaks, a turn that completed on the slot, restarts a loop
already stopped for them. Every other stop stays: a person's pause, a finished
subject, the loop's own stop file, a spent bound.

Nothing here waits on time: the timer's idle wait is collapsed, the revival's one
arm is recorded instead of run, and the detached revival is awaited by its task.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import (
    AUTONUDGE_STOP_REASON,
    CONSECUTIVE_FAILURE_REASON,
    MANUAL_STOP_REASON,
    MONITOR_TERMINAL_REASON,
    SESSION_START_FAILURE_REASON,
    STOP_SENTINEL_REASON,
    AutoNudgeService,
    NudgeLoop,
)
from kiro_crew.autonudge_service import subject_reads
from kiro_crew.autonudge_service.model import (
    _REPLACEABLE_LOOP_STOP_REASONS,
    FAILURE_STANDDOWN_REASONS,
)
from kiro_crew.dashboard.chat_turn import stop_recovery
from kiro_crew.monitoring.models import MonitorState

SLOT = "chat-1-771"

#: The streak field each stand-down reads, so a test reaches the stop through the
#: timer's own branch instead of writing the reason itself.
_STREAK_FIELD = {
    CONSECUTIVE_FAILURE_REASON: "consecutive_failed_cycles",
    SESSION_START_FAILURE_REASON: "consecutive_start_failures",
}


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _no_published_service_outlives_the_test():
    """Unpublish the singleton and bound leftover work after every test.

    Sync on purpose: this suite's pytest-asyncio pin errors on async-generator
    fixtures at setup.
    """
    yield
    svc = _an.get_instance()
    if svc is None:
        return
    try:
        inflight = getattr(svc, "_inflight_adds", None)
        if inflight is not None:
            for task in list(inflight):
                task.cancel()
            inflight.clear()
        svc.stop()
    finally:
        _an._INSTANCE = None


@pytest.fixture
def store_dir(tmp_path_factory):
    """Owned by the session: a store write handed to a thread can land after the test."""
    return tmp_path_factory.mktemp("autonudge-standdown-resume")


@pytest.fixture
def svc(store_dir):
    return AutoNudgeService(base_dir=store_dir)


@pytest.fixture
def _nosleep(monkeypatch):
    """Collapse the timer's idle wait, at the seam the wait goes through."""

    async def _noop(_secs):
        return None

    monkeypatch.setattr(subject_reads, "_nap", _noop)


async def _armed(svc: AutoNudgeService, **kwargs) -> NudgeLoop:
    await svc.start()
    loop = await svc.add(slot_key=SLOT, message="go", idle_secs=600, **kwargs)
    # The tests tick the timer themselves, so the countdown ``add`` armed is retired.
    svc._cancel_timer(loop.id)
    return loop


async def _stood_down(svc: AutoNudgeService, reason: str, **kwargs) -> NudgeLoop:
    """A loop the timer itself stopped for *reason*, five failures in."""
    loop = await _armed(svc, **kwargs)
    setattr(svc._loops[loop.id], _STREAK_FIELD[reason], 5)
    await svc._timer(svc._loops[loop.id])
    stopped = svc._loops[loop.id]
    assert (stopped.active, stopped.stopped_reason) == (False, reason)
    return stopped


def _record_arms(svc: AutoNudgeService, monkeypatch) -> list[str]:
    """Record each countdown the service arms instead of starting it."""
    armed: list[str] = []
    monkeypatch.setattr(svc, "_arm_from_deadline", lambda loop: armed.append(loop.id))
    return armed


async def _settle(svc: AutoNudgeService) -> None:
    """Await every detached write and revival the hook scheduled."""
    for _ in range(3):
        inflight = list(svc._inflight_adds)
        if not inflight:
            return
        await asyncio.gather(*inflight, return_exceptions=True)


async def _stop_and_drain(svc: AutoNudgeService) -> None:
    timers = list(svc._timers.values())
    svc.stop()
    if timers:
        await asyncio.gather(*timers, return_exceptions=True)
    await _settle(svc)


def test_the_restart_set_is_the_two_failure_stand_downs():
    """The reasons are named here, not read from the set: a set that lost or gained
    one would otherwise change which cases the tests below run."""
    assert FAILURE_STANDDOWN_REASONS == {CONSECUTIVE_FAILURE_REASON, SESSION_START_FAILURE_REASON}


@pytest.mark.parametrize("reason", sorted(_STREAK_FIELD))
@pytest.mark.asyncio
async def test_a_completed_turn_restarts_a_loop_stood_down_for_failures(
    svc, _nosleep, monkeypatch, reason
):
    """The outage case: five failures stop the loop, the model road comes back, a
    turn completes on the slot. The loop is running again, on a fresh run."""
    loop = await _stood_down(svc, reason)
    generation = loop.config_generation
    armed = _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)

    resumed = svc._loops[loop.id]
    assert resumed.active is True
    assert resumed.stopped_reason == ""
    assert (resumed.consecutive_failed_cycles, resumed.consecutive_start_failures) == (0, 0)
    assert resumed.config_generation == generation + 1
    assert armed == [loop.id], "the revival arms exactly one countdown"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_restart_reaches_the_store(svc, store_dir, _nosleep, monkeypatch):
    """A gateway restart right after must find the loop running, not stood down."""
    loop = await _stood_down(svc, CONSECUTIVE_FAILURE_REASON)
    _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)
    await _stop_and_drain(svc)
    _an._INSTANCE = None

    reloaded = AutoNudgeService(base_dir=store_dir)
    _record_arms(reloaded, monkeypatch)
    await reloaded.start()
    try:
        assert reloaded._loops[loop.id].active is True
        assert reloaded._loops[loop.id].stopped_reason == ""
    finally:
        await _stop_and_drain(reloaded)


@pytest.mark.asyncio
async def test_a_stand_down_read_back_after_a_gateway_restart_still_restarts(
    svc, store_dir, _nosleep, monkeypatch
):
    """The stop is read from the stored reason, so it outlives the process that
    recorded it: an outage that spans a restart ends the same way."""
    loop = await _stood_down(svc, CONSECUTIVE_FAILURE_REASON)
    await svc._persist_locked()
    await _stop_and_drain(svc)
    _an._INSTANCE = None

    reloaded = AutoNudgeService(base_dir=store_dir)
    armed = _record_arms(reloaded, monkeypatch)
    await reloaded.start()
    try:
        assert reloaded._loops[loop.id].active is False
        reloaded.notify_cycle_landed(SLOT, succeeded=True)
        await _settle(reloaded)
        assert reloaded._loops[loop.id].active is True
        assert armed == [loop.id]
    finally:
        await _stop_and_drain(reloaded)


@pytest.mark.asyncio
async def test_a_stood_down_work_ledger_patrol_comes_back_with_its_watch(
    svc, _nosleep, monkeypatch
):
    """The loop this exists for: a conductor's gated work-ledger watch. It comes back
    as the same record, still gated on the same subject."""
    await svc.start()
    monitor = MonitorState(
        kind="work-ledger", target=SLOT, objective="review_ready", created_ts=1_000.0
    )
    patrol = NudgeLoop(
        id="patrol-1",
        slot_key=SLOT,
        message="wake the conductor from its work ledger",
        idle_secs=300,
        monitor=monitor,
        gate=True,
        default_patrol=True,
        active=False,
        stopped_reason=CONSECUTIVE_FAILURE_REASON,
        consecutive_failed_cycles=5,
    )
    svc._loops[patrol.id] = patrol
    armed = _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)

    resumed = svc._loops[patrol.id]
    assert resumed.active is True and resumed.stopped_reason == ""
    assert resumed.gate is True and resumed.default_patrol is True
    assert resumed.monitor is monitor and monitor.outcome is None
    assert armed == [patrol.id]
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_loop_a_person_stopped_stays_stopped(svc, _nosleep, monkeypatch):
    """A pause is a decision. Turns completing on the slot do not undo it, even
    with a failure streak on the record."""
    loop = await _armed(svc)
    svc._loops[loop.id].consecutive_failed_cycles = 4
    await svc.update(loop.id, active=False)  # the pause route records "manual"
    armed = _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)

    stopped = svc._loops[loop.id]
    assert (stopped.active, stopped.stopped_reason) == (False, MANUAL_STOP_REASON)
    assert armed == []
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_loop_whose_subject_finished_stays_stopped(svc, _nosleep, monkeypatch):
    """A merged or closed subject leaves nothing to watch, whatever lands after."""
    loop = await _armed(svc)
    await svc.update(loop.id, active=False, stopped_reason=MONITOR_TERMINAL_REASON)
    armed = _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)

    stopped = svc._loops[loop.id]
    assert (stopped.active, stopped.stopped_reason) == (False, MONITOR_TERMINAL_REASON)
    assert armed == []
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_loop_stopped_by_its_kill_switch_stays_stopped(
    svc, _nosleep, monkeypatch, tmp_path
):
    """The stop file says the goal is reached. The timer stops the loop on it, and
    a completed turn does not start it again."""
    stop_file = tmp_path / "stop"
    loop = await _armed(svc, stop_sentinel_path=str(stop_file))
    stop_file.write_text("done", encoding="utf-8")
    await svc._timer(svc._loops[loop.id])
    assert svc._loops[loop.id].stopped_reason == STOP_SENTINEL_REASON
    armed = _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)

    stopped = svc._loops[loop.id]
    assert (stopped.active, stopped.stopped_reason) == (False, STOP_SENTINEL_REASON)
    assert armed == []
    await _stop_and_drain(svc)


@pytest.mark.parametrize(
    "reason",
    sorted(
        (_REPLACEABLE_LOOP_STOP_REASONS - set(_STREAK_FIELD))
        | {AUTONUDGE_STOP_REASON, "a_reason_a_later_version_adds"}
    ),
)
@pytest.mark.asyncio
async def test_no_other_stop_is_restarted(svc, _nosleep, monkeypatch, reason):
    """Every other system stop, an app's tombstone and an unknown reason: each says
    something a completed turn does not answer, so each fails closed to stopped."""
    loop = await _armed(svc)
    await svc.update(loop.id, active=False, stopped_reason=reason)
    armed = _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)

    stopped = svc._loops[loop.id]
    assert (stopped.active, stopped.stopped_reason) == (False, reason)
    assert armed == []
    await _stop_and_drain(svc)


@pytest.mark.parametrize("call", [{}, {"succeeded": False}], ids=["unknown", "not-succeeded"])
@pytest.mark.asyncio
async def test_a_turn_that_did_not_succeed_restarts_nothing(svc, _nosleep, monkeypatch, call):
    """A turn the person stopped reaches the hook too, and proves nothing about the
    model answering. Unknown reads the same way: a loop restarted on no evidence
    spends five more turns failing."""
    loop = await _stood_down(svc, CONSECUTIVE_FAILURE_REASON)
    armed = _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, **call)
    await _settle(svc)

    stopped = svc._loops[loop.id]
    assert (stopped.active, stopped.stopped_reason) == (False, CONSECUTIVE_FAILURE_REASON)
    assert armed == []
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_stop_that_changed_before_the_revival_runs_is_left_alone(
    svc, _nosleep, monkeypatch
):
    """The hook only schedules the revival, and the stop is read again under the
    lock. A person's pause that is ahead of it in the queue for that lock wins."""
    loop = await _stood_down(svc, CONSECUTIVE_FAILURE_REASON)
    armed = _record_arms(svc, monkeypatch)

    # Created first, so it reaches the lock first; the hook still reads a stand-down.
    pause = asyncio.ensure_future(svc.update(loop.id, active=False))
    svc.notify_cycle_landed(SLOT, succeeded=True)
    await pause
    await _settle(svc)

    stopped = svc._loops[loop.id]
    assert (stopped.active, stopped.stopped_reason) == (False, MANUAL_STOP_REASON)
    assert armed == []
    await _stop_and_drain(svc)


@pytest.mark.parametrize("reason", sorted(_STREAK_FIELD))
@pytest.mark.asyncio
async def test_a_pause_on_a_loop_that_just_stood_down_holds(svc, _nosleep, monkeypatch, reason):
    """Pause pressed off a reading that still said running, a moment after the timer
    stood the loop down. The route sends ``active=False`` and no reason. That is a
    person's decision, so it is recorded as one and a completed turn leaves the
    loop stopped."""
    loop = await _stood_down(svc, reason)
    armed = _record_arms(svc, monkeypatch)

    await svc.update(loop.id, active=False)
    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)

    stopped = svc._loops[loop.id]
    assert (stopped.active, stopped.stopped_reason) == (False, MANUAL_STOP_REASON)
    assert armed == []
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_persons_resume_beside_the_revival_is_one_revival(svc, _nosleep, monkeypatch):
    """Play pressed in the same moment: one revival, one generation, one arm."""
    loop = await _stood_down(svc, CONSECUTIVE_FAILURE_REASON)
    generation = loop.config_generation
    armed = _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, succeeded=True)
    await svc.update(loop.id, active=True)
    await _settle(svc)

    resumed = svc._loops[loop.id]
    assert resumed.active is True
    assert resumed.config_generation == generation + 1
    assert armed == [loop.id]
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_pause_after_the_restart_holds_against_the_next_turn(svc, _nosleep, monkeypatch):
    """Restarted, then paused by a person: the next completed turn leaves it paused."""
    loop = await _stood_down(svc, CONSECUTIVE_FAILURE_REASON)
    armed = _record_arms(svc, monkeypatch)
    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)
    assert svc._loops[loop.id].active is True

    await svc.update(loop.id, active=False)
    svc.notify_cycle_landed(SLOT, succeeded=True)
    await _settle(svc)

    stopped = svc._loops[loop.id]
    assert (stopped.active, stopped.stopped_reason) == (False, MANUAL_STOP_REASON)
    assert armed == [loop.id]
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_loop_cleared_before_the_revival_runs_is_left_gone(svc, _nosleep, monkeypatch):
    loop = await _stood_down(svc, CONSECUTIVE_FAILURE_REASON)
    armed = _record_arms(svc, monkeypatch)

    svc.notify_cycle_landed(SLOT, succeeded=True)
    svc.remove_sync(loop.id)
    await _settle(svc)

    assert loop.id not in svc._loops
    assert armed == []
    assert not svc._inflight_adds
    await _stop_and_drain(svc)


def test_the_turn_end_passes_whether_the_turn_ended_ok():
    """The hook restarts nothing unless its caller says the turn succeeded, so the
    one caller must say it, from the turn's own outcome."""
    tree = ast.parse(Path(stop_recovery.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "notify_cycle_landed"
    ]
    assert len(calls) == 1
    passed = {kw.arg: ast.unparse(kw.value) for kw in calls[0].keywords}
    assert passed == {"succeeded": "_turn_outcome(_stop_reason) == 'ok'"}
