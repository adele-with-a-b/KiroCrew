"""A start cut during its settings-seed write leaves no holder behind, and a
chat whose tools a missing seed withholds says so.

The seed write (``_write_claude_local_settings``) runs on a worker thread. A
cancellation delivered to the start that awaits it used to return at once while the
thread kept writing: the cut start's teardown then found nothing to hand back, and
the thread went on to create the file and publish the owner holder for a client
nobody would tear down again. That holder carries the gateway's live identity, so
it read as a live sibling until the gateway exited, and every later chat on that
folder that could neither share those bytes nor leave the file out ran with its
whole ``mcpServers`` array withheld.

Each test here holds the write thread at its first line, cuts the start, lets the
cut start's own teardown run as far as it can, and only then lets the thread
finish. No process is started.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from test_seed_claim_released_on_teardown import (  # noqa: F401 -- autouse fixtures
    _SERVED,
    _client,
    _pin_torn_down_process,
    _pinned_kiro_cli_version,
    _settings,
    _tear_down_without_shutdown,
    isolated_records,
    warm_model_cache,
)

from kiro_crew.acp import seed_provenance as sp
from kiro_crew.acp.claude_shared_session import ClaudeSharedSession
from kiro_crew.acp.client_claude import ClaudeAcpClient
from kiro_crew.acp.harness import claude as claude_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.testing.wait import async_wait_until


class _HeldWrite:
    """The seed write, held at its first line until the test lets it run."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self, client: Any) -> None:
        self.entered.set()
        self.release.wait()
        try:
            ClaudeAcpClient._write_claude_local_settings(client)
        finally:
            self.finished.set()


async def _loop_turns(turns: int = 20) -> None:
    """Run everything the loop already has scheduled, *turns* times over (no clock)."""
    loop = asyncio.get_running_loop()
    for _ in range(turns):
        turn = loop.create_future()
        loop.call_soon(turn.set_result, None)
        await turn


async def _cut_while_writing(start: Any, write: _HeldWrite, teardown: Any = None) -> None:
    """Cut *start* inside its seed write, run *teardown* after it, then let the write end.

    *teardown* is the coroutine function the cut start's caller runs once the start
    has returned. A start that returns while its write is still held is torn down
    right then, before the write is let go, as it was in the field; one that waits
    for its write is torn down after it.
    """
    started = asyncio.ensure_future(start)
    torn_down = False
    try:
        await async_wait_until(write.entered.is_set)
        started.cancel()
        await _loop_turns()
        if started.done() and teardown is not None:
            await teardown()
            torn_down = True
    finally:
        write.release.set()
    await async_wait_until(write.finished.is_set)
    with suppress(asyncio.CancelledError):
        await started
    if teardown is not None and not torn_down:
        await teardown()
    assert started.cancelled()


def _owner_holders(path: Path) -> dict[str, Any]:
    """The persisted owner holders on *path*, as the next start reads them."""
    entry = sp._read_disk_seeds().get(os.fspath(path), {})
    return dict(entry.get("holders", {}).get(sp._HOLDER_OWNERS, {}))


def _assert_no_holder(path: Path) -> None:
    assert sp._LIVE == {}
    assert _owner_holders(path) == {}


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    async def _argv() -> list[str]:
        return ["/opt/bin/claude-agent-acp"]

    monkeypatch.setattr(claude_mod, "_resolve_claude_argv", _argv)
    monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", lambda _argv: "0.94.0")
    return harness_for(ACP_BACKEND_CLAUDE)


def test_a_shared_session_cut_during_its_seed_write_leaves_no_holder(
    harness: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``prepare_session`` ends the session itself when its setup is cut.
    write = _HeldWrite()

    def _held(session: ClaudeSharedSession) -> None:
        write(session)

    monkeypatch.setattr(ClaudeSharedSession, "_write_claude_local_settings", _held, raising=False)
    start = harness.prepare_session(
        work_dir=tmp_path,
        agent="kirocrew",
        model="",
        session_key="dashboard:cut",
        channel_id="",
        memory_mode="persistent",
        options={"permission_mode": "default"},
    )
    asyncio.run(_cut_while_writing(start, write))
    _assert_no_holder(_settings(tmp_path))
    assert not _settings(tmp_path).exists()


def test_a_shared_session_cut_during_its_substitution_reseed_leaves_no_holder(
    harness: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The re-seed that names the model the backend offered instead: the same
    # write, on the retry of a refused start.
    write = _HeldWrite()

    def _held(session: ClaudeSharedSession) -> None:
        write(session)

    monkeypatch.setattr(
        ClaudeAcpClient, "_substitution_retry_model", lambda self, substitute: substitute
    )

    async def scenario() -> None:
        state = await harness.prepare_session(
            work_dir=tmp_path,
            agent="kirocrew",
            model="",
            session_key="dashboard:cut",
            channel_id="",
            memory_mode="persistent",
            options={"permission_mode": "default"},
        )
        monkeypatch.setattr(
            ClaudeSharedSession, "_write_claude_local_settings", _held, raising=False
        )

        async def _end() -> None:
            await harness.end_session(state)

        await _cut_while_writing(harness.adopt_substituted_model(state, _SERVED[1]), write, _end)

    asyncio.run(scenario())
    _assert_no_holder(_settings(tmp_path))
    assert not _settings(tmp_path).exists()


def _cut_chat(tmp_path: Path) -> tuple[Any, _HeldWrite]:
    """A per-chat client whose seed write is held, and the hold."""
    cut = _client(tmp_path)
    write = _HeldWrite()
    cut._write_claude_local_settings = lambda: write(cut)
    return cut, write


def _hard_kill_of(client: Any) -> Any:
    """The teardown a cut cold start gets: its provider is ended without ``shutdown``
    (``_dispatch_hard_kill``), which hands back what the client holds."""

    async def _teardown() -> None:
        _tear_down_without_shutdown(client)

    return _teardown


def test_a_chat_cut_during_its_seed_write_leaves_no_holder(tmp_path: Path) -> None:
    cut, write = _cut_chat(tmp_path)
    asyncio.run(_cut_while_writing(cut._seed_session_settings(), write, _hard_kill_of(cut)))
    _assert_no_holder(_settings(tmp_path))


def test_a_reseed_cut_during_its_write_leaves_no_holder(tmp_path: Path) -> None:
    # The re-seed after the model capture: the same write, later in the start.
    cut = _client(tmp_path)
    cut._write_claude_local_settings()
    write = _HeldWrite()
    cut._write_claude_local_settings = lambda: write(cut)

    async def _end() -> None:
        # The order every caller of ``_reset_state`` uses.
        try:
            await cut._discard_settings_seed()
        finally:
            cut._reset_state()

    asyncio.run(_cut_while_writing(cut._reseed_after_capture(), write, _end))
    _assert_no_holder(_settings(tmp_path))
    assert not _settings(tmp_path).exists()


def test_later_chats_keep_their_tools_after_a_cut_start(tmp_path: Path) -> None:
    # The later chat asks for another permission mode, so it can neither share the
    # cut chat's bytes nor leave the file out: under a leftover holder it is withheld.
    cut, write = _cut_chat(tmp_path)
    asyncio.run(_cut_while_writing(cut._seed_session_settings(), write, _hard_kill_of(cut)))
    for _ in range(3):
        later = _client(tmp_path, permission_mode="acceptEdits")
        later._write_claude_local_settings()
        assert later._permission_surface_governed
        assert later.settings_withheld_cause() == ""
        asyncio.run(later._discard_settings_seed())
        later._reset_state()


def test_a_chat_withheld_under_a_missing_seed_names_the_open_session(tmp_path: Path) -> None:
    # An open session holds the path and its seed file is gone: the second chat is
    # not allowed to recreate it, and must say whose claim keeps its tools off.
    owner = _client(tmp_path)
    owner._write_claude_local_settings()
    _settings(tmp_path).unlink()
    second = _client(tmp_path)
    second._write_claude_local_settings()
    assert not second._permission_surface_governed
    assert not _settings(tmp_path).exists()
    assert second.settings_withheld_cause() == "session"
