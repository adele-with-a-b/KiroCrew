"""Whole-machine CPU time as ``(busy, total)`` counters.

The reading the remote-dispatch ``crew_busy`` gate turns into a busy share:
Mach ``host_statistics`` on macOS, ``/proc/stat`` on Linux, ``GetSystemTimes``
on Windows.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from kiro_crew.platform_compat_native import IS_LINUX, IS_MACOS, IS_WINDOWS

#: ``host_statistics`` flavor selector for ``host_cpu_load_info_data_t``.
_HOST_CPU_LOAD_INFO = 3


class _HostCpuLoadInfo(ctypes.Structure):
    """``host_cpu_load_info_data_t``: ticks per state (user, system, idle, nice),
    summed over every CPU.

    Module scope is load-bearing: ``ctypes.POINTER(T)`` memoises T in a
    module-level dict inside ctypes that is never evicted, so declaring this
    inside the probe would pin a fresh pair of type objects on every call.
    """

    _fields_ = [("ticks", ctypes.c_uint * 4)]


def _macos_cpu_times() -> "tuple[float, float] | None":
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
        libc.mach_host_self.restype = ctypes.c_uint
        libc.mach_task_self.restype = ctypes.c_uint
        libc.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        libc.host_statistics.restype = ctypes.c_int
        libc.host_statistics.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(_HostCpuLoadInfo),
            ctypes.POINTER(ctypes.c_uint),
        ]
        host_port = libc.mach_host_self()
        try:
            info = _HostCpuLoadInfo()
            count = ctypes.c_uint(4)
            kern_return = libc.host_statistics(
                host_port, _HOST_CPU_LOAD_INFO, ctypes.byref(info), ctypes.byref(count)
            )
        finally:
            try:
                libc.mach_port_deallocate(libc.mach_task_self(), host_port)
            except (AttributeError, OSError, ValueError):
                pass
    except (AttributeError, OSError, ValueError):
        return None
    if kern_return != 0 or count.value < 4:
        return None
    user, system, idle, nice = (float(t) for t in info.ticks)
    return user + system + nice, user + system + idle + nice


def _linux_cpu_times() -> "tuple[float, float] | None":
    try:
        with open("/proc/stat") as f:
            fields = f.readline().split()
        if len(fields) < 5 or fields[0] != "cpu":
            return None
        # user nice system idle iowait irq softirq steal; guest is already in user.
        vals = [float(x) for x in fields[1:9]]
    except (OSError, ValueError):
        return None
    total = sum(vals)
    # iowait is a CPU with nothing to run, so it counts as idle here.
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)
    return total - idle, total


def _windows_cpu_times() -> "tuple[float, float] | None":
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
    except Exception:
        return None

    def _ticks(ft: "wintypes.FILETIME") -> float:
        return float((ft.dwHighDateTime << 32) | ft.dwLowDateTime)

    # Kernel time already includes idle time.
    total = _ticks(kernel) + _ticks(user)
    return total - _ticks(idle), total


def system_cpu_times() -> "tuple[float, float] | None":
    """Whole-machine CPU time as ``(busy, total)`` counters, or ``None``.

    Stateless: the counters only grow, so a caller takes two readings and
    divides the deltas. Units differ per platform (ticks, jiffies, 100 ns) and
    cancel in that ratio. macOS reads Mach ``host_statistics`` in process,
    Linux ``/proc/stat``, Windows ``GetSystemTimes``. macOS counters are 32-bit
    and wrap; a delta across a wrap is not a reading, which the caller must
    reject (a negative or out-of-range delta).
    """
    if IS_MACOS:
        return _macos_cpu_times()
    if IS_LINUX:
        return _linux_cpu_times()
    if IS_WINDOWS:
        return _windows_cpu_times()
    return None
