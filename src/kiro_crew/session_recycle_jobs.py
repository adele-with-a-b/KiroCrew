"""What a recycle did to the processes its session's commands had running.

A session whose agent launched background work is recycled once that launch is
older than the hold (:mod:`kiro_crew.session_background_hold`), or at once when its
tree passes the hard memory ceiling. The launch record names the work, never a
process, so from it alone a recycle notice can only say the work "may" have been
stopped. This module is how the notice says what happened, by pid.

It keeps no record of its own. Before the reset it reads the two the kill path
already has: the runtime's descendants, identity-pinned
(``session_pid._provider_descendant_records``: the spawn-time snapshot merged with a
live walk), and the members of the runtime's session that its tree does not reach
(``session_pid.find_session_leftovers``). After the reset it reads each start
identity again: the same identity is a process still running, anything else is one
that was stopped. Nothing here signals a process, not even to probe it.

Of the descendants, only those outside the runtime's own process group are listed.
The group holds the harness and the servers it started, which the group signal ends
together; a command the harness ran sits in a group of its own.

A job that left both the tree and the session before the read (``nohup`` from a
command that has ended, ``setsid``) is in neither record, so the notice says it is
not tracked and how to check it, instead of guessing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew import session_pid
from kiro_crew.session_cleanup_leftovers import NOTICE_MAX_LISTED

if TYPE_CHECKING:
    from kiro_crew.session_cleanup import SessionCleanup

#: How long a signalled descendant gets to leave the process table before it is
#: read as still running: the kill waits for the runtime's root, not for each
#: descendant it signals.
SETTLE_SECS = 0.5

#: The clause for a read that could not be made (no runtime pid, Windows, an
#: unreadable process table).
FATE_UNREADABLE = (
    "what the recycle stopped could not be read on this host: "
    "check a job by its pid or its exit file"
)

#: Closes every readable notice: the one kind of job neither record holds.
_UNTRACKED = (
    "a job that had detached from its command (nohup, setsid) is not tracked: "
    "check it by its pid or its exit file"
)


class Job(NamedTuple):
    """One process a session's commands had running when it was recycled."""

    pid: int
    start_id: str
    name: str
    #: True for a descendant of the runtime, which the recycle signals; False for a
    #: session member outside its tree.
    in_tree: bool


class RuntimeJobs(NamedTuple):
    """What :func:`read_jobs` found under one runtime root, before its reset."""

    root: int
    root_start: str
    jobs: list[Job]


class Fate(NamedTuple):
    """What :func:`read_fate` found after the reset."""

    root_gone: bool
    stopped: list[Job]
    running: list[Job]


def read_jobs(provider: object, root: int) -> RuntimeJobs | None:
    """The processes the commands of runtime *root* have running right now.

    None when they cannot be read: Windows has neither process groups nor
    sessions, and a root with no readable identity or group cannot anchor either
    record. Blocking: executor thread, never the loop.
    """
    compat = session_pid.platform_compat
    if compat.IS_WINDOWS or type(root) is not int or root <= 1:
        return None
    root_start = compat.get_process_start_id(root)
    group = compat.pgroup_of(root)
    if root_start is None or group is None:
        return None
    found: dict[int, tuple[str, bool]] = {}
    records = session_pid._provider_descendant_records(
        provider, root, recorded_start=root_start, gated=True
    )
    for pid, (start_id, _basename) in records.items():
        if (
            start_id
            and compat.pgroup_of(pid) != group
            and compat.get_process_start_id(pid) == start_id
        ):
            found[pid] = (start_id, True)
    leftovers = session_pid.find_session_leftovers(root)
    for item in leftovers.items if leftovers is not None else ():
        found.setdefault(item.pid, (item.start_id, False))
    jobs = [
        Job(pid, start_id, session_pid._process_label(pid), in_tree)
        for pid, (start_id, in_tree) in sorted(found.items())
    ]
    return RuntimeJobs(root, root_start, jobs)


def _same_process(pid: int, start_id: str) -> bool:
    compat = session_pid.platform_compat
    return compat.get_process_start_id(pid) == start_id and compat.pid_is_zombie(pid) is not True


def read_fate(before: RuntimeJobs, settle: Callable[[float], None] = time.sleep) -> Fate:
    """Which of *before*'s processes are still the process they were.

    A descendant read as running although its runtime is gone was signalled a
    moment ago, so it is read once more after *settle*. Blocking: executor thread.
    """
    root_gone = not _same_process(before.root, before.root_start)
    running = [job for job in before.jobs if _same_process(job.pid, job.start_id)]
    if root_gone and any(job.in_tree for job in running):
        settle(SETTLE_SECS)
        running = [job for job in running if _same_process(job.pid, job.start_id)]
    stopped = [job for job in before.jobs if job not in running]
    return Fate(root_gone, stopped, running)


def _listed(jobs: list[Job]) -> str:
    text = ", ".join(f"pid {job.pid} {job.name}" for job in jobs[:NOTICE_MAX_LISTED])
    extra = len(jobs) - NOTICE_MAX_LISTED
    return f"{text} and {extra} more" if extra > 0 else text


def fate_clause(before: RuntimeJobs, fate: Fate) -> str:
    """The notice clause saying what the recycle of *before*'s runtime stopped."""
    if fate.root_gone:
        parts = [
            f"its runtime (pid {before.root}) was ended, and a workflow or task "
            "running inside it with it"
        ]
    else:
        parts = [f"its runtime (pid {before.root}) is still running"]
    if fate.stopped:
        parts.append(f"stopped{' with it' if fate.root_gone else ''}: {_listed(fate.stopped)}")
    if fate.running:
        parts.append(f"still running: {_listed(fate.running)}")
    parts.append(_UNTRACKED)
    return "; ".join(parts)


async def before_reset(cleanup: SessionCleanup, key: str, session: Any) -> RuntimeJobs | None:
    """Read *key*'s jobs ahead of its reset; None with no launch, or unreadable.

    Suspends, so the caller awaits it before its last synchronous guards.
    """
    if cleanup._harness_background_launch(session) is None:
        return None
    pid = cleanup._owner.get_pid(key)
    if pid is None:
        return None
    try:
        return await asyncio.get_running_loop().run_in_executor(
            cleanup._deps.get_maintenance_executor(),
            read_jobs,
            getattr(session, "provider", None),
            pid,
        )
    except Exception:
        cleanup._deps.logger.debug("Job read failed for session %s", key, exc_info=True)
        return None


async def after_reset(cleanup: SessionCleanup, key: str, before: RuntimeJobs | None) -> str:
    """``"; <clause>"`` for the recycle notice of *key*, read after its reset."""
    clause = FATE_UNREADABLE
    if before is not None:
        try:
            fate = await asyncio.get_running_loop().run_in_executor(
                cleanup._deps.get_maintenance_executor(), read_fate, before
            )
            clause = fate_clause(before, fate)
        except Exception:
            cleanup._deps.logger.debug("Job fate read failed for %s", key, exc_info=True)
    cleanup._deps.logger.warning("Recycle of %s: %s", key, clause)
    return f"; {clause}"
