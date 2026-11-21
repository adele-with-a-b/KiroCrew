"""A session may ``session_send`` to the one session that created it, and nothing more.

Driven against real slots and the real gate (:func:`authorize_target`) and the real
delivery (:func:`send_to_target`). The creator is read off the CALLING slot's own
``_created_by``; every other verb and every other target keeps the creator fence.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- autouse fixtures apply by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
    _steerable,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_managers
from kiro_crew.dashboard.session_control_parts import target_authorization as ta
from kiro_crew.dashboard.session_control_parts.session_messaging import BROADCAST_VIA
from kiro_crew.dashboard.workflow_session_port import WORKFLOW_SEND_VIA

#: Every verb but ``send`` that goes through the gate, spelled out so a new one is a
#: decision here rather than an inherited admission.
OTHER_VERBS = (
    "read",
    "summary",
    "queue",
    "stop",
    "close",
    "adopt",
    "release",
    "handoff",
    "fork",
    "rename",
    "set_model",
    "set_color",
    "set_note",
    "end_wait",
    "reload",
    "status",
    "retry",
    "continue",
    "generate_title",
)


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: False)
    asyncio.run(sc.refresh_manager_chain())
    session_managers.reset_cache()
    yield
    session_managers.reset_cache()


def _team(state):
    """The person's tab, a lead it started, a conductor the lead opened, and two
    workers the conductor opened."""
    owner = _slot(state, "chat-owner")
    lead = _slot(state, "chat-lead")
    conductor = _slot(state, "chat-conductor")
    worker = _slot(state, "chat-worker")
    sibling = _slot(state, "chat-sibling")
    lead._created_by = owner.key
    conductor._created_by = lead.key
    worker._created_by = conductor.key
    sibling._created_by = conductor.key
    return owner, lead, conductor, worker, sibling


def _gate(state, caller, target: str, operation: str = "send", **kwargs):
    return sc.authorize_target(
        state, caller_session_key=_key(caller), target=target, operation=operation, **kwargs
    )


def _refusal(
    state, caller, target: str, operation: str = "send", **kwargs
) -> sc.SessionControlError:
    with pytest.raises(sc.SessionControlError) as info:
        _gate(state, caller, target, operation, **kwargs)
    return info.value


def _send(state, caller, target: str, **kwargs):
    return asyncio.run(
        sc.send_to_target(
            state, caller_session_key=_key(caller), target=target, message="done: PR 12", **kwargs
        )
    )


def _relay_senders(slot) -> list[str]:
    return [
        ((entry.get("meta") or {}).get("relay") or {}).get("senderKey", "")
        for entry in slot._queue
        if "done: PR 12" in entry.get("content", "")
    ]


# ── the one admission ───────────────────────────────────────────────────────


def test_a_worker_sends_to_its_creator_by_key_and_lands_tagged(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)

    out = _send(state, worker, conductor.key)

    # A busy creator queues it, exactly as any session_send to a busy target does.
    assert out["started"] is False
    assert out["steered"] is False
    assert _relay_senders(conductor) == [worker.key]
    entry = next(e for e in conductor._queue if "done: PR 12" in e.get("content", ""))
    assert entry["content"].startswith(f"[sent by session {worker.key} via session_send]")


def test_a_worker_sends_to_its_creator_by_exact_title(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    conductor.title = "Fork Conductor"
    _busy(conductor)

    assert _gate(state, worker, "Fork Conductor", creator_message=True) is conductor
    _send(state, worker, "Fork Conductor")

    assert _relay_senders(conductor) == [worker.key]


# ── every other verb on the creator ─────────────────────────────────────────


@pytest.mark.parametrize("operation", OTHER_VERBS)
def test_every_other_verb_on_the_creator_is_refused(tmp_path, operation):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)

    exc = _refusal(state, worker, conductor.key, operation)

    assert exc.code == "not_creator"
    assert exc.message == ta.CREATOR_MESSAGE_ONLY


def test_the_admission_lifts_only_for_a_send_that_asks_for_it(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)

    # A send that does not ask for it keeps the fence, so no other caller of the
    # gate inherits the reach by spelling its operation "send".
    assert _refusal(state, worker, conductor.key).code == "not_creator"
    # And the flag lifts nothing for any other verb.
    for operation in OTHER_VERBS:
        exc = _refusal(state, worker, conductor.key, operation, creator_message=True)
        assert exc.code == "not_creator", operation


def test_a_real_stop_on_the_creator_is_refused(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)

    with pytest.raises(sc.SessionControlError) as info:
        asyncio.run(sc.stop_target(state, caller_session_key=_key(worker), target=conductor.key))

    assert info.value.code == "not_creator"
    assert conductor.running


def test_a_steer_to_the_creator_is_refused_and_never_injected(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)
    conductor._acp_client = _steerable(accepted=True)

    with pytest.raises(sc.SessionControlError) as info:
        _send(state, worker, conductor.key, steer=True)

    assert info.value.code == "not_creator"
    assert info.value.message == ta.CREATOR_MESSAGE_ONLY
    conductor._acp_client.steer.assert_not_awaited()
    assert not conductor._queue


@pytest.mark.parametrize("via", [BROADCAST_VIA, WORKFLOW_SEND_VIA, "session_create"])
def test_a_broadcast_a_workflow_send_or_a_seed_does_not_reach_the_creator(tmp_path, via):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)

    with pytest.raises(sc.SessionControlError) as info:
        _send(state, worker, conductor.key, via=via)

    assert info.value.code == "not_creator"
    assert not conductor._queue


# ── every other target ──────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["sibling", "owner", "lead"])
def test_a_worker_still_cannot_send_to_anyone_else(tmp_path, who):
    state = _make_state(tmp_path)
    owner, lead, _, worker, sibling = _team(state)
    target = {"sibling": sibling, "owner": owner, "lead": lead}[who]
    _busy(target)

    with pytest.raises(sc.SessionControlError) as info:
        _send(state, worker, target.key)

    assert info.value.code == "not_creator"
    assert info.value.message != ta.CREATOR_MESSAGE_ONLY
    assert not target._queue


def test_the_creator_is_the_calling_slots_own_never_one_it_names(tmp_path):
    state = _make_state(tmp_path)
    _, lead, conductor, worker, _ = _team(state)
    other = _slot(state, "chat-other-conductor")
    other._created_by = lead.key
    stranger = _slot(state, "chat-stranger")
    stranger._created_by = other.key

    # The stranger's own slot names `other`; naming `conductor`, whose worker it is
    # not, reaches nothing.
    assert _refusal(state, stranger, conductor.key, creator_message=True).code == "not_creator"
    assert _gate(state, stranger, other.key, creator_message=True) is other
    assert _gate(state, worker, conductor.key, creator_message=True) is conductor


def test_an_unverified_creator_reaches_nothing(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    worker._created_by = sc.UNVERIFIED_CREATOR

    assert _refusal(state, worker, conductor.key, creator_message=True).code == "not_creator"


def test_a_fenced_caller_with_a_blank_creator_reaches_nothing(tmp_path):
    state = _make_state(tmp_path)
    owner, _, conductor, _, _ = _team(state)
    bot_chat = _slot(state, "chat-bot")
    assert bot_chat._created_by == ""

    # Fenced by a verified member admission, the HTTP gate's carried verdict.
    for target in (owner, conductor):
        exc = _refusal(
            state, bot_chat, target.key, creator_message=True, precomputed_ownership_fenced=True
        )
        assert exc.code == "not_creator"


def test_a_handoff_does_not_change_who_the_creator_is(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    new_manager = _slot(state, "chat-new-manager")
    new_manager._created_by = conductor.key
    # The conductor hands its worker to another session it manages.
    result = asyncio.run(
        sc.handoff_target(
            state, caller_session_key=_key(conductor), target=worker.key, to=new_manager.key
        )
    )

    assert result["manager"] == new_manager.key
    assert session_managers.holder(worker) == new_manager.key
    assert worker._created_by == conductor.key
    assert _gate(state, worker, conductor.key, creator_message=True) is conductor
    assert _refusal(state, worker, new_manager.key, creator_message=True).code == "not_creator"
    assert _refusal(state, worker, conductor.key, "stop").code == "not_creator"


# ── the nearby callers keep their reach ─────────────────────────────────────


def test_a_scheduled_runs_child_still_cannot_reach_the_scheduled_run(tmp_path):
    state = _make_state(tmp_path)
    cron_tab = _slot(state, "cron-nightly")
    child = _slot(state, "chat-cron-child")
    child._created_by = cron_tab.key

    assert _refusal(state, child, cron_tab.key, creator_message=True).code == "unattended_target"


def test_a_child_of_a_channel_session_still_cannot_reach_it(tmp_path):
    state = _make_state(tmp_path)
    dm = _slot(state, "chat-owner-dm")
    dm.linked_session_key = "slack:T1:D1"
    child = _slot(state, "chat-dm-child")
    child._created_by = dm.key

    exc = _refusal(state, child, dm.key, creator_message=True)
    assert exc.code == "linked_session_target"


def test_an_app_scoped_child_still_cannot_reach_its_creator(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    worker._app = "work-items"

    assert _refusal(state, worker, conductor.key, creator_message=True).code == "app_scoped_caller"


def test_a_child_in_another_workspace_still_cannot_reach_its_creator(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    worker.workspace = "elsewhere"

    assert _refusal(state, worker, conductor.key, creator_message=True).code == "workspace_mismatch"
