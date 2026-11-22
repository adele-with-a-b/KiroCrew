"""MemoDir: a directory is scanned once per run, however many of its files a run names.

Each case runs a real pytest in a scratch rootdir whose conftest counts
``pytest_ignore_collect`` calls, which pytest makes once per directory entry on every
scan of that directory, and binds the floor's ``pytest_collect_directory`` hook unless
``MEMODIR_OFF`` is set (the control: plain pytest, which rescans).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_FLOOR_DIR = Path(__file__).resolve().parent

_CONFTEST = """
import collections, json, os, pathlib

if not os.environ.get("MEMODIR_OFF"):
    from conftest_floor_collect import pytest_collect_directory  # noqa: F401

SCANS = collections.Counter()

def pytest_ignore_collect(collection_path, config):
    SCANS[collection_path.name] += 1
    return None

def pytest_sessionfinish(session):
    pathlib.Path(session.config.rootpath, "scans.json").write_text(json.dumps(SCANS))
"""


def _run(root: Path, *args: str, memodir: bool = True) -> tuple[str, int, int]:
    """Run pytest in *root*; answer its stdout, its exit code and the scans of ``pkg/``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.pop("MEMODIR_OFF", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(_FLOOR_DIR)
    if not memodir:
        env["MEMODIR_OFF"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    scans = json.loads((root / "scans.json").read_text())
    # notes.txt sits only in pkg/, so its count is how many times pkg/ was scanned.
    return proc.stdout + proc.stderr, proc.returncode, scans.get("notes.txt", 0)


def _tree(root: Path) -> None:
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "conftest.py").write_text(_CONFTEST)
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "test_a.py").write_text("def test_a():\n    pass\n")
    (pkg / "test_b.py").write_text("def test_b():\n    pass\n")
    (pkg / "notes.txt").write_text("not a test\n")


def test_two_files_in_one_folder_scan_it_once(tmp_path: Path) -> None:
    _tree(tmp_path)
    out, code, scans = _run(tmp_path, "pkg/test_a.py", "pkg/test_b.py")
    assert code == 0, out
    assert "2 passed" in out, out
    assert scans == 1, out


def test_plain_pytest_scans_the_folder_once_per_named_file(tmp_path: Path) -> None:
    """The control: without MemoDir the same run scans ``pkg/`` twice."""
    _tree(tmp_path)
    out, code, scans = _run(tmp_path, "pkg/test_a.py", "pkg/test_b.py", memodir=False)
    assert code == 0, out
    assert scans == 2, out


def test_a_file_named_twice_still_runs_twice(tmp_path: Path) -> None:
    """``--keep-duplicates`` keeps both copies of a repeated file; MemoDir must not drop one."""
    _tree(tmp_path)
    out, code, scans = _run(tmp_path, "--keep-duplicates", "pkg/test_a.py", "pkg/test_a.py")
    assert code == 0, out
    assert "2 passed" in out, out
    assert scans == 1, out
