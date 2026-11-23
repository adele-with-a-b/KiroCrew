"""A chat kept across a gateway restart is still vouched for: its session token.

The adapter and every MCP server under it carry, in their environment, the session
token the launching gateway minted (``client_spawn_env``), and an identity-bound tool
call is admitted only when that token verifies to the chat's key
(``member_memory_auth.session_key_is_attested``). These tests start a real keeper
over the stand-in adapter (``fixtures/chat_keeper/fake_adapter.py``), let the first
gateway go away, adopt the keeper as the next gateway does, and ask the adapter
which token it carries. Design: runtime-ownership.md, "Chats that outlive the
gateway".
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import chat_keeper, session_pid_sig, session_token_sig
from kiro_crew.acp import client_keeper
from kiro_crew.chat_keeper import records
from kiro_crew.chat_keeper.transport import KeeperProcess
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV, mint_stub_session_token
from kiro_crew.member_memory_auth import session_key_is_attested
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = [
    pytest.mark.skipif(not chat_keeper.supported(), reason="the chat keeper is POSIX only"),
    pytest.mark.xdist_group(name="chat_keeper_process"),
]

FAKE_ADAPTER = Path(__file__).parent / "fixtures" / "chat_keeper" / "fake_adapter.py"
KEY = "dashboard:chat-kept"
OTHER_KEY = "dashboard:chat-other"
FINGERPRINT = "sandbox-fp"


@pytest.fixture
def home(short_sock_dir: Path, monkeypatch):
    """The keepers directory under a short root, as the gateway's own data home."""
    monkeypatch.setattr(
        records, "keepers_dir", lambda h=None: chat_keeper.keepers_dir(h or short_sock_dir)
    )
    monkeypatch.setattr(client_keeper, "_HOLDS", {})

    async def _on() -> bool:
        return True

    monkeypatch.setattr(client_keeper, "_config_async", _on)
    yield short_sock_dir
    for record in records.live_records(short_sock_dir):
        try:
            os.killpg(record.keeper_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.fixture
def mappings(tmp_path):
    """An isolated token-mapping directory and a synthetic SEL trust root."""
    key_path = tmp_path / "sel_hmac.key"
    key_path.write_bytes(b"\x05" * 32)
    with (
        patch.object(session_token_sig, "config_dir", return_value=tmp_path),
        patch.object(session_token_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_pid_sig, "sel_hmac_key_path", return_value=key_path),
        patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None),
    ):
        yield tmp_path


class _Client(SimpleNamespace):
    """A gateway's client for the kept chat; the adopt hold keeps a weak reference."""


def _client() -> Any:
    """A gateway's client for the kept chat: like ``AcpClient``, it mints its own token."""
    return _Client(
        _session_key=KEY,
        backend="claude",
        memory_mode="persistent",
        _stub_session_token=mint_stub_session_token(),
    )


async def _launch_kept(client: Any) -> KeeperProcess:
    """The first gateway's launch: the client's token rides the adapter's environment."""
    spawn = await client_keeper.keeper_spawn_for(client, "launch-1")
    assert spawn is not None
    spawn.launch = {"sandbox": FINGERPRINT}
    env = {**os.environ, STUB_SESSION_TOKEN_ENV: client._stub_session_token}
    launcher = await asyncio.create_subprocess_exec(
        *spawn.wrap([sys.executable, str(FAKE_ADAPTER)]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    return await spawn.finish(launcher)


async def _carried_token(process: KeeperProcess) -> str:
    """The token the kept adapter's environment holds, which its MCP servers present."""
    body = {
        "jsonrpc": "2.0",
        "id": 50,
        "method": "getenv",
        "params": {"name": STUB_SESSION_TOKEN_ENV},
    }
    process.stdin.write(json.dumps(body).encode("utf-8") + b"\n")
    await process.stdin.drain()
    line = await await_until(asyncio.ensure_future(process.stdout.readline()), what="getenv")
    answer = json.loads(line)
    assert answer["id"] == 50
    return str(answer["result"]["value"])


class _Request(dict):
    """The members of an internal request the session-key gate reads."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.headers = {"X-Session-Token": token} if token else {}


async def _restart_and_adopt() -> tuple[Any, Any, KeeperProcess]:
    first = _client()
    process = await _launch_kept(first)
    # No turn ran under the first gateway, so it never published its token: the
    # shape of the live chat whose keeper a restart had just started. Then it exits.
    await process.detach(process.stdout.consumed_seq)
    second = _client()
    adopted = await client_keeper.adopt_for(second, lambda: FINGERPRINT)
    assert adopted is not None, "the next gateway adopts the kept chat"
    await asyncio.to_thread(client_keeper.restore_launch_state, second, adopted.record)
    return first, second, adopted


@pytest.mark.asyncio
async def test_a_kept_chat_is_vouched_for_by_the_gateway_that_adopts_it(home, mappings) -> None:
    first, second, adopted = await _restart_and_adopt()
    carried = await _carried_token(adopted)
    assert carried == first._stub_session_token, "the kept adapter still holds its own token"
    assert (
        session_token_sig.verify_session_token(carried) == KEY
    ), "the kept chat's servers present a token the new gateway must vouch for"
    assert session_key_is_attested(_Request(carried), KEY)
    # Every later turn republishes the client's token, so it must be the carried one.
    assert second._stub_session_token == carried

    # Another chat still cannot claim the kept chat's identity: its own token
    # verifies to its own key, and no token at all attests nothing.
    other = mint_stub_session_token()
    session_token_sig.publish_session_token(other, OTHER_KEY)
    assert not session_key_is_attested(_Request(other), KEY)
    assert not session_key_is_attested(_Request(""), KEY)

    adopted.request_terminate()
    instance = adopted.record.instance
    await async_wait_until(
        lambda: not any(p.name.startswith(instance) for p in records.keepers_dir(home).iterdir()),
        describe=lambda: sorted(p.name for p in records.keepers_dir(home).iterdir()),
    )


@pytest.mark.asyncio
async def test_a_keeper_from_a_build_that_kept_no_token_leaves_the_client_its_own(
    home, mappings
) -> None:
    first = _client()
    process = await _launch_kept(first)
    (records.keepers_dir(home) / f"{process.record.instance}.token").unlink()
    await process.detach(process.stdout.consumed_seq)
    second = _client()
    own = second._stub_session_token
    adopted = await client_keeper.adopt_for(second, lambda: FINGERPRINT)
    assert adopted is not None
    await asyncio.to_thread(client_keeper.restore_launch_state, second, adopted.record)
    assert second._stub_session_token == own
    assert session_token_sig.verify_session_token(first._stub_session_token) == ""
    adopted.request_terminate()


INSTANCE = "abc123def456"


@pytest.fixture
def kdir(tmp_path, monkeypatch) -> Path:
    """The gateway's keepers directory, under *tmp_path*; returns the token's path."""
    base = tmp_path / "keeper-home"
    monkeypatch.setattr(records, "keepers_dir", lambda h=None: chat_keeper.keepers_dir(h or base))
    records.ensure_dir()
    return records.keepers_dir() / f"{INSTANCE}.token"


def _record(session_key: str = KEY) -> records.KeeperRecord:
    return records.KeeperRecord(
        instance=INSTANCE,
        session_key=session_key,
        backend="claude",
        keeper_pid=4242,
        keeper_start="s",
        adapter_pid=4243,
        socket=str(records.keepers_dir() / f"{INSTANCE}.s"),
        wire=1,
        created=0.0,
    )


def test_a_kept_token_is_restored_and_vouched_for(kdir, mappings) -> None:
    kept = mint_stub_session_token()
    records.write_session_token(INSTANCE, kept)
    client = _client()
    client_keeper.restore_launch_state(client, _record())
    assert client._stub_session_token == kept
    assert session_token_sig.verify_session_token(kept) == KEY


def test_a_token_vouched_for_another_chat_is_never_moved_to_this_one(kdir, mappings) -> None:
    other = mint_stub_session_token()
    session_token_sig.publish_session_token(other, OTHER_KEY)
    records.write_session_token(INSTANCE, other)
    client = _client()
    own = client._stub_session_token
    client_keeper.restore_launch_state(client, _record())
    assert client._stub_session_token == own
    assert session_token_sig.verify_session_token(other) == OTHER_KEY


def test_a_record_for_another_chat_restores_no_token(kdir, mappings) -> None:
    kept = mint_stub_session_token()
    records.write_session_token(INSTANCE, kept)
    client = _client()
    own = client._stub_session_token
    client_keeper.restore_launch_state(client, _record(session_key=OTHER_KEY))
    assert client._stub_session_token == own
    assert session_token_sig.verify_session_token(kept) == ""


def test_an_unreadable_token_still_restores_the_launch_state(kdir, mappings, monkeypatch) -> None:
    records.write_session_token(INSTANCE, mint_stub_session_token())

    def _eio(path: Path) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(records, "_read_private", _eio)
    client = _client()
    own = client._stub_session_token
    record = dataclasses.replace(_record(), launch={"ssh_auth_sock_forwarded": True})
    client_keeper.restore_launch_state(client, record)
    assert client._stub_session_token == own
    assert client._ssh_auth_sock_forwarded is True


@pytest.mark.parametrize("body", [b"", b"not-a-token", b"A" * 64, b"0" * 63])
def test_a_token_file_that_is_not_a_token_reads_as_none(kdir, body: bytes) -> None:
    records._write_private(kdir, body)
    assert records.read_session_token(INSTANCE) == ""


def test_a_linked_token_file_is_not_followed(kdir, tmp_path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text(mint_stub_session_token(), encoding="ascii")
    kdir.symlink_to(target)
    assert records.read_session_token(INSTANCE) == ""
