"""The keepers directory: each keeper's record and key, read and written by the gateway.

``<data home>/chat-keepers/`` (0700) holds, per keeper instance, ``<id>.json`` (the
record: nothing secret), ``<id>.key`` (the shared secret, 0600) and ``<id>.s`` (the socket) and
``<id>.log``, which the keeper itself creates. The keeper removes all four when it
ends. A record is LIVE when its keeper pid exists and still carries the start
identity recorded for it, so a recycled pid never reads as a keeper.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.chat_keeper import SOCKET_SUFFIX, keepers_dir
from kiro_crew.owner_only_files import mkdirs_owner_only

logger = logging.getLogger("kiro_crew.chat_keeper")

#: The longest socket path AF_UNIX takes on every POSIX host Crew runs on
#: (``sun_path`` is 104 bytes on macOS, 108 on Linux), with room for the NUL.
MAX_SOCKET_PATH = 103

#: The suffixes of one keeper's files.
_SUFFIXES = (SOCKET_SUFFIX, ".json", ".key", ".log")


@dataclass(frozen=True)
class KeeperRecord:
    """What a gateway needs to find, authenticate and attach to one keeper."""

    instance: str
    session_key: str
    backend: str
    keeper_pid: int
    keeper_start: str
    adapter_pid: int
    socket: str
    wire: int
    created: float
    #: The launch's own instance id (``AcpClient.process_instance``), stamped on the
    #: adapter's environment, so an adopting client names the process as the
    #: launching one did.
    process_instance: str = ""  # pid-owner-ok: keeper launch id
    #: What the launching gateway's ``launch()`` set on its client, and the sandbox
    #: fingerprint it launched under (``spawn.launch_state``), for the adopter.
    launch: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, body: object) -> "KeeperRecord | None":
        if not isinstance(body, dict):
            return None
        try:
            launch_id = str(body.get("process_instance") or "")  # pid-owner-ok: keeper launch id
            record = cls(
                instance=str(body["instance"]),
                session_key=str(body["session_key"]),
                backend=str(body["backend"]),
                keeper_pid=int(body["keeper_pid"]),
                keeper_start=str(body["keeper_start"]),
                adapter_pid=int(body["adapter_pid"]),
                socket=str(body["socket"]),
                wire=int(body["wire"]),
                created=float(body["created"]),
                process_instance=launch_id,  # pid-owner-ok: keeper launch id
                launch=dict(body["launch"]) if isinstance(body.get("launch"), dict) else {},
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not record.instance.isalnum() or record.keeper_pid <= 1:
            return None
        return record


def ensure_dir(home: Path | None = None) -> Path:
    """Create the keepers directory owner-only, refusing a link at its name."""
    path = keepers_dir(home)
    mkdirs_owner_only(path)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{path} is not a directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def socket_path(instance: str, home: Path | None = None) -> Path:
    return keepers_dir(home) / f"{instance}{SOCKET_SUFFIX}"


def socket_path_fits(instance: str, home: Path | None = None) -> bool:
    """Whether this instance's socket path is short enough for AF_UNIX."""
    return len(os.fsencode(socket_path(instance, home))) <= MAX_SOCKET_PATH


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def write_key(instance: str, secret: bytes, home: Path | None = None) -> None:
    _write_private(keepers_dir(home) / f"{instance}.key", secret.hex().encode("ascii"))


def read_key(instance: str, home: Path | None = None) -> bytes | None:
    path = keepers_dir(home) / f"{instance}.key"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        raw = os.read(fd, 256)
    finally:
        os.close(fd)
    try:
        return bytes.fromhex(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None


def write_record(record: KeeperRecord, home: Path | None = None) -> None:
    body = json.dumps(asdict(record), sort_keys=True).encode("utf-8")
    _write_private(keepers_dir(home) / f"{record.instance}.json", body)


def rewrite_record(record: KeeperRecord) -> None:
    """:func:`write_record` for *record*, in the directory its socket was made in."""
    body = json.dumps(asdict(record), sort_keys=True).encode("utf-8")
    _write_private(Path(record.socket).parent / f"{record.instance}.json", body)


def load_records(home: Path | None = None) -> list[KeeperRecord]:
    """Every parseable record in the keepers directory, live or not."""
    directory = keepers_dir(home)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    records: list[KeeperRecord] = []
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            raw = _read_bounded(fd)
        finally:
            os.close(fd)
        try:
            record = KeeperRecord.from_json(json.loads(raw))
        except ValueError:
            continue
        if record is not None and f"{record.instance}.json" == name:
            records.append(record)
    return records


#: A record past this is not read. It holds the launch state, child env names and
#: the session's deny and ask rules, so it is sized well above any real one.
_RECORD_MAX_BYTES = 4 * 1024 * 1024


def _read_bounded(fd: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while size <= _RECORD_MAX_BYTES:
        chunk = os.read(fd, 256 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def is_dead(record: KeeperRecord) -> bool:
    """Whether *record*'s keeper is provably gone: no pid, or one with another start.

    A start identity that cannot be read is NOT death: such a keeper stays live for
    the sweeps' skip list and keeps its files, as ``platform_compat`` asks of a caller
    holding a None start id.
    """
    from kiro_crew import platform_compat

    if not platform_compat.pid_exists(record.keeper_pid):
        return True
    start = platform_compat.get_process_start_id(record.keeper_pid)
    return start is not None and start != record.keeper_start


def is_live(record: KeeperRecord) -> bool:
    """Whether *record*'s keeper may still be running (see :func:`is_dead`)."""
    return not is_dead(record)


def live_records(home: Path | None = None) -> list[KeeperRecord]:
    return [record for record in load_records(home) if is_live(record)]


def live_keeper_pids(home: Path | None = None) -> frozenset[int]:
    """The pids of every live keeper: the sweeps leave these to their keepers."""
    try:
        return frozenset(record.keeper_pid for record in live_records(home))
    except Exception:
        logger.debug("reading the chat keeper records failed", exc_info=True)
        return frozenset()


def remove_files(instance: str, home: Path | None = None) -> None:
    """Remove one keeper's files (a keeper removes its own; this is for a dead one)."""
    _remove_in(keepers_dir(home), instance)


def remove_record_files(record: KeeperRecord) -> None:
    """:func:`remove_files` for *record*, in the directory its socket was made in."""
    _remove_in(Path(record.socket).parent, record.instance)


def _remove_in(directory: Path, instance: str) -> None:
    if not instance.isalnum():
        return
    for suffix in _SUFFIXES:
        try:
            (directory / f"{instance}{suffix}").unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("could not remove a keeper file", exc_info=True)


def prune_dead(home: Path | None = None) -> int:
    """Remove the files of every record whose keeper is gone; how many were removed."""
    removed = 0
    for record in load_records(home):
        if is_dead(record):
            remove_files(record.instance, home)
            removed += 1
    return removed
