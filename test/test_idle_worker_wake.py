"""An idle worker owing its next move wakes its conductor within a minute.

A worker bound to an open item can stop with no ``done``, ``question`` or ``blocked``
report: its turn ended, nothing of its own will wake it, and the conductor heard
nothing. The stall wake fired once per REPORT and only past the fifteen-minute window,
so a worker nudged once and stopped again was never found, and the owner had to nudge.
``work_ledger.idle_spell`` is the one rule for "idle with nothing to wake it"; the
probe's ``reason=worker_idle`` wake and the Board's Quiet (``stale``) both read it.

One clock (:class:`ManualClock`) is installed on every module the rule reads, so no
test sleeps and no stamp depends on when the test ran.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import conductor_wake, work_ledger, work_ledger_records, work_ledger_rules
from kiro_crew.dashboard import slot_retention
from kiro_crew.probes import work_ledger as probe_module
from kiro_crew.testing.clock import ManualClock

CONDUCTOR = "chat-conductor"
WORKER = "chat-worker"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


@pytest.fixture
def clock(monkeypatch) -> ManualClock:
    """One virtual instant for the store's stamps, the rule's now and the probe's."""
    manual = ManualClock()
    manual.install(monkeypatch, work_ledger_records, time=False, datetime=True)
    manual.install(monkeypatch, work_ledger_rules, time=False, datetime=True)
    manual.install(monkeypatch, probe_module, time=True, datetime=False)
    return manual


def _bound_item(status: str | None = "progress") -> str:
    work_ledger.ensure_conductor(CONDUCTOR, goal="ship it")
    out = work_ledger.apply_conductor_action(
        CONDUCTOR, "create", title="a task", acceptance={"kind": "human_approval"}
    )
    item = str(out["item"].item_id)
    work_ledger.apply_conductor_action(CONDUCTOR, "bind", item_id=item, worker_session_key=WORKER)
    if status is not None:
        work_ledger.apply_worker_report(CONDUCTOR, item, status=status, summary="moving")
    return item


class _Job:
    id = "loop-1"


class _Ctx:
    def __init__(self) -> None:
        self.message = json.dumps({"conductor": CONDUCTOR})
        self.job = _Job()


def _briefs(idle_since: Any, **resolvers: Any) -> str:
    """One probe tick with the worker's chat idle since *idle_since*; its wake text."""
    from kiro_crew.probes.work_ledger import WorkLedgerProbe

    asked: list[str] = resolvers.pop("asked", [])

    def _idle(key: str) -> float | None:
        asked.append(key)
        return idle_since() if callable(idle_since) else idle_since

    probe = WorkLedgerProbe(worker_idle_since=_idle, **resolvers)
    ctx = _Ctx()
    probe.identity(ctx)
    return "\n".join(obs.brief for obs in probe.observe(ctx).observations)


# ── the wake ──────────────────────────────────────────────────────────────────


def test_an_idle_worker_wakes_its_conductor_a_minute_after_its_turn_ended(clock):
    item = _bound_item()
    clock.advance(5)
    ended = clock.time()
    clock.advance(work_ledger.IDLE_WAKE_SECS - 1)
    assert "reason=worker_idle" not in _briefs(ended), "under a minute is not idle yet"
    clock.advance(2)
    text = _briefs(ended)
    assert f"[work-ledger wake] item={item} reason=worker_idle last_status=progress" in text
    assert f"idle_secs={int(work_ledger.IDLE_WAKE_SECS + 1)}" in text
    # Well inside the fifteen-minute stall window: the stall wake says nothing yet.
    assert "reason=stall" not in text


def test_one_idle_spell_wakes_once_and_a_new_spell_wakes_again(clock):
    """The old stall token was one per report, so a worker nudged and stopped again
    without reporting was never found a second time."""
    item = _bound_item()
    clock.advance(1)
    first = clock.time()
    clock.advance(90)
    assert "reason=worker_idle" in _briefs(first)
    assert "reason=worker_idle" not in _briefs(first), "the same spell wakes once"
    # Nudged: a turn ran (no report) and ended again.
    clock.advance(30)
    second = clock.time()
    clock.advance(work_ledger.IDLE_WAKE_SECS + 1)
    text = _briefs(second)
    assert f"item={item} reason=worker_idle" in text


@pytest.mark.parametrize("status", ["blocked", "question", "done"])
def test_a_worker_waiting_on_a_person_or_finished_raises_no_idle_wake(clock, status):
    _bound_item(status)
    clock.advance(1)
    ended = clock.time()
    clock.advance(600)
    assert "reason=worker_idle" not in _briefs(ended)


def test_a_running_looping_or_unknown_worker_raises_no_idle_wake(clock):
    _bound_item()
    clock.advance(1)
    ended = clock.time()
    clock.advance(600)
    assert "reason=worker_idle" not in _briefs(ended, worker_running=lambda _k: True)
    assert "reason=worker_idle" not in _briefs(ended, worker_looping=lambda _k: True)
    # ``None`` is the resolver's answer for a turn in flight, a pending approval or
    # question card, sub-agents still out, and a standing manager.
    assert "reason=worker_idle" not in _briefs(None)


def test_an_unbound_closed_or_remote_item_raises_no_idle_wake(clock):
    work_ledger.ensure_conductor(CONDUCTOR, goal="ship it")
    work_ledger.apply_conductor_action(
        CONDUCTOR, "create", title="no worker", acceptance={"kind": "human_approval"}
    )
    closed = _bound_item()
    work_ledger.apply_conductor_action(CONDUCTOR, "close", item_id=closed, state="abandoned")
    remote = str(
        work_ledger.apply_conductor_action(
            CONDUCTOR, "create", title="on a crew", acceptance={"kind": "human_approval"}
        )["item"].item_id
    )
    work_ledger.apply_remote_bind(
        CONDUCTOR, remote, worker_session_key="peer-a", worker_instance_id="lab"
    )
    clock.advance(1)
    ended = clock.time()
    clock.advance(600)
    asked: list[str] = []
    assert "reason=worker_idle" not in _briefs(ended, asked=asked)
    assert asked == [], "no local worker is open on any of them, so no slot is read"


def test_a_chat_idle_since_before_its_last_report_has_not_stopped_on_it(clock):
    """Idle since before the report (or the bind) means the clock was read wrong or
    the spell belongs to earlier work: never an idle wake."""
    before = clock.time()
    clock.advance(10)
    _bound_item()
    clock.advance(600)
    assert "reason=worker_idle" not in _briefs(before)


def test_the_boards_quiet_reads_the_same_rule(clock):
    """``stale`` turns on with the idle spell, inside the fifteen-minute window."""
    item_id = _bound_item()
    clock.advance(1)
    ended = clock.time()
    clock.advance(work_ledger.IDLE_WAKE_SECS + 1)
    item = work_ledger.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert work_ledger.is_stale(item, worker_running=False) is False
    assert work_ledger.is_stale(item, worker_running=False, worker_idle_since=ended) is True
    assert (
        work_ledger.is_stale(
            item, worker_running=False, worker_looping=True, worker_idle_since=ended
        )
        is False
    )


# ── the slot half: idle with nothing of its own to wake it ─────────────────────


class _Slot:
    def __init__(self, **fields: Any) -> None:
        self.key = fields.pop("key", f"dashboard_{WORKER}")
        self.turn_running = fields.pop("turn_running", False)
        self.title = fields.pop("title", "Port the gate")
        self._question_pending = fields.pop("question_pending", None)
        assert not fields


def _state(slot: _Slot | None) -> SimpleNamespace:
    return SimpleNamespace(get_slot=lambda key: slot if slot and key == slot.key else None)


@pytest.mark.parametrize(
    "slot",
    [
        _Slot(turn_running=True),
        _Slot(title="Manager: website"),
        _Slot(question_pending={"ask-1": object()}),
        None,
    ],
    ids=["running", "standing-manager", "question-card", "no-slot"],
)
def test_a_chat_with_something_to_wake_it_is_not_idle(monkeypatch, slot):
    monkeypatch.setattr(slot_retention, "slot_last_activity", lambda _s: 1_000.0)
    monkeypatch.setattr(slot_retention, "has_pending_approval", lambda _st, _s: False)
    monkeypatch.setattr(slot_retention, "subagents_attached_now", lambda _st, _s: False)
    assert slot_retention.worker_idle_since(_state(slot), WORKER) is None


def test_an_idle_chat_answers_its_last_activity_and_an_approval_or_subagent_holds_it(
    monkeypatch,
):
    slot = _Slot()
    monkeypatch.setattr(slot_retention, "slot_last_activity", lambda _s: 1_000.0)
    pending = {"approval": False, "agents": False}
    monkeypatch.setattr(slot_retention, "has_pending_approval", lambda _st, _s: pending["approval"])
    monkeypatch.setattr(slot_retention, "subagents_attached_now", lambda _st, _s: pending["agents"])
    # Bare and prefixed spellings both find the slot.
    assert slot_retention.worker_idle_since(_state(slot), WORKER) == 1_000.0
    assert slot_retention.worker_idle_since(_state(slot), slot.key) == 1_000.0
    pending["approval"] = True
    assert slot_retention.worker_idle_since(_state(slot), WORKER) is None
    pending.update(approval=False, agents=True)
    assert slot_retention.worker_idle_since(_state(slot), WORKER) is None


# ── the push: within a minute, not at the patrol's cadence ────────────────────


class _Svc:
    def __init__(self, idle_since: float | None) -> None:
        self.idle_since = idle_since
        self.asked: list[str] = []

    def _worker_idle_since(self, key: str) -> float | None:
        self.asked.append(key)
        return self.idle_since


@pytest.mark.asyncio
async def test_a_run_end_arms_the_conductors_tick_one_idle_threshold_later(monkeypatch, clock):
    from kiro_crew import autonudge

    clock.install(monkeypatch, conductor_wake, time=True, datetime=False)
    item = _bound_item()
    clock.advance(1)
    svc = _Svc(idle_since=clock.time())
    waits: list[float] = []
    fired: list[tuple[str, str, str]] = []

    async def _wait(secs: float) -> None:
        waits.append(secs)
        clock.advance(secs)

    async def _fire(_svc, conductor, item_id, *, budget_key=""):
        fired.append((conductor, item_id, budget_key))
        return "loop-wl"

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
    monkeypatch.setattr(conductor_wake, "_service_loop", lambda _svc: loop)
    monkeypatch.setattr(conductor_wake, "_idle_wait", _wait)
    monkeypatch.setattr(conductor_wake, "_fire", _fire)

    future = await asyncio.to_thread(conductor_wake.wake_for_idle_worker, WORKER)
    assert future is not None
    assert await asyncio.wait_for(asyncio.wrap_future(future), 5) == "loop-wl"
    assert waits == [work_ledger.IDLE_WAKE_SECS + conductor_wake._IDLE_PUSH_SLACK_SECS]
    # Its own budget: many short turns cannot spend the one its done report needs.
    assert fired == [(CONDUCTOR, item, f"idle:{item}")]


@pytest.mark.asyncio
async def test_a_worker_that_started_again_in_the_wait_costs_no_tick(monkeypatch, clock):
    from kiro_crew import autonudge

    clock.install(monkeypatch, conductor_wake, time=True, datetime=False)
    _bound_item()
    svc = _Svc(idle_since=None)
    fired: list[str] = []

    async def _wait(secs: float) -> None:
        clock.advance(secs)

    async def _fire(*_a, **_k):
        fired.append("x")
        return "loop-wl"

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
    monkeypatch.setattr(conductor_wake, "_service_loop", lambda _svc: loop)
    monkeypatch.setattr(conductor_wake, "_idle_wait", _wait)
    monkeypatch.setattr(conductor_wake, "_fire", _fire)
    future = await asyncio.to_thread(conductor_wake.wake_for_idle_worker, WORKER)
    assert future is not None
    assert await asyncio.wait_for(asyncio.wrap_future(future), 5) == ""
    assert fired == [] and svc.asked == [WORKER]


@pytest.mark.parametrize("status", ["done", "question", "blocked"])
def test_a_run_that_ended_on_a_waking_report_arms_no_idle_push(monkeypatch, clock, status):
    """Those reports push their own wake; an unbound chat owes none."""
    from kiro_crew import autonudge

    _bound_item(status)
    monkeypatch.setattr(autonudge, "get_instance", lambda: pytest.fail("no push is owed"))
    assert conductor_wake.wake_for_idle_worker(WORKER) is None
    assert conductor_wake.wake_for_idle_worker("chat-unbound") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "armed"),
    [
        ({"turn": 3}, True),
        ({"turn": 3, "continuing": True}, False),
        ({"turn": 3, "needs_input": True}, False),
        ({}, False),
    ],
    ids=["run-ended", "continuing", "question-to-person", "no-turn"],
)
async def test_only_a_run_end_arms_the_idle_push(monkeypatch, payload, armed):
    from kiro_crew.dashboard.chat_turn import queue_cycle

    seen: list[str] = []
    done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _push(key: str) -> None:
        seen.append(key)
        loop.call_soon_threadsafe(done.set)

    monkeypatch.setattr(conductor_wake, "wake_for_idle_worker", _push)
    queue_cycle._push_idle_wake(SimpleNamespace(key=f"dashboard_{WORKER}"), payload)
    if armed:
        await asyncio.wait_for(done.wait(), 5)
        assert seen == [f"dashboard_{WORKER}"]
    else:
        # Refused before anything is scheduled, so there is nothing to wait for.
        assert seen == []
