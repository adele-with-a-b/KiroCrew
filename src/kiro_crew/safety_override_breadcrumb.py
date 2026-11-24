"""The kept YOLO grant: a record of the live ad-hoc grant, so an exit the user did not choose keeps it.

The rule is ``kiro_crew.kept_trust``'s (kirodotdev/KiroCrew#12275): an ad-hoc grant
(Slack, the dashboard picker, the API) stays on across every gateway exit the user
did not choose, with its original expiry, and an ``until_shutdown`` grant lasts
until the user stops Kiro Crew. This module writes, on one daemon worker, the
wall-clock deadline and source of the live grant to ``kept-trust/yolo.json``, and at
startup ``take_dropped_grant`` reads it once, deletes it, and returns a
``DroppedGrant`` for an ad-hoc grant with time left (or no expiry), which the boot
then restores (``SafetyOverride.restore_kept``). After an explicit stop
(``kept_trust.take_owner_stop``) there is no record to read. The record is a grant,
so it lives in the sealed ``kept-trust`` directory: an agent cannot write it.

:mod:`kiro_crew.safety_override` stays the import path and the patch surface for every
name here, and this module logs under that name. It imports only
:mod:`kiro_crew.safety_override_records`, never the facade.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import stat
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

# Logs under the facade's name: a record names the import path callers use.
logger = logging.getLogger("kiro_crew.safety_override")


@dataclass
class DroppedGrant:
    """An ad-hoc grant that was live when the process went down.

    Returned by :func:`take_dropped_grant` so the startup can restore it, or TELL
    the operator it is gone when policy now refuses it.
    """

    source: str
    remaining_secs: int
    #: An ``until_shutdown`` grant: no expiry, kept until the user stops Kiro Crew.
    permanent: bool = False


# ─── The kept grant ──────────────────────────────────────────────────────────
#
# A grant lives in memory, so every exit would end it. The rule
# (``kiro_crew.kept_trust``) is that an exit the user did not choose must not: an
# operator who grants six hours of auto-approval and has the gateway restarted
# under them (an install, a crash, a reboot) an hour later still has five hours.
#
# So the file below records the live grant. It holds the wall-clock deadline (the
# in-memory deadlines are ``time.monotonic()``, which means nothing to another
# process), the source, and whether the grant had an expiry at all. Startup reads
# it once, deletes it, and restores the grant, which the current policy ceiling
# still bounds (``SafetyOverride.restore_kept``). An explicit stop leaves the
# ``kept_trust`` owner-stop mark instead, and the startup restores nothing.
#
# It is a grant, so only the gateway may write it: it lives in the sealed
# ``kept-trust`` directory, read-only in the OS sandbox and write-fenced at the
# file-tool gate. Not signed: where the OS sandbox confines agents they cannot
# write it, and where none does (Windows) an agent can already write
# ``config.json``, so a signature would add no protection there.
#
# One record per data home is correct, not a shared-state hazard: the file lives
# under ``config_dir()``, which is the very directory ``gateway_lock`` holds an
# exclusive advisory flock on for a gateway's whole lifetime, and a second
# gateway on the same home is REFUSED at startup rather than allowed to race
# (see gateway_lock's module docstring -- the invariant exists because shared-home
# writers clobber ``sessions/*.jsonl``). So there is never a sibling gateway to
# consume this record out from under the one that wrote it.
_BREADCRUMB_FILE = "yolo.json"

#: Where an older build wrote the same record, at the agent-writable data-home root.
#: Deleted at startup, never read: a record an agent could write is not a grant.
_LEGACY_BREADCRUMB_FILE = "safety_override_last_grant.json"

#: The sources whose grant the startup arms again itself (``_apply_startup_yolo``):
#: the declared config grant and ``gateway --approval yolo``. Never restored from the
#: record, so a declaration the operator removed does not come back.
_REARMED_SOURCES = ("config", "cli")

#: Serializes breadcrumb I/O. Separate from the state lock ON PURPOSE: this
#: module's rule is that no I/O happens while the state lock is held (the SEL
#: writes obey it too), so ordering the file against concurrent transitions
#: needs its own lock plus the generation counter below -- not the state lock.
_breadcrumb_io_lock = threading.Lock()

#: Publishes run on ONE long-lived daemon worker, never inline. ``activate`` and
#: ``is_active`` are called from async request handlers (a Slack YOLO toggle, and
#: the approval-policy read on every dashboard turn), so a synchronous
#: ``atomic_write`` there would put filesystem latency on the gateway's event
#: loop -- and a slow or stalled filesystem would then stall gateway traffic and
#: the heartbeat with it. A queue rather than a thread per
#: transition so thread churn is bounded, and FIFO ordering reinforces the
#: generation guard.
_breadcrumb_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
_breadcrumb_worker: Optional[threading.Thread] = None
_breadcrumb_worker_lock = threading.Lock()
#: Set while nothing is queued AND nothing is mid-write. ``flush`` waits on this
#: rather than joining the queue: a write stalled on a hung filesystem would make
#: an unconditional ``join()`` wait forever, and the caller is a restart path, so
#: blocking it is worse than losing the record.
_breadcrumb_idle = threading.Event()
_breadcrumb_idle.set()
_breadcrumb_pending = 0
_breadcrumb_pending_lock = threading.Lock()

#: A record is ~120 bytes. Reading it unbounded let anything that can write into
#: the data home turn startup into a memory-exhaustion restart loop (found in
#: review), so an oversized file is discarded rather than parsed. Generous enough
#: that a future field cannot trip it.
_BREADCRUMB_MAX_BYTES = 4096

#: Identity of THIS process image, minted at import. Not the PID: both instrumented
#: restart paths end in ``os.execv``, which replaces the image but PRESERVES the
#: pid -- so a pid comparison would read the previous image's record as our own and
#: swallow the notice on exactly the restarts this feature exists for (found in
#: review). ``process_start_time`` is no better, since exec preserves that too. A
#: fresh import is the one thing an exec guarantees, so a value minted here is the
#: discriminator.
_IMAGE_TOKEN = uuid.uuid4().hex


def _breadcrumb_pump() -> None:
    global _breadcrumb_pending
    while True:
        job = _breadcrumb_queue.get()
        try:
            job()
        except Exception:
            logger.debug("safety override: breadcrumb publish failed", exc_info=True)
        finally:
            _breadcrumb_queue.task_done()
            with _breadcrumb_pending_lock:
                _breadcrumb_pending -= 1
                if _breadcrumb_pending <= 0:
                    _breadcrumb_pending = 0
                    _breadcrumb_idle.set()


def _enqueue_breadcrumb(job: Callable[[], None]) -> None:
    """Hand a publish to the worker, starting it on first use."""
    global _breadcrumb_worker, _breadcrumb_pending
    with _breadcrumb_worker_lock:
        if _breadcrumb_worker is None or not _breadcrumb_worker.is_alive():
            _breadcrumb_worker = threading.Thread(
                target=_breadcrumb_pump, name="kirocrew-safety-breadcrumb", daemon=True
            )
            _breadcrumb_worker.start()
    with _breadcrumb_pending_lock:
        _breadcrumb_pending += 1
        _breadcrumb_idle.clear()
    _breadcrumb_queue.put(job)


def flush_breadcrumb_writes(timeout: float = 5.0) -> bool:
    """Wait up to *timeout* for queued publishes to land; report whether they did.

    STRICTLY bounded, by construction rather than by a polling loop. Callers are
    restart paths: a flush that could wait forever on a stalled write would freeze
    a gateway that was trying to re-exec, which is a worse failure than losing the
    record it was trying to save (found in review). Also used by tests.
    """
    return _breadcrumb_idle.wait(max(0.0, timeout))


def _breadcrumb_path() -> Path:
    from kiro_crew import kept_trust

    # ``config_dir`` from this module's own globals, the facade's patch seam, so
    # not ``kept_trust.kept_path``.
    return config_dir() / kept_trust.KEPT_DIR / _BREADCRUMB_FILE


def _write_breadcrumb(*, path: Path, source: str, expires_at_wall: float, permanent: bool) -> None:
    """Record that a grant is live. Best-effort: never raises into the grant path.

    A failed write costs the operator a notice, never a grant, so it must not
    fail an activation -- and above all must not fail a DEACTIVATION, where
    raising would leave auto-approval on.

    *path* is supplied by the caller rather than resolved here, for the same
    reason ``expires_at_wall`` is computed at the transition: this runs on the
    worker thread, so resolving ``_breadcrumb_path()`` here would bind the file
    as it is WHENEVER THE WORKER GETS TO IT instead of as it was when the
    transition was made.
    """
    try:
        payload = json.dumps(
            {
                "version": 1,
                "source": source,
                # Wall clock, because the reader is a different process.
                "expires_at": expires_at_wall,
                "permanent": permanent,
                # Whose record this is. A reader in the SAME process image must not
                # treat it as a dropped grant -- that is what lets the read happen
                # anywhere in startup rather than having to run before this
                # process can write one of its own. Keyed on the
                # import-time nonce, NOT the pid: os.execv keeps the pid.
                "image": _IMAGE_TOKEN,
            }
        )
        if path.parent.is_symlink():
            logger.warning("safety override: not keeping the grant through a linked directory")
            return
        path.parent.mkdir(mode=0o700, exist_ok=True)
        # 0600: the record names the auto-approval posture and its deadline.
        atomic_write(path, payload, mode=0o600)
    except Exception:
        logger.debug("safety override: breadcrumb write failed", exc_info=True)


def _clear_breadcrumb(path: Path) -> None:
    """Drop the record. Best-effort, for the same reason the write is.

    Takes the path for the same reason the write does: a queued clear that
    resolved ``_breadcrumb_path()`` on the worker thread would unlink whatever
    the module names at that moment, not the file its transition was about.
    """
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.debug("safety override: breadcrumb clear failed", exc_info=True)


def take_dropped_grant() -> Optional[DroppedGrant]:
    """Consume the record; return the ad-hoc grant the previous process held.

    ``None`` after an explicit stop (``kept_trust.take_owner_stop``), which removed
    the record. Audited by the caller, which restores the grant or says it is gone.

    Serialized against the publisher: ``_consume_breadcrumb`` runs under
    ``_breadcrumb_io_lock`` from the open through the identity check to the
    clear, because a publish landing in the middle of that span would be
    UNLINKED by the clear -- deleting the record for a grant that is live right
    now and leaving the next restart with nothing to report (found in review).
    The audit below stays outside the lock: it is unrelated I/O.
    """
    from kiro_crew import kept_trust

    _clear_breadcrumb(config_dir() / _LEGACY_BREADCRUMB_FILE)
    if kept_trust.take_owner_stop():
        return None
    with _breadcrumb_io_lock:
        return _consume_breadcrumb()


def _consume_breadcrumb() -> Optional[DroppedGrant]:
    """Read and consume the record. Caller MUST hold ``_breadcrumb_io_lock``.

    Single-shot by construction: the record is deleted whatever the verdict, so
    one dropped grant cannot notify twice, and a stale record from an older
    install cannot notify forever.

    Returns ``None`` -- nothing to restore -- in three cases:

    * **No record.** No grant was live.
    * **A grant the startup arms itself** (:data:`_REARMED_SOURCES`): the
      DECLARED config grant and ``gateway --approval yolo`` come from config or
      argv on this very startup, and a declaration since removed must not return.
    * **The deadline has passed.** The grant would have expired on its own by now.

    An ``until_shutdown`` grant (``permanent``) is returned with no time: it lasts
    until the user stops Kiro Crew, and an exit they did not choose is not that.
    """
    path = _breadcrumb_path()
    # Opened by descriptor rather than read by name, because the SHAPE of the file
    # matters as much as its size. A FIFO planted at this path reports st_size 0,
    # so it sails through a size check and then blocks FOREVER on read -- which
    # would hang gateway initialization before readiness. So:
    # O_NONBLOCK so no open or read can wait on a writer, O_NOFOLLOW so a symlink
    # cannot redirect the read at something else, and an fstat that refuses
    # anything that is not a regular file.
    #
    # Neither flag exists on Windows, so an lstat check carries the symlink
    # refusal there: without it the link is FOLLOWED and its target parses
    # normally (caught by the Windows CI shard). O_NOFOLLOW stays as the
    # race-free guard where it exists -- lstat-then-open is TOCTOU, which is
    # tolerable only because a forged record confers no authority.
    _nonblock = getattr(os, "O_NONBLOCK", 0)
    _nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        if path.parent.is_symlink():
            logger.warning("safety override: not reading a kept grant through a linked directory")
            return None
        if path.is_symlink():
            logger.warning("safety override: discarding a restart record that is a link")
            _clear_breadcrumb(path)
            return None
    except OSError:
        return None

    try:
        fd = os.open(path, os.O_RDONLY | _nonblock | _nofollow)
    except FileNotFoundError:
        return None
    except OSError:
        # ELOOP from O_NOFOLLOW lands here: a symlink IS a refusal, not an error
        # to investigate.
        logger.debug("safety override: breadcrumb could not be opened", exc_info=True)
        _clear_breadcrumb(path)
        return None

    # The verdict is decided while the descriptor is open, but every unlink
    # happens AFTER it is closed: Windows refuses to remove an open file, so
    # clearing here left an oversized or malformed record in place to be
    # rediscovered on every subsequent startup (caught by the Windows CI shard).
    raw: Optional[str] = None
    discard = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            logger.warning("safety override: discarding a restart record that is not a file")
            discard = True
        elif info.st_size > _BREADCRUMB_MAX_BYTES:
            logger.warning(
                "safety override: discarding an oversized restart record (%d bytes)",
                info.st_size,
            )
            discard = True
        else:
            # Capped at the bound regardless of what fstat said, so a file that
            # grew between the stat and the read cannot exceed it either.
            raw = os.read(fd, _BREADCRUMB_MAX_BYTES).decode("utf-8", errors="replace")
    except Exception:
        logger.debug("safety override: breadcrumb read failed", exc_info=True)
        discard = True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    if discard or raw is None:
        _clear_breadcrumb(path)
        return None

    try:
        record = json.loads(raw)
        if not isinstance(record, dict):
            _clear_breadcrumb(path)
            return None
        writer_image = str(record.get("image") or "")
        permanent = bool(record.get("permanent"))
        expires_at = float(record.get("expires_at") or 0.0)
        source = str(record.get("source") or "")
    except Exception:
        logger.debug("safety override: breadcrumb unreadable", exc_info=True)
        _clear_breadcrumb(path)
        return None

    # THIS process image's own live grant. Left untouched -- consuming it would
    # delete the record for a grant that is in force, so a later restart would
    # have nothing to report. This is also what frees the read from having to run
    # before the startup grant is applied. Compared on the
    # import-time nonce, because os.execv preserves the pid and every restart this
    # feature instruments goes through exec.
    if writer_image and writer_image == _IMAGE_TOKEN:
        return None

    # From here the record belongs to a previous process, so it is consumed
    # whatever the verdict: one dropped grant cannot notify twice, and a record
    # left by an older install cannot notify forever.
    _clear_breadcrumb(path)

    if source in _REARMED_SOURCES or not source:
        return None
    if permanent:
        return DroppedGrant(source=source, remaining_secs=0, permanent=True)

    remaining = int(expires_at - datetime.now(tz=timezone.utc).timestamp())
    if remaining <= 0:
        return None

    dropped = DroppedGrant(
        source=source,
        remaining_secs=remaining,
    )
    # The audit is emitted by the CALLER, outside the lock -- see
    # ``take_dropped_grant``. Nothing unrelated to the file belongs in this span.
    return dropped
