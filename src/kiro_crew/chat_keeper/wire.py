"""The keeper's wire: framing, frame kinds, and the mutual-authentication proofs.

One frame is a 4-byte big-endian length (of what follows), a 1-byte kind and the
payload. JSON payloads are UTF-8 objects; a sequence number is 8 bytes big-endian.
Nothing here knows Crew or a harness: the keeper relays ACP lines byte for byte, and
``KEEPER_WIRE_VERSION`` versions only this relay.

Stdlib only, because the keeper process imports it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
from typing import Any

#: The relay's version. A gateway refuses to adopt a keeper that speaks another.
KEEPER_WIRE_VERSION = 1

# Keeper -> gateway, during the handshake.
K_HELLO = 1
K_WELCOME = 3
K_REFUSED = 4
# Gateway -> keeper, during the handshake.
K_AUTH = 2
# Gateway -> keeper, attached.
K_STDIN = 10
K_STDIN_EOF = 11
K_ACK = 20
K_DETACH = 21
K_RESUME = 22
K_TERMINATE = 23
# Keeper -> gateway, attached.
K_STDOUT = 12
K_STDERR = 13
K_EXIT = 14
#: Sent after the backlog a held attach was owed: no line delivered after it carries
#: a lower sequence number than one delivered before it.
K_REPLAYED = 15

#: The largest frame either side accepts. An adapter line above this is dropped by
#: the keeper (its sequence number is still spent, so the gap is visible); the
#: gateway's own reader drops anything over 10 MB anyway.
MAX_FRAME_BYTES = 64 * 1024 * 1024

#: The largest handshake frame either side accepts.
MAX_HANDSHAKE_BYTES = 1024 * 1024

#: The secret's length in bytes.
SECRET_BYTES = 32

#: The nonce's length in bytes.
NONCE_BYTES = 32

_HEADER = struct.Struct(">IB")
_SEQ = struct.Struct(">Q")

#: Domain separation for the proofs, so a proof made here means nothing elsewhere.
_PROOF_DOMAIN = b"kiro-crew-chat-keeper"

ROLE_GATEWAY = "gateway"
ROLE_KEEPER = "keeper"


class WireError(ValueError):
    """A frame that breaks the wire's rules; the connection carrying it is dropped."""


def frame(kind: int, payload: bytes = b"") -> bytes:
    """One frame of *kind* carrying *payload*."""
    if len(payload) + 1 > MAX_FRAME_BYTES:
        raise WireError(f"frame of {len(payload)} bytes is over the {MAX_FRAME_BYTES} limit")
    return _HEADER.pack(len(payload) + 1, kind) + payload


def json_frame(kind: int, body: dict[str, Any]) -> bytes:
    """One frame of *kind* carrying *body* as a compact JSON object."""
    return frame(kind, json.dumps(body, separators=(",", ":")).encode("utf-8"))


def seq_frame(kind: int, seq: int, payload: bytes = b"") -> bytes:
    """One frame of *kind* carrying the sequence number *seq*, then *payload*."""
    return frame(kind, _SEQ.pack(seq) + payload)


def split_seq(payload: bytes) -> tuple[int, bytes]:
    """The sequence number a ``seq_frame`` payload starts with, and the rest."""
    if len(payload) < _SEQ.size:
        raise WireError("a sequenced frame is shorter than its sequence number")
    return _SEQ.unpack_from(payload)[0], payload[_SEQ.size :]


def parse_json(payload: bytes) -> dict[str, Any]:
    """The JSON object a frame carries; ``WireError`` for anything else."""
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WireError("a JSON frame does not hold JSON") from exc
    if not isinstance(body, dict):
        raise WireError("a JSON frame does not hold an object")
    return body


class FrameDecoder:
    """Incremental decoder: feed bytes, get whole frames in arrival order."""

    def __init__(self, max_frame: int = MAX_FRAME_BYTES) -> None:
        self._buf = bytearray()
        self._max = max_frame

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf.extend(data)
        out: list[tuple[int, bytes]] = []
        while len(self._buf) >= _HEADER.size:
            length, kind = _HEADER.unpack_from(self._buf)
            if length < 1 or length > self._max:
                raise WireError(f"frame length {length} is out of bounds")
            end = 4 + length
            if len(self._buf) < end:
                break
            out.append((kind, bytes(self._buf[_HEADER.size : end])))
            del self._buf[:end]
        return out

    def set_limit(self, max_frame: int) -> None:
        """Change the largest frame accepted from here on (raised after a handshake)."""
        self._max = max_frame

    @property
    def pending(self) -> int:
        """Bytes buffered that do not yet make a whole frame."""
        return len(self._buf)


def proof(
    secret: bytes, role: str, instance: str, keeper_nonce: bytes, gateway_nonce: bytes
) -> str:
    """The proof *role* sends: an HMAC over the instance, the version and both nonces.

    Binding both nonces makes every proof fresh for its connection; binding the role
    stops one side's proof being reflected back as the other's.
    """
    message = b"|".join(
        (
            _PROOF_DOMAIN,
            role.encode("ascii"),
            instance.encode("utf-8"),
            str(KEEPER_WIRE_VERSION).encode("ascii"),
            keeper_nonce,
            gateway_nonce,
        )
    )
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def proof_matches(expected: str, presented: object) -> bool:
    """Constant-time comparison of a presented proof with the expected one."""
    return isinstance(presented, str) and hmac.compare_digest(expected, presented)
