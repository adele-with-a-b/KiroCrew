"""Tests for the macOS Mach memory probe (subagent._macos_vm_reclaimable_pages).

``mach_host_self``'s send right must be released with ``mach_port_deallocate`` on success
and on ``host_statistics64`` failure. Linux CI cannot run Mach, so a fake ``libSystem`` is
injected via ``ctypes.CDLL``.
"""

from __future__ import annotations

import ctypes

import pytest

import kiro_crew.subagent as subagent

# This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
# so the shared macOS handle caches are put back after each one (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_SENTINEL_PORT = 0x1111
_SENTINEL_TASK = 0x2222


class _FakeFn:
    """A stand-in for a ctypes function pointer: callable, and tolerant of the
    ``.restype`` / ``.argtypes`` assignments the probe performs."""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self._fn(*args)


class _FakeLibc:
    def __init__(self, kern_return: int = 0):
        self.kern_return = kern_return
        self.dealloc_calls: list[tuple[int, int]] = []
        self.mach_host_self = _FakeFn(lambda: _SENTINEL_PORT)
        self.mach_task_self = _FakeFn(lambda: _SENTINEL_TASK)
        self.mach_port_deallocate = _FakeFn(self._deallocate)
        self.host_statistics64 = _FakeFn(self._host_statistics64)

    def _deallocate(self, task, port):
        self.dealloc_calls.append((task, port))
        return 0

    def _host_statistics64(self, host_port, flavor, stats_ref, count_ref):
        # Populate the caller's vm_statistics64 struct through the byref target.
        obj = getattr(stats_ref, "_obj", None)
        if obj is not None:
            obj.free_count = 100
            obj.inactive_count = 50
            obj.speculative_count = 10
            obj.purgeable_count = 5
        return self.kern_return


def test_deallocates_send_right_on_success(monkeypatch):
    fake = _FakeLibc(kern_return=0)
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: fake)

    result = subagent._macos_vm_reclaimable_pages()

    assert result == 150  # free + active (0) + inactive; speculative is inside free
    assert fake.dealloc_calls == [(_SENTINEL_TASK, _SENTINEL_PORT)]


def test_deallocates_send_right_on_failure(monkeypatch):
    fake = _FakeLibc(kern_return=1)  # non-zero kern_return_t -> failure
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: fake)

    result = subagent._macos_vm_reclaimable_pages()

    assert result is None
    # The send right must still be released once even when the stats call fails.
    assert fake.dealloc_calls == [(_SENTINEL_TASK, _SENTINEL_PORT)]


#: A loaded 36 GB Apple silicon Mac whose Activity Monitor shows green memory
#: pressure: large wired and compressed figures, little file cache. ``vm_stat``
#: shape (16 KiB pages). It is a sample built to that shape, not a capture from
#: one machine; its sums check out (the page classes add up to under the total,
#: and anonymous + file-backed == active + inactive + speculative).
_VM_STAT_GREEN_PRESSURE = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               12000.
Pages active:                            900000.
Pages inactive:                          220000.
Pages speculative:                         3000.
Pages throttled:                              0.
Pages wired down:                        330000.
Pages purgeable:                           4000.
File-backed pages:                       140000.
Anonymous pages:                         983000.
Pages occupied by compressor:            760000.
"""

_PAGE = 16384
_TOTAL_PAGES = 36 * 1024**3 // _PAGE


def _mach_fields(vm_stat_text: str) -> dict[str, int]:
    """The ``host_statistics64`` fields behind a ``vm_stat`` print.

    ``vm_stat`` prints ``free_count - speculative_count`` as "Pages free", so the
    Mach ``free_count`` is the two lines added back together.
    """
    from kiro_crew.dashboard.handlers_system import _parse_vm_stat

    _page, c = _parse_vm_stat(vm_stat_text)
    return {
        "free_count": c["Pages free"] + c["Pages speculative"],
        "active_count": c["Pages active"],
        "inactive_count": c["Pages inactive"],
        "speculative_count": c["Pages speculative"],
        "wire_count": c["Pages wired down"],
        "purgeable_count": c["Pages purgeable"],
        "external_page_count": c["File-backed pages"],
        "internal_page_count": c["Anonymous pages"],
        "compressor_page_count": c["Pages occupied by compressor"],
    }


class _SampleMacLibc(_FakeLibc):
    def __init__(self, fields: dict[str, int]):
        super().__init__()
        self.fields = fields

    def _host_statistics64(self, host_port, flavor, stats_ref, count_ref):
        obj = getattr(stats_ref, "_obj", None)
        if obj is not None:
            for name, value in self.fields.items():
                setattr(obj, name, value)
        return self.kern_return


def _present_as_mac(monkeypatch, vm_stat_text: str) -> None:
    from kiro_crew import platform_compat

    for name in ("LINUX", "WINDOWS", "MACOS"):
        monkeypatch.setattr(platform_compat, "IS_" + name, name == "MACOS")
    fake = _SampleMacLibc(_mach_fields(vm_stat_text))
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: fake)
    real_sysconf = getattr(platform_compat.os, "sysconf", None)

    def fake_sysconf(name: str) -> int:
        if name == "SC_PAGE_SIZE":
            return _PAGE
        if real_sysconf is not None:
            return real_sysconf(name)
        raise ValueError(name)

    # Scoped to SC_PAGE_SIZE: ``os`` is process-wide; raising=False for Windows.
    monkeypatch.setattr(platform_compat.os, "sysconf", fake_sysconf, raising=False)


def test_the_figure_is_what_memory_pressure_reports(monkeypatch):
    """Wired and compressed pages are the only ones left out.

    ``memory_pressure`` prints the kernel's free percentage, which is
    (active + inactive + free + speculative) / total on macOS. Here that is
    1,135,000 of 2,359,296 pages: 48 %, about 17.3 GB.
    """
    _present_as_mac(monkeypatch, _VM_STAT_GREEN_PRESSURE)

    free_percentage = 100 * 1_135_000 // _TOTAL_PAGES
    assert free_percentage == 48
    assert subagent._available_memory_gb() == round(1_135_000 * _PAGE / 1024**3, 2)


def test_a_mac_with_green_pressure_is_not_read_as_low(monkeypatch):
    """The Slack report: big wired and compressed figures, green pressure.

    Before the fix the floor counted only free, purgeable and file-backed
    inactive pages (2.43 GB) and the posture added the rest of inactive
    (3.69 GB), so the default 2 GB floor plus a 1 GB start refused every
    sub-agent and the posture said memory was tight.
    """
    from kiro_crew.resource_status import POSTURE_AMPLE, _classify

    _present_as_mac(monkeypatch, _VM_STAT_GREEN_PRESSURE)

    ok, avail = subagent.check_memory_available(min_gb=3.0)
    assert ok is True and avail > 17
    assert _classify(subagent._available_memory_gb(), 4.0, 2.0) == POSTURE_AMPLE


def test_the_floor_and_the_posture_read_one_figure(monkeypatch):
    """One macOS figure, so a start is never refused on a number the posture
    and the dashboard do not show."""
    _present_as_mac(monkeypatch, _VM_STAT_GREEN_PRESSURE)

    _ok, floor_gb = subagent.check_memory_available(min_gb=0.0)
    assert floor_gb == subagent._available_memory_gb()


def test_a_mac_that_is_really_full_still_reads_low(monkeypatch):
    """Most pages wired or compressed: the figure is small and the floor refuses."""
    full = _VM_STAT_GREEN_PRESSURE.replace(
        "Pages active:                            900000.",
        "Pages active:                             60000.",
    ).replace(
        "Pages occupied by compressor:            760000.",
        "Pages occupied by compressor:           1600000.",
    )
    _present_as_mac(monkeypatch, full)

    ok, avail = subagent.check_memory_available(min_gb=6.0)
    assert ok is False and avail < 6


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
