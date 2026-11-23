"""An ad-hoc safety-override grant is kept across a restart the user did not choose.

The rule (``kiro_crew.kept_trust``): a timed grant comes back with the time it had
left, an ``until_shutdown`` grant with no expiry, and only an explicit stop clears
it. The breadcrumb the live grant keeps is what the next boot reads
(``take_dropped_grant``) and re-arms (``SafetyOverride.restore_kept``), through the
policy ceiling; a restore the policy refuses is audited and owed the notice.

The read is a pure function of a small file plus the wall clock, so no restart is
simulated: writing the record and then reading it back through
``take_dropped_grant`` IS the restart, from the reader's point of view.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import safety_override as so


@pytest.fixture(autouse=True)
def _isolated_breadcrumb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate the record, and run publishes INLINE.

    Two separate concerns. The path is redirected so no test touches the real
    data home. And ``_enqueue_breadcrumb`` is replaced with direct execution so
    the tests never start the daemon worker: a queued publish would otherwise
    leave a thread running past the test that started it (found in review), and
    inline execution also makes every assertion on the file deterministic instead
    of waiting on a worker.

    Production keeps the worker -- see ``_sync_breadcrumb``; the callers there sit
    on the gateway's event loop.
    """
    monkeypatch.setattr(so, "_breadcrumb_path", lambda: tmp_path / "last_grant.json")
    monkeypatch.setattr(so, "_enqueue_breadcrumb", lambda job: job())
    so.reset_singleton()
    yield tmp_path / "last_grant.json"
    so.reset_singleton()


def _record(path: Path) -> dict:
    """Read the published record."""
    return json.loads(path.read_text(encoding="utf-8"))


def _published(path: Path) -> bool:
    return path.exists()


def _take() -> object:
    return so.take_dropped_grant()


def _write(path: Path, *, source: str, offset_secs: float, permanent: bool = False) -> None:
    """Write a record whose deadline is *offset_secs* from now (negative = past)."""
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": source,
                "expires_at": datetime.now(tz=timezone.utc).timestamp() + offset_secs,
                "permanent": permanent,
            }
        ),
        encoding="utf-8",
    )


class TestTheNoticeIsOwed:
    def test_a_timed_grant_with_time_left_is_reported(self, _isolated_breadcrumb: Path) -> None:
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=3600)
        dropped = so.take_dropped_grant()
        assert dropped is not None
        assert dropped.source == "dashboard"
        # Rounded down by the elapsed microseconds, so allow the boundary.
        assert 3500 <= dropped.remaining_secs <= 3600

    def test_the_notice_names_the_source_and_what_was_lost(self) -> None:
        text = so.describe_dropped_grant(so.DroppedGrant(source="slack", remaining_secs=7200))
        # The operator has to learn three things: it is OFF, how much they lost,
        # and that this is what a restart does -- not a malfunction.
        assert "OFF" in text
        assert "2h" in text
        assert "slack" in text
        assert "restart" in text.lower()

    def test_an_until_shutdown_grant_is_taken_as_one_with_no_expiry(
        self, _isolated_breadcrumb: Path
    ) -> None:
        # It lasts until the user stops Kiro Crew, and a restart is not that.
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=0, permanent=True)
        dropped = so.take_dropped_grant()
        assert dropped is not None and dropped.permanent is True
        assert dropped.source == "dashboard"

    def test_an_audit_failure_still_yields_the_notice(
        self, _isolated_breadcrumb: Path, monkeypatch
    ) -> None:
        # The notice is the point; a broken audit sink must not swallow it.
        class _Boom:
            def log_api_access(self, **kw):
                raise RuntimeError("sel down")

        monkeypatch.setattr(so, "sel", lambda: _Boom())
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=600)
        assert so.take_dropped_grant() is not None


class TestNoNoticeIsOwed:
    def test_no_record_is_silent(self) -> None:
        assert so.take_dropped_grant() is None

    def test_a_lapsed_grant_is_silent(self, _isolated_breadcrumb: Path) -> None:
        # The clock was taking it anyway, so the restart cost nothing.
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=-1)
        assert so.take_dropped_grant() is None

    @pytest.mark.parametrize("source", ["config", "cli"])
    def test_a_grant_the_boot_arms_again_is_not_taken(
        self, _isolated_breadcrumb: Path, source: str
    ) -> None:
        # A declared grant and the ``gateway --approval yolo`` one are re-armed by
        # this same startup from config and argv, never from the record.
        _write(_isolated_breadcrumb, source=source, offset_secs=99999, permanent=True)
        assert so.take_dropped_grant() is None

    def test_an_owner_stop_takes_nothing_and_removes_the_record(self) -> None:
        # ``kirocrew stop``, Quit or Ctrl+C: the user ended Kiro Crew.
        from kiro_crew import kept_trust

        record = kept_trust.kept_path(kept_trust.YOLO_FILE)
        record.parent.mkdir(parents=True, exist_ok=True)
        _write(record, source="dashboard", offset_secs=3600)
        assert kept_trust.mark_owner_stop("kirocrew stop") is True
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(so, "_breadcrumb_path", lambda: record)
            assert so.take_dropped_grant() is None
        assert not record.exists()
        assert not kept_trust.kept_path(kept_trust.OWNER_STOP_FILE).exists()

    def test_a_corrupt_record_is_silent_and_removed(self, _isolated_breadcrumb: Path) -> None:
        _isolated_breadcrumb.write_text("{not json", encoding="utf-8")
        assert so.take_dropped_grant() is None
        assert not _isolated_breadcrumb.exists()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are a POSIX shape")
    def test_a_fifo_is_refused_without_hanging(
        self, _isolated_breadcrumb: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The size bound alone does not cover this: a FIFO reports st_size 0, so
        # it passes a size check and then blocks forever on read, hanging gateway
        # initialization before readiness (found in review). O_NONBLOCK plus an
        # fstat shape check is what refuses it. Asserted as the calls this thread
        # makes on the FIFO: its open asks for O_NONBLOCK (one without it is
        # refused on the spot instead of hanging) and nothing reads from it.
        os.mkfifo(_isolated_breadcrumb)
        real_open, real_read = os.open, os.read
        this_thread = threading.get_ident()
        nonblock = os.O_NONBLOCK
        fifo_fds: list[int] = []
        fifo_reads: list[int] = []

        def recorded_open(path, flags, *args, **kwargs):
            if threading.get_ident() == this_thread and Path(path) == _isolated_breadcrumb:
                assert flags & nonblock, "the FIFO was opened without O_NONBLOCK"
                fd = real_open(path, flags, *args, **kwargs)
                fifo_fds.append(fd)
                return fd
            return real_open(path, flags, *args, **kwargs)

        def recorded_read(fd, n):
            if threading.get_ident() == this_thread and fd in fifo_fds:
                fifo_reads.append(n)
            return real_read(fd, n)

        with monkeypatch.context() as mp:
            mp.setattr(os, "open", recorded_open)
            mp.setattr(os, "read", recorded_read)
            assert so.take_dropped_grant() is None
        assert fifo_fds, "the record was never opened"
        assert fifo_reads == [], "the FIFO was read instead of refused by its shape"

    @requires_symlinks
    def test_a_symlink_is_refused(self, _isolated_breadcrumb: Path, tmp_path: Path) -> None:
        # O_NOFOLLOW: a link planted here must not redirect the read at another
        # file, even one that would parse.
        real = tmp_path / "elsewhere.json"
        real.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "dashboard",
                    "expires_at": datetime.now(tz=timezone.utc).timestamp() + 3600,
                    "permanent": False,
                }
            ),
            encoding="utf-8",
        )
        os.symlink(real, _isolated_breadcrumb)
        assert so.take_dropped_grant() is None

    def test_an_oversized_record_is_discarded_unread(self, _isolated_breadcrumb: Path) -> None:
        # Anything that can write into the data home could otherwise leave a huge
        # file here and turn every startup into a memory-exhaustion restart loop
        # (found in review). Size is checked before the read, so the bytes are
        # never taken in, and the file is removed so it cannot do it again.
        _isolated_breadcrumb.write_text("x" * (so._BREADCRUMB_MAX_BYTES + 1), encoding="utf-8")
        assert so.take_dropped_grant() is None
        assert not _isolated_breadcrumb.exists()

    def test_a_record_at_the_size_bound_is_still_read(self, _isolated_breadcrumb: Path) -> None:
        # The bound must not reject a legitimate record: pad one with whitespace
        # up to the limit and it still produces its notice.
        payload = json.dumps(
            {
                "version": 1,
                "source": "dashboard",
                "expires_at": datetime.now(tz=timezone.utc).timestamp() + 3600,
                "permanent": False,
            }
        )
        padded = payload + " " * (so._BREADCRUMB_MAX_BYTES - len(payload))
        _isolated_breadcrumb.write_text(padded, encoding="utf-8")
        assert len(padded) == so._BREADCRUMB_MAX_BYTES
        assert so.take_dropped_grant() is not None

    def test_the_notice_fires_at_most_once(self, _isolated_breadcrumb: Path) -> None:
        # Single-shot by construction: the record is consumed, so a restart
        # cannot re-notify on every subsequent startup.
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=3600)
        assert so.take_dropped_grant() is not None
        assert not _isolated_breadcrumb.exists()
        assert so.take_dropped_grant() is None


class TestThisProcessOwnRecordIsNotADropNotice:
    """A reader in the writing process IMAGE must leave the live record alone.

    This is what frees the read from having to run before the startup grant is
    applied -- the constraint that would otherwise force it onto the pre-bind boot
    path. Identity is an import-time nonce rather than the pid, because both
    restart paths end in ``os.execv``, which PRESERVES the pid (and the process
    start time), so a pid check would swallow the notice on exactly the restarts
    this feature exists for (found in review).
    """

    def test_our_own_live_record_is_not_reported(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        assert _published(_isolated_breadcrumb)
        # Written by THIS image, so it is a live grant's record, not a dropped one.
        assert so.take_dropped_grant() is None

    def test_our_own_live_record_is_not_consumed(self, _isolated_breadcrumb: Path) -> None:
        # Consuming it would delete the record for a grant that is in force, so a
        # LATER restart would have nothing to report.
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        so.take_dropped_grant()
        assert _published(_isolated_breadcrumb)

    def test_a_restart_that_keeps_the_pid_still_reports(self, _isolated_breadcrumb: Path) -> None:
        # THE regression this identity exists for. `reexec_python_module` is
        # os.execv on POSIX: same pid, new process image. Simulated by writing a
        # record with this very pid but a different image token -- exactly what the
        # previous image leaves behind -- which must still be reported.
        _isolated_breadcrumb.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "dashboard",
                    "expires_at": datetime.now(tz=timezone.utc).timestamp() + 3600,
                    "permanent": False,
                    "image": "a-previous-process-image",
                    "pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        dropped = so.take_dropped_grant()
        assert dropped is not None, "an execv restart keeps the pid; the notice must survive it"
        assert dropped.source == "dashboard"
        assert not _isolated_breadcrumb.exists()

    def test_a_consume_serializes_against_a_publish(self, _isolated_breadcrumb: Path) -> None:
        # A publish landing between the consumer's open and its clear would be
        # UNLINKED by that clear, deleting the record for a grant that is live
        # right now (found in review). The consumer therefore holds the publisher's
        # lock across the whole span -- proven by holding it here and showing the
        # consumer waits rather than proceeding.
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=3600)
        finished = threading.Event()

        def _consume() -> None:
            so.take_dropped_grant()
            finished.set()

        so._breadcrumb_io_lock.acquire()
        try:
            threading.Thread(target=_consume, daemon=True).start()
            assert not finished.wait(0.25), "consumer did not wait for the publisher's lock"
        finally:
            so._breadcrumb_io_lock.release()
        assert finished.wait(5.0), "consumer never completed after the lock was released"
        assert not _isolated_breadcrumb.exists()

    def test_a_record_from_another_process_is_reported(self, _isolated_breadcrumb: Path) -> None:
        _write(_isolated_breadcrumb, source="dashboard", offset_secs=3600)
        # _write stamps no image token, so it reads as "not this image" -- the same
        # verdict a previous gateway's record gets.
        dropped = so.take_dropped_grant()
        assert dropped is not None
        assert not _isolated_breadcrumb.exists()


class TestAQueuedPublishActsOnThePathItWasQueuedFor:
    """A publish runs LATER, on the worker thread. It must act on the file its own
    transition was about -- not on whatever ``_breadcrumb_path`` names by then.

    Sibling test files in the same xdist worker call real
    transitions, so they enqueue real publishes onto this module's long-lived
    queue; this file's autouse fixture repoints ``_breadcrumb_path`` at its own
    tmp_path. A job that resolved the path when it RAN therefore deleted or
    overwrote THIS test's record, and the read that followed reported no notice --
    surfacing on CI as ``assert None is not None`` in whichever test happened to
    be running, unreproducible by rerunning and structurally unreproducible when
    this file is run alone, because then nothing else ever enqueues anything.

    The publish is CAPTURED rather than run on the worker: that makes the
    interleaving exact instead of load-dependent, runs the real ``_publish``
    closure, and leaves no thread behind. Each test asserts on BOTH paths, so a
    job that quietly did nothing cannot pass either of them.
    """

    def test_a_queued_clear_does_not_unlink_a_later_path(self, tmp_path: Path, monkeypatch) -> None:
        sibling = tmp_path / "sibling" / "last_grant.json"
        victim = tmp_path / "victim" / "last_grant.json"
        for parent in (sibling.parent, victim.parent):
            parent.mkdir(parents=True, exist_ok=True)

        jobs: list = []
        monkeypatch.setattr(so, "_breadcrumb_path", lambda: sibling)
        monkeypatch.setattr(so, "_enqueue_breadcrumb", jobs.append)

        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("slack")
        override.deactivate("slack")
        assert len(jobs) == 2, "expected a publish for the activation and one for the revocation"

        jobs[0]()  # the sibling's activation lands its own record
        assert sibling.exists(), "the captured publish never ran, so this test proves nothing"

        # Now this file's fixture-style repoint, and this test's own record.
        monkeypatch.setattr(so, "_breadcrumb_path", lambda: victim)
        _write(victim, source="dashboard", offset_secs=600)

        jobs[1]()  # the sibling's revocation, arriving late

        assert not sibling.exists(), "the queued clear did not act at all"
        assert victim.exists(), "a clear queued for another path unlinked this record"
        dropped = so.take_dropped_grant()
        assert dropped is not None, "the notice was owed and a foreign clear swallowed it"
        assert dropped.source == "dashboard"

    def test_a_queued_write_does_not_overwrite_a_later_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The same defect's other arm, and the one the issue's remaining candidate
        # named: a foreign write stamps THIS image's token, so the own-image guard
        # then correctly refuses a record that is not this test's.
        sibling = tmp_path / "sibling" / "last_grant.json"
        victim = tmp_path / "victim" / "last_grant.json"
        for parent in (sibling.parent, victim.parent):
            parent.mkdir(parents=True, exist_ok=True)

        jobs: list = []
        monkeypatch.setattr(so, "_breadcrumb_path", lambda: sibling)
        monkeypatch.setattr(so, "_enqueue_breadcrumb", jobs.append)

        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("slack")
        assert len(jobs) == 1

        monkeypatch.setattr(so, "_breadcrumb_path", lambda: victim)
        _write(victim, source="dashboard", offset_secs=600)

        jobs[0]()  # the sibling's activation, arriving late

        # Each path holds its OWN record -- pairing, not merely non-emptiness.
        assert sibling.exists(), "the queued write did not act at all"
        assert _record(sibling)["source"] == "slack"
        assert _record(victim)["source"] == "dashboard"
        assert "image" not in _record(victim), "a foreign write stamped this image onto the record"
        dropped = so.take_dropped_grant()
        assert dropped is not None, "the notice was owed and a foreign write swallowed it"
        assert dropped.source == "dashboard"


class TestTheCourtesyNeverEndangersTheGrant:
    """The record is a courtesy; the grant is a decision. Failures stay separated."""

    def test_an_enqueue_failure_does_not_fail_the_activation(self, monkeypatch) -> None:
        # The grant is already committed when the publish is dispatched, so an
        # exception escaping the dispatch would report a FAILED activation while
        # tools are in fact auto-approved (found in review). Starting the worker
        # can fail on its own -- a thread quota is a real limit.
        def _boom(job):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(so, "_enqueue_breadcrumb", _boom)
        override = so.safety_override()
        override.adhoc_ttl = 3600
        assert override.activate("dashboard").active is True
        assert override.is_active() is True
        # And the reverse direction: a revocation must still complete.
        override.deactivate("dashboard")
        assert override.is_active() is False

    def test_the_flush_is_bounded_when_a_write_is_stalled(self, monkeypatch) -> None:
        # A restart path calls this. An unbounded wait would freeze a gateway
        # that was trying to re-exec, which is worse than losing the record
        # (found in review). Simulate a publish that never finishes.
        from kiro_crew import safety_override_breadcrumb as breadcrumb

        real_idle = breadcrumb._breadcrumb_idle
        real_idle.clear()
        waits: list[float | None] = []
        flusher = threading.get_ident()
        real_wait = real_idle.wait

        def _recorded_wait(timeout: float | None = None) -> bool:
            # The real event stays in place for every other thread (a pump left over
            # from another test may still set or clear it); only this thread's waits
            # are recorded.
            if threading.get_ident() == flusher:
                waits.append(timeout)
                if timeout is None:
                    raise AssertionError("flush waited without a bound on a stalled write")
            return real_wait(timeout)

        monkeypatch.setattr(real_idle, "wait", _recorded_wait)
        try:
            drained = so.flush_breadcrumb_writes(0.05)
            assert drained is False
            assert waits == [0.05], f"flush waited {waits} on a stalled write, not its 0.05s bound"
        finally:
            real_idle.set()


class TestOneRecordPerHomeIsSafe:
    """No sibling gateway can consume this record out from under its writer.

    The record is per data home, which is only safe because a home already has at
    most one gateway: ``gateway_lock`` takes an exclusive advisory flock for the
    process lifetime and REFUSES a second gateway on the same home. Pinned here
    because a reviewer read the single file as shared-gateway state, and the thing
    that makes it not shared is a guard in another module.
    """

    def test_the_record_sits_in_the_directory_the_gateway_lock_guards(self) -> None:
        from kiro_crew import gateway_lock
        from kiro_crew.config.loader import config_dir

        # Built from the module constant rather than from ``_breadcrumb_path()``,
        # which the autouse fixture redirects into tmp_path -- asserting on the
        # patched accessor would only test the fixture.
        from kiro_crew import kept_trust

        real_record = kept_trust.kept_path(so._BREADCRUMB_FILE)
        # Under the same home, so the lock that admits one gateway per home is the
        # same boundary that admits one writer of this record.
        assert real_record.parent == config_dir() / kept_trust.KEPT_DIR
        assert (config_dir() / gateway_lock.LOCK_FILENAME).parent == real_record.parent.parent

    def test_a_second_gateway_on_the_same_home_is_refused(self, tmp_path: Path) -> None:
        # The invariant itself, exercised rather than asserted from the docstring:
        # the second acquire raises instead of proceeding alongside the first.
        from kiro_crew.gateway_lock import GatewayLock, GatewayLockError

        first = GatewayLock(tmp_path).acquire()
        try:
            with pytest.raises(GatewayLockError):
                GatewayLock(tmp_path).acquire()
        finally:
            first.release()


class TestOrderingAgainstConcurrentTransitions:
    """A delayed publish must not resurrect a grant the operator revoked."""

    def test_a_late_publish_cannot_undo_a_revocation(self, _isolated_breadcrumb: Path) -> None:
        # Reproduces the interleaving directly: activation snapshots state, a
        # deactivation lands, and only THEN does the activation's publish run.
        # Because the publish derives its content from live state rather than
        # from the activation's locals, it writes the revocation's truth.
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        override.deactivate("dashboard")
        # A stale sync arriving after the revocation must clear, not write.
        override._sync_breadcrumb()
        assert not _published(_isolated_breadcrumb)
        assert _take() is None

    def test_an_older_generation_does_not_overwrite_a_newer_publish(
        self, _isolated_breadcrumb: Path
    ) -> None:
        # The generation counter is what orders two overlapping transitions: a
        # publish holding a stale generation returns without touching the file.
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        assert _published(_isolated_breadcrumb)
        override._breadcrumb_published_gen += 5  # pretend a newer publish landed
        _isolated_breadcrumb.unlink()
        override._sync_breadcrumb()
        assert not _published(_isolated_breadcrumb)


class TestTheGrantLifecycleMaintainsTheRecord:
    def test_activating_records_the_grant(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        assert _published(_isolated_breadcrumb)
        record = _record(_isolated_breadcrumb)
        assert record["source"] == "dashboard"
        assert record["permanent"] is False
        # Wall clock, because the reader is a different process: the in-memory
        # deadline is time.monotonic() and means nothing after a restart.
        assert record["expires_at"] > datetime.now(tz=timezone.utc).timestamp()

    def test_an_explicit_deactivate_leaves_no_notice(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 3600
        override.activate("dashboard")
        override.deactivate("dashboard")
        assert _take() is None

    def test_a_natural_expiry_leaves_no_notice(
        self, _isolated_breadcrumb: Path, monkeypatch
    ) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 1
        override.activate("dashboard")
        # Walk the monotonic clock past the deadline and let is_active() reap it.
        real_monotonic = so.time.monotonic
        monkeypatch.setattr(so.time, "monotonic", lambda: real_monotonic() + 10.0)
        assert override.is_active() is False
        assert _take() is None

    def test_a_renew_moves_the_recorded_deadline(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.adhoc_ttl = 60
        override.activate("dashboard")
        first = _record(_isolated_breadcrumb)["expires_at"]
        override.adhoc_ttl = 7200
        assert override.renew("dashboard").renewed is True
        second = _record(_isolated_breadcrumb)["expires_at"]
        # Otherwise a restart after a renewal reports the stale remaining time.
        assert second > first

    def test_a_declared_grant_records_itself_as_no_expiry(self, _isolated_breadcrumb: Path) -> None:
        override = so.safety_override()
        override.activate_declared()
        record = _record(_isolated_breadcrumb)
        assert record["permanent"] is True
        # And therefore owes no notice on the next startup.
        assert _take() is None

    def test_a_write_failure_never_breaks_the_grant(self, tmp_path: Path, monkeypatch) -> None:
        # A breadcrumb is a courtesy; a grant is a decision. The courtesy failing
        # must not fail the decision -- and must never fail a DEACTIVATION,
        # where raising would leave auto-approval on.
        #
        # The unwritable path stays UNDER tmp_path: atomic_write() does
        # parent.mkdir(parents=True), so a path outside the fixture's workspace
        # would be CREATED by this test and survive teardown (found in review).
        # A file standing where a directory must be makes the write fail without
        # letting anything escape tmp_path.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setattr(so, "_breadcrumb_path", lambda: blocker / "last_grant.json")
        override = so.safety_override()
        override.adhoc_ttl = 3600
        assert override.activate("dashboard").active is True
        override.deactivate("dashboard")
        assert override.is_active() is False


class TestTheBootRestoresTheGrant:
    """``restore_kept``: the boot's half, through the same commit every grant takes."""

    @pytest.fixture
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        calls: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(so, "sel", lambda: _Sel())
        return calls

    def test_a_timed_grant_comes_back_with_the_time_it_had_left(self, audits) -> None:
        override = so.safety_override()
        assert override.restore_kept("dashboard", 1800, permanent=False) is True
        assert override.is_active() is True and override._source == "dashboard"
        assert 1790 <= override.remaining_secs() <= 1800
        ops = [c["operation"] for c in audits]
        assert "safety_override:activate" in ops and "safety_override:restored" in ops

    def test_an_until_shutdown_grant_comes_back_with_no_expiry(self, audits) -> None:
        override = so.safety_override()
        assert override.restore_kept("slack", 0, permanent=True) is True
        assert override.is_active() is True and override.is_permanent is True

    def test_a_policy_that_denies_yolo_now_refuses_it_and_says_so(
        self, audits, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Through the facade, which reaches every owner holding the name.
        monkeypatch.setattr(so, "yolo_policy_permits", lambda: False)
        override = so.safety_override()
        assert override.restore_kept("dashboard", 1800, permanent=False) is False
        assert override.is_active() is False
        assert "safety_override:dropped_by_restart" in [c["operation"] for c in audits]

    def test_a_grant_the_boot_already_armed_is_left_alone(self, audits) -> None:
        override = so.safety_override()
        override.activate_declared()
        assert override.restore_kept("dashboard", 1800, permanent=False) is False
        assert override.is_declared is True

    def test_a_restored_grant_is_kept_again_by_the_next_restart(
        self, _isolated_breadcrumb: Path, audits
    ) -> None:
        override = so.safety_override()
        override.restore_kept("dashboard", 1800, permanent=False)
        # Written by this image, so read as a live grant here, and as a kept one
        # by the next process.
        record = _record(_isolated_breadcrumb)
        record["image"] = "a-previous-process-image"
        _isolated_breadcrumb.write_text(json.dumps(record), encoding="utf-8")
        so.reset_singleton()
        dropped = so.take_dropped_grant()
        assert dropped is not None and 1790 <= dropped.remaining_secs <= 1800

    def test_an_until_shutdown_grant_the_policy_now_forbids_is_not_restored(
        self, audits, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A new grant gets no untimed duration under this policy, so a kept one
        # does not come back untimed either.
        monkeypatch.setattr(so, "until_shutdown_permitted", lambda: False)
        override = so.safety_override()
        assert override.restore_kept("slack", 0, permanent=True) is False
        assert override.is_active() is False
        [dropped] = [c for c in audits if c["operation"] == "safety_override:dropped_by_restart"]
        assert "policy forbids until_shutdown" in dropped["resources"]

    def test_the_policy_cap_bounds_a_restored_timed_grant(
        self, audits, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(so, "yolo_ttl_cap", lambda: 600)
        override = so.safety_override()
        assert override.restore_kept("dashboard", 1800, permanent=False) is True
        assert override.remaining_secs() <= 600

    def test_a_restore_a_live_grant_skips_is_audited(self, audits) -> None:
        override = so.safety_override()
        override.activate_declared()
        assert override.restore_kept("dashboard", 1800, permanent=False) is False
        [dropped] = [c for c in audits if c["operation"] == "safety_override:dropped_by_restart"]
        assert "a grant is already live" in dropped["resources"]


def test_a_revoke_is_on_disk_when_it_returns(
    _isolated_breadcrumb: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart keeps a recorded grant, so a revoke still queued at a crash would
    bring it back. ``deactivate`` waits for its publish; here the worker is slow."""
    import time

    from kiro_crew import safety_override_breadcrumb as bc

    override = so.safety_override()
    override.activate("dashboard", ttl=1800)
    assert _published(_isolated_breadcrumb)
    threads: list[threading.Thread] = []

    def slow_enqueue(job) -> None:
        with bc._breadcrumb_pending_lock:
            bc._breadcrumb_pending += 1
            bc._breadcrumb_idle.clear()

        def run() -> None:
            time.sleep(0.2)
            try:
                job()
            finally:
                with bc._breadcrumb_pending_lock:
                    bc._breadcrumb_pending -= 1
                    if bc._breadcrumb_pending <= 0:
                        bc._breadcrumb_pending = 0
                        bc._breadcrumb_idle.set()

        thread = threading.Thread(target=run)
        threads.append(thread)
        thread.start()

    monkeypatch.setattr(so, "_enqueue_breadcrumb", slow_enqueue)
    try:
        override.deactivate("dashboard")
        assert not _published(_isolated_breadcrumb)
    finally:
        for thread in threads:
            thread.join(5)


def test_a_headless_gateway_reads_the_stop_then_restores_the_grant() -> None:
    """``--slack-only`` never runs ``start_dashboard``, and Slack YOLO is its main
    use: its boot reads the explicit-stop verdict, then restores, before it serves."""
    import inspect

    from kiro_crew.dashboard.server_runtime import api_entrypoint

    source = inspect.getsource(api_entrypoint.start_api_server)
    take = source.index("kept_trust.take_owner_stop")
    restore = source.index("await _notify_restart_dropped_grant(state)")
    assert take < restore < source.index("web.Application(")
