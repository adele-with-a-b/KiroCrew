"""Starting an adapter through a keeper, inside the shared launch tail.

``acp.launch.launch`` builds the adapter's argv, environment, sandbox wrap, scope and
resource profile exactly as for a direct spawn. A :class:`KeeperSpawn` on the
request changes two steps of it: :meth:`KeeperSpawn.wrap` puts the keeper in front
of the sandbox-wrapped argv (inside the cgroup scope, so the scope still bounds the
whole tree), and :meth:`KeeperSpawn.finish` turns the created launcher into an
attached :class:`~.transport.KeeperProcess`. The secret goes to the launcher on its
stdin, never argv or the environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import kiro_crew
from kiro_crew.chat_keeper import INSTANCE_HEX_DIGITS, KEEPER_ARGV_MARKER, records, wire
from kiro_crew.chat_keeper.transport import KeeperProcess, attach

logger = logging.getLogger("kiro_crew.chat_keeper")

#: How long the launcher may take to report the keeper ready (adapter started,
#: socket bound). An import on a cold disk is the slow part.
READY_TIMEOUT_SECS = 30.0

#: How long the launcher may take to exit once the keeper has forked off it.
LAUNCHER_EXIT_SECS = 10.0

#: The program the keeper's interpreter runs. ``-I`` keeps the chat's working
#: directory and every ``PYTHON*`` variable out of its import path, so a module
#: planted in a checkout is never imported; the root inserted is the one this
#: gateway imported its own code from, so the keeper runs the same code.
_BOOT = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from kiro_crew.chat_keeper.keeper import main; main(sys.argv[1:])"
)


class KeeperStartError(OSError):
    """The keeper did not come up; the launch fails like any failed spawn."""


def _end_group(pid: int, start: str) -> None:
    """SIGKILL a keeper's group while *pid* still carries *start* and leads its group."""
    from kiro_crew import platform_compat, process_identity

    group = process_identity.isolated_group_of(pid, start)
    if group is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, ValueError, OSError):
        platform_compat.kill_process_group(group, platform_compat.SIGKILL)


def package_root() -> str:
    """The directory this gateway's ``kiro_crew`` package was imported from."""
    return str(Path(kiro_crew.__file__).resolve().parent.parent)


def keeper_argv(instance: str, directory: Path, adopt_secs: int) -> list[str]:
    """The keeper's command line; the adapter's argv goes on its stdin (keeper.parse_args)."""
    from kiro_crew import platform_compat

    return platform_compat.isolated_python_argv(
        "-I",
        "-c",
        _BOOT.format(root=package_root()),
        KEEPER_ARGV_MARKER,
        "--instance",
        instance,
        "--dir",
        str(directory),
        "--adopt-secs",
        str(int(adopt_secs)),
    )


def new_instance() -> str:
    return uuid.uuid4().hex[:INSTANCE_HEX_DIGITS]


def sandbox_fingerprint(
    sandbox_mode: str,
    hidden_dirs: tuple[str, ...] | list[str],
    expose_files: tuple[str, ...] | list[str] = (),
    internal_sandbox: bool = False,
) -> str:
    """The sandbox inputs an adopt compares, as one comparable value; blocking.

    The sandbox mode as the wrap applies it (clamped up to the governance
    ``sandbox.min_level`` floor, as ``sandbox_wrap.wrap_argv`` does), the operator's
    read-only paths and exceptions, the extra hidden directories and exposed files of
    the launch, whether the harness runs its own internal sandbox, and the crew-home
    leaf tables an update may change (``sandbox_leaves``). A gateway adopts a kept
    adapter only when its own launch would have the same value, so a raised floor, a
    new read-only path or a new ``HIDDEN`` leaf is never left out of a chat that
    outlived a restart. It reads the policy and config files, so call it off the loop.
    """
    import hashlib

    from kiro_crew import sandbox_leaves
    from kiro_crew.sandbox_tiers import (
        _clamp_sandbox_mode_to_floor,
        _governance_sandbox_floor,
        configured_readonly_layer,
    )

    applied = _clamp_sandbox_mode_to_floor(str(sandbox_mode), _governance_sandbox_floor())
    readonly, exceptions = configured_readonly_layer()
    body = json.dumps(
        [
            applied,
            sorted(readonly),
            sorted(exceptions),
            sorted(str(d) for d in hidden_dirs),
            sorted(str(f) for f in expose_files),
            bool(internal_sandbox),
            sorted(sandbox_leaves._CREW_HIDDEN_LEAVES),
            sorted(sandbox_leaves._CREW_READONLY_LEAVES),
        ]
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def launch_state(host: Any, sandbox: str) -> dict[str, Any]:
    """The launch's client state an adopter restores, with its *sandbox* fingerprint."""
    scratch = getattr(host, "_scratch_dir", None)
    return {
        "sandbox": sandbox,
        "scratch_dir": str(scratch) if scratch is not None else "",
        "sandbox_wrapped_by_crew": bool(getattr(host, "_sandbox_wrapped_by_crew", False)),
        "sandbox_hidden_dirs": [str(d) for d in getattr(host, "_sandbox_hidden_dirs", ())],
        "child_env_names": sorted(getattr(host, "_child_env_names", ()) or ()),
        "ssh_auth_sock_forwarded": bool(getattr(host, "_ssh_auth_sock_forwarded", False)),
    }


@dataclass
class KeeperSpawn:
    """One launch through a keeper: who it is for, and what the gateway records."""

    session_key: str
    backend: str
    adopt_secs: int
    stdout_limit: int
    instance: str = field(default_factory=new_instance)
    home: Path | None = None
    #: The launching client's process instance, kept in the record for the adopter.
    process_instance: str = ""  # pid-owner-ok: keeper launch id
    secret: bytes = field(default_factory=lambda: os.urandom(wire.SECRET_BYTES), repr=False)
    #: The session token the adapter's environment carries (``client_spawn_env``),
    #: kept beside the key so the gateway that adopts the keeper can vouch for it.
    session_token: str = field(default="", repr=False)
    #: :func:`launch_state`, set by ``launch()`` before :meth:`finish` records it.
    launch: dict[str, Any] = field(default_factory=dict)
    _inner: list[str] = field(default_factory=list, repr=False)

    def prepare(self) -> bool:
        """Create the keepers directory; False when this launch cannot use a keeper.

        Blocking (a mkdir and an lstat); run off the event loop.
        """
        try:
            records.ensure_dir(self.home)
        except OSError:
            logger.warning("chat keeper: the keepers directory is unusable", exc_info=True)
            return False
        if not records.socket_path_fits(self.instance, self.home):
            logger.warning("chat keeper: the socket path is too long for AF_UNIX on this host")
            return False
        return True

    def wrap(self, argv: list[str]) -> list[str]:
        """The launcher's argv; *argv*, the adapter's, is handed over on its stdin."""
        self._inner = list(argv)
        return keeper_argv(self.instance, records.keepers_dir(self.home), self.adopt_secs)

    async def finish(self, launcher: Any) -> KeeperProcess:
        """Hand the launcher its secret, wait for the keeper, record it and attach.

        Raises ``KeeperStartError`` when the keeper did not come up; the caller's
        failed-spawn cleanup then runs as for any spawn that failed.
        """
        boot = json.dumps(
            {"secret": self.secret.hex(), "session_key": self.session_key, "argv": self._inner}
        )
        try:
            launcher.stdin.write(boot.encode("utf-8") + b"\n")
            await launcher.stdin.drain()
            launcher.stdin.close()
            line = await asyncio.wait_for(launcher.stdout.readline(), READY_TIMEOUT_SECS)
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            raise KeeperStartError(f"the chat keeper did not report ready ({exc!r})") from exc
        try:
            await asyncio.wait_for(launcher.wait(), LAUNCHER_EXIT_SECS)
        except asyncio.TimeoutError as exc:
            raise KeeperStartError("the chat keeper's launcher did not exit") from exc
        try:
            ready = json.loads(line) if line else {}
        except ValueError:
            ready = {}
        if not isinstance(ready, dict) or ready.get("ready") is not True:
            detail = ready.get("error") if isinstance(ready, dict) else None
            if not detail and launcher.stderr is not None:
                try:
                    tail = await asyncio.wait_for(launcher.stderr.read(4096), 1.0)
                    detail = tail.decode("utf-8", "replace").strip().splitlines()[-1:] or ""
                except (asyncio.TimeoutError, OSError):
                    detail = ""
            raise KeeperStartError(f"the chat keeper failed to start: {detail or 'no report'}")
        keeper_pid = int(ready["keeper_pid"])
        adapter_pid = int(ready["adapter_pid"])
        from kiro_crew import platform_compat

        start = platform_compat.get_process_start_id(keeper_pid)
        if not start:
            raise KeeperStartError("the chat keeper's start identity could not be read")
        try:
            return await self._record_and_attach(keeper_pid, start, adapter_pid)
        except BaseException:
            # The keeper is up but this launch will not use it: end its group now
            # rather than leave a second adapter to its orphan timer.
            await asyncio.to_thread(_end_group, keeper_pid, start)
            await asyncio.to_thread(records.remove_files, self.instance, self.home)
            raise

    async def _record_and_attach(
        self, keeper_pid: int, start: str, adapter_pid: int
    ) -> KeeperProcess:
        record = records.KeeperRecord(
            instance=self.instance,
            session_key=self.session_key,
            backend=self.backend,
            keeper_pid=keeper_pid,
            keeper_start=start,
            adapter_pid=adapter_pid,
            socket=str(records.socket_path(self.instance, self.home)),
            wire=wire.KEEPER_WIRE_VERSION,
            created=time.time(),
            process_instance=self.process_instance,  # pid-owner-ok: keeper launch id
            launch=dict(self.launch),
        )
        await asyncio.to_thread(records.write_key, self.instance, self.secret, self.home)
        if self.session_token:
            await asyncio.to_thread(
                records.write_session_token, self.instance, self.session_token, self.home
            )
        await asyncio.to_thread(records.write_record, record, self.home)
        process = await attach(record, self.secret, hold=False, stdout_limit=self.stdout_limit)
        logger.info(
            "chat keeper %s started for %s: keeper pid %d, adapter pid %d",
            self.instance,
            self.backend,
            keeper_pid,
            adapter_pid,
        )
        return process
