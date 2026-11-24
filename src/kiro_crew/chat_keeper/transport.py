"""The gateway's end of a keeper: authenticate, attach, and look like a subprocess.

:func:`attach` connects to a keeper named by a :class:`~.records.KeeperRecord`,
checks the kernel's word on who answered (the peer pid must be the record's keeper
pid with its recorded start identity, the peer uid this process's own), runs the
mutual HMAC handshake, and returns a :class:`KeeperProcess`. That object has the
members ``AcpClient`` reads on an ``asyncio.subprocess.Process`` -- ``pid``,
``returncode``, ``stdin`` (a real ``asyncio.StreamWriter``), ``stdout``, ``stderr``,
``wait()``, ``kill()``, ``terminate()``, ``send_signal()`` -- so nothing above it can
tell a keeper from a pipe.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.chat_keeper import wire
from kiro_crew.chat_keeper.records import KeeperRecord

logger = logging.getLogger("kiro_crew.chat_keeper")

#: How long the connect and the handshake may take.
ATTACH_TIMEOUT_SECS = 10.0

#: How long a detach waits for its frame to leave before it closes anyway.
DETACH_DRAIN_SECS = 2.0

#: The exit status reported for a keeper whose connection ended with no exit frame.
LOST_RETURNCODE = -int(platform_compat.SIGKILL)

#: The exit status this gateway reads once it detached on purpose: the keeper and
#: its adapter are alive, and they are the next gateway's to adopt.
DETACHED_RETURNCODE = 0


class KeeperAttachError(Exception):
    """An attach the gateway refused, or the keeper did; ``reason`` names which check."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"chat keeper attach refused: {reason}{': ' + detail if detail else ''}")
        self.reason = reason


#: The bound on a gap the welcome may report: past a keeper's adopt window by far.
_GAP_SECS_MAX = 7 * 24 * 3600.0


def _gap_secs(value: object) -> float | None:
    """A reported gap length in seconds, or None when absent or not a sane number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= _GAP_SECS_MAX:  # also refuses NaN
        return None
    return float(value)


def _gap_count(value: object) -> int:
    """A reported count of waiting requests, 0 when absent or malformed."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


@dataclass(frozen=True)
class AttachState:
    """The keeper's own report, sent inside its authenticated welcome."""

    session_key: str
    keeper_pid: int
    adapter_pid: int
    returncode: int | None
    max_client_id: int
    last_ack: int
    seq: int
    gap: bool
    open_agent_requests: int
    session_answers: dict[str, str] = field(default_factory=dict)
    session_updates: dict[str, dict[str, str]] = field(default_factory=dict)
    #: How long no gateway was attached before this attach; None on a keeper's first
    #: attach, or from a keeper too old to say.
    detached_secs: float | None = None
    #: Whether a turn was still running when that gap began.
    turn_at_detach: bool = False
    #: Open permission requests that waited through that gap, and the longest wait.
    permission_waits: int = 0
    permission_wait_secs: float = 0.0

    @classmethod
    def from_json(cls, body: object) -> "AttachState":
        if not isinstance(body, dict):
            raise KeeperAttachError("welcome", "no state")
        try:
            returncode = body.get("returncode")
            answers = body.get("session_answers") or {}
            updates = body.get("session_updates") or {}
            # The gap fields mean nothing without the gap they describe.
            detached = _gap_secs(body.get("detached_secs"))
            gap_known = detached is not None
            return cls(
                detached_secs=detached,
                turn_at_detach=gap_known and body.get("turn_at_detach") is True,
                permission_waits=_gap_count(body.get("permission_waits")) if gap_known else 0,
                permission_wait_secs=(
                    (_gap_secs(body.get("permission_wait_secs")) or 0.0) if gap_known else 0.0
                ),
                session_key=str(body["session_key"]),
                keeper_pid=int(body["keeper_pid"]),
                adapter_pid=int(body["adapter_pid"]),
                returncode=None if returncode is None else int(returncode),
                max_client_id=int(body["max_client_id"]),
                last_ack=int(body["last_ack"]),
                seq=int(body["seq"]),
                gap=bool(body["gap"]),
                open_agent_requests=int(body["open_agent_requests"]),
                session_answers={str(k): str(v) for k, v in dict(answers).items()},
                session_updates={
                    str(sid): {str(k): str(v) for k, v in dict(kinds).items()}
                    for sid, kinds in dict(updates).items()
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise KeeperAttachError("welcome", "malformed state") from exc

    def session_answer(self, session_id: str) -> dict[str, Any] | None:
        """The cached ``result`` of the last session/new, load or resume of *session_id*."""
        line = self.session_answers.get(session_id)
        try:
            body = json.loads(line) if line else None
        except ValueError:
            return None
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else None

    def cached_updates(self, session_id: str) -> list[dict[str, Any]]:
        """The cached ``session/update`` frames of *session_id*, as parsed objects."""
        out: list[dict[str, Any]] = []
        for line in self.session_updates.get(session_id, {}).values():
            try:
                body = json.loads(line)
            except ValueError:
                continue
            if isinstance(body, dict):
                out.append(body)
        return out


class SeqStreamReader(asyncio.StreamReader):
    """A ``StreamReader`` fed whole numbered lines, which knows what was consumed.

    A line is consumed once a reader has taken every byte of it out of the buffer,
    by whatever read method. ``consumed_seq`` is the highest sequence number the
    keeper may drop: every line numbered at or below it was consumed. Lines do not
    arrive in number order on a held attach: the answers to this gateway's own
    requests come first, the older backlog after the resume. So until the keeper
    says the backlog is out (``K_REPLAYED``) and the reader is past it,
    ``consumed_seq`` stays at or below *ceiling*, the ack the keeper reported at
    attach, and it is never above a line still in the buffer.
    """

    def __init__(
        self, limit: int, loop: asyncio.AbstractEventLoop, *, ceiling: int | None = None
    ) -> None:
        super().__init__(limit=limit, loop=loop)
        self._fed = 0
        self._ends: collections.deque[tuple[int, int]] = collections.deque()
        self._max_consumed = 0
        self._last_line = 0
        self._ceiling = ceiling
        self._replayed_at: int | None = None

    def feed_line(self, seq: int, line: bytes) -> None:
        self._fed += len(line)
        self._ends.append((self._fed, seq))
        self.feed_data(line)

    def mark_replayed(self) -> None:
        """The keeper sent its backlog: lines fed after here are numbered above it."""
        self._replayed_at = self._fed

    def discard_unconsumed(self) -> None:
        """Drop every line not yet consumed, so no reader streams what a detach left."""
        self._advance()
        self._buffer.clear()  # type: ignore[attr-defined]
        self._fed = 0
        self._ends.clear()
        if self._replayed_at is not None:
            self._replayed_at = 0

    def _advance(self) -> int:
        consumed = self._fed - len(self._buffer)  # type: ignore[attr-defined]
        while self._ends and self._ends[0][0] <= consumed:
            self._last_line = self._ends.popleft()[1]
            self._max_consumed = max(self._max_consumed, self._last_line)
        return consumed

    @property
    def last_consumed_line(self) -> int:
        """The sequence number of the line consumed last (0 before any)."""
        self._advance()
        return self._last_line

    @property
    def consumed_seq(self) -> int:
        consumed = self._advance()
        if (
            self._ceiling is not None
            and self._replayed_at is not None
            and consumed >= self._replayed_at
        ):
            self._ceiling = None
        safe = self._max_consumed
        if self._ends:
            safe = min(safe, min(seq for _, seq in self._ends) - 1)
        if self._ceiling is not None:
            safe = min(safe, self._ceiling)
        return max(0, safe)


class _KeeperProtocol(asyncio.streams.FlowControlMixin, asyncio.Protocol):
    def __init__(self, process: "KeeperProcess", leftover: list[tuple[int, bytes]], rest: bytes):
        super().__init__(loop=process._loop)
        self._process = process
        self._decoder = wire.FrameDecoder()
        self._leftover = leftover
        self._rest = rest
        self._closed: asyncio.Future[None] = process._loop.create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._process._transport = transport  # type: ignore[assignment]
        for kind, payload in self._leftover:
            self._process._on_frame(kind, payload)
        if self._rest:
            self.data_received(self._rest)

    def data_received(self, data: bytes) -> None:
        try:
            frames = self._decoder.feed(data)
        except wire.WireError:
            logger.warning("a chat keeper broke the wire; dropping its connection")
            transport = self._process._transport
            if transport is not None:
                transport.abort()
            return
        for kind, payload in frames:
            self._process._on_frame(kind, payload)

    def connection_lost(self, exc: Exception | None) -> None:
        self._process._on_lost()
        if not self._closed.done():
            self._closed.set_result(None)
        super().connection_lost(exc)

    def _get_close_waiter(self, stream: object) -> "asyncio.Future[None]":
        return self._closed


class _StdinTransport(asyncio.WriteTransport):
    """The adapter's stdin as a write transport: each write is one ``stdin`` frame."""

    def __init__(self, process: "KeeperProcess") -> None:
        super().__init__()
        self._process = process
        self._closing = False

    def _inner(self) -> asyncio.WriteTransport | None:
        return self._process._transport

    def write(self, data: bytes | bytearray | memoryview) -> None:
        inner = self._inner()
        if self._closing or self._process.detached or inner is None or inner.is_closing():
            return
        data = bytes(data)
        step = wire.MAX_FRAME_BYTES - 16
        for at in range(0, len(data), step):
            inner.write(wire.frame(wire.K_STDIN, data[at : at + step]))

    def writelines(self, list_of_data: Any) -> None:
        for data in list_of_data:
            self.write(data)

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        inner = self._inner()
        if not self._process.detached and inner is not None and not inner.is_closing():
            inner.write(wire.frame(wire.K_STDIN_EOF))

    def get_write_buffer_size(self) -> int:
        inner = self._inner()
        return inner.get_write_buffer_size() if inner is not None else 0

    def get_write_buffer_limits(self) -> tuple[int, int]:
        inner = self._inner()
        return inner.get_write_buffer_limits() if inner is not None else (0, 0)

    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        inner = self._inner()
        if inner is not None:
            inner.set_write_buffer_limits(high, low)

    def is_closing(self) -> bool:
        inner = self._inner()
        return self._closing or self._process.detached or inner is None or inner.is_closing()

    def close(self) -> None:
        """Close the adapter's stdin, as closing a pipe would: it reads EOF."""
        if self._closing:
            return
        self.write_eof()
        self._closing = True

    def abort(self) -> None:
        self._closing = True
        inner = self._inner()
        if inner is not None:
            inner.abort()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        inner = self._inner()
        return inner.get_extra_info(name, default) if inner is not None else default


class KeeperProcess:
    """An attached keeper, in the shape ``AcpClient`` reads a subprocess in."""

    def __init__(
        self,
        record: KeeperRecord,
        state: AttachState,
        loop: asyncio.AbstractEventLoop,
        *,
        held: bool,
        stdout_limit: int,
    ) -> None:
        self.record = record
        self.state = state
        self.pid = record.keeper_pid
        # Set only by the keeper's exit frame (or a lost connection): an adapter that
        # exited while no gateway was attached is reported after the journal.
        self.returncode: int | None = None
        self.held = held
        self.detached = False
        self._loop = loop
        self._transport: asyncio.Transport | None = None
        self._protocol: _KeeperProtocol | None = None
        self._exited = asyncio.Event()
        self.stdout = SeqStreamReader(
            limit=stdout_limit, loop=loop, ceiling=state.last_ack if held else None
        )
        self.stderr = asyncio.StreamReader(loop=loop)
        self.stdin: asyncio.StreamWriter

    # ── frames ───────────────────────────────────────────────────────────────

    def _on_frame(self, kind: int, payload: bytes) -> None:
        if kind == wire.K_STDOUT:
            if self.detached:
                return  # past the detach ack: the next gateway is replayed it
            seq, line = wire.split_seq(payload)
            self.stdout.feed_line(seq, line)
        elif kind == wire.K_REPLAYED:
            self.stdout.mark_replayed()
        elif kind == wire.K_STDERR:
            self.stderr.feed_data(payload)
        elif kind == wire.K_EXIT:
            try:
                code = wire.parse_json(payload).get("returncode")
                self._set_returncode(int(code) if code is not None else LOST_RETURNCODE)
            except (wire.WireError, TypeError, ValueError):
                self._set_returncode(LOST_RETURNCODE)

    def _set_returncode(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
        self._exited.set()

    def _on_lost(self) -> None:
        # Detached on purpose, the process reads as exited cleanly to this gateway,
        # so a turn still reading it unwinds as at any gateway exit (its streamed
        # text saved) while the process itself runs on for the next gateway.
        self._set_returncode(DETACHED_RETURNCODE if self.detached else LOST_RETURNCODE)
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    # ── what the client reads ────────────────────────────────────────────────

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, sig: int) -> None:
        """Ask the keeper to end its adapter's group (it signals its own group).

        The keeper, not this process, delivers the signal, because only the keeper
        may address the group it leads; a dead connection asks nothing, and the
        caller's group kill on ``pid`` still reaches the whole tree.
        """
        if sig in (signal.SIGTERM, platform_compat.SIGKILL):
            self.request_terminate()

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(platform_compat.SIGKILL)

    # ── keeper control ───────────────────────────────────────────────────────

    def _send(self, data: bytes) -> bool:
        transport = self._transport
        if self.detached or transport is None or transport.is_closing():
            return False
        transport.write(data)
        return True

    def request_terminate(self) -> bool:
        return self._send(wire.frame(wire.K_TERMINATE))

    def ack(self, seq: int) -> bool:
        """Tell the keeper it may drop every line up to *seq*."""
        return seq > 0 and self._send(wire.seq_frame(wire.K_ACK, seq))

    def resume(self) -> bool:
        """End the held delivery an adopting attach started with."""
        if not self.held:
            return False
        self.held = False
        return self._send(wire.frame(wire.K_RESUME))

    async def detach(self, ack: int) -> None:
        """Hand the keeper back: ack what was consumed, then close, killing nothing.

        What was read off the socket but not consumed is dropped here, before the
        first await: the keeper replays it to the next gateway, so a reader of this
        one must not also stream it.
        """
        transport = self._transport
        self.stdout.discard_unconsumed()
        if self.detached or transport is None:
            self.detached = True
            return
        if not transport.is_closing():
            transport.write(wire.seq_frame(wire.K_DETACH, max(0, ack)))
        self.detached = True
        # ``close`` flushes what is buffered before it closes, so the detach frame
        # leaves even when the keeper is slow to read; the wait only bounds that.
        transport.close()
        protocol = self._protocol
        if protocol is not None:
            try:
                await asyncio.wait_for(asyncio.shield(protocol._closed), DETACH_DRAIN_SECS)
            except asyncio.TimeoutError:
                transport.abort()
        logger.info(
            "detached from chat keeper %s (pid %d) at seq %d",
            self.record.instance,
            self.pid,
            ack,
        )


def _bound(sock: socket.socket, deadline: float) -> None:
    """Give *sock*'s next blocking call only what is left before *deadline*."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("the attach deadline passed")
    sock.settimeout(left)


def _recv_frame(
    sock: socket.socket,
    decoder: wire.FrameDecoder,
    queue: collections.deque[tuple[int, bytes]],
    deadline: float,
) -> tuple[int, bytes]:
    while not queue:
        _bound(sock, deadline)
        data = sock.recv(256 * 1024)
        if not data:
            raise KeeperAttachError("closed", "the keeper closed the connection")
        queue.extend(decoder.feed(data))
    return queue.popleft()


def _handshake(
    record: KeeperRecord, secret: bytes, hold: bool, deadline: float
) -> tuple[socket.socket, AttachState, list[tuple[int, bytes]], bytes]:
    """Connect, verify the peer, and run the mutual proof; blocking, run off-loop.

    Every blocking step is bounded by *deadline* (``time.monotonic``), fixed by the
    caller before the thread is queued, so a handshake the caller stopped waiting for
    cannot start a step later. The keeper swaps its attachment when the proof checks,
    before its welcome, so one that times out waiting for the welcome may already
    hold the attachment; closing the socket then leaves the keeper detached.
    """
    from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self, get_peer_pid

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        _bound(sock, deadline)
        try:
            sock.connect(record.socket)
        except OSError as exc:
            raise KeeperAttachError("connect", type(exc).__name__) from exc
        peer = get_peer_pid(sock)
        if peer != record.keeper_pid:
            raise KeeperAttachError("peer", f"pid {peer} is not the recorded keeper")
        if check_peer_is_self(sock) is not PeerCredResult.MATCH:
            raise KeeperAttachError("peer", "not this user")
        if platform_compat.get_process_start_id(peer) != record.keeper_start:
            raise KeeperAttachError("peer", "the keeper pid was recycled")
        decoder = wire.FrameDecoder()
        queue: collections.deque[tuple[int, bytes]] = collections.deque()
        kind, payload = _recv_frame(sock, decoder, queue, deadline)
        if kind != wire.K_HELLO:
            raise KeeperAttachError("hello", f"frame kind {kind}")
        hello = wire.parse_json(payload)
        if hello.get("wire") != wire.KEEPER_WIRE_VERSION:
            raise KeeperAttachError("wire", f"keeper speaks {hello.get('wire')!r}")
        if hello.get("instance") != record.instance:
            raise KeeperAttachError("hello", "instance mismatch")
        try:
            keeper_nonce = bytes.fromhex(str(hello.get("nonce", "")))
        except ValueError as exc:
            raise KeeperAttachError("hello", "bad nonce") from exc
        if len(keeper_nonce) != wire.NONCE_BYTES:
            raise KeeperAttachError("hello", "bad nonce")
        gateway_nonce = os.urandom(wire.NONCE_BYTES)
        auth = {
            "wire": wire.KEEPER_WIRE_VERSION,
            "nonce": gateway_nonce.hex(),
            "proof": wire.proof(
                secret, wire.ROLE_GATEWAY, record.instance, keeper_nonce, gateway_nonce
            ),
            "hold": hold,
        }
        _bound(sock, deadline)
        sock.sendall(wire.json_frame(wire.K_AUTH, auth))
        kind, payload = _recv_frame(sock, decoder, queue, deadline)
        if kind == wire.K_REFUSED:
            raise KeeperAttachError("refused", str(wire.parse_json(payload).get("reason")))
        if kind != wire.K_WELCOME:
            raise KeeperAttachError("welcome", f"frame kind {kind}")
        welcome = wire.parse_json(payload)
        expected = wire.proof(
            secret, wire.ROLE_KEEPER, record.instance, keeper_nonce, gateway_nonce
        )
        if not wire.proof_matches(expected, welcome.get("proof")):
            raise KeeperAttachError("proof", "the keeper did not prove the secret")
        state = AttachState.from_json(welcome.get("state"))
        if state.session_key != record.session_key:
            raise KeeperAttachError("session", "the keeper serves another chat")
        if state.keeper_pid != record.keeper_pid:
            raise KeeperAttachError("peer", "the keeper names another pid")
        # Checked last: past the deadline the caller has given up on this socket.
        _bound(sock, deadline)
        sock.settimeout(None)
        sock.setblocking(False)
        rest = bytes(decoder._buf)  # noqa: SLF001 - bytes the decoder holds
        return sock, state, list(queue), rest
    except BaseException:
        sock.close()
        raise


async def attach(
    record: KeeperRecord,
    secret: bytes,
    *,
    hold: bool,
    stdout_limit: int,
    timeout: float = ATTACH_TIMEOUT_SECS,
) -> KeeperProcess:
    """Attach to *record*'s keeper; ``KeeperAttachError`` when any check fails."""
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + timeout
    try:
        sock, state, leftover, rest = await asyncio.wait_for(
            asyncio.to_thread(_handshake, record, secret, hold, deadline), timeout + 1.0
        )
    except (asyncio.TimeoutError, socket.timeout) as exc:
        raise KeeperAttachError("timeout") from exc
    except (OSError, wire.WireError) as exc:
        raise KeeperAttachError("handshake", type(exc).__name__) from exc
    process = KeeperProcess(record, state, loop, held=hold, stdout_limit=stdout_limit)
    protocol = _KeeperProtocol(process, leftover, rest)
    process._protocol = protocol
    try:
        await loop.create_unix_connection(lambda: protocol, sock=sock)
    except BaseException:
        sock.close()
        raise
    process.stdin = asyncio.StreamWriter(_StdinTransport(process), protocol, None, loop)
    return process
