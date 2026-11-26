"""Why a steer fell back to the queue: each fallback names its reason.

``steer_into_running_turn`` answers :data:`STEER_UNAVAILABLE` for every case where
the text cannot go into the running turn. The answer still compares equal to
that constant, so every caller keeps its queue path, and it also carries a
``kiro_crew.steer_refusal`` code the sender and the queued row show.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _slot,
    _steerable,
)

from kiro_crew import steer_refusal
from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import steer_carry


def _running(tmp_path, *, accepted: bool = True):
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    slot._acp_client = _steerable(accepted=accepted)
    slot._acp_client.steer_needs_loss_recovery = False
    slot._acp_client.has_active_turn = lambda: True
    return state, slot


def _steer(state, slot, text: str = "rebase first", **kwargs):
    return asyncio.run(cd.steer_into_running_turn(state, slot, text, **kwargs))


def _assert_fell_back(outcome, reason: str) -> None:
    assert outcome == cd.STEER_UNAVAILABLE, "every fallback keeps the caller's queue path"
    assert cd.steer_refusal_of(outcome) == reason


def test_a_turn_that_has_no_agent_yet(tmp_path):
    """The turn publishes its agent once the session is up: none yet says the turn
    is starting, not that its agent takes no steer."""
    state, slot = _running(tmp_path)
    slot._acp_client = None
    _assert_fell_back(_steer(state, slot), steer_refusal.BETWEEN_STEPS)


def test_a_client_that_does_not_steer(tmp_path):
    state, slot = _running(tmp_path)
    slot._acp_client.supports_steer = False
    _assert_fell_back(_steer(state, slot), steer_refusal.NO_STEER)


def test_a_peer_steer_this_harness_may_drop_with_recovery_off(tmp_path, monkeypatch):
    state, slot = _running(tmp_path)
    slot._acp_client.steer_needs_loss_recovery = True
    monkeypatch.setattr(
        cd.live,
        "snapshot",
        lambda: MagicMock(agent=MagicMock(peer_steer_loss_recovery=False)),
    )
    _assert_fell_back(_steer(state, slot), steer_refusal.PEER_STEER_OFF)


def test_no_prompt_in_flight(tmp_path):
    state, slot = _running(tmp_path)
    slot._acp_client.has_active_turn = lambda: False
    _assert_fell_back(_steer(state, slot), steer_refusal.BETWEEN_STEPS)


def test_a_compact_turn(tmp_path):
    state, slot = _running(tmp_path)
    slot._compact_command_turn = True
    _assert_fell_back(_steer(state, slot), steer_refusal.COMPACTING)


def test_the_same_text_is_already_pending(tmp_path):
    state, slot = _running(tmp_path)
    slot._pending_steers.append("rebase first")
    _assert_fell_back(_steer(state, slot), steer_refusal.SAME_PENDING)


def test_too_many_pending_steers(tmp_path):
    state, slot = _running(tmp_path)
    slot._pending_steers.extend(f"earlier {i}" for i in range(cd.MAX_PENDING_STEERS))
    _assert_fell_back(_steer(state, slot), steer_refusal.TOO_MANY)


def _written(slot, count: int, *, held: int = 0, size: int = 8, backend: str = "claude") -> None:
    """*count* steers the client took, *held* of them not written yet."""
    slot._acp_client.backend = backend
    slot._acp_client.held_steer_count = held
    slot._pending_steers.extend(f"{i:0{size}d}" for i in range(count))


@pytest.mark.parametrize("backend", sorted(steer_carry.WRITTEN_STEERS_STAY_PENDING))
def test_written_steers_leave_room_for_the_next(tmp_path, backend):
    """A harness that reports a steer read only at its turn's end keeps every
    written steer pending: the turn's 33rd note still goes in."""
    state, slot = _running(tmp_path)
    _written(slot, cd.MAX_PENDING_STEERS, backend=backend)
    assert _steer(state, slot) == cd.STEER_STEERED


@pytest.mark.parametrize("backend", ["goose", "pi", ""])  # "" is kiro-cli
def test_a_backend_outside_the_named_set_counts_every_pending_steer(tmp_path, backend):
    """goose and pi report each read as it happens, so a pending steer there is
    unread: 32 of them fill the cap even with a held count of 0."""
    assert backend not in steer_carry.WRITTEN_STEERS_STAY_PENDING
    state, slot = _running(tmp_path)
    _written(slot, cd.MAX_PENDING_STEERS, backend=backend)
    _assert_fell_back(_steer(state, slot), steer_refusal.TOO_MANY)


def test_held_steers_still_fill_the_cap(tmp_path):
    state, slot = _running(tmp_path)
    _written(slot, cd.MAX_PENDING_STEERS, held=cd.MAX_PENDING_STEERS)
    _assert_fell_back(_steer(state, slot), steer_refusal.TOO_MANY)


def test_a_steer_call_still_in_flight_counts_as_not_written(tmp_path):
    state, slot = _running(tmp_path)
    _written(slot, cd.MAX_PENDING_STEERS)
    slot._steer_rpc_in_flight.update(slot._pending_steers)
    _assert_fell_back(_steer(state, slot), steer_refusal.TOO_MANY)


@pytest.mark.parametrize("bound", ["count", "chars"])
def test_written_steers_are_still_bounded_in_memory(tmp_path, bound):
    state, slot = _running(tmp_path)
    if bound == "count":
        _written(slot, steer_carry.MAX_RETAINED_STEERS)
    else:
        size = steer_carry.MAX_RETAINED_STEER_CHARS // cd.MAX_PENDING_STEERS
        _written(slot, cd.MAX_PENDING_STEERS, size=size)
    _assert_fell_back(_steer(state, slot), steer_refusal.TOO_MANY)


@pytest.mark.parametrize(
    "client_reason",
    [
        steer_refusal.AGENT_TURN,
        steer_refusal.COMMAND_TURN,
        steer_refusal.PERMISSION_WAIT,
        steer_refusal.TOO_LONG,
        steer_refusal.TOO_MANY,
        steer_refusal.TURN_ENDED,
        steer_refusal.REFUSED,
    ],
)
def test_the_client_refused_and_said_why(tmp_path, client_reason):
    state, slot = _running(tmp_path, accepted=False)
    slot._acp_client.steer_refusal = client_reason
    _assert_fell_back(_steer(state, slot), client_reason)
    assert slot._pending_steers == [], "a refused steer leaves nothing pending"


@pytest.mark.parametrize("client_reason", ["", "not-a-code", None])
def test_a_client_that_says_nothing_useful_reads_as_refused(tmp_path, client_reason):
    state, slot = _running(tmp_path, accepted=False)
    slot._acp_client.steer_refusal = client_reason
    _assert_fell_back(_steer(state, slot), steer_refusal.REFUSED)


def test_a_steer_that_went_in_carries_no_reason(tmp_path):
    state, slot = _running(tmp_path)
    outcome = _steer(state, slot)
    assert outcome == cd.STEER_STEERED
    assert cd.steer_refusal_of(outcome) == ""


def test_a_composer_steer_that_fell_back_queues_with_its_reason(tmp_path):
    """The composer's queue arm (``queue_for_next_turn``) stamps the cause it is given."""
    state, slot = _running(tmp_path)
    state.broadcast_ws = MagicMock()
    qid = cd.queue_for_next_turn(state, slot, "rebase first", steer_refused="too_many")
    # The live card's frame carries it too, not only a reload.
    frame = state.broadcast_ws.call_args_list[0].args
    assert frame[0] == "queue_push"
    assert frame[1]["meta"][steer_refusal.QUEUE_META_KEY] == "too_many"
    plain = cd.queue_for_next_turn(state, slot, "next item", steer_refused="<script>")
    entries = {e["id"]: e for e in slot._queue}
    assert entries[qid]["meta"][steer_refusal.QUEUE_META_KEY] == "too_many"
    assert steer_refusal.QUEUE_META_KEY not in entries[plain].get("meta", {})


def test_the_queue_view_projects_only_a_known_reason():
    shown = cd.queue_entry_view(
        {"id": "q1", "content": "x", "meta": {steer_refusal.QUEUE_META_KEY: "agent_turn"}}
    )
    assert shown["meta"][steer_refusal.QUEUE_META_KEY] == "agent_turn"
    hidden = cd.queue_entry_view(
        {"id": "q2", "content": "x", "meta": {steer_refusal.QUEUE_META_KEY: "<script>"}}
    )
    assert steer_refusal.QUEUE_META_KEY not in (hidden.get("meta") or {})


@pytest.mark.parametrize("code", steer_refusal.CODES)
def test_every_code_reads_as_a_sentence_and_a_row_note(code):
    long = steer_refusal.describe(code)
    assert long.startswith("Your steer could not go into the running turn: ")
    assert long.endswith(".") and ".." not in long
    assert steer_refusal.describe_short(code).startswith("steer fell back: ")


def test_every_code_has_a_row_label_in_the_english_catalog():
    import json
    from pathlib import Path

    catalog = json.loads(
        (
            Path(__file__).resolve().parents[1] / "website" / "src" / "i18n" / "locales" / "en.json"
        ).read_text(encoding="utf-8")
    )
    labels = catalog["components"]["queueStack"]["steer_refusal"]
    assert set(labels) == set(steer_refusal.CODES)
