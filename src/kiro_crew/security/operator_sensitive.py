"""Operator-declared sensitive paths: ``KIROCREW_EXTRA_SENSITIVE_PATHS``.

The read-plus-write floor (``paths_lists._SENSITIVE_HOME_DIRS``) names the credential
stores Kiro Crew knows about. An operator whose own secrets live elsewhere under
``$HOME`` (a service's env files, a credential helper's folder) adds them here, and every
surface that asks ``is_sensitive_path`` -- the file tools' gate, the dashboard's file
preview and search, apps' file APIs -- refuses them like ``~/.ssh``.

The value is a ``:``-separated list of ``$HOME``-relative paths, POSIX separators. A
segment may hold ``*`` and is expanded once, when this module is imported (gateway
start), so ``.config/*/auth`` covers each such folder that exists then; a literal entry is
kept even when the path does not exist yet. Absolute entries, ``..`` and empty segments
are dropped with a warning. It only ever ADDS paths to the floor; nothing here can remove
one.

The variable belongs in the gateway's own environment (its service definition), not in
anything the agent can write. With the OS sandbox on, a listed path is also masked from
sandboxed children like every other floor entry, so never list a file the agent's own
harness must read to run (a ``credential_process`` helper, for one).

adele-with-a-b fork only.
"""

from __future__ import annotations

import glob
import logging
import os

logger = logging.getLogger(__name__)

ENV_NAME = "KIROCREW_EXTRA_SENSITIVE_PATHS"


def _valid(entry: str) -> bool:
    parts = entry.split("/")
    return (
        bool(entry) and not entry.startswith("/") and all(p and p not in (".", "..") for p in parts)
    )


def operator_sensitive_paths(raw: str | None = None, home: str | None = None) -> list[str]:
    """The ``$HOME``-relative entries *raw* (default: the environment) declares."""
    raw = os.environ.get(ENV_NAME, "") if raw is None else raw
    home = home or os.path.expanduser("~")
    out: list[str] = []
    for entry in (e.strip() for e in raw.split(":")):
        if not entry:
            continue
        if not _valid(entry):
            logger.warning("%s: ignored %r (must be $HOME-relative, no '..')", ENV_NAME, entry)
            continue
        if "*" not in entry:
            out.append(entry)
            continue
        for hit in sorted(glob.glob(os.path.join(home, entry))):
            out.append(os.path.relpath(hit, home).replace(os.sep, "/"))
    return list(dict.fromkeys(out))


__all__ = ["ENV_NAME", "operator_sensitive_paths"]
