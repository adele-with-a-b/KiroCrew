"""A stopped legacy loop: the agent's own stop frees the slot, a person's pause is kept.

The two stop kinds a conductor's work-ledger patrol meets, end to end:

* the agent's own ``monitor_stop`` REMOVES the legacy row, so its next
  ``monitor_start`` arms;
* a person's Pause (the goal popover's ``PATCH active:false``) records
  ``stopped_reason="manual"``, which the arm path keeps as evidence. The turn
  boundary refuses the re-arm, and that refusal is only a transcript ``notice``,
  which a model never reads. So the arming tools must refuse IN BAND, naming the
  reason and the person's remedy, instead of answering "requested".
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import autonudge as _an
from kiro_crew import mcp_core, session_directive
from kiro_crew.autonudge import AutoNudgeService, MonitorUpdateConflict
from kiro_crew.dashboard import session_directive_apply_monitor as apply_monitor
from kiro_crew.dashboard.handlers.autonudge import _autonudge_loop_reading
from kiro_crew.mcp_tools import control

SLOT = "chat-1-123"
SESSION = "dashboard:chat-1-123"


@pytest.fixture(autouse=True)
def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture
def svc(tmp_path_factory: pytest.TempPathFactory) -> AutoNudgeService:
    """An unstarted service: no timers, so nothing outlives the test."""
    return AutoNudgeService(base_dir=tmp_path_factory.mktemp("autonudge-store"))


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the tools believe they run on a session that can host a loop."""
    monkeypatch.setattr(mcp_core, "_autonudge_binding_key", lambda sk: SLOT)
    monkeypatch.setattr(mcp_core, "_structured_monitor_binding_key", lambda sk: SLOT)
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda *a, **k: SESSION)


def _wire(loop: Any) -> dict[str, Any]:
    """What ``api_session_monitor_get`` returns for a legacy loop, through JSON."""
    reading = {"enabled": True, "monitor": None, "autonudge_loop": _autonudge_loop_reading(loop)}
    return json.loads(json.dumps(reading))


def _emitted(text: str, tool: str) -> bool:
    return session_directive.decode(text, tool) is not None


async def _arm(svc: AutoNudgeService) -> Any:
    return await svc.add(slot_key=SLOT, message="patrol", idle_secs=300, gate=True)


async def _rearm(svc: AutoNudgeService) -> Any:
    """Exactly the store write ``_monitor_start`` asks for at the turn boundary."""
    return await svc.add(
        slot_key=SLOT,
        message="patrol again",
        idle_secs=300,
        gate=True,
        replace_existing=False,
        replace_stopped=True,
    )


def _start() -> str:
    return control.monitor_start(
        "monitor_start", {"message": "Read the work ledger", "watch": "work-ledger"}
    )


class TestBothStopKindsAtTheStore:
    @pytest.mark.asyncio
    async def test_the_agents_own_stop_never_blocks_its_next_start(
        self, svc: AutoNudgeService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = await _arm(svc)
        monkeypatch.setattr(_an, "get_instance", lambda: svc)

        out = await apply_monitor._monitor_stop(None, SESSION, {})

        assert "stopped" in out, out
        assert svc.get_by_slot(SLOT) is None, "the agent's own stop leaves no row behind"
        rearmed = await _rearm(svc)
        assert rearmed.active and rearmed.id != loop.id

    @pytest.mark.asyncio
    async def test_a_persons_pause_is_kept_and_refuses_the_rearm(
        self, svc: AutoNudgeService
    ) -> None:
        loop = await _arm(svc)
        await svc.update(loop.id, active=False)
        assert svc.get_by_id(loop.id).stopped_reason == "manual"

        with pytest.raises(
            MonitorUpdateConflict, match=r"retained as evidence \(stop reason: manual\)"
        ):
            await _rearm(svc)

        kept = svc.get_by_id(loop.id)
        assert kept is not None and not kept.active


class TestTheReadingCarriesTheArmPathsAnswer:
    @pytest.mark.asyncio
    async def test_a_persons_pause_reads_as_retained(self, svc: AutoNudgeService) -> None:
        loop = await _arm(svc)
        await svc.update(loop.id, active=False)

        assert _wire(svc.get_by_id(loop.id))["autonudge_loop"]["retained_stop"] is True

    @pytest.mark.asyncio
    async def test_a_live_or_system_stopped_loop_carries_no_mark(
        self, svc: AutoNudgeService
    ) -> None:
        loop = await _arm(svc)
        assert "retained_stop" not in _wire(loop)["autonudge_loop"]

        await svc.update(loop.id, active=False, stopped_reason="cycle_cap")
        assert "retained_stop" not in _wire(svc.get_by_id(loop.id))["autonudge_loop"]


class TestTheToolRefusesInBand:
    @pytest.mark.asyncio
    async def test_start_names_the_persons_pause_and_its_remedy(
        self, svc: AutoNudgeService, bound: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = await _arm(svc)
        await svc.update(loop.id, active=False)
        reading = _wire(svc.get_by_id(loop.id))
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        out = _start()

        assert not _emitted(out, "monitor_start"), out
        assert "requested" not in out.lower(), out
        assert "stop reason: manual" in out and "by a person" in out, out
        assert loop.id in out and "goal popover" in out, out

    @pytest.mark.asyncio
    async def test_update_is_refused_in_band_too(
        self, svc: AutoNudgeService, bound: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = await _arm(svc)
        await svc.update(loop.id, active=False)
        reading = _wire(svc.get_by_id(loop.id))
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        out = control.monitor_update("monitor_update", {"watch": "work-ledger"})

        assert not _emitted(out, "monitor_update"), out
        assert "stop reason: manual" in out and "stays paused" in out, out

    @pytest.mark.parametrize(
        "legacy",
        [
            # An older gateway's reading, with no mark: fail open.
            {"id": "a1", "active": False, "stopped_reason": "manual"},
            # A system stop the arm path displaces.
            {"id": "a1", "active": False, "stopped_reason": "cycle_cap"},
            {"id": "a1", "active": True, "stopped_reason": ""},
        ],
    )
    def test_a_loop_the_arm_path_would_displace_still_arms(
        self, bound: None, monkeypatch: pytest.MonkeyPatch, legacy: dict[str, Any]
    ) -> None:
        reading = {"enabled": True, "monitor": None, "autonudge_loop": legacy}
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        assert _emitted(_start(), "monitor_start")

    @pytest.mark.parametrize("tool", ["monitor_start", "monitor_update"])
    def test_the_agents_own_finish_record_is_not_blamed_on_a_person(
        self, bound: None, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        """A tombstone slot keeps the agent's own stop; Play or Clear would harm it."""
        legacy = {
            "id": "a1",
            "active": False,
            "stopped_reason": _an.AUTONUDGE_STOP_REASON,
            "retained_stop": True,
        }
        reading = {"enabled": True, "monitor": None, "autonudge_loop": legacy}
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        args: dict[str, Any] = {"watch": "work-ledger"}
        if tool == "monitor_start":
            args["message"] = "m"
        out = getattr(control, tool)(tool, args)

        assert not _emitted(out, tool), out
        assert "own monitor_stop" in out and "by a person" not in out, out
        assert "Do NOT ask the user to press Play or Clear" in out, out

    def test_update_with_no_recorded_reason_is_left_to_the_boundary(
        self, bound: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The boundary's cycle-count fallback may still revive such a row."""
        legacy = {"id": "a1", "active": False, "stopped_reason": "", "retained_stop": True}
        reading = {"enabled": True, "monitor": None, "autonudge_loop": legacy}
        monkeypatch.setattr(mcp_core, "_get", lambda *a, **k: reading)

        out = control.monitor_update("monitor_update", {"max_cycles": 50})

        assert _emitted(out, "monitor_update"), out


class TestTheBoundaryNoLongerOffersADeadEnd:
    @pytest.mark.asyncio
    async def test_update_on_a_persons_pause_does_not_send_the_agent_to_monitor_start(
        self, svc: AutoNudgeService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = await _arm(svc)
        await svc.update(loop.id, active=False)
        monkeypatch.setattr(_an, "get_instance", lambda: svc)

        with pytest.raises(Exception) as denied:
            await apply_monitor._monitor_update(
                None, SESSION, {"patch": {"message": "new"}}, self_arm_ok=True
            )

        text = str(denied.value)
        assert "paused manually by a person" in text, text
        assert "monitor_start is refused" in text, text

    @pytest.mark.asyncio
    async def test_update_on_the_agents_own_finish_record_names_its_own_stop(
        self, svc: AutoNudgeService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop = await _arm(svc)
        await svc.update(loop.id, active=False, stopped_reason=_an.AUTONUDGE_STOP_REASON)
        monkeypatch.setattr(_an, "get_instance", lambda: svc)

        with pytest.raises(Exception) as denied:
            await apply_monitor._monitor_update(
                None, SESSION, {"patch": {"message": "new"}}, self_arm_ok=True
            )

        text = str(denied.value)
        assert "own monitor_stop ended it" in text, text
        assert "by a person" not in text and "Play" not in text, text
