"""The ``crew=`` dispatch record's stored copy (rfc-conductor-remote-dispatch, wave 1).

The route-level behaviour (a hub restart keeps the opener's right, another session
is still refused, a damaged or unreadable record admits nothing and is never
overwritten) is pinned in ``test_session_control_crew.py``. These pin the store
and load edges under it:

* a board directory or record reached through a link is not loaded;
* a stored entry over the per-caller cap loads only the cap;
* a record that was never loaded is never written;
* a second first load that finishes after the record changed does not undo it;
* the conductor purge removes the board's record with its other remote records.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading

import pytest

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew
from kiro_crew.testing.wait import async_wait_until, await_until
from kiro_crew.work_ledger_purge import _remove_remote_records

CALLER = "chat-1"
INSTANCE = "inst-lab"


class _State:
    """A gateway state: only its identity matters to the record."""


def _stored(caller: str = CALLER) -> list[tuple[str, str]]:
    return work_ledger.read_crew_sessions().get(caller, [])


@pytest.mark.skipif(sys.platform == "win32", reason="directory links need privileges on Windows")
def test_a_board_directory_reached_through_a_link_is_not_loaded(tmp_path):
    board_dir = work_ledger.crew_sessions_path(CALLER).parent
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / work_ledger.CREW_SESSIONS_FILE).write_text(
        json.dumps({"callers": {CALLER: [[INSTANCE, "chat-77"]]}}), encoding="utf-8"
    )
    board_dir.parent.mkdir(parents=True, exist_ok=True)
    board_dir.symlink_to(elsewhere, target_is_directory=True)
    assert work_ledger.read_crew_sessions() == {}


@pytest.mark.skipif(sys.platform == "win32", reason="file links need privileges on Windows")
def test_a_record_file_reached_through_a_link_is_not_loaded(tmp_path):
    path = work_ledger.crew_sessions_path(CALLER)
    path.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "record.json"
    elsewhere.write_text(json.dumps({"callers": {CALLER: [[INSTANCE, "chat-77"]]}}), "utf-8")
    path.symlink_to(elsewhere)
    assert work_ledger.read_crew_sessions() == {}


def test_a_stored_entry_over_the_cap_loads_only_the_cap():
    pairs = [(INSTANCE, f"chat-{n:03d}") for n in range(crew.MAX_CREW_SLOTS_PER_CALLER + 6)]
    work_ledger.write_crew_sessions(CALLER, pairs)
    state = _State()
    assert asyncio.run(crew.load_record(state)) is True
    assert len(crew._record(state)[CALLER]) == crew.MAX_CREW_SLOTS_PER_CALLER


def test_a_record_that_was_never_loaded_is_never_written():
    work_ledger.write_crew_sessions(CALLER, [(INSTANCE, "chat-77")])
    state = _State()
    crew._record_dispatch(state, CALLER, INSTANCE, "chat-78")
    assert asyncio.run(crew._persist(state, CALLER)) is False
    assert _stored() == [(INSTANCE, "chat-77")]


def test_a_second_first_load_never_undoes_a_change_made_after_the_first(monkeypatch):
    """Two first loads read the store at once. The one that finishes later read it
    before a close the record took in between; it must not fold that copy back in."""
    work_ledger.write_crew_sessions(CALLER, [(INSTANCE, "chat-77")])
    real = work_ledger.read_crew_sessions
    started: list[threading.Event] = []
    lock = threading.Lock()

    def held_read():
        # The store is read FIRST, then held: a late finisher carries a stale copy.
        data = real()
        gate = threading.Event()
        with lock:
            started.append(gate)
        assert gate.wait(30), "the test never released this read"
        return data

    monkeypatch.setattr(work_ledger, "read_crew_sessions", held_read)
    state = _State()

    async def go() -> None:
        loads = [asyncio.ensure_future(crew.load_record(state)) for _ in range(2)]
        await async_wait_until(lambda: len(started) == 2)
        started[0].set()
        done, pending = await asyncio.wait(loads, return_when=asyncio.FIRST_COMPLETED)
        assert [task.result() for task in done] == [True]
        async with crew.cap_lock(state):
            crew._forget_dispatch(state, CALLER, INSTANCE, "chat-77")
            assert await crew._persist(state, CALLER) is True
        started[1].set()
        (late,) = pending
        assert await await_until(late, what="the late first load") is True
        assert not crew.dispatched_by(state, CALLER, INSTANCE, "chat-77")

    asyncio.run(go())
    assert real().get(CALLER, []) == []


def test_the_conductor_purge_removes_the_boards_record():
    work_ledger.write_crew_sessions(CALLER, [(INSTANCE, "chat-77")])
    path = work_ledger.crew_sessions_path(CALLER)
    assert path.exists()
    _remove_remote_records(CALLER)
    assert not path.exists()
