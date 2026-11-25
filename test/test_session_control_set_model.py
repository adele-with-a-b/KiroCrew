"""``session_set_model``: a pending model pick on an idle session.

The verb records the pick; ``apply_pending_model_pick`` commits it at the start
of the target's next turn after re-running the target gate in the same
synchronous step. The tests cover the gate at both points, the refusals that
change nothing, and the MCP tool's rendering.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import restore_open_slots, save_slot_off_loop
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.testing.synthetic_credentials import github_classic


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _key(slot) -> str:
    return slot_history_key(slot)


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _set_model(state, caller, target: str, model: str) -> dict:
    return asyncio.run(
        sc.set_model_target(state, caller_session_key=_key(caller), target=target, model=model)
    )


def test_an_idle_target_gets_a_pending_pick_and_keeps_its_model(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model

    out = _set_model(state, caller, "chat-2", "sonnet")

    assert out == {"ok": True, "target": "chat-2", "model": out["model"], "pending": True}
    assert out["model"]
    assert target.model == before, "nothing changes until the next turn starts"
    assert target._pending_model_pick.model == out["model"]


def test_the_next_turn_commits_the_pick(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.jev_route = True
    gen = target._model_pick_gen
    out = _set_model(state, caller, "chat-2", "sonnet")

    changed = sc.apply_pending_model_pick(state, target)

    assert changed is True
    assert target.model == out["model"]
    assert target.jev_route is False
    assert target._model_pick_gen == gen + 1
    assert target._pending_model_pick is None
    assert sc.apply_pending_model_pick(state, target) is False, "a pick applies once"


def test_a_later_pick_replaces_an_unapplied_one(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _set_model(state, caller, "chat-2", "sonnet")
    second = _set_model(state, caller, "chat-2", "opus")

    sc.apply_pending_model_pick(state, target)

    assert target.model == second["model"]


def test_a_pick_is_dropped_if_the_target_became_channel_linked(tmp_path):
    """The turn-start commit re-runs the gate. Mutation guard: committing the
    stored pick without it lets the change reach a channel-backed session."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    target.linked_session_key = "slack:1786300000.000100"

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before
    assert target._pending_model_pick is None


def test_a_pick_is_dropped_if_the_target_became_mirrored(tmp_path):
    """An outbound mirror added while the pick waits makes the target unreachable."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    state.sessions.set_mirror_link(_key(target), "C0FFEE", "1786300000.000200")

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before
    assert target._pending_model_pick is None


def test_a_pick_is_dropped_if_the_caller_slot_was_reused(tmp_path):
    """A closed caller's key handed to a new occupant does not inherit the pick.
    Mutation guard: authorizing on the key alone would commit it."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    assert target._pending_model_pick.caller_tab_id == caller._tab_id
    # A new occupant under the same key carries a different tab identity.
    caller._tab_id = "replacement-tab"

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before


def test_a_pick_is_dropped_if_the_caller_is_gone(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    state._slots.pop("chat-1")

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before


def test_a_newer_picker_choice_wins_over_the_pending_pick(tmp_path):
    """Mutation guard: without the pick-generation check the stale pick would
    overwrite a model the user chose after it was queued."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _set_model(state, caller, "chat-2", "sonnet")
    # The model picker's explicit pick: new model, bumped generation.
    target.model = "opus"
    target._model_pick_gen += 1

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == "opus"


def test_a_pick_is_dropped_if_session_control_was_disabled(tmp_path, monkeypatch):
    """An operator who turns session control off after a pick was queued stops
    it: the turn-start gate re-runs the enabled check."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before
    assert target._pending_model_pick is None


def test_a_turn_during_a_picker_switch_keeps_the_pick_pending(tmp_path):
    """The picker's generation bump is provisional while it holds
    _model_pick_lock; a turn starting then must not drop the pick on a
    generation that may still roll back. It applies on the next turn."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    out = _set_model(state, caller, "chat-2", "sonnet")
    gen = target._model_pick_gen

    async def _turn_during_switch() -> bool:
        async with target._model_pick_lock:
            target._model_pick_gen += 1  # provisional picker bump
            applied = sc.apply_pending_model_pick(state, target)
            target._model_pick_gen = gen  # the picker's rollback
            return applied

    assert asyncio.run(_turn_during_switch()) is False
    assert target.model == before
    assert target._pending_model_pick is not None, "the pick survives the in-flight switch"
    assert sc.apply_pending_model_pick(state, target) is True
    assert target.model == out["model"]


def test_the_model_check_uses_the_targets_backend(tmp_path, monkeypatch):
    """A Claude-backend target takes canonical keys as wire ids, so the check is
    judged on the TARGET's backend, not the global ACP provider."""
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_handlers

    seen: list[str | None] = []

    def _reason(_model, backend=None):
        seen.append(backend)
        return None if backend == "claude-backend" else "rejected"

    monkeypatch.setattr(chat_handlers, "_model_rejected_reason", _reason)
    monkeypatch.setattr(sc, "is_claude_code", lambda p: p == "claude-seam")
    monkeypatch.setattr(sc, "select_provider_backend", lambda *_a, **_k: "claude-backend")
    monkeypatch.setattr(
        sc,
        "capabilities_for",
        lambda _b: SimpleNamespace(model_id_namespace="claude_code", provider_seam="claude-seam"),
    )
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")

    out = _set_model(state, caller, "chat-2", "fable-5-1m")

    assert seen == ["claude-backend"]
    assert out["model"] == "fable-5-1m"


def test_a_caller_fenced_after_queuing_is_rechecked(tmp_path, monkeypatch):
    """A False fence verdict stored at call time is re-derived at turn start."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    assert target._pending_model_pick.caller_fenced is False
    # Now fenced: the caller may reach only sessions it created, and it did not
    # create chat-2.
    monkeypatch.setattr(sc, "_caller_is_ownership_fenced", lambda _state, _key: True)

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before


@pytest.mark.parametrize(
    "model",
    [
        "auto",
        "Auto",
        " auto ",
        "Auto (decision model)",
        "AUTO (DECISION MODEL)",
        "Auto (Jev)",
        "auto (jev)",
        "AUTO:JEV",
        "auto:jev",
    ],
)
def test_auto_is_owner_only(tmp_path, model):
    """With the Jev preview on, a slot on ``auto`` routes each turn through Jev;
    the picker's display label and any case of the sentinel are refused too."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", model)

    assert exc.value.code == "model_owner_only"
    assert target._pending_model_pick is None


def test_an_empty_model_is_refused(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "  ")

    assert exc.value.code == "model_rejected"


def test_an_alias_is_folded_to_the_kiro_cli_id(tmp_path, monkeypatch):
    """``sonnet`` is not a kiro-cli id; stored as-is it would be withheld at
    session start and the target would stay on the default."""
    monkeypatch.setattr(sc.model_registry, "acp_id_correction", lambda m: f"wire-{m}")
    monkeypatch.setattr(sc, "is_claude_code", lambda _provider: False)
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    out = _set_model(state, caller, "chat-2", "sonnet")

    assert out["model"] == "wire-sonnet"
    assert target._pending_model_pick.model == "wire-sonnet"


def test_an_alias_is_left_alone_on_a_non_kiro_backend(tmp_path, monkeypatch):
    """Another backend's ids live in their own namespace; folding them to a
    kiro-cli id would name a model that harness does not serve."""
    from types import SimpleNamespace

    monkeypatch.setattr(sc.model_registry, "acp_id_correction", lambda m: f"wire-{m}")
    monkeypatch.setattr(sc, "is_claude_code", lambda _provider: False)
    monkeypatch.setattr(
        sc,
        "capabilities_for",
        lambda _backend: SimpleNamespace(model_id_namespace="claude_code", provider_seam="acp"),
    )
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")

    out = _set_model(state, caller, "chat-2", "claude-opus-4-8")

    assert out["model"] == "claude-opus-4-8"


def test_the_alias_correction_follows_the_targets_own_backend(tmp_path, monkeypatch):
    """A member DM routes to agent.member_acp_backend, which can be kiro-cli
    while the configured default is not. The correction must key on the
    backend the target will actually run, not the global default."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_utils import effective_session_key

    monkeypatch.setattr(sc.model_registry, "acp_id_correction", lambda m: f"wire-{m}")
    monkeypatch.setattr(sc, "is_claude_code", lambda _provider: False)
    monkeypatch.setattr(
        sc,
        "capabilities_for",
        lambda backend: SimpleNamespace(
            model_id_namespace=sc.MODEL_NAMESPACE_ACP if backend == "member-kiro" else "other",
            provider_seam="acp",
        ),
    )
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target_key = effective_session_key(target)
    seen: list[str | None] = []

    def _select(session_key, _member_backend, _default, **_kw):
        seen.append(session_key)
        return "member-kiro" if session_key == target_key else "default-other"

    monkeypatch.setattr(sc, "select_provider_backend", _select)

    out = _set_model(state, caller, "chat-2", "sonnet")

    assert seen == [target_key]
    assert out["model"] == "wire-sonnet"


def test_a_padded_model_is_stored_stripped(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.model_registry, "acp_id_correction", lambda _m: None)
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    out = _set_model(state, caller, "chat-2", "  some-model ")

    assert out["model"] == "some-model"
    assert target._pending_model_pick.model == "some-model"


def test_a_pick_equal_to_the_pin_still_resets_while_a_fallback_serves(tmp_path):
    """With a throttle fallback on the wire, an unchanged pin still needs the
    reset, the same way the model picker treats it."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    out = _set_model(state, caller, "chat-2", "sonnet")
    target.model = out["model"]
    target._active_fallback_model = "some-fallback"

    assert sc.apply_pending_model_pick(state, target) is True


def _live_provider(
    served: str, *, backend: str = "", refused: str = "", substituted: str | None = None
):
    """A live ACP session reporting *served*, judged by the provider's real check."""
    import functools
    from types import SimpleNamespace

    from kiro_crew.acp.types import ACP_BACKEND_KIRO
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.providers.acp import AcpProvider

    provider = MagicMock(spec=AcpProvider)
    provider.capabilities = capabilities_for(backend or ACP_BACKEND_KIRO)
    provider.available_models.return_value = []
    provider.has_active_turn.return_value = False
    provider.served_model = served
    provider.model_pin_refused = refused
    provider._client = SimpleNamespace(_last_substitution_model=substituted)
    for name in ("recorded_model_spellings", "serves_another_model"):
        setattr(provider, name, functools.partial(getattr(AcpProvider, name), provider))
    return provider


def _claude_spellings(pick: str) -> tuple[str, str, str]:
    """The three spellings a claude switch to *pick* can record on a cold cache:
    the registry wire id, prefix-stripped, then prefix- and window-stripped."""
    from kiro_crew import model_registry
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
    from kiro_crew.dashboard.chat_handlers import _wire_model_id

    wire = _wire_model_id(_live_provider("", backend=ACP_BACKEND_CLAUDE), pick)
    stripped = model_registry.strip_provider_id_prefix(wire)
    return wire, stripped, stripped.replace("[1m]", "")


def test_a_pick_equal_to_the_pin_resets_a_session_serving_another_model(tmp_path):
    """After a restart the pin can already name the pick while the resumed
    session runs its old model. The pick must still move the session: the
    caller was told it switches. Mutation guard: comparing pins alone commits
    the pick, arms no reset, and the turn runs the old model."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    out = _set_model(state, caller, "chat-2", "sonnet")
    target.model = out["model"]
    state.sessions.get_provider = MagicMock(return_value=_live_provider("old-served-model"))

    assert sc.apply_pending_model_pick(state, target) is True
    assert target.model == out["model"]


def test_a_switch_holds_across_a_restart_and_a_second_switch_lands(tmp_path, monkeypatch):
    """The reported sequence end to end at the dashboard layer: a pick lands and
    is saved as the turn saves it; after a restart the pin and the caller's tab
    identity come back, but the session it resumes reports the old model; the
    caller picks the same model again. That pick must reset the session.
    Mutation guard: comparing pins alone answers False and the turn runs old."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    state = _make_state(sessions)
    caller = state.get_or_create_slot("chat-1-1")
    target = state.get_or_create_slot("chat-2-1")
    for slot in (caller, target):
        state.conversation_log.append(_key(slot), "user", "hi")
    target.model = "old-model"
    asyncio.run(save_slot_off_loop(state, caller, force=True))
    picked = _set_model(state, caller, "chat-2-1", "sonnet")["model"]
    assert sc.apply_pending_model_pick(state, target) is True
    assert asyncio.run(
        save_slot_off_loop(state, target, force=True, expected_history_key=_key(target))
    )
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-1", "chat-2-1"], "ts": 0.0})
    )

    restarted = _make_state(sessions)
    assert restore_open_slots(restarted) == 2
    caller2, target2 = restarted._slots["chat-1-1"], restarted._slots["chat-2-1"]
    assert target2.model == picked, "the committed pin survives the restart"
    assert caller2._tab_id == caller._tab_id, "the caller keeps its tab identity"
    restarted.sessions.get_provider = MagicMock(return_value=_live_provider("old-served-model"))

    _set_model(restarted, caller2, "chat-2-1", "sonnet")
    assert sc.apply_pending_model_pick(restarted, target2) is True
    assert target2._model_pick_dropped == {}


def test_a_pick_equal_to_the_pin_and_the_served_model_resets_nothing(tmp_path):
    """The other side of the check: a session already on the pick, or one that
    reported no model, is not torn down for an equal pin."""
    from kiro_crew import model_registry

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    out = _set_model(state, caller, "chat-2", "sonnet")
    target.model = out["model"]
    served = model_registry.to_acp_id(out["model"])
    state.sessions.get_provider = MagicMock(return_value=_live_provider(served))
    assert sc.apply_pending_model_pick(state, target) is False

    _set_model(state, caller, "chat-2", "sonnet")
    state.sessions.get_provider = MagicMock(return_value=_live_provider(""))
    assert sc.apply_pending_model_pick(state, target) is False


@pytest.mark.parametrize("rung", [0, 1, 2], ids=["wire", "bare-1m", "bare"])
def test_an_equal_pick_keeps_a_claude_session_on_any_spelling_of_it(tmp_path, rung):
    """A claude switch records the spelling the adapter took: the wire id, or a
    rung of the config-option ladder. A session on any of them runs the pick,
    and a reset would only restart it. Mutation guard: a strict compare with
    the wire id resets the bare spellings."""
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    out = _set_model(state, caller, "chat-2", "sonnet")
    target.model = out["model"]
    served = _claude_spellings(out["model"])[rung]
    assert served
    provider = _live_provider(served, backend=ACP_BACKEND_CLAUDE)
    state.sessions.get_provider = MagicMock(return_value=provider)

    assert sc.apply_pending_model_pick(state, target) is False


@pytest.mark.parametrize("listed", ["bare-1m", "alias"])
def test_an_equal_pick_keeps_a_claude_session_on_its_advertised_fold(tmp_path, listed):
    """With a warm advertised list a claude switch first folds the id onto the
    listed spelling (a bare ``[1m]`` id, or the version-less family alias), and
    that is what the session reports. Mutation guard: a set without the fold
    resets it."""
    from kiro_crew import model_registry
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    out = _set_model(state, caller, "chat-2", "sonnet")
    target.model = out["model"]
    wire, stripped, _bare = _claude_spellings(out["model"])
    namespace = capabilities_for(ACP_BACKEND_CLAUDE).model_id_namespace
    model_registry.refresh_advertised_models(
        namespace, ["default", stripped if listed == "bare-1m" else "sonnet"]
    )
    folded = model_registry.resolve_wire_model_id(wire, namespace)
    assert folded != wire
    provider = _live_provider(folded, backend=ACP_BACKEND_CLAUDE)
    state.sessions.get_provider = MagicMock(return_value=provider)

    assert sc.apply_pending_model_pick(state, target) is False


def test_an_equal_pick_does_not_reset_when_a_reset_cannot_help(tmp_path):
    """A pin the backend refused or withheld leaves the session on its default,
    and a policy substitution serves another model on purpose: a new session
    gets the same answer, so neither is reset. A truly different model is."""
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    other = _claude_spellings(_set_model(state, caller, "chat-2", "opus")["model"])[0]
    pick = _set_model(state, caller, "chat-2", "sonnet")["model"]
    wire = _claude_spellings(pick)[0]
    target.model = pick
    cases = [
        (_live_provider(other, backend=ACP_BACKEND_CLAUDE, refused=wire), False),
        (_live_provider(other, backend=ACP_BACKEND_CLAUDE, substituted=other), False),
        (_live_provider(other, backend=ACP_BACKEND_CLAUDE), True),
    ]
    for provider, resets in cases:
        _set_model(state, caller, "chat-2", "sonnet")
        state.sessions.get_provider = MagicMock(return_value=provider)
        assert sc.apply_pending_model_pick(state, target) is resets, provider.model_pin_refused


def test_the_turn_arms_the_reset_for_a_live_or_spawning_session_only():
    """Source pin: a committed pick arms the reset through the shared helper,
    whose behaviour is pinned below."""
    import kiro_crew.dashboard.chat_runner as runner

    source = "\n".join(
        turn_file.read_text(encoding="utf-8")
        for turn_file in [
            *sorted(Path(runner.__file__).with_name("chat_turn").glob("*.py")),
            Path(runner.__file__),
        ]
        if turn_file.name != "__init__.py"
    )
    body = source[source.index("if apply_pending_model_pick(state, slot):") :]
    body = body[: body.index("await _consume_pending_reset(state, slot)")]
    assert "arm_model_switch_reset(state, slot)" in body


def test_the_reset_is_armed_for_a_live_or_spawning_session_only(tmp_path):
    """A registered session or an eager spawn in flight can still be on the old
    model, so the reset is armed; with neither, the next turn cold-starts on the
    new model and an armed reset would tear that session down for nothing."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    key = sc.effective_session_key(slot)

    sc.arm_model_switch_reset(state, slot)
    assert slot._pending_reset_history_key is None

    spawning = MagicMock()
    spawning.done.return_value = False
    slot._eager_spawn_task = spawning
    sc.arm_model_switch_reset(state, slot)
    assert slot._pending_reset_history_key == key

    slot._pending_reset_history_key = None
    slot._eager_spawn_task = None
    state.sessions.get_provider = MagicMock(return_value=object())
    sc.arm_model_switch_reset(state, slot)
    assert slot._pending_reset_history_key == key
    state.sessions.get_provider.assert_called_with(key)


def test_a_switch_waits_for_a_picker_transaction_in_flight(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    assert sc.model_switch_deferred(state, slot) == ""

    async def held() -> str:
        async with slot._model_pick_lock:
            return sc.model_switch_deferred(state, slot)

    assert asyncio.run(held()) == "picker in flight"


def test_the_turn_persists_a_committed_pick_before_it_acquires_a_session():
    """Source pin: the memory-only commit is saved off the loop before the
    provider is acquired, so a gateway exit mid-turn keeps the new pin, and a
    refused save leaves the slot dirty for the periodic flush."""
    import kiro_crew.dashboard.chat_runner as runner

    source = "\n".join(
        turn_file.read_text(encoding="utf-8")
        for turn_file in [
            *sorted(Path(runner.__file__).with_name("chat_turn").glob("*.py")),
            Path(runner.__file__),
        ]
        if turn_file.name != "__init__.py"
    )
    body = source[source.index("if apply_pending_model_pick(state, slot):") :]
    body = body[: body.index("await _consume_pending_reset(state, slot)")]
    assert "await save_slot_off_loop(" in body
    assert "expected_history_key=_pick_history_key" in body
    assert "slot._dirty = True" in body


def test_the_turn_start_commit_cannot_suspend():
    """The gate and the write must run with no await between them."""
    assert not inspect.iscoroutinefunction(sc.apply_pending_model_pick)


def test_the_turn_applies_the_pick_before_it_acquires_a_session():
    """Source-order pin: the pick is applied in the turn's admission phase
    (``_turn_admit``, which ``_run_chat`` awaits) before the pending reset is
    consumed and before get_or_create reads ``slot.model``."""
    import kiro_crew.dashboard.chat_runner as runner

    source = "\n".join(
        turn_file.read_text(encoding="utf-8")
        for turn_file in [
            *sorted(Path(runner.__file__).with_name("chat_turn").glob("*.py")),
            Path(runner.__file__),
        ]
        if turn_file.name != "__init__.py"
    )
    body = source[source.index("async def _turn_admit(") :]
    apply_at = body.index("apply_pending_model_pick(state, slot)")
    assert apply_at < body.index("await _consume_pending_reset(state, slot)")
    assert apply_at < body.index("_requested_model = slot.model")


def test_a_busy_target_is_refused_and_gets_no_pick(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "target_busy"
    assert exc.value.status == 409
    assert "session busy, model not changed" in exc.value.message
    assert target._pending_model_pick is None


def test_a_picker_switch_in_flight_is_refused_and_gets_no_pick(tmp_path):
    """The picker holds _model_pick_lock across a provisional generation bump
    that it may roll back, so a pick stored inside that window would carry a
    generation the rollback invalidates and be dropped silently."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    async def _run() -> None:
        async with target._model_pick_lock:
            await sc.set_model_target(
                state, caller_session_key=_key(caller), target="chat-2", model="sonnet"
            )

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(_run())

    assert exc.value.code == "target_busy"
    assert target._pending_model_pick is None


def test_a_target_with_attached_subagents_is_refused(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    async def _children(*_a, **_kw):
        return chat_handlers.web.json_response(
            {"error": "sub-agents are running", "code": "slot_subagents_running"}, status=409
        )

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _children)

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "target_busy"
    assert target._pending_model_pick is None


def test_a_subagent_refusal_names_the_attached_runs(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")

    async def _children(*_a, **_kw):
        return chat_handlers.web.json_response(
            {
                "error": "sub-agents are running",
                "code": "slot_subagents_running",
                "runs": [{"id": "r1", "state": "running"}],
                "queued": [],
                "queued_partial": False,
            },
            status=409,
        )

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _children)

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.message.endswith("Still running or queued: r1 (running).")


def test_a_turn_starting_during_the_idle_check_is_refused(tmp_path, monkeypatch):
    """The idle check re-runs after the sub-agent probe's await."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    async def _turn_starts_meanwhile(*_a, **_kw):
        _busy(target)
        return None

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _turn_starts_meanwhile)

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "target_busy"
    assert target._pending_model_pick is None


def test_a_caller_fenced_at_call_time_stays_fenced(tmp_path, monkeypatch):
    """A True verdict stored with the pick is kept even when the fence can no
    longer be derived at turn start."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    pick = target._pending_model_pick
    target._pending_model_pick = sc.PendingModelPick(
        model=pick.model,
        caller_session_key=pick.caller_session_key,
        caller_tab_id=pick.caller_tab_id,
        caller_fenced=True,
        pick_gen=pick.pick_gen,
    )
    monkeypatch.setattr(sc, "_caller_is_ownership_fenced", lambda _state, _key: False)

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before


def test_a_target_linked_during_the_idle_check_is_refused(tmp_path, monkeypatch):
    """The gate re-runs after the sub-agent probe's await, right before the pick
    is stored."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    async def _link_meanwhile(*_a, **_kw):
        target.linked_session_key = "slack:1786300000.000100"
        return None

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _link_meanwhile)

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "linked_session_target"
    assert target._pending_model_pick is None


def test_auto_jev_is_owner_only(tmp_path):
    from kiro_crew.dashboard.chat_handlers import JEV_ROUTE_MODEL

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", JEV_ROUTE_MODEL)

    assert exc.value.code == "model_owner_only"
    assert target._pending_model_pick is None


def test_an_out_of_bounds_target_is_refused(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-hidden", memory_mode="incognito")

    with pytest.raises(sc.SessionControlError):
        _set_model(state, caller, "chat-hidden", "sonnet")


def test_a_remote_crew_target_is_refused(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.executor = "remote"

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "relay_archive_read_only"
    assert target._pending_model_pick is None


def test_a_credential_shaped_model_is_refused_before_it_is_stored(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", github_classic(seed=107))

    assert exc.value.code == "model_rejected"
    assert target._pending_model_pick is None


def test_model_rejection_check_receives_resolved_provider(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")
    seen: list[str | None] = []

    def _capture_provider(_model_name, provider=None):
        seen.append(provider)
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._model_rejected_reason", _capture_provider
    )

    _set_model(state, caller, "chat-2", "sonnet")

    assert seen and seen[0] is not None


# ── Route ────────────────────────────────────────────────────────────────────


def _request(tmp_path, *, internal: bool, body: dict):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/set-model"
    request.method = "POST"
    request.headers = {"X-Session-Key": _key(caller)}
    request.query = {}
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )

    async def _json():
        return body

    request.json = _json
    return request


def test_route_without_the_secret_is_forbidden(tmp_path):
    req = _request(tmp_path, internal=False, body={"target": "chat-2", "model": "sonnet"})
    resp = asyncio.run(handlers_sc.api_session_control_set_model(req))
    assert resp.status == 403


def test_route_refuses_a_non_string_model(tmp_path):
    req = _request(tmp_path, internal=True, body={"target": "chat-2", "model": 5})
    resp = asyncio.run(handlers_sc.api_session_control_set_model(req))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_request"


def test_route_renders_busy_as_409(tmp_path, monkeypatch):
    req = _request(tmp_path, internal=True, body={"target": "chat-2", "model": "sonnet"})

    async def _busy_verb(*_a, **_kw):
        raise sc.SessionControlError("session busy", status=409, code="target_busy")

    monkeypatch.setattr(sc, "set_model_target", _busy_verb)
    resp = asyncio.run(handlers_sc.api_session_control_set_model(req))
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "target_busy"


# ── MCP tool ─────────────────────────────────────────────────────────────────

_VERIFIED = "dashboard:chat-verified"


def _tool(name: str, args: dict, route: str, reply: dict) -> tuple[str, list[DashboardRequest]]:
    """One frame of ``name`` as the verified caller, against one dashboard route."""
    dash = InMemoryDashboardClient({route: reply})
    out = TABLE.call(name, args, ToolContext(dash, Caller.strict(_VERIFIED)))
    return out, dash.requests


def test_tool_carries_the_verified_key_and_reports_the_model():
    out, (post,) = _tool(
        "session_set_model",
        {"target": "chat-2", "model": "sonnet"},
        "POST /api/session-control/set-model",
        {"ok": True, "target": "chat-2", "model": "sonnet", "pending": True},
    )
    assert post.path == "/api/session-control/set-model"
    assert post.session_key == _VERIFIED
    assert "`chat-2` will switch to `sonnet` when its next turn starts" in out


def test_a_read_shows_the_model_and_the_pending_pick(tmp_path):
    """A caller can see whether its pick is still pending or has taken."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.model = "old-model"
    out = _set_model(state, caller, "chat-2", "sonnet")

    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert read["model"] == "old-model"
    assert read["pending_model"] == out["model"]

    sc.apply_pending_model_pick(state, target)
    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert read["model"] == out["model"]
    assert "pending_model" not in read


def test_a_dropped_pick_is_visible_to_the_caller_and_logged(tmp_path, caplog):
    """The caller was told the target will switch, so a pick its turn start drops
    must say so where the caller looks. Mutation guard: an audit-only drop leaves
    the read showing the old model with no pick and no reason."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.model = "old-model"
    out = _set_model(state, caller, "chat-2", "sonnet")
    caller._tab_id = "replacement-tab"

    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.session_control"):
        assert sc.apply_pending_model_pick(state, target) is False

    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert read["model"] == "old-model"
    assert read["model_pick_dropped"] == {
        "model": {"value": out["model"], "code": "caller_replaced", "stage": "turn_start"}
    }
    assert any("dropped (turn_start): caller_replaced" in r.message for r in caplog.records)

    _set_model(state, caller, "chat-2", "sonnet")
    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert "model_pick_dropped" not in read, "a new pick clears the old drop"


def test_the_read_tool_renders_a_dropped_pick():
    out, _ = _tool(
        "session_read_message",
        {"target": "chat-2"},
        "GET /api/session-control/read",
        {
            "target": "chat-2",
            "title": "w",
            "messages": [],
            "total": 0,
            "next_since": 0,
            "model": "old-model",
            "model_pick_dropped": {
                "model": {"value": "sonnet", "code": "caller_replaced", "stage": "turn_start"},
                "reasoning_effort": {
                    "value": "high",
                    "code": "harness_changed",
                    "stage": "harness_change",
                },
            },
        },
    )
    assert "its last model pick sonnet was dropped at its turn start (caller_replaced)" in out
    assert "its last effort pick high was dropped when its harness changed (harness_changed)" in out


def test_each_half_of_a_dropped_pick_keeps_its_own_record(tmp_path):
    """Both halves overtaken: two records, the effort one named as an effort.
    Mutation guard: one record per slot keeps only the last half."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    out = asyncio.run(
        sc.set_model_target(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            model="sonnet",
            reasoning_effort="high",
        )
    )
    target._model_pick_gen += 1
    target._effort_pick_gen += 1

    assert sc.apply_pending_model_pick(state, target) is False
    assert target._model_pick_dropped == {
        "model": {
            "value": out["model"],
            "code": "superseded_by_newer_pick",
            "stage": "turn_start",
        },
        "reasoning_effort": {
            "value": "high",
            "code": "superseded_by_newer_effort",
            "stage": "turn_start",
        },
    }


def test_a_second_call_records_the_half_it_replaced(tmp_path):
    """A model-only call then an effort-only call: the second replaces the whole
    pick, so the model half is recorded as dropped, not lost without a word."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    first = _set_model(state, caller, "chat-2", "sonnet")
    asyncio.run(
        sc.set_model_target(
            state, caller_session_key=_key(caller), target="chat-2", reasoning_effort="high"
        )
    )

    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert read["pending_reasoning_effort"] == "high"
    assert "pending_model" not in read
    assert read["model_pick_dropped"] == {
        "model": {
            "value": first["model"],
            "code": "replaced_by_newer_call",
            "stage": "set_model",
        }
    }
    # The next model-only call drops the effort-only pick's effort the same way.
    _set_model(state, caller, "chat-2", "opus")
    assert target._model_pick_dropped == {
        "reasoning_effort": {
            "value": "high",
            "code": "replaced_by_newer_call",
            "stage": "set_model",
        }
    }
    # One that carries the same half only replaces it: nothing is lost.
    _set_model(state, caller, "chat-2", "sonnet")
    assert target._model_pick_dropped == {}


def test_a_read_reports_the_served_model_over_the_pin(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.model = "pinned-model"
    target.served_model = "fallback-model"

    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert read["model"] == "fallback-model"


def test_a_read_redacts_a_credential_shaped_model(tmp_path):
    """The owner's picker stores any string, so the read must not echo a secret."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    secret = github_classic(seed=108)
    target.model = secret

    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert secret not in read["model"]


def test_the_read_tool_renders_the_model_and_the_pending_pick():
    out, _ = _tool(
        "session_read_message",
        {"target": "chat-2"},
        "GET /api/session-control/read",
        {
            "target": "chat-2",
            "title": "w",
            "messages": [],
            "total": 0,
            "next_since": 0,
            "model": "old-model",
            "pending_model": "new-model",
        },
    )
    assert "model old-model" in out
    assert "pending model new-model for its next turn" in out


def test_tool_reports_a_busy_refusal_as_an_error():
    out, _ = _tool(
        "session_set_model",
        {"target": "chat-2", "model": "sonnet"},
        "POST /api/session-control/set-model",
        {"error": "session busy, model not changed"},
    )
    assert out.startswith("Error:")
    assert "session busy, model not changed" in out


def test_a_pick_waits_while_an_alias_slot_runs_the_same_session(tmp_path):
    """Two alias slots can drive one session. While the other one has a turn
    in flight, a commit would reset nothing this turn attaches to, so the
    pick stays pending and applies once the alias is idle."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    alias = state.get_or_create_slot("chat-3")
    before = target.model
    out = _set_model(state, caller, "chat-2", "sonnet")
    # The alias starts a turn on the target's session after the pick was queued.
    _busy(alias)
    alias._active_turn_session_key = effective_session_key(target)

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before
    assert target._pending_model_pick is not None

    alias.task = None
    assert sc.apply_pending_model_pick(state, target) is True
    assert target.model == out["model"]


def test_unknown_worker_harness_pin_refuses_without_echoing_it(tmp_path, monkeypatch, caplog):
    from kiro_crew import members
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    secret = "sk-test-not-a-real-secret-0000"
    cfg = KiroCrewConfig()
    cfg.agents["atlas"] = KiroCrewAgentConfig(
        member_id="atlas-0123456789ab", kiro_agent="atlas", acp_backend=secret
    )
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    monkeypatch.setattr(members, "_CREWMATE_WORKER_SLOTS", set())
    monkeypatch.setattr(members, "_CREWMATE_WORKER_MEMBERS", {})
    members.note_crewmate_worker_session(_key(target), "atlas-0123456789ab")
    with pytest.raises(sc.SessionControlError) as raised:
        _set_model(state, caller, target.key, "some-model")
    assert raised.value.status == 409
    assert raised.value.code == "crewmate_harness_unavailable"
    assert "an unknown harness" in raised.value.message
    assert secret not in str(raised.value) + caplog.text
    assert target._pending_model_pick is None
    assert cfg.agents["atlas"].acp_backend == secret
