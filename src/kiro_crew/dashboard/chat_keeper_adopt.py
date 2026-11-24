"""The boot-time adopt pass: dashboard chats a chat keeper kept running across a restart.

Runs from the startup restore (``server_runtime.session_restore``) in two steps:

* :func:`arm`, before the open tabs are rebuilt, reads the live keeper records and
  remembers which chat sessions they serve, so the restore does not stamp "this turn
  was interrupted" on a chat whose turn is in fact still running
  (:func:`kept_by_a_keeper`, read by ``slot_persistence.turn_marker``);
* :func:`adopt_kept_chats`, after the rebuild, starts each such chat's session
  through the eager start, one at a time, so ``AcpClient._spawn`` attaches to its
  keeper (``acp.client_keeper.adopt_for``). It then registers the chat's
  agent-started-turn watch and starts the idle reader, so the turn that was running,
  and any approval it waits on, reaches the dashboard without waiting for a message.

A keeper whose chat did not come back, or any keeper while ``agent.chat_keeper`` is
off, is ended here, so no second adapter runs its session. Design:
``docs/system-specs/modules/runtime-ownership.md``, "Chats that outlive the gateway".
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.chat_keeper.records import KeeperRecord

logger = logging.getLogger("kiro_crew.dashboard.chat_keeper_adopt")

#: The chat sessions a live keeper serves, as :func:`arm` read them at this boot.
#: Process state for this gateway's one restore; empty when the feature is off.
_KEPT_SESSION_KEYS: frozenset[str] = frozenset()


def _feature_on(cfg: Any) -> bool:
    from kiro_crew import chat_keeper
    from kiro_crew.agent_sdk import chat_keeper as keeper

    return chat_keeper.supported() and keeper.keeps(getattr(cfg, "agent", None))


def arm(cfg: Any) -> list[KeeperRecord]:
    """Read the live keepers before the restore; blocking, run off the loop."""
    global _KEPT_SESSION_KEYS
    from kiro_crew import chat_keeper
    from kiro_crew.chat_keeper import records

    if not chat_keeper.supported():
        return []
    try:
        records.prune_dead()
        live = records.live_records()
    except OSError:
        logger.warning("chat keeper: the keepers directory could not be read", exc_info=True)
        return []
    _KEPT_SESSION_KEYS = (
        frozenset(record.session_key for record in live) if _feature_on(cfg) else frozenset()
    )
    return live


def kept_by_a_keeper(slot: Any) -> bool:
    """Whether a live keeper serves *slot*'s session, so its turn may still be running."""
    if not _KEPT_SESSION_KEYS:
        return False
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    try:
        return effective_session_key(slot) in _KEPT_SESSION_KEYS
    except Exception:
        return False


async def adopt_kept_chats(state: Any, cfg: Any, live: list[KeeperRecord]) -> int:
    """Adopt each live keeper whose chat the restore rebuilt; how many were adopted."""
    if not live:
        return 0
    from kiro_crew.agent_sdk.chat_keeper import end_unadopted
    from kiro_crew.dashboard import restart_receipt
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    enabled = _feature_on(cfg)
    slots = {effective_session_key(slot): slot for slot in list(state._slots.values())}
    adopted = 0
    for record in sorted(live, key=lambda r: r.created):
        slot = slots.get(record.session_key)
        if not enabled or slot is None:
            why = "the flag is off" if not enabled else "its chat was not restored"
            await end_unadopted(record, why)
            restart_receipt.note_ended(record.session_key, slot, why)
            # A slot built later (a member slot's retry) then gets its interrupted row.
            _no_longer_kept(record.session_key)
            continue
        if await _adopt_one(state, slot, cfg, record):
            adopted += 1
        elif _left_to_another_start(slot, record):
            logger.info(
                "chat keeper %s: left to the start of %s that is running",
                record.instance,
                record.session_key,
            )
            restart_receipt.note_left(slot)
        else:
            # Not left for the first message to adopt: its prompt would then read
            # the old turn's frames as its own. The chat gets its interrupted row.
            why = "the eager adopt did not attach"
            await end_unadopted(record, why)
            restart_receipt.note_ended(record.session_key, slot, why)
            _no_longer_kept(record.session_key)
            _owe_no_longer(slot)
    # Every keeper is adopted or ended now: a later reconcile of these chats (Recent,
    # Resume) holds back no row that nothing would append.
    _clear_kept()
    if adopted:
        logger.info("chat keeper: adopted %d chat(s) kept running across the restart", adopted)
    return adopted


def _left_to_another_start(slot: Any, record: KeeperRecord) -> bool:
    """Whether a start of *slot* other than the pass's own has, or will take, the keeper.

    One holding it serves the chat on it. A running turn's own start adopts the
    keeper, or ends it before it spawns (``client_keeper.adopt_for``).
    """
    from kiro_crew.agent_sdk import chat_keeper as keeper

    return keeper.holder_of(record.instance) is not None or slot.running is True


def _keeper_of(provider: Any) -> Any:
    """*provider*'s keeper process, or None when its client runs on none."""
    from kiro_crew.agent_sdk.chat_keeper import keeper_process

    client = getattr(provider, "_client", None)
    return keeper_process(client) if client is not None else None


def _serves(provider: Any, record: KeeperRecord) -> bool:
    """Whether *provider*'s client runs on *record*'s keeper."""
    process = _keeper_of(provider)
    return process is not None and process.record.instance == record.instance


#: How long the pass waits on another start of the chat: a start already in flight
#: when the pass reaches it, or the one holding its keeper (two adopt requests of
#: up to 30 s each, then the registration).
_OTHER_START_WAIT_SECS = 75.0
_HELD_START_POLL_SECS = 0.1


async def _await_in_flight(slot: Any, deadline: float) -> None:
    """Wait, until *deadline*, for *slot*'s eager start already running (not cancelled)."""
    in_flight = getattr(slot, "_eager_spawn_task", None)
    if not isinstance(in_flight, asyncio.Task) or in_flight.done():
        return
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining > 0:
        await asyncio.wait({in_flight}, timeout=remaining)


async def _await_held_start(
    state: Any, session_key: str, record: KeeperRecord, deadline: float
) -> Any:
    """The provider of the start holding *record*'s keeper once it is registered.

    None when that start let go of the keeper first, or ran past *deadline*.
    """
    from kiro_crew.agent_sdk import chat_keeper as keeper

    loop = asyncio.get_running_loop()
    while True:
        provider = state.sessions.get_provider(session_key)
        if _serves(provider, record):
            return provider
        if keeper.holder_of(record.instance) is None or loop.time() >= deadline:
            return None
        await asyncio.sleep(_HELD_START_POLL_SECS)


def _clear_kept() -> None:
    global _KEPT_SESSION_KEYS
    _KEPT_SESSION_KEYS = frozenset()


def _no_longer_kept(session_key: str) -> None:
    global _KEPT_SESSION_KEYS
    _KEPT_SESSION_KEYS = _KEPT_SESSION_KEYS - {session_key}


def _owe_no_longer(slot: Any) -> None:
    """Append the "interrupted" row the restore held back for *slot*'s kept turn."""
    if getattr(slot, "_restart_interruption_owed", False) is not True:
        return
    from kiro_crew.dashboard.slot_persistence.turn_marker import append_restart_interruption

    append_restart_interruption(slot, broadcast=True)
    slot._dirty = True


class _StartReasons(logging.Handler):
    """Keeps what the eager start logs while one adopt runs (bounded)."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.reasons: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.reasons) < 8:
            try:
                self.reasons.append(record.getMessage()[:300])
            except Exception:
                pass


async def _adopt_one(state: Any, slot: Any, cfg: Any, record: KeeperRecord) -> bool:
    # A chat with a landed session resumes it; one whose first turn was still running
    # maps an id the session map will not resume yet, and the speculative resume
    # refuses before any process starts, so the start is made once more as a fresh
    # one. Either way ``AcpClient._spawn`` finds the keeper and attaches to it.
    reasons: list[str] = []
    # One bound for every wait on another start of this chat, so a slow one holds
    # back the later kept chats, whose keepers wait 300 s, by 75 s at most.
    deadline = asyncio.get_running_loop().time() + _OTHER_START_WAIT_SECS
    for allow_resume in (True, False):
        attached = await _start_once(state, slot, cfg, record, allow_resume, reasons, deadline)
        if attached is not None:
            return attached
    logger.warning(
        "chat keeper %s: the eager adopt of %s did not attach (%s)",
        record.instance,
        record.session_key,
        "; ".join(reasons) or "no session was started",
    )
    return False


async def _start_once(
    state: Any,
    slot: Any,
    cfg: Any,
    record: KeeperRecord,
    allow_resume: bool,
    reasons: list[str],
    deadline: float,
) -> bool | None:
    """One eager start; True when it attached, None when the next try may."""
    from kiro_crew.dashboard import chat_runner

    # Read off the runner, which composes its owners' functions at import, so a
    # patch of ``chat_runner.<name>`` reaches this call as it reaches a turn's.
    _armed_prefetches = chat_runner._armed_prefetches
    _watch_agent_turns = getattr(chat_runner, "_watch_agent_turns")
    effective_session_key = getattr(chat_runner, "effective_session_key")

    # A start of this chat already running (the page's own, on reconnect) is waited
    # for, not raced: it may be the one to adopt the keeper.
    await _await_in_flight(slot, deadline)
    session_key = effective_session_key(slot)
    provider = state.sessions.get_provider(session_key)
    if not _serves(provider, record):
        provider = await _start_here(state, slot, record, allow_resume, reasons, deadline)
    if not _serves(provider, record):
        if provider is not None:
            reasons.append("another process serves the chat")
            return False
        return None
    # The page's start that adopted it arms its prefetch TTL and cap after the
    # provider is registered, so it is waited for before they are disarmed below.
    await _await_in_flight(slot, deadline)
    client = getattr(provider, "_client", None)
    # Not a speculative prefetch: the chat was already running, so no TTL may end it.
    ttl = getattr(slot, "_prefetch_ttl_task", None)
    if ttl is not None and not ttl.done():
        ttl.cancel()
    _armed_prefetches.pop(session_key, None)
    from kiro_crew.dashboard import restart_receipt

    # Before the watch, so the receipt row lands above the continuing turn's rows.
    await restart_receipt.note_kept(state, slot, getattr(_keeper_of(provider), "state", None))
    _watch_agent_turns(state, slot, provider, cfg)
    kick = getattr(client, "_maybe_start_idle_reader", None)
    if callable(kick):
        kick()
    return True


async def _start_here(
    state: Any,
    slot: Any,
    record: KeeperRecord,
    allow_resume: bool,
    reasons: list[str],
    deadline: float,
) -> Any:
    """The pass's own eager start of *slot*; the provider serving the chat after it."""
    from kiro_crew.agent_sdk import chat_keeper as keeper
    from kiro_crew.dashboard import chat_runner

    _eager_spawn = getattr(chat_runner, "_eager_spawn")
    effective_session_key = getattr(chat_runner, "effective_session_key")
    from kiro_crew.start_priority import StartPriority

    failures: list[Exception] = []
    # The eager start says why it stands down at INFO, below what a gateway logs by
    # default; its words are kept here so a start that did not attach names them.
    said = _StartReasons()
    start_logger = logging.getLogger("kiro_crew.dashboard.chat_runner")
    level = start_logger.level
    start_logger.addHandler(said)
    if not start_logger.isEnabledFor(logging.INFO):
        start_logger.setLevel(logging.INFO)
    try:
        await _eager_spawn(
            state,
            slot,
            allow_resume=allow_resume,
            start_priority=StartPriority.FOREGROUND,
            on_ended=lambda exc: failures.append(exc) if exc is not None else None,
            debounce=0.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failures.append(exc)
    finally:
        start_logger.removeHandler(said)
        start_logger.setLevel(level)
    session_key = effective_session_key(slot)
    provider = state.sessions.get_provider(session_key)
    if not _serves(provider, record) and keeper.holder_of(record.instance) is not None:
        # Another start of the chat took the keeper first; this one stood down.
        provider = await _await_held_start(state, session_key, record, deadline)
    if not _serves(provider, record):
        # The start went another way (refused, spawned fresh, or not at all), and
        # the pass then ends the keeper: see the spec's Known gaps on a stand-down.
        reasons.extend(str(exc) for exc in failures)
        reasons.extend(said.reasons)
    return provider
