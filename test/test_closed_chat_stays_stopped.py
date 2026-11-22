"""Closing a busy chat stops it, and a worker brought back keeps its creator's posture.

Two halves of one bug. A close cancels the running turn, and that turn's own tail
drains the queue (``_start_next_queued_turn``): with no guard it started the next
queued entry on the popped slot, so each entry started a new runtime that
``session_status`` reported as closed. And a worker's posture lives in memory, so
every path that rebuilds it from history (the History tab, a revive, a rehydrate for
a queued send or a wake) brought it back asking for every tool even while the
conductor that made it held Trust (``approval_inheritance.reapply_creator_posture``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from session_control_revive_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _archive,
    _enabled,
    _fresh_windows,
    _settle_metadata_rewrite_before_read,
    _slot,
)

from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.approval_inheritance import reapply_creator_posture

NAME = "chat-1-1785"

# ── A close stops the queue ──────────────────────────────────────────────────


def _queued_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.subagents = None
    slot = state.get_or_create_slot(NAME)
    slot.queue_append("the next thing", directive_user_origin=True)
    return state, slot


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["closing", "closed", "replaced"])
async def test_no_queued_turn_starts_on_a_slot_being_closed(tmp_path, monkeypatch, how):
    state, slot = _queued_state(tmp_path, monkeypatch)
    if how == "replaced":
        state._slots[NAME] = handlers._ChatSlot(NAME)
    else:
        slot.begin_close()
    if how == "closed":
        # What a close that went through leaves: popped, its fence still up.
        state._slots.pop(NAME)
    spawn = MagicMock()
    with patch.object(chat_runner, "spawn_guarded_turn", spawn):
        assert await chat_runner._start_next_queued_turn(state, slot) is False
    spawn.assert_not_called()
    assert [e["content"] for e in slot._queue] == ["the next thing"]


@pytest.mark.asyncio
async def test_the_drain_still_runs_on_an_open_slot(tmp_path, monkeypatch):
    """The guard's control: the same queue on an open slot starts its turn."""
    state, slot = _queued_state(tmp_path, monkeypatch)
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True
    spawn.assert_called_once()


@pytest.mark.asyncio
async def test_closing_a_busy_chat_does_not_start_its_queued_message(tmp_path, monkeypatch):
    """The real close path: the cancelled turn's tail drains, and nothing starts."""
    state, slot = _queued_state(tmp_path, monkeypatch)
    slot.append("user", "do the long thing")
    slot.drain()
    started = asyncio.Event()
    drained: list[bool] = []

    async def _busy_turn() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            # What ``_end_turn_tail`` does once the close's cancel ends the turn.
            drained.append(await chat_runner._start_next_queued_turn(state, slot))

    slot.task = asyncio.create_task(_busy_turn())
    await started.wait()
    spawn = MagicMock()
    with patch.object(chat_runner, "spawn_guarded_turn", spawn):
        await handlers.close_slot(state, slot, NAME)

    assert NAME not in state._slots
    assert drained == [False]
    spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_close", [False, True], ids=["last-close", "another-close-holds"])
async def test_a_refused_close_starts_the_queue_it_held_back(tmp_path, monkeypatch, other_close):
    """A turn that ended inside a refused close had its drain refused; the release restarts it."""
    state, slot = _queued_state(tmp_path, monkeypatch)
    if other_close:
        slot.begin_close()

    def _refuse() -> None:
        raise handlers.SlotCloseError("refused for the test", code="test_refused", status=409)

    spawn = MagicMock()
    with (
        patch("kiro_crew.dashboard.queue_edit_hold.spawn_drain_if_idle", spawn),
        pytest.raises(handlers.SlotCloseError),
    ):
        await handlers.close_slot(state, slot, NAME, pre_pop_check=_refuse)

    assert state._slots.get(NAME) is slot
    assert [e["content"] for e in slot._queue] == ["the next thing"]
    if other_close:
        assert slot.is_closing
        spawn.assert_not_called()
    else:
        assert not slot.is_closing
        spawn.assert_called_once_with(state, slot)


# ── A worker brought back keeps its creator's posture ────────────────────────


def _creation_records(monkeypatch, parents: dict[str, str]) -> None:
    """Stand in for the crew log's creation records, read by both checks."""
    monkeypatch.setattr(sc, "_slot_tree_parent", lambda key: (True, parents.get(key, ""), {}))
    monkeypatch.setattr(sc, "_creation_parent", lambda key: parents.get(key, ""))
    monkeypatch.setattr(
        "kiro_crew.crew_log.session_tree_projection.creator_of", lambda key: parents.get(key, "")
    )


def _worker(state, creator, name="chat-2"):
    worker = _slot(state, name)
    worker._created_by = creator.key
    worker._posture_inherited = True
    return worker


@pytest.mark.parametrize(
    ("trust", "card_scoped", "reads", "expected"),
    [
        (True, False, False, (True, False)),
        (True, True, False, (False, False)),
        (False, False, True, (False, True)),
        (False, False, False, (False, False)),
    ],
    ids=["trust", "card-trust-carries-none", "reads", "normal"],
)
def test_a_rebuilt_worker_takes_what_its_live_creator_carries(
    tmp_path, monkeypatch, trust, card_scoped, reads, expected
):
    state = _make_state(tmp_path)
    conductor = _slot(state, "chat-1")
    conductor._trust, conductor._trust_card_scoped, conductor._trust_reads = (
        trust,
        card_scoped,
        reads,
    )
    worker = _worker(state, conductor)
    _creation_records(monkeypatch, {worker.key: conductor.key})

    reapply_creator_posture(state, worker)

    assert (worker._trust, worker._trust_reads) == expected
    assert worker._posture_inherited is True


@pytest.mark.parametrize(
    "case", ["creator-closed", "no-creation-record", "explicit-mode", "crewmate-worker", "shared"]
)
def test_a_rebuilt_worker_is_not_raised_without_a_live_verified_creator(
    tmp_path, monkeypatch, case
):
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    state = _make_state(tmp_path)
    conductor = _slot(state, "chat-1")
    conductor._trust = True
    worker = _worker(state, conductor)
    _creation_records(
        monkeypatch, {} if case == "no-creation-record" else {worker.key: conductor.key}
    )
    if case == "creator-closed":
        state._slots.pop(conductor.key)
    elif case == "explicit-mode":
        worker._posture_inherited = False
    elif case == "crewmate-worker":
        worker.agent = MATE_WORKER_AGENT
    elif case == "shared":
        twin = _slot(state, "chat-3")
        twin.linked_session_key = f"dashboard:{worker.key}"

    reapply_creator_posture(state, worker)

    assert worker._trust is False


def test_a_person_resuming_a_closed_worker_gets_it_back_with_its_creators_trust(
    tmp_path, monkeypatch
):
    """The History tab's resume: the person, not the conductor, brings it back."""
    state = _make_state(tmp_path)
    conductor = _slot(state, "chat-1")
    conductor._trust = True
    worker = _worker(state, conductor)
    key = _archive(state, conductor, worker)
    _creation_records(monkeypatch, {key: conductor.key})

    outcome = asyncio.run(
        handlers.resume_slot_from_history(state, name=key, history_key=f"dashboard:{key}")
    )

    assert outcome.refusal is None
    resumed = state._slots[key]
    assert resumed._created_by == conductor.key
    assert resumed._trust is True
    assert resumed._posture_inherited is True


def test_a_resume_restores_no_creator_the_crew_log_does_not_confirm(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    conductor = _slot(state, "chat-1")
    conductor._trust = True
    worker = _worker(state, conductor)
    key = _archive(state, conductor, worker)
    _creation_records(monkeypatch, {key: "chat-9"})

    asyncio.run(handlers.resume_slot_from_history(state, name=key, history_key=f"dashboard:{key}"))

    assert state._slots[key]._created_by == ""
    assert state._slots[key]._trust is False


def test_a_worker_rehydrated_for_a_send_gets_its_creators_trust(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    conductor = _slot(state, "chat-1")
    conductor._trust = True
    worker = _worker(state, conductor)
    key = _archive(state, conductor, worker)
    _creation_records(monkeypatch, {key: conductor.key})

    from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

    rebuilt = asyncio.run(rehydrate_slot_from_history_async(state, key, adopt_closed=True))

    assert rebuilt is not None
    assert rebuilt._trust is True
