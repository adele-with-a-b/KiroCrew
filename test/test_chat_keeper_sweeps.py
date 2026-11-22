"""The sweeps leave a live chat keeper to its holder (runtime-ownership.md,
"Chats that outlive the gateway").

The pid of a live keeper record is skipped by ``session_pid._sweep_pid_entries``,
the one per-entry pass behind the boot, periodic and shutdown sweeps, before any
liveness or argv test. The positive control is the same row with no record: the
pass reaches it and disposes of it.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from kiro_crew import chat_keeper, platform_compat
from kiro_crew import session_pid as sp
from kiro_crew.chat_keeper import records

pytestmark = pytest.mark.skipif(not chat_keeper.supported(), reason="POSIX only")

#: An owning gateway pid no OS allocates, so the row reads as a dead gateway's.
_DEAD_GATEWAY = 2**22 + 12345


@pytest.fixture
def live_child():
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=30)


def _record_for(pid: int) -> records.KeeperRecord:
    start = platform_compat.get_process_start_id(pid)
    assert start, "the child's start identity is readable"
    return records.KeeperRecord(
        instance="abc123def456",
        session_key="dashboard:1",
        backend="claude",
        keeper_pid=pid,
        keeper_start=start,
        adapter_pid=pid,
        socket="/nonexistent.s",
        wire=1,
        created=0.0,
    )


def _sweep(row: str) -> tuple[set[str], list[int]]:
    _killed, disposed, candidates = sp._sweep_pid_entries(
        [row],
        should_skip_tagged=lambda _gw, _pid: False,
        should_skip_bare=lambda _pid: False,
        dry_run=True,
    )
    return disposed, candidates


def test_a_live_keepers_pid_is_skipped_and_without_its_record_it_is_not(
    tmp_path, monkeypatch, live_child
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    pid = live_child.pid
    # A token-less row, so the pass's own verdict on this unmanaged child is visible:
    # it disposes of the row (without a kill, as the child names no harness).
    row = f"{_DEAD_GATEWAY}:{pid}"

    disposed, candidates = _sweep(row)
    assert row in disposed, "the control row is reached and disposed of"

    records.ensure_dir(tmp_path)
    records.write_record(_record_for(pid), tmp_path)
    assert records.live_keeper_pids(tmp_path) == {pid}
    disposed, candidates = _sweep(row)
    assert row not in disposed and pid not in candidates


def test_a_record_whose_pid_was_recycled_is_not_live(tmp_path, live_child) -> None:
    record = _record_for(live_child.pid)
    records.ensure_dir(tmp_path)
    records.write_record(
        records.KeeperRecord(**{**record.__dict__, "keeper_start": "another-process"}),
        tmp_path,
    )
    assert records.live_keeper_pids(tmp_path) == frozenset()
    assert records.prune_dead(tmp_path) == 1
    assert records.load_records(tmp_path) == []


def test_a_live_keeper_is_not_a_reparented_orphan(tmp_path, monkeypatch, live_child) -> None:
    """Reparented to init with a sandbox-marked argv, a keeper still is not swept."""
    from kiro_crew import session_pid_orphan_scan as scan

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    pid = live_child.pid
    monkeypatch.setattr(scan, "_reparented_pids", lambda: [pid])
    assert scan._our_orphan_pids() == [pid], "the control: an unrecorded pid is listed"
    records.ensure_dir(tmp_path)
    records.write_record(_record_for(pid), tmp_path)
    assert scan._our_orphan_pids() == []


def test_the_session_root_cleanup_leaves_a_live_keepers_row(tmp_path, monkeypatch, live_child):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    pid = live_child.pid
    rows = [f"{_DEAD_GATEWAY}:{pid}"]
    path = tmp_path / "rows.txt"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(sp, "_session_pid_file_path", lambda: path)
    sp.cleanup_orphaned_session_roots()
    assert path.read_text(encoding="utf-8").strip() == "", "the control: the row is settled"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    records.ensure_dir(tmp_path)
    records.write_record(_record_for(pid), tmp_path)
    sp.cleanup_orphaned_session_roots()
    assert path.read_text(encoding="utf-8").strip() == rows[0]
