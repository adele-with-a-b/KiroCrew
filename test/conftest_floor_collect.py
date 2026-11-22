"""Collection speed: a directory is scanned once per run, however many files are named.

pytest re-collects a file's parent directory for EVERY file argument that names it:
``Session.collect`` passes ``handle_dupes=False`` whenever the part left to match is a
single file, so a file named twice is not deduplicated, and that bypasses the collection
cache for the directory too. A run naming N files in ``test/`` therefore rescans
``test/`` N times, building a node and running ``pytest_ignore_collect`` and
``pytest_collect_file`` for each of its entries every time. ``scripts/local-gate.py``
names hundreds of files per batch, and ``test/`` holds thousands of entries: a 651-file
batch spent over 8 minutes per xdist worker in that loop before the first test ran,
which reads as a stalled run.

:class:`MemoDir` keeps the children of its first collection and hands the same nodes
back on every later one. The children are the same nodes pytest would have built, and
a file named twice still yields its tests twice, since the repeat is decided by the
argument loop, not by the directory's child list.

Part of the rootdir ``conftest.py``'s floor. That file loads this module by path and
binds the hook, so pytest registers it as the rootdir conftest's own; see "the floor's
own modules" there. Module-level imports are stdlib and pytest only.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterable

import pytest


class MemoDir(pytest.Dir):
    """A ``Dir`` whose ``collect`` runs once; a repeat returns the first result."""

    _children: list[pytest.Item | pytest.Collector] | None = None

    def collect(self) -> Iterable[pytest.Item | pytest.Collector]:
        if self._children is None:
            self._children = list(super().collect())
        return list(self._children)


@pytest.hookimpl(wrapper=True)
def pytest_collect_directory(path: pathlib.Path, parent: pytest.Collector):
    """Swap pytest's plain ``Dir`` for :class:`MemoDir`; any other collector is kept."""
    node = yield
    if type(node) is pytest.Dir:
        return MemoDir.from_parent(parent, path=path)
    return node
