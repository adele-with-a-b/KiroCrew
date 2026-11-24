"""The chat keeper process: owns one adapter's stdio and outlives the gateway.

Started by the gateway (``chat_keeper.spawn``) as::

    python -I -c <boot> --kirocrew-chat-keeper --instance ID --dir DIR --adopt-secs N

It reads one bootstrap line from stdin: the secret and the session key, which never
ride argv or the environment (any same-uid process can read those), and the adapter's
argv, which stays off the keeper's own command line (see :func:`parse_args`). It forks so that it is
no gateway's child, calls ``setsid`` so it leads a session and a group of its own,
starts the adapter as its child in that group, binds ``DIR/ID.s``, and writes one
ready line to stdout. From then on it relays: adapter stdout lines, numbered and
journaled, to the attached gateway; the gateway's stdin bytes to the adapter.

What it knows about ACP is the JSON-RPC envelope (``id``, ``method``,
``result``/``error``) and three names it caches (``session/new``/``load``/``resume``
answers and three ``session/update`` kinds). Design:
``docs/system-specs/modules/runtime-ownership.md``, "Chats that outlive the gateway".
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from kiro_crew.chat_keeper import (
    FIRST_ATTACH_SECS,
    KEEPER_ARGV_MARKER,
    KEEPER_TERM_GRACE_SECS,
    SOCKET_SUFFIX,
    wire,
)

logger = logging.getLogger("kiro_crew.chat_keeper.keeper")

#: The journal's bound. Lines past the last ack beyond this are dropped oldest
#: first and the journal is marked as having a gap.
KEEPER_JOURNAL_MAX_BYTES = 64 * 1024 * 1024

#: An attachment whose unsent bytes pass this is dropped: the gateway stopped
#: reading, and the journal, not the socket buffer, is what holds frames for it.
_ATTACHMENT_MAX_BUFFER = 128 * 1024 * 1024

#: How long the handshake may take once a connection is accepted.
_HANDSHAKE_SECS = 10.0

#: Bounds on what the keeper remembers per session and per open request.
_MAX_CACHED_SESSIONS = 64
_MAX_OPEN_REQUESTS = 4096

#: The bound on the bytes of ``tool_call`` lines kept for re-delivered requests.
_TOOL_CALL_CACHE_MAX_BYTES = 16 * 1024 * 1024

#: The bootstrap line's bound.
_BOOTSTRAP_MAX_BYTES = 4 * 1024 * 1024

#: Methods whose answer is a session's state, cached for an adopting gateway.
SESSION_ANSWER_METHODS = frozenset({"session/new", "session/load", "session/resume"})
#: The ``session/update`` kinds cached per session (the latest of each).
CACHED_UPDATE_KINDS = frozenset({"current_mode_update", "config_option_update", "usage_update"})
#: The one request whose answer belongs to the session rather than to the
#: attachment that sent it: an adopting gateway reads it as the end of the turn.
METHOD_SESSION_PROMPT = "session/prompt"
#: Requests whose success changes the session's state without a ``session/update``
#: from claude-agent-acp: the keeper writes the change into what it caches, so an
#: adopting gateway shows the mode and model the old one last set.
METHOD_SET_MODE = "session/set_mode"
METHOD_SET_MODEL = "session/set_model"
_SESSION_STATE_METHODS = frozenset({METHOD_SET_MODE, METHOD_SET_MODEL})
#: The adapter's request for a tool approval. The keeper times how long each open
#: one has gone unanswered with no gateway attached, for the restart receipt.
METHOD_REQUEST_PERMISSION = "session/request_permission"


@dataclass(frozen=True)
class KeeperArgs:
    instance: str
    directory: Path
    adopt_secs: float
    argv: list[str]


def parse_args(argv: list[str]) -> KeeperArgs:
    """The keeper's command line; ``SystemExit(2)`` for anything malformed.

    The adapter's argv is not on it: it rides the bootstrap (:func:`main`). A keeper is
    reparented to init by design, and a command line carrying the sandbox-wrapped
    argv (with its ``kirocrew_sandbox_`` profile path) reads, to the orphan-MCP sweep
    of every gateway on the host, of any version and any data home, as an escaped
    sandboxed MCP server.
    """
    if not argv or argv[0] != KEEPER_ARGV_MARKER:
        raise SystemExit(2)
    head = argv[1:]
    opts: dict[str, str] = {}
    it = iter(head)
    for token in it:
        if not token.startswith("--"):
            raise SystemExit(2)
        try:
            opts[token[2:]] = next(it)
        except StopIteration:
            raise SystemExit(2) from None
    try:
        instance = opts["instance"]
        directory = Path(opts["dir"])
        adopt_secs = float(opts["adopt-secs"])
    except (KeyError, ValueError):
        raise SystemExit(2) from None
    if not instance.isalnum() or adopt_secs <= 0:
        raise SystemExit(2)
    return KeeperArgs(instance=instance, directory=directory, adopt_secs=adopt_secs, argv=[])


def _id_key(value: Any) -> str | None:
    """A JSON-RPC id as a dict key (ints and strings never collide)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, str)):
        return json.dumps(value)
    return None


def _parse_line(line: bytes) -> dict[str, Any] | None:
    try:
        body = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


@dataclass
class _Entry:
    seq: int
    line: bytes
    #: The attachment generation this line may be delivered to; None for any.
    owner: int | None


@dataclass
class _Attachment:
    writer: asyncio.StreamWriter
    generation: int
    held: bool
    #: Seqs delivered while held (responses to this attachment's own requests).
    early: set[int] = field(default_factory=set)
    stdin_partial: bytearray = field(default_factory=bytearray)


class Keeper:
    """One keeper's state and its event loop's coroutines."""

    def __init__(
        self, args: KeeperArgs, secret: bytes, session_key: str, adapter_umask: int = -1
    ) -> None:
        self.args = args
        #: The umask the gateway launched the keeper with, handed back to the adapter:
        #: the keeper's own 0o077 is for its files, not for what the agent creates.
        self.adapter_umask = adapter_umask
        self.secret = secret
        self.session_key = session_key
        self.sock_path = args.directory / f"{args.instance}{SOCKET_SUFFIX}"
        self.adapter: asyncio.subprocess.Process | None = None
        self.seq = 0
        self.journal: collections.deque[_Entry] = collections.deque()
        self.journal_bytes = 0
        self.last_ack = 0
        #: The highest seq dropped from the journal before any gateway acked it.
        self.dropped_through = 0
        self.open_agent_requests: dict[str, tuple[int, bytes]] = {}
        #: When each open permission request arrived (monotonic), by request id.
        self.permission_since: dict[str, float] = {}
        #: When the last attachment detached (monotonic); None while one is attached
        #: and before the first ever detached.
        self.detached_at: float | None = None
        #: Whether a ``session/prompt`` was still unanswered at that detach: a turn
        #: ran on through the gap.
        self.turn_at_detach = False
        #: The ``tool_call`` line of each recent tool call, by ``toolCallId``: what a
        #: re-delivered permission request is judged and shown with.
        self.tool_calls: collections.OrderedDict[str, tuple[int, bytes]] = collections.OrderedDict()
        self.tool_call_bytes = 0
        self.client_open: collections.OrderedDict[
            str, tuple[str, int, str, dict[str, Any] | None]
        ] = collections.OrderedDict()
        self.max_client_id = 0
        self.session_answers: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self.session_updates: dict[str, dict[str, bytes]] = {}
        self.attach: _Attachment | None = None
        self.generation = 0
        self.returncode: int | None = None
        self.stdout_done = asyncio.Event()
        self.exit_delivered = False
        self.terminating = False
        self._self_signalled = False
        self._orphan_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._server: asyncio.AbstractServer | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> dict[str, Any]:
        """Start the adapter and the listener; the ready line's body."""
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        for ignored in (signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(ignored, lambda: None)
        self.adapter = await asyncio.create_subprocess_exec(
            *self.args.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            umask=self.adapter_umask,
        )
        self._server = await asyncio.start_unix_server(self._on_connect, path=str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        self._spawn(self._pump_stdout())
        self._spawn(self._pump_stderr())
        self._start_orphan_timer(min(FIRST_ATTACH_SECS, self.args.adopt_secs))
        return {"ready": True, "keeper_pid": os.getpid(), "adapter_pid": self.adapter.pid}

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _on_sigterm(self) -> None:
        if self._self_signalled:
            return
        self._spawn(self.terminate("SIGTERM"))

    def _start_orphan_timer(self, secs: float) -> None:
        self._cancel_orphan_timer()
        if self.terminating:
            return
        self._orphan_task = self._spawn(self._orphan_after(secs))

    def _cancel_orphan_timer(self) -> None:
        task = self._orphan_task
        self._orphan_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _orphan_after(self, secs: float) -> None:
        await asyncio.sleep(secs)
        logger.warning("no gateway attached for %.0fs; ending the chat", secs)
        await self.terminate("orphaned")

    async def terminate(self, reason: str) -> None:
        """End the adapter's group, hand an attached gateway the exit, and exit."""
        if self.terminating:
            return
        self.terminating = True
        self._cancel_orphan_timer()
        try:
            logger.info("terminating (%s)", reason)
            adapter = self.adapter
            if adapter is not None and adapter.returncode is None:
                self._signal_group(signal.SIGTERM)
                try:
                    await asyncio.wait_for(adapter.wait(), KEEPER_TERM_GRACE_SECS)
                except asyncio.TimeoutError:
                    pass
            if adapter is not None and adapter.returncode is not None:
                self.returncode = adapter.returncode
                try:
                    await asyncio.wait_for(self.stdout_done.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
                await self._deliver_exit()
        finally:
            # ``terminating`` has disarmed the orphan timer, so no error above may
            # stop the keeper from ending.
            self.finish()

    def _signal_group(self, sig: int) -> None:
        self._self_signalled = True
        try:
            _signal_own_group()(sig)
        except Exception:  # noqa: BLE001 - teardown must still reach os._exit
            logger.warning("could not signal the keeper's group", exc_info=True)

    def finish(self) -> NoReturn:
        """Remove this keeper's files and end its whole group, itself included.

        Every step is best effort and ``os._exit`` always runs: a keeper that failed to
        exit would outlive its record's purpose while every sweep skips its pid.
        """
        try:
            self.remove_files()
            logging.shutdown()
            self._signal_group(signal.SIGKILL)
        finally:
            os._exit(0)

    def remove_files(self) -> None:
        for suffix in (SOCKET_SUFFIX, ".json", ".key", ".log"):
            with _suppress_missing():
                (self.args.directory / f"{self.args.instance}{suffix}").unlink()

    # ── adapter side ─────────────────────────────────────────────────────────

    async def _pump_stdout(self) -> None:
        adapter = self.adapter
        assert adapter is not None and adapter.stdout is not None
        partial = bytearray()
        #: Inside a line already dropped as oversize: its rest is dropped up to its newline.
        skipping = False
        while True:
            chunk = await adapter.stdout.read(256 * 1024)
            if not chunk:
                break
            if skipping:
                at = chunk.find(b"\n")
                if at < 0:
                    continue
                chunk = chunk[at + 1 :]
                skipping = False
            partial.extend(chunk)
            while True:
                at = partial.find(b"\n")
                if at < 0:
                    break
                line = bytes(partial[: at + 1])
                del partial[: at + 1]
                self._on_stdout_line(line)
            if len(partial) > wire.MAX_FRAME_BYTES:
                self.seq += 1
                self._drop_oversize(self.seq)
                partial.clear()
                skipping = True
        if partial:
            self._on_stdout_line(bytes(partial) + b"\n")
        self.stdout_done.set()
        self.returncode = await adapter.wait()
        logger.info("adapter exited with %s", self.returncode)
        await self._on_adapter_exit()

    async def _pump_stderr(self) -> None:
        adapter = self.adapter
        assert adapter is not None and adapter.stderr is not None
        while True:
            chunk = await adapter.stderr.read(64 * 1024)
            if not chunk:
                return
            att = self.attach
            if att is not None:
                self._write(att, wire.frame(wire.K_STDERR, chunk))

    def _on_stdout_line(self, line: bytes) -> None:
        self.seq += 1
        if len(line) + 16 > wire.MAX_FRAME_BYTES:
            # Not tracked either: a request or answer kept from it could never be framed.
            self._drop_oversize(self.seq)
            return
        entry = _Entry(self.seq, line, self._track_stdout(self.seq, line))
        self.journal.append(entry)
        self.journal_bytes += len(line)
        while self.journal_bytes > KEEPER_JOURNAL_MAX_BYTES and self.journal:
            dropped = self.journal.popleft()
            self.journal_bytes -= len(dropped.line)
            if dropped.seq > self.last_ack:
                self.dropped_through = max(self.dropped_through, dropped.seq)
        att = self.attach
        if att is None:
            return
        if not att.held:
            self._deliver(att, entry)
        elif entry.owner == att.generation:
            self._deliver(att, entry)
            att.early.add(entry.seq)

    def _drop_oversize(self, seq: int) -> None:
        """A line too large for a frame reaches no gateway: it counts as a gap."""
        logger.warning("dropped an oversize adapter line (seq %d)", seq)
        self.dropped_through = max(self.dropped_through, seq)

    def _track_stdout(self, seq: int, line: bytes) -> int | None:
        msg = _parse_line(line)
        if msg is None:
            return None
        key = _id_key(msg.get("id"))
        method = msg.get("method")
        if isinstance(method, str) and key is not None:
            if len(self.open_agent_requests) < _MAX_OPEN_REQUESTS:
                self.open_agent_requests[key] = (seq, line)
                if method == METHOD_REQUEST_PERMISSION:
                    self.permission_since[key] = time.monotonic()
            else:
                # Not re-delivered to an adopting gateway: its card would not open.
                logger.warning(
                    "over %d open adapter requests; %s not kept", _MAX_OPEN_REQUESTS, key
                )
            return None
        if method is None and key is not None:
            entry = self.client_open.pop(key, None)
            if entry is None:
                return None
            req_method, generation, sid, params = entry
            if params is not None and sid and "result" in msg and msg.get("error") is None:
                self._note_session_state(req_method, sid, params)
            if req_method in SESSION_ANSWER_METHODS and isinstance(msg.get("result"), dict):
                answered = msg["result"].get("sessionId") or sid
                if isinstance(answered, str) and answered:
                    self.session_answers[answered] = line
                    self.session_answers.move_to_end(answered)
                    while len(self.session_answers) > _MAX_CACHED_SESSIONS:
                        self.session_answers.popitem(last=False)
            if req_method == METHOD_SESSION_PROMPT:
                return None
            return generation
        if method == "session/update":
            params = msg.get("params")
            if isinstance(params, dict):
                update_sid = params.get("sessionId")
                update = params.get("update")
                kind = update.get("sessionUpdate") if isinstance(update, dict) else None
                if kind == "tool_call" and isinstance(update, dict):
                    self._note_tool_call(update.get("toolCallId"), seq, line)
                elif kind == "tool_call_update" and isinstance(update, dict):
                    if update.get("status") in ("completed", "failed"):
                        self._forget_tool_call(update.get("toolCallId"))
                if isinstance(update_sid, str) and kind in CACHED_UPDATE_KINDS:
                    if update_sid in self.session_updates or len(self.session_updates) < (
                        _MAX_CACHED_SESSIONS
                    ):
                        self.session_updates.setdefault(update_sid, {})[kind] = line
        return None

    def _note_tool_call(self, call_id: object, seq: int, line: bytes) -> None:
        if not isinstance(call_id, str) or not call_id:
            return
        self._forget_tool_call(call_id)
        self.tool_calls[call_id] = (seq, line)
        self.tool_call_bytes += len(line)
        while self.tool_calls and (
            len(self.tool_calls) > _MAX_OPEN_REQUESTS
            or self.tool_call_bytes > _TOOL_CALL_CACHE_MAX_BYTES
        ):
            _, (_, dropped) = self.tool_calls.popitem(last=False)
            self.tool_call_bytes -= len(dropped)

    def _forget_tool_call(self, call_id: object) -> None:
        """Drop a finished call's line: no request for it can still be open."""
        if isinstance(call_id, str):
            found = self.tool_calls.pop(call_id, None)
            if found is not None:
                self.tool_call_bytes -= len(found[1])

    def _tool_call_of(self, request: bytes) -> tuple[int, bytes] | None:
        """The ``tool_call`` line a request names, when a replay would not carry it."""
        msg = _parse_line(request)
        params = msg.get("params") if msg is not None else None
        call = params.get("toolCall") if isinstance(params, dict) else None
        call_id = call.get("toolCallId") if isinstance(call, dict) else None
        found = self.tool_calls.get(call_id) if isinstance(call_id, str) else None
        return found if found is not None and found[0] <= self.last_ack else None

    def _note_session_state(self, method: str, sid: str, params: dict[str, Any]) -> None:
        """Write a ``set_mode`` / ``set_model`` the adapter accepted into the cache."""
        if method == METHOD_SET_MODE and isinstance(params.get("modeId"), str):
            if sid not in self.session_updates and len(self.session_updates) >= (
                _MAX_CACHED_SESSIONS
            ):
                return
            update = {"sessionUpdate": "current_mode_update", "currentModeId": params["modeId"]}
            body = {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": sid, "update": update},
            }
            line = json.dumps(body).encode("utf-8") + b"\n"
            self.session_updates.setdefault(sid, {})["current_mode_update"] = line
        elif method == METHOD_SET_MODEL and isinstance(params.get("modelId"), str):
            answer = _parse_line(self.session_answers.get(sid, b""))
            result = answer.get("result") if answer is not None else None
            models = result.get("models") if isinstance(result, dict) else None
            if answer is None or not isinstance(models, dict):
                return
            models["currentModelId"] = params["modelId"]
            self.session_answers[sid] = json.dumps(answer).encode("utf-8") + b"\n"

    def _track_stdin(self, att: _Attachment, data: bytes) -> None:
        att.stdin_partial.extend(data)
        while True:
            at = att.stdin_partial.find(b"\n")
            if at < 0:
                break
            line = bytes(att.stdin_partial[:at])
            del att.stdin_partial[: at + 1]
            self._track_stdin_line(att, line)
        if len(att.stdin_partial) > wire.MAX_FRAME_BYTES:
            att.stdin_partial.clear()

    def _track_stdin_line(self, att: _Attachment, line: bytes) -> None:
        msg = _parse_line(line)
        if msg is None:
            return
        raw_id = msg.get("id")
        key = _id_key(raw_id)
        method = msg.get("method")
        if isinstance(method, str) and key is not None:
            if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                self.max_client_id = max(self.max_client_id, raw_id)
            params = msg.get("params")
            sid = params.get("sessionId") if isinstance(params, dict) else None
            kept = params if method in _SESSION_STATE_METHODS and isinstance(params, dict) else None
            self.client_open[key] = (
                method,
                att.generation,
                sid if isinstance(sid, str) else "",
                kept,
            )
            while len(self.client_open) > _MAX_OPEN_REQUESTS:
                dropped, _ = self.client_open.popitem(last=False)
                logger.warning(
                    "over %d open gateway requests; forgot %s", _MAX_OPEN_REQUESTS, dropped
                )
        elif method is None and key is not None:
            self.open_agent_requests.pop(key, None)
            self.permission_since.pop(key, None)

    async def _on_adapter_exit(self) -> None:
        att = self.attach
        if att is not None and not att.held:
            await self._deliver_exit()
            self.finish()
        # Otherwise the exit waits for a gateway (resume delivers it) or the timer.

    async def _deliver_exit(self) -> None:
        att = self.attach
        if att is None or att.held or self.exit_delivered or self.returncode is None:
            return
        self.exit_delivered = True
        self._write(att, wire.json_frame(wire.K_EXIT, {"returncode": self.returncode}))
        try:
            # A drain returns below the high-water mark; with it at zero it returns
            # once the buffer is empty, so ``finish`` loses none of it.
            att.writer.transport.set_write_buffer_limits(high=0)
            await asyncio.wait_for(att.writer.drain(), 2.0)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass

    # ── gateway side ─────────────────────────────────────────────────────────

    def _write(self, att: _Attachment, data: bytes) -> None:
        if att.writer.is_closing():
            return
        att.writer.write(data)
        transport = att.writer.transport
        if transport.get_write_buffer_size() > _ATTACHMENT_MAX_BUFFER:
            logger.warning("the gateway stopped reading; dropping its attachment")
            transport.abort()

    def _deliver(self, att: _Attachment, entry: _Entry) -> None:
        if entry.owner is not None and entry.owner != att.generation:
            return
        self._write(att, wire.seq_frame(wire.K_STDOUT, entry.seq, entry.line))

    def _flush_backlog(self, att: _Attachment) -> None:
        """Deliver what this attachment is owed, in order (see the spec's Replay)."""
        owed = sorted(
            (seq, line) for seq, line in self.open_agent_requests.values() if seq <= self.last_ack
        )
        sent: set[int] = set()
        for seq, line in owed:
            # The old gateway read this request's tool_call and cached its shell
            # kind and raw params in memory; the new one gets the line first.
            tool_call = self._tool_call_of(line)
            if tool_call is not None and tool_call[0] not in sent:
                sent.add(tool_call[0])
                self._write(att, wire.seq_frame(wire.K_STDOUT, *tool_call))
            self._write(att, wire.seq_frame(wire.K_STDOUT, seq, line))
        for entry in self.journal:
            if entry.seq > self.last_ack and entry.seq not in att.early:
                # A journaled request whose tool_call the old gateway already read.
                tool_call = self._tool_call_of(entry.line) if b'"toolCall"' in entry.line else None
                if tool_call is not None and tool_call[0] not in sent:
                    sent.add(tool_call[0])
                    self._write(att, wire.seq_frame(wire.K_STDOUT, *tool_call))
                self._deliver(att, entry)
        self._write(att, wire.frame(wire.K_REPLAYED))

    def _ack(self, seq: int) -> None:
        if seq <= self.last_ack:
            return
        self.last_ack = min(seq, self.seq)
        while self.journal and self.journal[0].seq <= self.last_ack:
            dropped = self.journal.popleft()
            self.journal_bytes -= len(dropped.line)

    @property
    def gap(self) -> bool:
        """A line was dropped that no gateway has acked: a replay would miss it.

        A gateway that acks past the dropped line has moved past it as the live
        stream did, so the gap closes: a journal line it read live, or an oversize
        line no gateway could read, live or on a replay.
        """
        return self.dropped_through > self.last_ack

    def state(self) -> dict[str, Any]:
        adapter = self.adapter
        return {
            "session_key": self.session_key,
            "keeper_pid": os.getpid(),
            "adapter_pid": adapter.pid if adapter is not None else 0,
            "returncode": self.returncode,
            "max_client_id": self.max_client_id,
            "last_ack": self.last_ack,
            "seq": self.seq,
            "gap": self.gap,
            "open_agent_requests": len(self.open_agent_requests),
            "session_answers": {
                sid: line.decode("utf-8", "replace") for sid, line in self.session_answers.items()
            },
            "session_updates": {
                sid: {kind: line.decode("utf-8", "replace") for kind, line in kinds.items()}
                for sid, kinds in self.session_updates.items()
            },
            **self._gap_report(),
        }

    def _gap_report(self) -> dict[str, Any]:
        """How long no gateway was attached, and the turn and approvals that went on in it.

        Measured on this keeper's own monotonic clock, at the attach that reads it. A
        permission request asked before the detach waited from the detach; one asked
        during the gap, from when it arrived. Empty on a keeper's first attach.
        """
        detached_at = self.detached_at
        if detached_at is None:
            return {}
        now = time.monotonic()
        waits = [now - max(since, detached_at) for since in self.permission_since.values()]
        return {
            "detached_secs": round(now - detached_at, 1),
            "turn_at_detach": self.turn_at_detach,
            "permission_waits": len(waits),
            "permission_wait_secs": round(max(waits), 1) if waits else 0.0,
        }

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except Exception:
            logger.warning("connection ended with an error", exc_info=True)
        finally:
            if not writer.is_closing():
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self

        if check_peer_is_self(writer.get_extra_info("socket")) is not PeerCredResult.MATCH:
            logger.warning("refused a connection from another user, or an unverifiable one")
            return
        keeper_nonce = os.urandom(wire.NONCE_BYTES)
        hello = {
            "wire": wire.KEEPER_WIRE_VERSION,
            "instance": self.args.instance,
            "nonce": keeper_nonce.hex(),
        }
        writer.write(wire.json_frame(wire.K_HELLO, hello))
        decoder = wire.FrameDecoder(wire.MAX_HANDSHAKE_BYTES)
        try:
            kind, payload = await asyncio.wait_for(
                _read_one(reader, decoder), timeout=_HANDSHAKE_SECS
            )
            body = wire.parse_json(payload)
            gateway_nonce = bytes.fromhex(str(body.get("nonce", "")))
        except (asyncio.TimeoutError, wire.WireError, ValueError, EOFError):
            return
        if kind != wire.K_AUTH or body.get("wire") != wire.KEEPER_WIRE_VERSION:
            writer.write(wire.json_frame(wire.K_REFUSED, {"reason": "wire"}))
            return
        expected = wire.proof(
            self.secret, wire.ROLE_GATEWAY, self.args.instance, keeper_nonce, gateway_nonce
        )
        if len(gateway_nonce) != wire.NONCE_BYTES or not wire.proof_matches(
            expected, body.get("proof")
        ):
            logger.warning("refused an attach that failed authentication")
            writer.write(wire.json_frame(wire.K_REFUSED, {"reason": "auth"}))
            return
        old = self.attach
        if old is not None:
            old.writer.transport.abort()
        self.generation += 1
        att = _Attachment(writer=writer, generation=self.generation, held=bool(body.get("hold")))
        self.attach = att
        self._cancel_orphan_timer()
        welcome = {
            "proof": wire.proof(
                self.secret, wire.ROLE_KEEPER, self.args.instance, keeper_nonce, gateway_nonce
            ),
            "state": self.state(),
        }
        self.detached_at = None
        writer.write(wire.json_frame(wire.K_WELCOME, welcome))
        logger.info("attached generation %d (held=%s)", att.generation, att.held)
        if not att.held:
            self._flush_backlog(att)
            if self.returncode is not None and self.stdout_done.is_set():
                await self._deliver_exit()
                self.finish()
        decoder.set_limit(wire.MAX_FRAME_BYTES)
        try:
            await self._serve(att, reader, decoder)
        finally:
            if self.attach is att:
                self.attach = None
                self.detached_at = time.monotonic()
                self.turn_at_detach = any(
                    entry[0] == METHOD_SESSION_PROMPT for entry in self.client_open.values()
                )
                logger.info("generation %d detached", att.generation)
                self._start_orphan_timer(self.args.adopt_secs)

    async def _serve(
        self, att: _Attachment, reader: asyncio.StreamReader, decoder: wire.FrameDecoder
    ) -> None:
        adapter = self.adapter
        assert adapter is not None
        while True:
            data = await reader.read(256 * 1024)
            if not data:
                return
            try:
                frames = decoder.feed(data)
            except wire.WireError:
                logger.warning("dropped an attachment that broke the wire")
                return
            for kind, payload in frames:
                if self.attach is not att:
                    return
                if kind == wire.K_STDIN:
                    self._track_stdin(att, payload)
                    try:
                        assert adapter.stdin is not None
                        adapter.stdin.write(payload)
                        await adapter.stdin.drain()
                    except (ConnectionError, OSError, AssertionError):
                        pass
                elif kind == wire.K_STDIN_EOF:
                    if adapter.stdin is not None and not adapter.stdin.is_closing():
                        adapter.stdin.close()
                elif kind == wire.K_ACK:
                    self._ack(wire.split_seq(payload)[0])
                elif kind == wire.K_DETACH:
                    self._ack(wire.split_seq(payload)[0])
                    return
                elif kind == wire.K_RESUME:
                    if att.held:
                        att.held = False
                        self._flush_backlog(att)
                        if self.returncode is not None and self.stdout_done.is_set():
                            await self._deliver_exit()
                            self.finish()
                elif kind == wire.K_TERMINATE:
                    self._spawn(self.terminate("requested by the gateway"))
                else:
                    logger.warning("dropped an attachment that sent frame kind %d", kind)
                    return


async def _read_one(reader: asyncio.StreamReader, decoder: wire.FrameDecoder) -> tuple[int, bytes]:
    while True:
        data = await reader.read(64 * 1024)
        if not data:
            raise EOFError
        frames = decoder.feed(data)
        if len(frames) > 1 or (frames and decoder.pending):
            # The gateway sends nothing after its auth until the welcome arrives.
            raise wire.WireError("frames after the auth frame, before the welcome")
        if frames:
            return frames[0]


class _suppress_missing:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, FileNotFoundError)


def _read_bootstrap(fd: int) -> dict[str, Any]:
    data = bytearray()
    while b"\n" not in data and len(data) < _BOOTSTRAP_MAX_BYTES:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data.extend(chunk)
    line = bytes(data).split(b"\n", 1)[0]
    body = _parse_line(line)
    if body is None:
        raise SystemExit(2)
    return body


def _write_ready(body: dict[str, Any]) -> None:
    os.write(1, json.dumps(body).encode("utf-8") + b"\n")


def _detach_stdio(log_path: Path) -> None:
    devnull = os.open(os.devnull, os.O_RDWR)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(log_fd, 2)
    os.close(devnull)
    os.close(log_fd)


#: ``platform_compat.signal_own_process_group``, bound once by :func:`main` before the
#: keeper reports ready. Teardown can run minutes later, after an update replaced the
#: installed package, so it must never be the first import of that module graph.
_SIGNAL_OWN_GROUP: Any = None


def _signal_own_group() -> Any:
    if _SIGNAL_OWN_GROUP is None:
        _bind_teardown()
    return _SIGNAL_OWN_GROUP


def _bind_teardown() -> None:
    global _SIGNAL_OWN_GROUP
    from kiro_crew import platform_compat

    _SIGNAL_OWN_GROUP = platform_compat.signal_own_process_group


def main(argv: list[str] | None = None) -> NoReturn:
    """The keeper's entry point; never returns."""
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    _bind_teardown()
    inherited_umask = os.umask(0o077)
    boot = _read_bootstrap(0)
    try:
        secret = bytes.fromhex(str(boot.get("secret", "")))
    except ValueError:
        raise SystemExit(2) from None
    session_key = boot.get("session_key")
    inner = boot.get("argv")
    if (
        len(secret) != wire.SECRET_BYTES
        or not isinstance(session_key, str)
        or not isinstance(inner, list)
        or not inner
        or not all(isinstance(token, str) for token in inner)
    ):
        raise SystemExit(2)
    args = KeeperArgs(
        instance=args.instance, directory=args.directory, adopt_secs=args.adopt_secs, argv=inner
    )
    # The launcher's child becomes the keeper: no gateway's child, so nothing in the
    # gateway's own lifecycle (its child watcher, a transport's close) can end it.
    if os.fork():
        os._exit(0)
    os.setsid()
    log_path = args.directory / f"{args.instance}.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(process)d %(levelname)s %(message)s",
    )
    keeper = Keeper(args, secret, session_key, adapter_umask=inherited_umask)

    async def run() -> None:
        try:
            ready = await keeper.start()
        except OSError as exc:
            _write_ready({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
            keeper.remove_files()
            os._exit(1)
        _write_ready(ready)
        _detach_stdio(log_path)
        logger.info("keeper %s ready: adapter pid %d", args.instance, ready["adapter_pid"])
        await asyncio.Event().wait()

    asyncio.run(run())
    os._exit(0)
