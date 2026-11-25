"""``work_ledger.remote_dispatch_granted_turns``: a granted conductor's every turn.

Rule 8 of the crew caller check admits a turn the owner sent, and with the patrol
setting a wake of the shipped patrol. This setting adds the conductor whose
ACTIVE loop is that shipped patrol and whose trust record says the owner
dispatched under it: any turn of that chat may use ``crew=``, so a note from
another conductor is acted on in the turn it opens instead of at the next
check-in. Everything rules 1-7 refuse stays refused.
"""

from __future__ import annotations

import dataclasses

import pytest
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
)
from test_remote_dispatch_patrol_wake import _patrol_loop, record_home  # noqa: F401
from test_session_control_crew import _REAL_OWNER_CHECK, _call, _create, env  # noqa: F401

from kiro_crew import autonudge_selfarm, conductor_patrol
from kiro_crew.config.section_builders import _build_work_ledger_config
from kiro_crew.dashboard import remote_dispatch_headroom as headroom
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew


def test_the_setting_defaults_off_and_reads_only_a_real_bool():
    build = _build_work_ledger_config
    assert build({}).remote_dispatch_granted_turns is False
    assert build({"remote_dispatch_granted_turns": "true"}).remote_dispatch_granted_turns is False
    assert build({"remote_dispatch_granted_turns": True}).remote_dispatch_granted_turns is True


def _settings(monkeypatch, *, enabled: bool = True, granted_turns: bool) -> None:
    value = dataclasses.replace(headroom._DISABLED, enabled=enabled, granted_turns=granted_turns)
    monkeypatch.setattr(headroom, "remote_dispatch_settings", lambda: value)


@pytest.fixture
def granted(env, record_home, monkeypatch):  # noqa: F811 -- the imported fixtures
    """The crew harness on a turn nobody sent, under a recorded shipped patrol."""
    monkeypatch.setattr(crew, "owner_unverifiable_reason", _REAL_OWNER_CHECK)
    env.caller._turn_owner_sent = False
    env.caller._turn_patrol_wake = False
    env.caller._turn_channel_origin = False
    loop = _patrol_loop()
    loop.active = True
    autonudge_selfarm.record_owner_patrol(loop.id, loop.slot_key)
    env.loop = loop
    monkeypatch.setattr(
        conductor_patrol, "_loop_on", lambda state, key: (object(), env.loop, loop.slot_key)
    )
    audits: list[tuple[str, str]] = []
    monkeypatch.setattr(crew, "_audit_granted_turn", lambda key, verb: audits.append((key, verb)))
    env.audits = audits
    return env


def _refused(env) -> None:  # noqa: F811 -- shadows the imported fixture name
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (403, crew.OWNER_UNVERIFIABLE)
    assert env.peer.calls == [] and env.audits == []


def test_a_granted_conductor_is_admitted_on_a_turn_nobody_sent_and_audited(granted, monkeypatch):
    _settings(monkeypatch, granted_turns=True)
    assert _create(granted) == "chat-77"
    assert [verb for _key, verb in granted.audits] == ["create"]


def test_the_grant_is_refused_with_the_setting_off(granted, monkeypatch):
    _settings(monkeypatch, granted_turns=False)
    _refused(granted)


def test_the_setting_needs_the_preview_switch(granted, monkeypatch):
    _settings(monkeypatch, enabled=False, granted_turns=True)
    _refused(granted)


@pytest.mark.parametrize(
    "shape", ["no_loop", "stopped", "message", "banner", "gate", "not_default", "other_slot"]
)
def test_only_the_active_shipped_patrol_the_owner_dispatched_under_carries_the_grant(
    granted, monkeypatch, shape: str
):
    _settings(monkeypatch, granted_turns=True)
    loop = granted.loop
    if shape == "no_loop":
        granted.loop = None
    elif shape == "stopped":
        loop.active = False
    elif shape == "message":
        loop.message = "Conductor patrol, reworded."
    elif shape == "banner":
        loop.banner = "patrol"
    elif shape == "gate":
        loop.gate = False
    elif shape == "not_default":
        loop.default_patrol = False
    else:
        loop.slot_key = "chat-someone-else"
    _refused(granted)


def test_a_patrol_the_owner_never_dispatched_under_carries_no_grant(granted, monkeypatch):
    _settings(monkeypatch, granted_turns=True)
    granted.loop.id = "never-recorded"
    _refused(granted)


def test_a_turn_a_messaging_channel_opened_is_never_admitted(granted, monkeypatch):
    _settings(monkeypatch, granted_turns=True)
    granted.caller._turn_channel_origin = True
    status, body = _call(granted, "create", crew="lab")
    assert status == 403, body
    assert granted.peer.calls == [] and granted.audits == []


def test_an_unreadable_loop_is_no_grant(granted, monkeypatch):
    _settings(monkeypatch, granted_turns=True)

    def boom(state, key):
        raise OSError("store unreadable")

    monkeypatch.setattr(conductor_patrol, "_loop_on", boom)
    _refused(granted)


@pytest.mark.parametrize("shape", ["worker", "not_conductor_mode"])
def test_the_grant_never_admits_a_worker_or_a_plain_chat(granted, monkeypatch, shape: str):
    """Rules 1-7 run first: the grant alone reaches no crew."""
    _settings(monkeypatch, granted_turns=True)
    if shape == "worker":
        granted.caller._created_by = "dashboard:chat-9"
    else:
        granted.caller.agent = ""
        granted.caller.agent_kind = ""
    status, body = _call(granted, "create", crew="lab")
    assert status == 403, body
    assert granted.peer.calls == [] and granted.audits == []
