"""A dashboard chat's ``AcpClient`` process under a chat keeper: spawn, adopt, detach.

Behind ``agent.chat_keeper`` (off by default), on a backend in
``ACP_BACKENDS_CHAT_KEEPER``, for a persistent ``dashboard:`` chat on a POSIX host:

* :func:`keeper_spawn_for` makes the launch start the adapter under a keeper
  (``chat_keeper.spawn.KeeperSpawn``), so the process outlives the gateway;
* :func:`adopt_for` is asked first by ``AcpClient._spawn``: when a keeper the previous
  gateway left serves this chat, it attaches to it instead of launching, and
  :func:`adopt_session` then replaces ``_initialize_session``'s handshake;
* :func:`detach` hands the keeper back at a gateway exit, killing nothing.

Each function takes the client as its first argument; nothing here is bound on the
class. Design: ``docs/system-specs/modules/runtime-ownership.md``, "Chats that outlive
the gateway".

Instance fields written: ``_keeper_adopt_spent``, ``_keeper_adopting``,
``_keeper_adopted_process``, ``_keeper_handed_off``,
``_resume_session_id``, ``_session_id``, ``_resumed``, ``_next_id``, ``_buffer``,
``_can_load_session``, ``_agent_accepts_image``, ``_agent_version``,
``_agent_version_read``, ``_steering_advertised``, ``_initialize_result``,
``_last_activity``, ``on_agent_turn``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import weakref
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew import chat_keeper
from kiro_crew.acp._dispatch import agent_version_from_init
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.transport_framing import _STDOUT_BUFFER_LIMIT
from kiro_crew.acp.types import METHOD_INITIALIZE, JsonRpcMessage, steering_advertised
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_CHAT_KEEPER, can_restore_session

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.chat_keeper.spawn import KeeperSpawn
    from kiro_crew.chat_keeper.transport import KeeperProcess

logger = logging.getLogger("kiro_crew.acp.client")

#: The ACP method a member answers ``open`` for a session it holds.
METHOD_SESSION_STATUS = "session/status"
SESSION_STATUS_OPEN = "open"

#: The session keys a keeper serves: dashboard chats. A channel's own session, a
#: cron job's, a sub-agent's and the background session keep the gateway-owned
#: process (runtime-ownership.md, "Gateway exit: detach, never kill").
DASHBOARD_SESSION_KEY_PREFIX = "dashboard:"

#: How long the adopt handshake's two requests may take.
_ADOPT_REQUEST_TIMEOUT = 30.0

#: How long a detach waits for the client's idle reader to put its frames back.
_IDLE_READER_STOP_SECS = 2.0

#: Attribute a frame read off a keeper carries: the sequence number of its line.
KEEPER_SEQ_ATTR = "_keeper_seq"
#: Set on a client once its keeper was handed to the next gateway.
_HANDED_OFF_ATTR = "_keeper_handed_off"


class KeeperAdoptRefused(AcpError):
    """The adopt handshake refused the keeper; the start falls back to a fresh spawn."""


class KeeperHeldElsewhere(AcpError):  # noqa: N818
    """Another start in this gateway holds the chat's keeper; this one starts nothing.

    Neither attaching (the keeper would hand the attachment over and break the other
    start) nor a fresh spawn (two adapters on one session) is safe, so this start
    ends here, without the retry ``ensure_ready`` gives other errors.
    """

    def __init__(self, instance: str) -> None:
        super().__init__(
            "another start of this chat is adopting its chat keeper",
            transient=False,
            user_worded=True,
        )
        self.instance = instance


#: Keeper instance -> the client of this gateway attaching or attached to it, and the
#: process once attached. A keeper hands its attachment to the last authenticated
#: attach, so two starts of one chat here would take it from each other; the hold
#: lets the second see the first. Loop-owned, read and written only on the loop.
_HOLDS: dict[str, tuple[weakref.ref[Any], KeeperProcess | None]] = {}


def holder_of(instance: str) -> AcpClient | None:
    """The client of this gateway holding keeper *instance*, or None.

    A hold lasts while its client lives and is attaching, or still owns the attached
    process and that process is neither detached nor ended; a stale one is dropped.
    A failed start's ``_reset_state`` closes the connection, which sets the process's
    ``returncode`` (``KeeperProcess._on_lost``), so a stale ``_keeper_adopting`` ends it.
    """
    entry = _HOLDS.get(instance)
    if entry is None:
        return None
    client = entry[0]()
    process = entry[1]
    live = client is not None and (
        process is None
        or (
            process.returncode is None
            and not process.detached
            and process
            in (getattr(client, "_process", None), getattr(client, "_keeper_adopting", None))
        )
    )
    if not live:
        _HOLDS.pop(instance, None)
        return None
    return client


def _held_by_another(instance: str, client: object) -> bool:
    holder = holder_of(instance)
    return holder is not None and holder is not client


def _release(instance: str, client: object) -> None:
    entry = _HOLDS.get(instance)
    if entry is not None and entry[0]() is client:
        del _HOLDS[instance]


def _keeps(agent: object) -> bool:
    """``agent.chat_keeper``, honoured only beside ``agent.agent_started_turns``.

    The rest of a kept turn reaches the next gateway as an agent-started turn, so
    with those turns off a kept chat would run on where nobody sees it.
    """
    return (
        getattr(agent, "chat_keeper", False) is True
        and getattr(agent, "agent_started_turns", False) is True
    )


def _config() -> bool:
    """:func:`_keeps` from the adopted snapshot."""
    from kiro_crew.config import live

    cfg = live.snapshot()
    return cfg is not None and _keeps(cfg.agent)


async def _config_async() -> bool:
    """:func:`_config`, loading the file off the loop when no snapshot is adopted yet."""
    from kiro_crew.config import live

    if live.snapshot() is not None:
        return _config()
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        logger.debug("chat keeper: config load failed; no keeper", exc_info=True)
        return False
    return _keeps(cfg.agent)


def eligible(client: AcpClient) -> bool:
    """Whether *client*'s process may run under a keeper (the flag aside)."""
    key = getattr(client, "_session_key", None) or ""
    return (
        chat_keeper.supported()
        and client.backend in ACP_BACKENDS_CHAT_KEEPER
        and key.startswith(DASHBOARD_SESSION_KEY_PREFIX)
        and getattr(client, "memory_mode", "") == "persistent"
        and not getattr(client, "_side_read_only", False)
    )


def keeper_process(client: AcpClient) -> KeeperProcess | None:
    """*client*'s process when it is a keeper, else None."""
    from kiro_crew.chat_keeper.transport import KeeperProcess

    process = getattr(client, "_process", None)
    return process if isinstance(process, KeeperProcess) else None


def adopting(client: AcpClient) -> bool:
    """Whether *client*'s process is the adopted keeper whose handshake is owed."""
    process = keeper_process(client)
    return process is not None and getattr(client, "_keeper_adopting", None) is process


def adopted(client: AcpClient) -> bool:
    """Whether *client*'s session was adopted from its keeper, not made on this process.

    Such a session already runs with what its launching gateway set on it, and a
    request sent before the adopted turn's watch reads the replay that turn is owed.
    """
    process = keeper_process(client)
    return process is not None and getattr(client, "_keeper_adopted_process", None) is process


def is_detached(client: AcpClient) -> bool:
    # Kept on the client, which a shutdown's ``_reset_state`` leaves alone: after it
    # the process is gone from the client, and a late start must still not run.
    if getattr(client, _HANDED_OFF_ATTR, False) is True:
        return True
    process = keeper_process(client)
    return process is not None and process.detached


def detachable(client: object) -> bool:
    """Whether a gateway exit hands *client*'s process back instead of ending it."""
    process = keeper_process(client)  # type: ignore[arg-type]
    return (
        process is not None
        and not process.detached
        and process.returncode is None
        and bool(getattr(client, "_session_id", None))
        and eligible(client)  # type: ignore[arg-type]
    )


async def forget_if_ended(client: AcpClient) -> None:
    """Remove the files of *client*'s keeper once a teardown has ended it.

    A keeper the teardown killed removes none of its own files. One check, no wait:
    a gateway stop closes its chats inside one time budget, so a keeper not yet
    reaped keeps its files until the next boot's ``records.prune_dead``.
    """
    process = keeper_process(client)
    if process is None or process.detached:
        return
    from kiro_crew.chat_keeper import records

    try:
        if await asyncio.to_thread(records.is_dead, process.record):
            await asyncio.to_thread(records.remove_record_files, process.record)
    except Exception:
        logger.debug("chat keeper: removing an ended keeper's files failed", exc_info=True)


async def keeper_spawn_for(client: AcpClient, launch_id: str) -> KeeperSpawn | None:
    """The keeper this launch starts the adapter under, or None for a direct spawn."""
    if not eligible(client):
        return None
    if not await _config_async():
        return None
    from kiro_crew.chat_keeper.spawn import KeeperSpawn

    spawn = KeeperSpawn(
        session_key=client._session_key or "",
        backend=client.backend,
        adopt_secs=chat_keeper.ADOPT_SECS,
        stdout_limit=_STDOUT_BUFFER_LIMIT,
        process_instance=launch_id,  # pid-owner-ok: keeper launch id
    )
    if not await asyncio.to_thread(spawn.prepare):
        return None
    return spawn


async def adopt_for(
    client: AcpClient, sandbox: Callable[[], str] = lambda: ""
) -> KeeperProcess | None:
    """Attach to the keeper the previous gateway left for this chat, held, or None.

    *sandbox* computes the fingerprint (``chat_keeper.spawn.sandbox_fingerprint``)
    this gateway's own launch would run under; it is blocking, run off the loop, and
    only when a keeper of this chat is compared. A keeper launched under another
    one is ended, not adopted. Asked once per client: a refusal falls back to a spawn,
    which must not find the refused keeper again while it is still ending.

    Every spawn asks, adoptable or not, because whatever this does not adopt it
    ends and awaits first: a keeper still running this chat's session beside a fresh
    adapter would be two adapters on one session. A later spawn of the same client,
    or one that is not eligible (another backend, a side read-only chat), finds
    the chat's keepers only to end them.
    """
    spent = getattr(client, "_keeper_adopt_spent", False)
    client._keeper_adopt_spent = True
    session_key = getattr(client, "_session_key", None)
    if not session_key or not chat_keeper.supported():
        return None
    from kiro_crew.chat_keeper import records
    from kiro_crew.chat_keeper.transport import KeeperAttachError, attach

    found = await asyncio.to_thread(records.live_records)
    mine = sorted(
        (r for r in found if r.session_key == session_key),
        key=lambda r: r.created,
        reverse=True,
    )
    if not mine:
        return None
    held_elsewhere = [r for r in mine if _held_by_another(r.instance, client)]
    if held_elsewhere:
        # Another start here is adopting this chat's keeper: it is neither taken
        # from that start nor ended under it, and no fresh adapter starts beside it.
        raise KeeperHeldElsewhere(held_elsewhere[0].instance)
    if spent or not eligible(client):
        for record in mine:
            await _end_unadopted(record, "a fresh adapter starts for this chat")
        return None
    enabled = await _config_async()
    adopted: KeeperProcess | None = None
    fingerprint: str | None = None
    for record in mine:
        if adopted is not None or not enabled or record.backend != client.backend:
            await _end_unadopted(
                record, "not adopted: another keeper serves this chat or the flag is off"
            )
            continue
        if fingerprint is None:
            fingerprint = await asyncio.to_thread(sandbox)
        if record.launch.get("sandbox") != fingerprint:
            await _end_unadopted(record, "its sandbox differs from this gateway's")
            continue
        secret = await asyncio.to_thread(records.read_key, record.instance)
        if secret is None:
            await _end_unadopted(record, "its key is missing")
            continue
        if _held_by_another(record.instance, client):
            # Taken by another start during an await above.
            raise KeeperHeldElsewhere(record.instance)
        _HOLDS[record.instance] = (weakref.ref(client), None)
        try:
            process = await attach(record, secret, hold=True, stdout_limit=_STDOUT_BUFFER_LIMIT)
        except BaseException as exc:
            _release(record.instance, client)
            if not isinstance(exc, KeeperAttachError):
                raise
            logger.warning("chat keeper %s not adopted: %s", record.instance, exc)
            await _end_unadopted(record, exc.reason)
            continue
        reason = _refusal(process)
        if reason:
            _release(record.instance, client)
            logger.warning("chat keeper %s not adopted: %s", record.instance, reason)
            process.request_terminate()
            await _end_unadopted(record, reason, asked=True)
            continue
        _HOLDS[record.instance] = (weakref.ref(client), process)
        # Set here, not after the loop: the hold reads it while older records end.
        client._keeper_adopting = adopted = process
    if adopted is not None:
        logger.info(
            "chat keeper %s adopted for %s: keeper pid %d, adapter pid %d, %d line(s) "
            "after the last ack, %d open request(s)",
            adopted.record.instance,
            client._session_key,
            adopted.pid,
            adopted.state.adapter_pid,
            adopted.state.seq - adopted.state.last_ack,
            adopted.state.open_agent_requests,
        )
    return adopted


def restore_launch_state(client: AcpClient, record: Any) -> None:
    """Set on *client* what the launching gateway's ``launch()`` set on its own.

    A scratch directory swept since is not restored: the client then has none, as
    after a launch whose allocation failed.
    """
    from pathlib import Path

    state = record.launch
    scratch = str(state.get("scratch_dir") or "")
    client._scratch_dir = Path(scratch) if scratch and Path(scratch).is_dir() else None
    client._sandbox_wrapped_by_crew = state.get("sandbox_wrapped_by_crew") is True
    client._sandbox_hidden_dirs = tuple(str(d) for d in state.get("sandbox_hidden_dirs") or ())
    client._child_env_names = frozenset(str(n) for n in state.get("child_env_names") or ())
    client._ssh_auth_sock_forwarded = state.get("ssh_auth_sock_forwarded") is True
    shipped = state.get(SHIPPED_RESTRICTIONS_KEY)
    if isinstance(shipped, list):
        # Read by the claude client's project-deny check (``ClaudeAcpClient``).
        restrictions = frozenset(
            (str(pair[0]), str(pair[1]))
            for pair in shipped
            if isinstance(pair, list) and len(pair) == 2
        )
        setattr(client, "_claude_shipped_restrictions", restrictions)  # noqa: B010


#: The launch-state key holding the deny and ask rules the session was created with.
SHIPPED_RESTRICTIONS_KEY = "shipped_restrictions"


async def record_session_restrictions(client: AcpClient) -> None:
    """Write the deny and ask rules *client*'s session shipped into its keeper record.

    The session froze them at creation, and only the gateway that created it built
    them (``_claude_session_meta``). An adopting gateway checks the project's rules
    against this set, as the creating one does; without it every project rule
    would read as gained and the kept chat would be stopped.
    """
    process = keeper_process(client)
    shipped = getattr(client, "_claude_shipped_restrictions", None)
    if process is None or process.detached or shipped is None:
        return
    from dataclasses import replace

    from kiro_crew.chat_keeper import records

    listed = sorted([kind, rule] for kind, rule in shipped)
    if process.record.launch.get(SHIPPED_RESTRICTIONS_KEY) == listed:
        return
    record = replace(
        process.record, launch={**process.record.launch, SHIPPED_RESTRICTIONS_KEY: listed}
    )
    try:
        await asyncio.to_thread(records.rewrite_record, record)
    except OSError:
        logger.warning("chat keeper: its record could not be updated", exc_info=True)
        return
    process.record = record


def _refusal(process: KeeperProcess) -> str:
    """Why an attached keeper cannot be adopted, or "" when it can."""
    state = process.state
    if state.gap:
        return "its journal overflowed while no gateway was attached"
    if state.returncode is not None:
        return f"its adapter already exited ({state.returncode})"
    return ""


#: How often :func:`_end_unadopted` looks whether a keeper it ended is gone.
_END_POLL_SECS = 0.05


async def _end_unadopted(record: Any, why: str, *, asked: bool = False) -> None:
    """End a keeper this gateway will not adopt, and wait until it is gone.

    No second adapter may run its session, so this returns only once the keeper's
    group is empty or the last resort was tried: SIGTERM (skipped when *asked*,
    because the keeper was already sent an authenticated ``terminate``), the
    keeper's own grace of ``KEEPER_TERM_GRACE_SECS`` plus a second, then SIGKILL.
    Each signal goes to the group only while the record's pid still carries its
    recorded start identity and leads its own group
    (``process_identity.isolated_group_of``), the identity pin every runtime group
    kill takes; a keeper that already exited is not signalled.
    """
    from kiro_crew import platform_compat, process_identity
    from kiro_crew.chat_keeper import KEEPER_TERM_GRACE_SECS

    def _signal(sig: int) -> int | None:
        group = process_identity.isolated_group_of(record.keeper_pid, record.keeper_start)
        if group is None:
            return None
        try:
            platform_compat.kill_process_group(group, sig)
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            return None
        return group

    async def _gone_within(group: int, secs: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + secs
        while await asyncio.to_thread(platform_compat.pgroup_exists, group):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_END_POLL_SECS)
        return True

    holder = holder_of(record.instance)
    if holder is not None:
        # A start of this gateway attached it since this one passed it over: that
        # start's session runs on it, so it is not ended under it.
        logger.info(
            "chat keeper %s (pid %d) not ended (%s): a start of %s holds it",
            record.instance,
            record.keeper_pid,
            why,
            getattr(holder, "_session_key", None) or "a chat",
        )
        return
    group = await asyncio.to_thread(
        process_identity.isolated_group_of, record.keeper_pid, record.keeper_start
    )
    if group is None:
        outcome = "already gone"
    else:
        if not asked:
            await asyncio.to_thread(_signal, platform_compat.SIGTERM)
        if await _gone_within(group, KEEPER_TERM_GRACE_SECS + 1.0):
            outcome = "ended"
        elif await asyncio.to_thread(_signal, platform_compat.SIGKILL) is None:
            outcome = "its leader exited; members it left were not signalled"
        else:
            outcome = "killed" if await _gone_within(group, 1.0) else "still running after SIGKILL"
    from kiro_crew.chat_keeper import records

    # A killed keeper removes none of its files; nor does one that died on its own.
    if await asyncio.to_thread(records.is_dead, record):
        await asyncio.to_thread(records.remove_record_files, record)
    logger.warning(
        "chat keeper %s (pid %d) not adopted (%s): %s",
        record.instance,
        record.keeper_pid,
        why,
        outcome,
    )


async def adopt_session(client: AcpClient) -> None:
    """The handshake on an adopted keeper, in place of ``_initialize_session``.

    ``initialize`` is sent again: the member's handler is stateless, and its answer
    gives this gateway the live adapter's version for the same checks a start runs.
    ``session/status`` must answer ``open`` for the mapped session; there is no
    ``session/load``, which on a session the adapter holds replays its transcript
    into the live stream. The session's state comes from the keeper's cached answer
    and updates, read through the same parsers a load answer goes through. A refusal
    raises :class:`KeeperAdoptRefused` with the resume id put back, so the start's
    retry spawns a fresh process and loads the session there.
    """
    process = keeper_process(client)
    client._keeper_adopting = None
    assert process is not None
    state = process.state
    resume_sid = client._resume_session_id
    # The mapped id, else the one session the keeper saw created: a chat whose first
    # turn was still running maps an id the session map will not resume yet
    # (``session_map.UNLANDED_SID_FIELD``), and that session is the live one.
    sid = resume_sid or _sole_session(state)
    client._resume_session_id = None
    try:
        if client._next_id <= state.max_client_id:
            client._next_id = state.max_client_id + 1
        init_id = await client._send_request(METHOD_INITIALIZE, client._initialize_params())
        init_resp = await client._wait_for_response(init_id, timeout=_ADOPT_REQUEST_TIMEOUT)
        capabilities = init_resp.get("agentCapabilities") or {}
        client._can_load_session = can_restore_session(client.backend, capabilities)
        prompt_capabilities = capabilities.get("promptCapabilities")
        client._agent_accepts_image = (
            isinstance(prompt_capabilities, dict) and prompt_capabilities.get("image") is True
        )
        client._agent_version = agent_version_from_init(init_resp)
        client._agent_version_read = True
        client._steering_advertised = steering_advertised(init_resp)
        client._initialize_result = init_resp if isinstance(init_resp, dict) else {}
        client._note_harness_adapter_version()
        if not sid:
            raise KeeperAdoptRefused("no session id is mapped to this chat")
        status_id = await client._send_request(METHOD_SESSION_STATUS, {"sessionId": sid})
        status = await client._wait_for_response(status_id, timeout=_ADOPT_REQUEST_TIMEOUT)
        if status.get("status") != SESSION_STATUS_OPEN:
            raise KeeperAdoptRefused(
                f"the adapter does not hold session {sid} ({status.get('status')!r})"
            )
        answer = _session_answer(state, sid)
        client._session_id = sid
        client._resumed = True
        client.last_prompt_stats.restore_cost_session(sid)
        client._capture_available_models(answer)
        client._store_session_config(answer)
        if client._reads_back_session_mode:
            client._verify_session_mode(answer)
        if client._pins_starting_mode:
            await client._pin_starting_mode(answer)
    except BaseException:
        client._session_id = None
        client._resumed = False
        client._resume_session_id = resume_sid
        raise
    # The cached updates go ahead of the replay, so the newest of each kind is what
    # the client ends on; they carry no sequence number and are never acked.
    for update in state.cached_updates(sid):
        client._buffer.append(
            JsonRpcMessage(
                id=update.get("id"),
                method=update.get("method"),
                result=update.get("result"),
                error=update.get("error"),
                params=update.get("params"),
            )
        )
    client._last_activity = time.monotonic()
    client._keeper_adopted_process = process
    process.resume()
    logger.info("ACP session adopted from chat keeper %s: %s", process.record.instance, sid)


def _sole_session(state: Any) -> str:
    """The one session the keeper saw created or loaded, or "" when not exactly one."""
    sessions = list(state.session_answers)
    return sessions[0] if len(sessions) == 1 else ""


def _session_answer(state: Any, sid: str) -> dict[str, Any]:
    """The keeper's cached session answer for *sid*, with its later mode and options."""
    answer = copy.deepcopy(state.session_answer(sid) or {})
    answer.setdefault("sessionId", sid)
    for update in state.cached_updates(sid):
        params = update.get("params") if isinstance(update, dict) else None
        body = params.get("update") if isinstance(params, dict) else None
        if not isinstance(body, dict):
            continue
        kind = body.get("sessionUpdate")
        if kind == "current_mode_update" and isinstance(body.get("currentModeId"), str):
            modes = answer.get("modes")
            if isinstance(modes, dict):
                modes["currentModeId"] = body["currentModeId"]
            else:
                answer["modes"] = {"currentModeId": body["currentModeId"]}
        elif kind == "config_option_update" and isinstance(body.get("configOptions"), list):
            answer["configOptions"] = body["configOptions"]
    return answer


def ack_safe_point(client: AcpClient) -> None:
    """Let the keeper drop what this client has consumed, at a point nothing is held."""
    process = keeper_process(client)
    if process is None or process.held or process.detached or client._buffer:
        return
    process.ack(process.stdout.consumed_seq)


async def detach(client: AcpClient) -> bool:
    """Hand *client*'s keeper back at a gateway exit: ack what was consumed, close.

    Nothing is killed, the settings seed stays, and no ``session/cancel`` is sent:
    the turn in flight runs on, and the next gateway adopts it. Returns False when
    *client* holds no keeper it can detach.
    """
    if not detachable(client):
        return False
    process = keeper_process(client)
    assert process is not None
    client.on_agent_turn = None
    try:
        await asyncio.wait_for(client._stop_idle_reader(), _IDLE_READER_STOP_SECS)
    except (asyncio.TimeoutError, Exception):
        logger.debug("chat keeper detach: the idle reader did not stop", exc_info=True)
    # Frames read but not yet handled are the keeper's to replay, not this turn's
    # to stream: the ack stops below the first of them and they are dropped, in the
    # same step as the reader's unconsumed lines (no await in between).
    tagged = [msg for msg in client._buffer if isinstance(getattr(msg, KEEPER_SEQ_ATTR, None), int)]
    ack = process.stdout.consumed_seq
    if tagged:
        ack = min(ack, min(getattr(msg, KEEPER_SEQ_ATTR) for msg in tagged) - 1)
        for msg in tagged:
            client._buffer.remove(msg)
    await process.detach(ack)
    setattr(client, _HANDED_OFF_ATTR, True)
    logger.info(
        "chat %s left running under keeper %s for the next gateway",
        client._session_key,
        process.record.instance,
    )
    return True
