"""Memory- and contention-aware worker budget for pytest-xdist ``-n auto``.

A plain module rather than a conftest, imported by the ROOTDIR ``conftest.py``,
which owns the two hook names. That split is what makes the budget reach all
testpath: living in ``test/conftest.py`` it was never loaded for the
in-package app suites, nor for any directory targeted explicitly, so those
invocations resolved
``-n auto`` to the raw core count and ignored every knob -- and nothing said so,
because an absent budget looks exactly like a budget that decided not to clamp.

It is a separate module, and not simply moved INTO the rootdir conftest, because
the module name ``conftest`` is ambiguous: with pytest's ``prepend`` import mode
``test/`` precedes the repository root on ``sys.path``, so ``import conftest``
from a test in ``test/`` binds ``test/conftest.py`` and can never reach the
rootdir file. A distinct name is reachable from both, and resolves to ONE module
object -- which matters, because the held-slot file descriptors are module state
and a second copy would release nothing.

Why a budget at all: two worktrees each running ``pytest -n auto`` on the same
10-core host took 10 workers EACH, swapped the machine to a load average of ~590,
and completed zero tests in 21 minutes. Memory, not cores, is the binding
constraint -- a worker costs ~1.5 GiB collecting the suite before it runs
anything, and grows to ~4.5 GiB over a long run -- so a machine that cannot back one worker per core must be told.
"""

from __future__ import annotations

import os
import pathlib
import socket
import warnings

from kiro_crew import platform_compat

_MAX_WORKERS_ENV = "KIROCREW_MAX_TEST_WORKERS"
_SLOT_DIR_ENV = "KIROCREW_TEST_SLOT_DIR"
#: Set to ``1`` to run an explicit ``-n N`` WITHOUT claiming host slots. The one
#: documented way past the shared pool, for someone who owns the whole host and
#: knows it; everything else that asks for N workers waits its turn in the pool.
_EXPLICIT_BYPASS_ENV = "KIROCREW_TEST_SLOTS_BYPASS"
#: xdist's own ceiling for ``-n auto``. Read here because this hook replaces
#: xdist's default implementation rather than running alongside it.
_XDIST_ENV_CAP = "PYTEST_XDIST_AUTO_NUM_WORKERS"
_DEFAULT_WORKER_CAP = 32
_GIB = 1024**3
# The LIVE memory readings are taken in MiB, because in GiB anything under 1 GiB
# truncates to 0 -- which is the same value they use for "could not determine", and an
# unknown reading is deliberately skipped. See _host_available_mib.
_MIB = 1024**2
# Headroom to reserve per worker.
#
# A worker's cost has two parts. The FLOOR is collection: every xdist worker
# independently collects every testpath -- 106,491 items -- for ~1,499 MiB of
# VmHWM before it runs a single test, 99% of it private, so there is no page
# sharing to exploit. On top of that a worker GROWS with the tests it runs, and
# that growth does not saturate.
#
# Both halves are REMEASURED, and the suite roughly doubled underneath the
# previous numbers (~57,000 items / ~747 MiB), which is why this constant moved
# from 2 to 3. Measured on a 32-core Linux host, five full runs at -n 12,
# sampling VmRSS after every test in every worker (60 worker-runs):
#
#     collection floor (--collect-only -n0):  1,499 MiB
#     worker peak at -n 12:  min 1,879  median 2,042  max 2,771 MiB
#     tests per worker:      7,349 - 10,429
#
# The max is not noise: the same worker slot hit 2,771 MiB in ALL FIVE runs,
# because the `tree_scan_*` xdist groups that parse every module under src/
# land together and one of them alone adds ~1.3 GiB of retained source text.
# The median-implied growth is ~60 MiB per 1,000 tests, not the ~25 previously
# recorded.
#
# The consequence is the whole reason this constant exists and must not be
# lowered: **per-worker footprint is inversely proportional to the worker
# count.** Fewer workers means more tests each, and the growth is per-test, so
# the projection is 1,499 + (106,491 / N) * 0.060 MiB:
#
#     N=32 -> 1,699 MiB   N=12 -> 2,031 MiB   N=8 -> 2,298 MiB   N=2 -> 4,694 MiB
#
# The N=12 projection lands within 11 MiB of the measured median, which is what
# makes the formula worth quoting at all -- but N=2 and N=1 are EXTRAPOLATIONS
# this measurement does not cover, and they are the cases where the budget
# actually binds. Treat them as a floor on the answer, not the answer.
#
# So: do NOT lower this on the strength of a measurement taken at high
# parallelism. The number that matters is the footprint at the worker count the
# budget is about to grant, not the one your dev host runs at. A divisor sized on
# the old -n8 figure would grant 4 workers on an 8 GiB laptop; those 4 would then
# want ~9 GiB between them and swap the machine -- which is the incident this
# budget exists to prevent, reintroduced by "optimizing" it.
#
# The constant is 5, not 3, because a later host OOM measured the real thing. On a
# 184 GiB / 48-core Linux host with no swap, ~40 workers filled RAM and the kernel
# OOM killer fired eight times. Its process table showed each worker at ~4.5 GiB of
# anonymous RSS -- well past the 2,771 MiB peak above, because those workers ran
# long suites and grew the way the formula predicts. At 3 GiB the budget granted
# ~60 workers on that host; 5 GiB grants ~36, which those workers fit in.
#
# Known limit, stated rather than hidden: at low N the projection can still exceed
# 5 GiB, so the single-worker floor can outgrow its own reservation. Nothing here
# can fix that -- one worker is already the minimum -- and it is the case where
# the run is slow but survivable rather than parallel and fatal.
#
# This sizes for EXPECTED footprint: it cannot save a host from a genuinely
# leaking worker, a separate bug.
_GIB_PER_WORKER = 5
# Headroom to reserve per worker against the LIVE availability reading.
#
# Deliberately the same as the static divisor above, because both describe the
# same worker. The two readings differ in KIND -- total RAM is a worst-case bound
# that never moves, availability is already the current headroom -- but that
# argues about how much margin to add on top, and at 5 GiB there is little: it is
# ~1.1x the ~4.5 GiB per-worker RSS the host OOM measured. Anything less admits
# more workers than the host has memory for at the moment it is asked.
_GIB_PER_WORKER_AVAILABLE = 5

# The two reservations above are REMEASURED for Linux (the host this suite's
# agents run on), but a worker's footprint is PLATFORM-DEPENDENT and the Linux
# number is fatally low elsewhere. A long-suite worker was measured at ~4.5 GiB
# on Linux but 14.9-16.1 GiB on macOS (issue #10061), where four workers granted
# by ``-n auto`` reserved 62 GiB on a 36 GiB host and the kernel jetsam-killed
# it. A single constant cannot be right for both: 5 GiB is fatally low for a
# macOS worker on a run over the tree, and 16 would clamp a healthy Linux host
# to a handful of workers for no reason. Still constants, not config keys:
# ``xdist_auto_cap`` remains the single operator knob.
#
# On macOS the reservation also follows the RUN, because what a worker holds
# scales with what it collects. #10061 was the FULL suite: every worker collects
# every testpath (283,605 items at the last re-measure, ~3 GiB of item tree
# before a test runs) and then runs a quarter of it. A related-set batch from
# ``scripts/local-gate.py`` names at most a few hundred test files -- its argv
# is capped at ``ARGV_CHAR_LIMIT`` characters. Measured 2026-11-22 on a 48 GiB /
# 18-core Mac, one 651-file batch (26,375 tests): at ``-n 4`` the four workers
# peaked at 1,477-1,923 MiB RSS and the run took 765 s; at ``-n 1`` the one
# worker running all of it peaked at 4,156 MiB and took 2,219 s. Reserving
# 16 GiB per worker had clamped every such gate on that Mac to one worker of 18
# with the CPU 73% idle.
#
# So a run that names at most ``_SCOPED_MAX_FILES`` existing test files
# (:func:`run_is_scoped`) reserves ``_MACOS_GIB_PER_SCOPED_WORKER``, about twice
# the ``-n 4`` per-worker peak. shortcut: a lone worker running a whole batch
# holds about 1x this (4,156 MiB), because a worker's share shrinks as workers
# are added; raise it if one-worker batch runs start to swap. Anything else -- no arguments (the
# testpaths), a directory, a module path, more files than that -- keeps the
# 16 GiB #10061 measured. Identity is positive: a run that cannot be shown to be
# a small file list gets the full-suite reservation.
#
# Both shapes share ONE slot pool, so the pool is counted in the small unit:
# one slot is ``_MACOS_GIB_PER_SCOPED_WORKER`` of RAM and a full-suite worker
# holds ``_slots_per_worker(False)`` of them. That keeps the aggregate
# ``slots x unit <= memory`` true across a mix of runs, and a full run on the
# reporter's 36 GiB host still gets at most 9 // 4 = 2 workers, never four.
_MACOS_GIB_PER_WORKER = 16
_MACOS_GIB_PER_SCOPED_WORKER = 4
#: The most test files a run may name and still count as scoped. Every batch
#: ``local-gate.py`` forms stays under it (the largest seen was 658 files), and
#: the measurement above was a 651-file batch; a longer list is unmeasured, so
#: it takes the full-suite reservation.
_SCOPED_MAX_FILES = 700


def _gib_per_worker() -> int:
    """GiB one slot reserves against the STATIC (total-RAM / cgroup) readings.

    A worker holds :func:`_slots_per_worker` slots. On Linux and Windows that is
    one, so a slot is a worker; on macOS a slot is a scoped worker.
    """
    return _MACOS_GIB_PER_SCOPED_WORKER if platform_compat.IS_MACOS else _GIB_PER_WORKER


def _gib_per_worker_available() -> int:
    """GiB one slot reserves against the LIVE (currently-available) reading."""
    return _MACOS_GIB_PER_SCOPED_WORKER if platform_compat.IS_MACOS else _GIB_PER_WORKER_AVAILABLE


def _slots_per_worker(scoped: bool) -> int:
    """How many slots one worker of this run holds: 1, except a macOS run over the tree."""
    if scoped or not platform_compat.IS_MACOS:
        return 1
    return -(-_MACOS_GIB_PER_WORKER // _gib_per_worker())  # ceiling division


def run_is_scoped(config: object | None) -> bool:
    """True when the run names at most ``_SCOPED_MAX_FILES`` test files and nothing else.

    Every command-line argument must be an existing FILE (a ``file::test`` node id
    counts as its file). No arguments means pytest fell back to ``testpaths``, the
    full suite; a directory, a ``--pyargs`` module or a missing path cannot be sized
    from here. All of those answer False, which is the full-suite reservation: the
    failure direction is a slower run, never an over-granted one.
    """
    args = getattr(config, "args", None)
    source = getattr(getattr(config, "args_source", None), "name", None)
    if not args or source != "ARGS":
        return False
    invocation = getattr(getattr(config, "invocation_params", None), "dir", None)
    base = pathlib.Path(invocation) if invocation else pathlib.Path.cwd()
    files: set[pathlib.Path] = set()
    for arg in args:
        path = base / str(arg).split("::", 1)[0]
        try:
            if not path.is_file():
                return False
            files.add(path.resolve())
        except (OSError, RuntimeError, ValueError):
            return False
        if len(files) > _SCOPED_MAX_FILES:
            return False
    return True


# Lock files this process holds for its whole lifetime -- the fds MUST stay open,
# because the lock lives exactly as long as the fd does.
_held_slots: list[int] = []


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _slot_root() -> pathlib.Path:
    override = os.environ.get(_SLOT_DIR_ENV)
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".cache" / "kirocrew" / "test-slots"


def _host_key() -> str:
    """Filesystem-safe identity for this machine, as a single path segment."""
    raw = socket.gethostname() or ""
    safe = "".join(ch if (ch.isalnum() or ch in "-._") else "_" for ch in raw)[:64]
    return safe.strip(".") or "unknown-host"


def _slot_dir() -> pathlib.Path:
    """Where concurrent pytest runs ON THIS HOST contend for worker capacity.

    Deliberately host-global and *not* derived from ``KIROCREW_HOME``: the point
    is that two worktrees -- which have different homes and know nothing about
    each other -- still coordinate over the one thing they truly share, the
    machine's cores and RAM.

    Scoped by hostname because ``~/.cache`` is frequently a network home shared
    by many machines, whose contention is not ours.
    """
    return _slot_root() / _host_key()


def _slot_path(slot_dir: pathlib.Path, index: int) -> pathlib.Path:
    return slot_dir / f"worker-{index:03d}.lock"


def _host_total_gib() -> int:
    """Total physical RAM in GiB, or 0 when it cannot be determined.

    Routed through ``platform_compat`` rather than reading ``os.sysconf`` directly,
    because that function does not exist on Windows -- so a direct read returns 0
    there, which is this budget's "unknown", which :func:`_bounded_by` SKIPS. The
    result was that Windows had no memory bound at all, static or live.

    GiB, not MiB, because a machine with under 1 GiB of RAM in total is not a
    configuration this suite runs on, and the unit is pinned by existing tests. The
    LIVE reading is in MiB for the opposite reason -- see :func:`_host_available_mib`.
    """
    return platform_compat.host_total_mib() // 1024


def _cgroup_limit_files() -> list[str]:
    """Every memory-ceiling file that bounds THIS process, tightest-first-ish.

    The ceiling lives at the process's OWN cgroup, not at the root of the hierarchy.
    Under cgroup v2 the root has no ``memory.max`` file at all, so reading
    ``/sys/fs/cgroup/memory.max`` finds something only where a cgroup NAMESPACE has
    remapped the container's own cgroup onto the mount root -- the docker/podman
    default, but not what a systemd slice with ``MemoryMax=``, a ``cgroupns=host``
    Kubernetes pod, or LXC gives you. There the limit is at
    ``/sys/fs/cgroup/<relative path>/memory.max``, and a root-only read returns nothing,
    which is indistinguishable from "no limit".

    A cgroup is bounded by the tightest limit anywhere on its ancestor chain, so the own
    cgroup and every ancestor are candidates. The bare root paths stay LAST so a
    namespaced container -- and a host where ``/proc/self/cgroup`` cannot be read at
    all -- keeps the reading it would otherwise have had.
    """
    files: list[str] = []
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        controllers, relative = fields[1], fields[2]
        if not controllers:  # v2 spells its single entry "0::<path>"
            base, leaf = "/sys/fs/cgroup", "memory.max"
        elif "memory" in controllers.split(","):  # v1: "<id>:<controllers>:<path>"
            base, leaf = "/sys/fs/cgroup/memory", "memory.limit_in_bytes"
        else:
            continue
        parts = [part for part in relative.split("/") if part]
        while parts:
            files.append("/".join([base, *parts, leaf]))
            parts.pop()
    files.append("/sys/fs/cgroup/memory.max")
    files.append("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    return files


def _cgroup_limit_mib() -> int:
    """The cgroup's memory ceiling in MiB, or 0 when there is none.

    ``SC_PHYS_PAGES`` reports the MACHINE's RAM, which is the wrong number inside a
    container: a 2-CPU/8 GiB CI container on a 256 GiB host reads 256 GiB and sizes its
    worker budget against memory it will be OOM-killed for touching.

    Takes the TIGHTEST limit found on the process's cgroup chain (see
    :func:`_cgroup_limit_files`), not the first one: an inner cgroup can be looser than
    an ancestor, and the ancestor still binds.

    MiB, not GiB, for the reason spelled out on :func:`_host_available_mib`: in GiB a
    512 MiB container ceiling truncates to ``0``, which is this function's own "there is
    no limit" answer -- so the tightest ceiling of all would be read as no ceiling.

    ``max`` is cgroup v2's spelling for "no limit". v1 uses a very large sentinel
    instead, which no ``min()`` will ever pick, so it needs no special case.
    """
    tightest = 0
    for path in _cgroup_limit_files():
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if not raw or raw == "max":
            continue
        try:
            mib = int(raw) // _MIB
        except ValueError:
            continue
        if mib > 0 and (tightest == 0 or mib < tightest):
            tightest = mib
    return tightest


def _host_available_mib() -> int:
    """RAM in MiB that is actually free for a new worker, or 0 when unknown.

    Bounding on this in ADDITION to total RAM is what makes the budget protective
    rather than decorative. The flock slots below already stop two pytest runs from
    oversubscribing each other, but they are blind to memory the host is using for
    anything else -- a build, a browser, a running gateway. Sizing 32 workers against
    total RAM on a machine with 2 GiB genuinely free is how a run starts swapping and
    then makes no progress at all, which is the incident the whole budget exists to
    prevent.

    Delegates to :func:`platform_compat.host_available_mib`, which answers on Linux,
    macOS and Windows. That breadth is the whole point of routing through it: a
    reading that only Linux can take makes this bound an identity function on the
    developer laptops the budget most needs to protect, and it fails silently --
    ``0`` means "unknown", :func:`_bounded_by` skips an unknown reading, and the run
    proceeds at the unbounded ceiling with nothing in the log.

    Kept as a named indirection rather than inlined at the call site because the
    budget's tests monkeypatch it to pin a host, and because it is the seam where a
    reading that turns out to be wrong for one platform can be corrected.
    """
    return platform_compat.host_available_mib()


def _bounded_by(limit: int, readings: tuple[tuple[int, int], ...]) -> int:
    """*limit*, reduced by each ``(MiB, GiB-per-worker)`` reading that is available.

    A reading of 0 means "could not determine" and is SKIPPED rather than treated as
    zero memory. That direction matters more than it looks: reading it as zero would
    collapse the run to a single worker on any platform without that reading -- macOS
    and Windows have no ``/proc/meminfo`` and no ``/sys/fs/cgroup`` -- and a run that
    silently drops to one worker looks like a hang, not like a bug.

    Which is exactly why the readings that can be genuinely SMALL are in MiB: in GiB a
    sub-1-GiB reading truncates to 0 and would be discarded as unknown, so the bound
    would vanish on the starved host it exists to protect.
    """
    for mib, per_worker_gib in readings:
        if mib > 0:
            limit = min(limit, max(1, mib // (per_worker_gib * 1024)))
    return max(1, limit)


def _static_memory_bounded_capacity(cores: int) -> int:
    """*cores*, reduced by the memory readings that are CONSTANT for the machine.

    Total RAM and the cgroup ceiling only. Both are properties of the host, so a number
    derived from them is stable across runs -- which is what makes it safe to use as the
    shared slot RANGE below. Sharing a range only works if every run computes the same
    one; a range that moves between runs is not a namespace, it is a race.

    This is also what keeps the memory budget genuinely SHARED rather than per-run. On a
    64-core / 32 GiB host the static bound is 16 workers, so there are 16 slots in
    total: a first run takes them all, and a second gets its floor of one. Put the same
    bound only on the per-run cap and both runs would take 16 each -- 32 workers against
    a 16-worker memory budget, which is the swapping incident the budget exists to
    prevent, reached from the opposite direction.

    Total RAM stays in GiB because a machine with under 1 GiB of RAM in total is not a
    configuration this suite runs on, and its unit is pinned by existing tests.

    The answer is in SLOTS (see :func:`_slots_per_worker`), so the core bound is
    scaled by the most slots one worker can hold: a core count is a bound on
    workers, and a full-suite macOS worker holds several slots.
    """
    return _bounded_by(
        cores * _slots_per_worker(False),
        (
            (_host_total_gib() * 1024, _gib_per_worker()),
            (_cgroup_limit_mib(), _gib_per_worker()),
        ),
    )


def _live_memory_bounded_cap(cap: int, slots_per_worker: int = 1) -> int:
    """*cap*, reduced by what is free on the host RIGHT NOW.

    Deliberately applied to the per-run cap and NOT to the shared slot range. The
    reading is transient, and slots fill from index 0 upward, so a range shortened by a
    momentary dip excludes precisely the slots an earlier run left free -- collapsing a
    later run to one worker while most of the machine sits idle. A cap is the right
    place for a transient reading: it throttles THIS run without reshaping the namespace
    every other run has to agree on.

    *cap* is in workers, and one worker of this run reserves *slots_per_worker* slots.
    """
    reading = (_host_available_mib(), _gib_per_worker_available() * slots_per_worker)
    return _bounded_by(cap, (reading,))


def _claim_worker_slots(
    capacity: int, cap: int, shared: int | None = None, slots_per_worker: int = 1
) -> int:
    """Take slots for up to ``cap`` workers out of the host's ``capacity`` and HOLD them.

    Each worker holds ``slots_per_worker`` slots, so ``cap`` is in workers and
    ``capacity`` and ``shared`` are in slots. Slots that do not make up a whole
    worker are released again, except at the one-worker floor.

    With ``shared``, slots from index ``shared`` on are taken only when this run got
    every slot below it, so a range wider than the shared one never adds workers
    beside another run.

    ``capacity`` is how many slots the HOST has -- its core count, a constant, never a
    transient memory reading -- and is the range probed; ``cap`` is the most any single
    run may take. Keeping these separate matters on a large host: with 64 cores and a
    cap of 32, a first run takes slots 0-31 and a second still finds 32-63 free and gets
    its full 32. Probing only ``cap`` slots would have collapsed that second run to one
    worker while half the machine sat idle.

    Each slot is an advisory lock on its own file, acquired non-blocking and
    never released until the process exits. That is the whole design: the kernel
    owns the lease, so capacity returns automatically when a run ends --
    including a run that is orphaned or terminated outright, which is exactly
    the state that caused the incident this budget exists to prevent.

    This deliberately replaces an earlier design where runs wrote reservation
    FILES describing themselves. Files outlive their owners, so that version
    needed PID-liveness probing, a staleness backstop, ownership proof against
    look-alike files, and boot/suspend forensics to decide when a reservation
    was defunct -- and every one of those cleanup paths produced a real bug. A
    held lock needs none of it.

    Returns the number of workers the held slots back, at least 1: a run arriving
    at a genuinely full host proceeds single-worker rather than stalling.
    """
    per = max(1, slots_per_worker)
    fail_open = min(max(1, capacity // per), cap)
    if _held_slots:
        # Already claimed in this process. Re-locking would fail: flock treats
        # two fds on one file as independent even within the same process, so a
        # second pass would take nothing and collapse the run to one worker.
        return max(1, len(_held_slots) // per)
    try:
        # Resolution itself must be inside the guard: Path.home() raises
        # RuntimeError when the home directory cannot be determined, and
        # gethostname() can raise OSError. Neither may break pytest startup.
        root = _slot_root()
        slot_dir = _slot_dir()
        # Refuse a symlink at either level: the root is caller-supplied via
        # KIROCREW_TEST_SLOT_DIR and could redirect our writes.
        if root.exists() and root.is_symlink():
            return fail_open
        slot_dir.mkdir(parents=True, exist_ok=True)
        if slot_dir.is_symlink() or not slot_dir.is_dir():
            return fail_open
    except (OSError, RuntimeError, ValueError):
        return fail_open  # fail open to the unbudgeted ceiling

    taken = 0
    for index in range(capacity):
        if taken >= cap * per or (index == shared and taken < index):
            break
        try:
            fd = os.open(str(_slot_path(slot_dir, index)), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            # The directory exists but this run cannot create a slot file in it: a
            # read-only bind mount, an exhausted quota, a leftover dir owned by another
            # account. `mkdir(exist_ok=True)` above does NOT catch that -- Linux reports
            # EEXIST before it checks write permission -- so this is where it surfaces.
            #
            # Fall through to the one-worker floor rather than failing open to the
            # unbudgeted ceiling. Fail-open is the wrong direction HERE specifically
            # because the failure is not local: if this run cannot take a lock then
            # neither can a concurrent one, so both would receive the full cap with no
            # coordination at all -- which is precisely the oversubscription this budget
            # exists to prevent (two runs, ten workers each, load average ~590, zero tests
            # completing in 21 minutes). One worker is slow; two unbudgeted runs make no
            # progress.
            #
            # Say so, though. Silently dropping to one worker is a suite that takes an
            # hour for a reason nobody can see, and the fix -- point
            # KIROCREW_TEST_SLOT_DIR somewhere writable -- is only obvious once the cause
            # is named.
            warnings.warn(
                "xdist worker budget: cannot create a slot file under "
                f"{slot_dir}, so this run falls back to a single worker. Point "
                f"{_SLOT_DIR_ENV} at a writable directory to restore parallelism.",
                stacklevel=2,
            )
            break
        if platform_compat.try_acquire_lock(fd, exclusive=True):
            _held_slots.append(fd)  # keep the fd -- closing it drops the lock
            taken += 1
        else:
            os.close(fd)
    workers = taken // per
    if workers:
        while len(_held_slots) > workers * per:
            _release_one(_held_slots.pop())
    return max(1, workers)


def _warn_if_clamped(resolved: int, cap: int, unbudgeted: int, slots_per_worker: int = 1) -> None:
    """Say why parallelism is lower than the core count, if it is.

    A run that quietly drops from 10 workers to 1 is indistinguishable from a hang:
    the suite takes forty minutes, nothing explains it, and the two fixes -- free
    some memory, or narrow the run -- are only obvious once the cause is named. So
    the budget explains itself whenever it binds.

    Two distinct causes, because the remedies differ: memory says the machine
    cannot back more workers right now, contention says another run holds them and
    capacity comes back on its own. Silent when neither binds, which is every CI
    runner (core-bound) and every idle workstation, so the common path pays nothing.
    """
    if cap < unbudgeted:  # a memory reading bound this run
        available = _host_available_mib()
        # Only report a reading we actually have; 0 means unknown, and the static
        # total-RAM bound is what bound us in that case.
        free = f"{available / 1024:.1f} GiB free" if available else "memory-bounded"
        warnings.warn(
            f"xdist worker budget: {cap} of {unbudgeted} workers ({free}, "
            f"{_host_total_gib()} GiB installed). Each worker needs about "
            f"{_gib_per_worker_available() * slots_per_worker} GiB, mostly to collect the "
            "suite. A run this "
            "narrow is slow, not stuck -- free some memory, or run a subset "
            "(pytest test/test_thing.py).",
            stacklevel=1,
        )
    if resolved < cap:  # slots were held by another run on this host
        warnings.warn(
            f"xdist worker budget: {resolved} of {cap} workers -- another run on this "
            "host holds the rest. Slow, not stuck; capacity returns when that run exits.",
            stacklevel=1,
        )


def resolve_workers(config: object | None = None) -> int:
    """Budget the worker count for ``-n auto`` (and ``-n logical``).

    Two separate quantities.

    **Host capacity** -- how many slots exist to compete for: cores, bounded by the
    memory readings that are CONSTANT for the machine (total RAM and the cgroup ceiling;
    see :func:`_static_memory_bounded_capacity`). It has to be constant, because it is
    the range of slot indices probed and every run must compute the same range for
    sharing to mean anything. Cores alone are the wrong unit: a 10-core / 32 GiB laptop
    cannot back 32 multi-GiB workers, and once it starts swapping the run stops making
    progress at all. Putting the static bound HERE rather than on the cap is also what
    keeps the memory budget shared: 16 slots on a 32 GiB host means two runs share 16
    workers, not 16 each.

    **Per-run cap** -- the most this single run may take, the tightest of:

    1. ``KIROCREW_MAX_TEST_WORKERS``, default 32. The optimal worker count for this
       suite plateaus around 24-32 and then *regresses*: every extra worker re-imports
       the full app (aiohttp/boto3/numpy/pdfplumber/transcribe) and writes its own
       ``.coverage.*`` file to combine at the end. Measured on a 64-core host:
       156s @ 64 workers vs 92s @ 32 workers (-41%).
    2. ``PYTEST_XDIST_AUTO_NUM_WORKERS``, xdist's OWN knob. Honoured as a ceiling
       because this hook, being ``firstresult``, runs instead of xdist's default
       implementation and would otherwise discard it silently -- and Kiro Crew itself
       seeds that variable with a memory-aware cap at every agent spawn boundary
       (``resource_status.inject_xdist_auto_cap``). A run that inherits a deliberate
       cap must not be handed more workers than it asked for.
    3. What is free on the host right now (see :func:`_live_memory_bounded_cap`). This
       reading is transient, so it throttles this run only and never reshapes the shared
       range -- it says nothing about the machine, only about what else is running on it.

    Keeping them separate is what makes a big host behave: with 64 cores and a
    cap of 32, two runs get 32 workers each rather than the second collapsing
    while half the machine idles.

    Sharing is what stops the failure this hook was extended for. Two worktrees
    each running ``-n auto`` on a 10-core box previously took 10 workers *each*,
    and the resulting swap thrash produced a load average of ~590 with zero
    tests completing in 21 minutes. Now each run holds a lock per worker it
    intends to spawn (under ``~/.cache/kirocrew/test-slots/<hostname>``, root
    overridable with ``KIROCREW_TEST_SLOT_DIR``): a run alone takes the whole
    machine, and a later run takes only what is unlocked. The locks are held for
    the process's lifetime and released by the kernel when it exits, so an
    orphaned or terminated run frees its share with no cleanup logic at all.

    The cost is fairness, not safety, and only when the host is GENUINELY full:
    a late run arriving at a fully-locked machine drops to its floor of one
    worker -- slow, but never stalled, and never oversubscribing the host the
    way the incident did. While free capacity remains, a later run gets its full
    share.

    An explicit ``-n <N>`` does not come through here; it claims slots from the
    same pool through :func:`claim_explicit_workers`.

    *config* is the pytest config, read only by :func:`run_is_scoped` to size a
    macOS worker by what the run names; without it the run is sized as the full suite.
    """
    # The two memory bounds go to different places, and which one goes where is the
    # whole correctness argument. The STATIC bound shapes the shared slot range, so the
    # budget is shared between concurrent runs rather than granted to each of them. The
    # LIVE bound only throttles this run, because a transient reading must not reshape a
    # namespace every other run has to agree on -- slots fill from index 0, so a shrunken
    # range excludes exactly the slots an earlier run left free.
    cores = os.cpu_count() or 1
    per = _slots_per_worker(run_is_scoped(config))
    # What the run would have got with no memory reading and no contention. Only used
    # to decide whether to SAY something, never to grant.
    unbudgeted = min(cores, max(1, _int_env(_MAX_WORKERS_ENV, _DEFAULT_WORKER_CAP)))
    capacity = _static_memory_bounded_capacity(cores)
    # Only a POSITIVE value is a ceiling. Unset, empty, non-numeric, zero and
    # negative all fall back to inert -- a typo must not silently serialize the
    # suite, which is what a negative value would do once floored at one worker.
    raw_env_cap = _int_env(_XDIST_ENV_CAP, 0)
    env_cap = raw_env_cap if raw_env_cap > 0 else unbudgeted
    cap = _live_memory_bounded_cap(min(capacity // per, unbudgeted, env_cap), per)
    resolved = _claim_worker_slots(capacity, cap, slots_per_worker=per)
    _warn_if_clamped(resolved, cap, unbudgeted, per)
    return resolved


def claim_explicit_workers(requested: int, scoped: bool = False) -> int:
    """Hold host slots for an explicit ``-n N`` too, and clamp N to what was free.

    An explicit count used to skip the pool entirely. That is how one ``pytest -n 16``
    ran beside a dozen ``-n auto`` runs on a 184 GiB host, none of them counting the
    others' workers, until ~40 workers at ~4.5 GiB each filled RAM and the kernel OOM
    killer fired eight times. A slot pool that some runs ignore protects nothing.

    The slots are the same files ``-n auto`` locks, from index 0, so both spellings
    share one namespace. The range probed is the static memory-bounded capacity, or
    N when that is smaller (up to the core count), but slots past the memory-bounded
    capacity are taken only by a run that got every slot below it, i.e. a run alone.
    No memory reading lowers a lone run's N: the caller chose it. Floors at one
    worker, like every other path here.

    ``KIROCREW_TEST_SLOTS_BYPASS=1`` skips the claim and returns N unchanged.
    *scoped* is :func:`run_is_scoped` for the run; it sets how many slots a worker holds.
    """
    if requested < 1 or os.environ.get(_EXPLICIT_BYPASS_ENV) == "1":
        return requested
    cores = os.cpu_count() or 1
    # The caller chose N, so the range reaches N (up to the core count) even where
    # the memory-bounded capacity is smaller: a CI shard that runs -n 2 on a 16 GiB
    # macOS runner keeps its 2 workers when it is alone. Beside another run it stays
    # inside the memory-bounded range, so the two never exceed it together.
    per = _slots_per_worker(scoped)
    static = _static_memory_bounded_capacity(cores)
    capacity = max(static, min(requested, cores) * per)
    granted = _claim_worker_slots(capacity, requested, shared=static, slots_per_worker=per)
    if granted < requested:
        why = (
            "other runs on this host hold the rest"
            if granted < min(requested, cores)
            else f"this host has {cores} cores"
        )
        warnings.warn(
            f"xdist worker budget: -n {requested} clamped to {granted} worker(s): {why}. "
            "Slow, not stuck.",
            stacklevel=1,
        )
    return granted


def budget_explicit_workers(config: object) -> None:
    """Apply :func:`claim_explicit_workers` to a pytest config's explicit ``-n N``.

    The body of the rootdir ``pytest_cmdline_main`` hook, kept here so tests reach it
    (``import conftest`` from ``test/`` binds the wrong conftest). ``auto``/``logical``
    are still strings at this point and are left to :func:`resolve_workers`; ``bool``
    is excluded because it is an ``int``. An xdist worker (``workerinput``) holds no
    slots: its controller already claimed them.
    """
    option = getattr(config, "option", None)
    requested = getattr(option, "numprocesses", None)
    if type(requested) is not int or requested < 1 or hasattr(config, "workerinput"):
        return
    setattr(option, "numprocesses", claim_explicit_workers(requested, run_is_scoped(config)))


def release_worker_slots() -> None:
    """Drop this run's worker slots promptly.

    Not strictly required -- the kernel releases every lock when the process
    exits -- but it returns capacity at the end of the run rather than at
    interpreter teardown, and is a no-op in xdist workers, which hold no slots.
    """
    while _held_slots:
        _release_one(_held_slots.pop())


def _release_one(fd: int) -> None:
    """Unlock and close one held slot fd."""
    platform_compat.release_lock(fd)
    try:
        os.close(fd)
    except OSError:
        pass
