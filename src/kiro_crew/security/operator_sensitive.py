"""Operator-declared sensitive paths: ``<crew home>/extra_sensitive_paths.txt``.

The read-plus-write floor (``paths_lists._SENSITIVE_HOME_DIRS``) names the credential
stores Kiro Crew knows about. An operator whose own secrets live elsewhere under
``$HOME`` (a service's env files, a credential helper's folder) lists them in this file,
and every surface that asks ``is_sensitive_path`` -- the file tools' gate, the
dashboard's file preview and search, app backends' file APIs -- refuses them like
``~/.ssh``.

A file rather than an environment variable because the gateway starts its children (app
backends, MCP servers, scripts) with an allowlisted environment (``minimal_env``), while
every one of them that imports this module can read the file: one source, every process.
It is read from ``$KIROCREW_HOME`` when that is set, else ``~/.kiro/crew``.

One ``$HOME``-relative path per line, POSIX separators; ``#`` starts a comment. A
segment may hold ``*`` and is expanded when this module is imported (process start), so
``.config/*/auth`` covers each such folder that exists then; a literal entry is kept even
when the path does not exist yet. Absolute entries, ``..`` and empty segments are dropped
with a warning. It only ever ADDS paths to the floor. With the OS sandbox on, a listed
path is also masked from sandboxed children like every other floor entry, so never list a
file the agent's own harness must read to run (a ``credential_process`` helper, for one).

adele-with-a-b fork only.
"""

from __future__ import annotations

import glob
import logging
import os

logger = logging.getLogger(__name__)

FILE_NAME = "extra_sensitive_paths.txt"
_MAX_BYTES = 64 * 1024


def _valid(entry: str) -> bool:
    parts = entry.split("/")
    return (
        bool(entry) and not entry.startswith("/") and all(p and p not in (".", "..") for p in parts)
    )


def declaration_file() -> str:
    crew_home = os.environ.get("KIROCREW_HOME") or os.path.join(
        os.path.expanduser("~"), ".kiro", "crew"
    )
    return os.path.join(os.path.expanduser(crew_home), FILE_NAME)


def _read_declarations() -> str:
    try:
        with open(declaration_file(), encoding="utf-8") as fh:
            return fh.read(_MAX_BYTES)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.warning("%s unreadable (%s); no operator paths added", FILE_NAME, exc)
        return ""


def operator_sensitive_paths(raw: str | None = None, home: str | None = None) -> list[str]:
    """The ``$HOME``-relative entries *raw* (default: the declaration file) declares."""
    raw = _read_declarations() if raw is None else raw
    home = home or os.path.expanduser("~")
    out: list[str] = []
    for line in raw.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if not _valid(entry):
            logger.warning("%s: ignored %r (must be $HOME-relative, no '..')", FILE_NAME, entry)
            continue
        if "*" not in entry:
            out.append(entry)
            continue
        for hit in sorted(glob.glob(os.path.join(home, entry))):
            out.append(os.path.relpath(hit, home).replace(os.sep, "/"))
    return list(dict.fromkeys(out))


__all__ = ["FILE_NAME", "declaration_file", "operator_sensitive_paths"]
