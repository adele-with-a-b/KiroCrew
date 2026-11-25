"""Refuse a test's own delete when its target is not inside a temp dir.

A test that removes a path a data-home resolver built (``crew_log_dir()``,
``app_dir()``, ``data_home() / ...``) trusts ``KIROCREW_HOME`` at the moment of the
delete. Under pytest the rootdir conftest pins it to a per-test temp dir. A run
outside pytest (``python -m unittest``, a module's ``__main__`` block) loads no
conftest, so the same resolver names the operator's real ``~/.kiro/crew`` and the
delete removes live data. Pass such a path through :func:`disposable` first::

    shutil.rmtree(disposable(wl.conductor_dir(CONDUCTOR)))

The check is on the resolved path, so a link cannot carry the delete out of the
temp dir, and it fails closed: a path that cannot be resolved is refused. A path
under a temp dir can still be inside a data home (an agent's ``TMPDIR`` is a scratch
tree inside it), so a path inside ``<home>/.kiro`` or ``<home>/.kirocrew`` is refused
too, unless it sits strictly inside a scratch tree, ``<home>/.kiro/crew/scratch/<tree>/``.
The module imports only the standard library, so it works with no conftest loaded.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["RefusedDelete", "disposable"]


class RefusedDelete(AssertionError):
    """A test tried to delete a path that is not provably its own temp data."""


def _resolved(value: "str | os.PathLike[str] | None", *, strict: bool) -> "Path | None":
    if not value:
        return None
    try:
        return Path(value).expanduser().resolve(strict=strict)
    except (OSError, RuntimeError):
        return None


def _account_home() -> "Path | None":
    """The account's home from the password database, which a faked ``HOME`` does not move."""
    if os.name != "nt":
        try:
            import pwd

            return _resolved(pwd.getpwuid(os.getuid()).pw_dir, strict=False)
        except (ImportError, KeyError, OSError):
            pass
    return _resolved(Path.home(), strict=False)


def _temp_roots(account: "Path | None", tmp_path: "str | os.PathLike[str] | None") -> "list[Path]":
    """Existing temp dirs a delete may go under; one that holds the account's home is no fence."""
    names = [tempfile.gettempdir(), tmp_path] + [
        os.environ.get(n) for n in ("TMPDIR", "TEMP", "TMP")
    ]
    if os.name == "nt":
        names += [str(account / "AppData" / "Local" / "Temp")] if account else []
    else:
        names += ["/tmp", "/var/tmp"]
    roots = []
    for name in names:
        root = _resolved(name, strict=True)
        if root is None or not root.is_dir():
            continue
        if account is not None and (root == account or root in account.parents):
            continue
        roots.append(root)
    return roots


def _inside_a_data_home(target: Path, homes: "list[Path]") -> bool:
    for home in homes:
        for leaf in (Path(".kiro"), Path(".kirocrew")):
            live = home / leaf
            if target == live or live in target.parents:
                scratch = home / ".kiro" / "crew" / "scratch"
                # strictly inside one scratch tree: <scratch>/<tree>/<something>
                return not (scratch in target.parents and target.parent != scratch)
    return False


def disposable(
    path: "str | os.PathLike[str]", *, tmp_path: "str | os.PathLike[str] | None" = None
) -> Path:
    """Return *path* as a ``Path`` when it resolves strictly inside a temp dir, else raise.

    *tmp_path* adds the calling test's own temp dir to the allowed roots. Raises
    :class:`RefusedDelete` (an ``AssertionError``, so the test fails rather than
    errors) and deletes nothing.
    """
    target = _resolved(path, strict=False)
    if target is None:
        raise RefusedDelete(f"refusing to delete {path!s}: its real path cannot be resolved")
    account = _account_home()
    homes = [h for h in (_resolved(Path.home(), strict=False), account) if h is not None]
    roots = _temp_roots(account, tmp_path)
    if not any(root in target.parents for root in roots):
        raise RefusedDelete(
            f"refusing to delete {path!s}: it resolves to {target}, which is not inside a temp "
            f"dir ({', '.join(map(str, roots)) or 'none found'}). Is KIROCREW_HOME unset? "
            "Run the test under pytest so the rootdir conftest pins it."
        )
    if _inside_a_data_home(target, homes):
        raise RefusedDelete(
            f"refusing to delete {path!s}: it resolves to {target}, inside a live data home"
        )
    return Path(path)
