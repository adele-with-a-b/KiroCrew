"""Refuse a test's own delete when its target is not inside a temp dir.

A test that removes a path a data-home resolver built (``crew_log_dir()``,
``app_dir()``, ``data_home() / ...``) trusts ``KIROCREW_HOME`` at the moment of the
delete. Under pytest the rootdir conftest pins it to a per-test temp dir. A run that
loads no rootdir conftest (``python -m unittest``, a module's ``__main__`` block,
``pytest --noconftest``, a test file copied out of the repo) gets no pin, so the same
resolver names the operator's real ``~/.kiro/crew`` and the delete removes live data.
Pass such a path through :func:`disposable` first::

    shutil.rmtree(disposable(wl.conductor_dir(CONDUCTOR)))

Both the entry the delete removes (its parent resolved, its own name kept) and the
real path it resolves to (every link followed) must pass, so neither a link in the
live home nor a link out of the temp dir can carry the delete there. It fails closed:
a path that cannot be resolved is refused, and so is every path when no home can be
found. Pytest's basetemp counts as a temp dir only once the rootdir conftest names
it through :func:`count_as_temp`, so a run with no conftest gets no such root. A path
under a temp dir can still be inside a data home (an agent's ``TMPDIR`` is a scratch
tree inside it), so a path inside ``<home>/.kiro`` or ``<home>/.kirocrew`` is refused
too, unless it sits strictly inside a scratch tree, ``<home>/.kiro/crew/scratch/<tree>/``.
The module imports only the standard library, so it works with no conftest loaded.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["RefusedDelete", "count_as_temp", "disposable"]


class RefusedDelete(AssertionError):
    """A test tried to delete a path that is not provably its own temp data."""


def _resolved(value: "str | os.PathLike[str] | None", *, strict: bool) -> "Path | None":
    if not value:
        return None
    try:
        return Path(value).resolve(strict=strict)
    except (OSError, RuntimeError):
        return None


#: Temp trees the rootdir conftest named for this process (pytest's basetemp). Empty in a
#: run that loaded no conftest.
_RUN_TEMP_TREES: "list[Path]" = []


def count_as_temp(path: "str | os.PathLike[str]") -> None:
    """Count the existing dir *path* as a temp root for this process.

    The rootdir conftest calls it with pytest's basetemp, which ``--basetemp`` can put
    outside every system temp dir. The account-home and data-home fences still apply.
    """
    resolved = _resolved(path, strict=True)
    if resolved is not None and resolved.is_dir() and resolved not in _RUN_TEMP_TREES:
        _RUN_TEMP_TREES.append(resolved)


#: The stem of the run temp root the rootdir conftest creates (``_tmp_root_prefix_for_run``
#: in ``test/conftest_floor_home.py``) and points ``tempfile`` and ``TMPDIR`` at.
_RUN_ROOT_PREFIX = "kc-pytest-"


def _home() -> "Path | None":
    """``Path.home()`` resolved, or None when there is none (no ``HOME`` and no pwd entry)."""
    try:
        return _resolved(Path.home(), strict=False)
    except (KeyError, RuntimeError):
        return None


def _entry(path: "str | os.PathLike[str]") -> "Path | None":
    """The real path of the entry *path* names: its parent resolved, its own name kept."""
    raw = Path(path)
    if raw.name in ("", ".", ".."):
        return _resolved(raw, strict=False)
    parent = _resolved(raw.parent, strict=False)
    return None if parent is None else parent / raw.name


def _account_home() -> "Path | None":
    """The account's own home: the password database's entry on POSIX, which a patched
    ``Path.home()`` or a faked ``HOME`` does not move. On Windows it is ``USERPROFILE``,
    which a test that fakes it does move."""
    if os.name == "nt":
        return _resolved(os.environ.get("USERPROFILE"), strict=False)
    try:
        import pwd

        return _resolved(pwd.getpwuid(os.getuid()).pw_dir, strict=False)
    except (ImportError, KeyError, OSError):
        return _home()


def _with_the_run_roots_parent(root: Path) -> "list[Path]":
    """*root*, plus the temp dir a pytest run started in when *root* is that run's own root.

    The floor pins pytest's basetemp (every ``tmp_path`` and per-test data home) under
    the temp dir the run started with, then points ``tempfile`` and ``TMPDIR`` at a fresh
    ``kc-pytest-*`` root beside it. Without the parent, a run whose temp dir is not
    ``/tmp`` would have every per-test path refused.
    """
    found = [root]
    for part in (root, *root.parents):
        if part.name.startswith(_RUN_ROOT_PREFIX):
            found.append(part.parent)
            break
    return found


def _temp_roots(account: "Path | None", tmp_path: "str | os.PathLike[str] | None") -> "list[Path]":
    """Existing temp dirs a delete may go under; one that holds the account's home is no fence."""
    names: "list[str | os.PathLike[str] | None]" = [tempfile.gettempdir(), tmp_path]
    names += [os.environ.get(n) for n in ("TMPDIR", "TEMP", "TMP")] + list(_RUN_TEMP_TREES)
    if os.name == "nt":
        names += [str(account / "AppData" / "Local" / "Temp")] if account else []
    else:
        names += ["/tmp", "/var/tmp"]
    roots = []
    for name in names:
        resolved = _resolved(name, strict=True)
        if resolved is None or not resolved.is_dir():
            continue
        for root in _with_the_run_roots_parent(resolved):
            if account is not None and (root == account or root in account.parents):
                continue
            roots.append(root)
    return roots


def _inside_a_data_home(target: Path, homes: "list[Path]") -> bool:
    for home in homes:
        for leaf in (".kiro", ".kirocrew"):
            live = _resolved(home / leaf, strict=False) or home / leaf
            if target == live or live in target.parents:
                scratch = _resolved(home / ".kiro" / "crew" / "scratch", strict=False)
                # strictly inside one scratch tree: <scratch>/<tree>/<something>
                return not (scratch in target.parents and target.parent != scratch)
    return False


def disposable(
    path: "str | os.PathLike[str]", *, tmp_path: "str | os.PathLike[str] | None" = None
) -> Path:
    """Return *path* as a ``Path`` when it and its real path are strictly inside a temp dir.

    *tmp_path* adds the calling test's own temp dir to the allowed roots. Raises
    :class:`RefusedDelete` (an ``AssertionError``, so the test fails rather than
    errors) and deletes nothing.
    """
    entry, real = _entry(path), _resolved(path, strict=False)
    if entry is None or real is None:
        raise RefusedDelete(f"refusing to delete {path!s}: its real path cannot be resolved")
    account = _account_home()
    homes = [h for h in (_home(), account) if h is not None]
    if not homes:
        raise RefusedDelete(
            f"refusing to delete {path!s}: no home directory could be found, so a live data "
            "home cannot be ruled out"
        )
    roots = _temp_roots(account, tmp_path)
    for target in dict.fromkeys((entry, real)):
        if not any(root in target.parents for root in roots):
            raise RefusedDelete(
                f"refusing to delete {path!s}: it resolves to {target}, which is not inside a "
                f"temp dir ({', '.join(map(str, roots)) or 'none found'}). Is KIROCREW_HOME "
                "unset? Run the test under pytest so the rootdir conftest pins it."
            )
        if _inside_a_data_home(target, homes):
            raise RefusedDelete(
                f"refusing to delete {path!s}: it resolves to {target}, inside a live data home"
            )
    return Path(path)
