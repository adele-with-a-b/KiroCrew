"""Where the trust a person gave is kept across an exit they did not choose.

The rule (kirodotdev/KiroCrew#12275, maintainer ruling 2026-11-18): YOLO and a
chat's Trust and Read modes stay on across every gateway exit the user did not
choose -- a dashboard or desktop Restart, ``kirocrew restart``, the re-exec after an
update, a crash, a watchdog or OOM kill, a host reboot, and a stop a service manager
sends (``launchctl kickstart -k``, ``systemctl restart``). Only an explicit stop by
the user clears them: ``kirocrew stop``, the desktop app's Quit, or Ctrl+C on a
foreground ``kirocrew gateway``.

So a SIGTERM keeps trust, whoever sent it, and the explicit stops say so instead:
each one leaves :data:`OWNER_STOP_FILE` in :data:`KEPT_DIR` before the gateway goes
down. The next boot reads that mark once (:func:`take_owner_stop`), before anything
is restored, and when it is there removes every kept record and then the mark.

:data:`KEPT_DIR` holds the records themselves too: the chats' modes
(``dashboard.chat_trust_restart``) and the ad-hoc YOLO grant
(``safety_override_breadcrumb``). A kept record is a grant, so the directory is
sealed read-only in the OS sandbox and write-fenced at the file-tool gate
(``sandbox_leaves``, ``sandbox_precreate``, ``security.paths_lists``): an agent can
neither plant a grant for its own chat nor remove the owner's stop mark. Every
writer is the gateway or the owner's own CLI and desktop app, outside the sandbox.

A failure falls toward asking again: a mark that cannot be read counts as present,
and a record that cannot be removed leaves the mark for the next boot to retry.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from kiro_crew.config.loader import config_dir
from kiro_crew.owner_only_files import mkdirs_owner_only

logger = logging.getLogger(__name__)

#: The sealed data-home directory every kept-trust file lives in. A directory, so
#: each ``atomic_write`` rename inside it leaves the sealed inode alone.
KEPT_DIR = "kept-trust"

#: The explicit-stop mark. Its content is the stop's name, for the audit only.
OWNER_STOP_FILE = "owner-stop"

#: The kept records an owner stop removes, by file name inside :data:`KEPT_DIR`.
CHATS_FILE = "chats.json"
YOLO_FILE = "yolo.json"
_RECORDS = (CHATS_FILE, YOLO_FILE)

#: Set to ``1`` by a caller whose ``kirocrew stop`` is part of a restart, not the
#: user ending Kiro Crew: the desktop app's stop of a stuck gateway it replaces.
KEEP_TRUST_ENV = "KIROCREW_STOP_KEEPS_TRUST"

_taken_lock = threading.Lock()
#: This process's verdict, read once. ``None`` until :func:`take_owner_stop` ran.
_taken: bool | None = None


def kept_dir() -> Path:
    """The sealed directory under the data home."""
    return config_dir() / KEPT_DIR


def kept_path(name: str) -> Path:
    """A kept file's path, by name."""
    return kept_dir() / name


def _mark_path() -> Path:
    return kept_path(OWNER_STOP_FILE)


def mark_owner_stop(how: str) -> bool:
    """Leave the explicit-stop mark. True when this call created it.

    Called by an explicit stop only (``kirocrew stop``, Ctrl+C). Never raises: a
    stop must still stop when the mark cannot be written, and then the next boot
    keeps trust as it would after a restart.
    """
    path = _mark_path()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # ``sudo kirocrew stop`` (allowed, ``data_home_owner``) would leave a
        # root-owned ``kept-trust`` the person's own gateway can never clear or
        # write, and Kiro Crew never re-owns files itself. Trust is kept instead.
        logger.warning("Not marking the owner stop as root; trust will be kept")
        return False
    try:
        if path.parent.is_symlink():
            logger.warning("Not marking the owner stop: %s is a link", path.parent)
            return False
        mkdirs_owner_only(path.parent)
        # O_EXCL: an earlier stop's mark is left as it is, and only the call that
        # created one may take it back (:func:`unmark_owner_stop`).
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except FileExistsError:
        return False
    except OSError:
        logger.warning("Could not mark the owner stop; trust will be kept", exc_info=True)
        return False
    try:
        os.write(fd, how.encode("ascii", "replace")[:64])
    except OSError:
        pass  # the mark is the file, not its content
    finally:
        os.close(fd)
    return True


def unmark_owner_stop() -> None:
    """Take back a mark this process created, for a stop that stopped nothing."""
    try:
        _mark_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not take back the owner-stop mark", exc_info=True)


def _marked(path: Path) -> bool:
    """Whether the mark is there. Anything but a clean "no file" counts as marked."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _audit(outcome: str, resources: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="gateway:kept_trust",
            operation="kept_trust:cleared_on_owner_stop",
            outcome=outcome,
            source="gateway",
            resources=resources,
        )
    except Exception:
        logger.debug("Could not audit the owner-stop clear", exc_info=True)


def owner_stop_marked() -> bool:
    """Whether the explicit-stop mark is on disk now. Reads it without taking it.

    For the gateway's own shutdown after the stop that left the mark: the next
    boot's :func:`take_owner_stop` must still find it. MAY BLOCK; never raises.
    """
    try:
        return _marked(_mark_path())
    except Exception:
        return True  # as :func:`_marked`: a mark that cannot be read counts as present


def take_owner_stop() -> bool:
    """Whether the previous process ended on an explicit stop. Read once per process.

    When it did, every kept record is removed and then the mark, so nothing is
    restored. A record that cannot be removed keeps the mark for the next boot, and
    this boot still restores nothing. MAY BLOCK: call it off the event loop, before
    any restore. Never raises.
    """
    global _taken
    with _taken_lock:
        if _taken is not None:
            return _taken
        mark = _mark_path()
        stopped = _marked(mark)
        if stopped:
            how = ""
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(mark, flags)
                try:
                    how = os.read(fd, 64).decode("ascii", "replace")
                finally:
                    os.close(fd)
            except OSError:
                pass  # the name is for the audit only
            failed = []
            for name in _RECORDS:
                try:
                    kept_path(name).unlink(missing_ok=True)
                except OSError:
                    failed.append(name)
            if failed:
                logger.warning("Could not clear kept trust after an owner stop: %s", failed)
                _audit("failed", f"stop:{how or 'unknown'}, kept:{','.join(failed)}")
            else:
                try:
                    mark.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove the owner-stop mark", exc_info=True)
                _audit("cleared", f"stop:{how or 'unknown'}")
        _taken = stopped
        return stopped


def reset_for_tests() -> None:
    """Forget this process's verdict."""
    global _taken
    with _taken_lock:
        _taken = None
