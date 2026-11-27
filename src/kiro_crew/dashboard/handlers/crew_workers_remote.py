"""A Conductor's chats on another crew that no ledger item names, as worker rows.

``session_create(crew=)`` without ``work_item`` opens a chat on a connected crew
that only the hub's dispatch record knows (``remote_dispatch_routes``): no ledger
item names it, so the ledger read behind ``GET .../crew-workers`` never lists it.
Only a conductor mode root may open one (``authorize_remote_dispatch``), and
:func:`untied_rows` lists each one under the root that opened it.

The record is read through :func:`dispatched_slots` alone and never written here:
``remote_dispatch_routes`` owns it and its stored copy. A chat's title and state are its crew's own,
from that crew's live session list (``read_peer_slots``, each row redacted by
``_clean_peer_slot``), read at most once per crew every :data:`PEER_READ_TTL_SECS`
and never waited on past :data:`PEER_READ_TIMEOUT_SECS`. A read that fails or is
late leaves the last state read, so a crew that went away keeps its rows.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Iterable, Mapping
from typing import Any

from kiro_crew.dashboard.handlers import crew_workers
from kiro_crew.dashboard.handlers.remote_dispatch_routes import (
    _DISPATCHED,
    _slot_ok,
    load_record,
)
from kiro_crew.dashboard.handlers_instances_peer import (
    PeerSlotsUnavailable,
    _clean_peer_slot,
    read_peer_slots,
)

logger = logging.getLogger(__name__)

#: How long one read of a crew's session list answers every worker list.
PEER_READ_TTL_SECS = 10.0
#: The longest a worker list waits on a crew's read; the read goes on behind it.
PEER_READ_TIMEOUT_SECS = 2.0

#: The clock the kept reads age by.
_now = time.monotonic


async def dispatched_slots(
    state: Any, callers: Iterable[str] | None = None
) -> list[tuple[str, str, str]]:
    """``(caller, instance id, crew slot)`` for each chat opened with ``session_create(crew=)``.

    By *callers* only when given. The one read of the dispatch record, after its
    owner's :func:`load_record` has folded in the copy the data home keeps, so a
    chat opened before a hub restart is listed too; a store that cannot be read
    leaves what this process recorded. A state the record cannot key (a test
    stub) holds none.
    """
    try:
        weakref.ref(state)
    except TypeError:
        return []
    await load_record(state)
    record = _DISPATCHED.get(state) or {}
    wanted = record if callers is None else dict.fromkeys(callers)
    return sorted(
        (caller, instance_id, slot)
        for caller in wanted
        for instance_id, slot in record.get(caller, ())
    )


class _Crew:
    """One crew's last session-list read: the chats asked for and the rows found."""

    def __init__(self) -> None:
        #: When the last read settled (failed or not); ``None`` before the first.
        self.at: float | None = None
        #: The chats the last read was started for.
        self.tried: frozenset[str] = frozenset()
        #: The chats the last answered read could say were open or gone.
        self.asked: frozenset[str] = frozenset()
        #: Crew slot -> its redacted row, for the asked chats that are open.
        self.rows: dict[str, dict[str, object]] = {}
        #: Whether the crew answered the last read that settled.
        self.answered = False
        self.task: asyncio.Future[None] | None = None


_CACHE: "weakref.WeakKeyDictionary[Any, dict[str, _Crew]]" = weakref.WeakKeyDictionary()


async def _read(state: Any, instance_id: str, crew: _Crew, wanted: frozenset[str]) -> None:
    try:
        got = await read_peer_slots(state, instance_id)
    except PeerSlotsUnavailable as exc:
        crew.answered = False
        logger.info("crew workers: %s's session list unread (%s)", instance_id, exc.code)
    except Exception:
        crew.answered = False
        logger.debug("crew workers: %s's session list unread", instance_id, exc_info=True)
    else:
        crew.answered = True
        rows: dict[str, dict[str, object]] = {}
        for raw in got.rows:
            key = raw.get("key") if isinstance(raw, Mapping) else None
            if isinstance(key, str) and key in wanted:
                clean = _clean_peer_slot(raw)
                if clean is not None:
                    rows[key] = clean
        # A list cut at its row cap cannot say a chat past the cut is gone.
        crew.rows, crew.asked = rows, wanted if not got.over_cap else frozenset(rows)
    finally:
        crew.at = _now()


async def _crew_rows(
    state: Any, instance_id: str, wanted: frozenset[str]
) -> tuple[dict[str, dict[str, object]], frozenset[str]]:
    """``(open rows, chats the read could answer for)`` from *instance_id*'s list.

    Read again when the kept read is older than the window or was not asked for
    every wanted chat; one read per crew at a time, shared by every list.
    """
    crew = _CACHE.setdefault(state, {}).setdefault(instance_id, _Crew())
    fresh = crew.at is not None and _now() - crew.at < PEER_READ_TTL_SECS and wanted <= crew.tried
    if not fresh and (crew.task is None or crew.task.done()):
        crew.tried = wanted
        crew.task = asyncio.ensure_future(_read(state, instance_id, crew, wanted))
    if crew.task is not None and not crew.task.done():
        try:
            await asyncio.wait_for(asyncio.shield(crew.task), PEER_READ_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            pass
    return crew.rows, crew.asked


async def untied_rows(
    state: Any,
    root: str,
    items: Mapping[str, crew_workers.LedgerFacts],
    names: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Worker rows for the chats *root* opened on a crew that no item in *items* names.

    *items* is the crew's ledger facts (a bound chat is its item's row already),
    *names* the crews' display names. A crew member is never a caller here: the
    dispatch refuses any chat but a conductor mode root.
    """
    tied = {
        (facts.crew, str(getattr(facts.work_item, "worker_session_key", "") or ""))
        for facts in items.values()
        if facts.crew
    }
    mine = [row for row in await dispatched_slots(state, (root,)) if row[1:] not in tied]
    if not mine:
        return []
    # Every chat on a crew is asked for, whoever opened it, so one read answers
    # every Conductor's list.
    wanted: dict[str, set[str]] = {}
    for _caller, instance_id, slot in await dispatched_slots(state):
        wanted.setdefault(instance_id, set()).add(slot)
    crews = sorted({instance_id for _caller, instance_id, _slot in mine})
    reads = await asyncio.gather(*(_crew_rows(state, i, frozenset(wanted[i])) for i in crews))
    seen = dict(zip(crews, reads))
    out: list[dict[str, Any]] = []
    for caller, instance_id, slot in mine:
        rows, asked = seen[instance_id]
        row = rows.get(slot)
        # The crew answered and has no such chat: it was closed there.
        gone = row is None and slot in asked
        if row is not None:
            status = crew_workers._status(row)
        elif gone:
            status = crew_workers.STATUS_CLOSED
        else:
            status = crew_workers.STATUS_IDLE
        title = row.get("title") if row is not None else ""
        out.append(
            {
                "key": f"{crew_workers.REMOTE_KEY_PREFIX}{instance_id}:{slot}",
                "title": (
                    title if isinstance(title, str) and title else crew_workers._short_key(slot)
                ),
                "agent": "",
                "status": status,
                "group": crew_workers._group(status),
                "last_activity": "",
                "ledger_item": "",
                "summary": "",
                "created_by": caller,
                "dispatched_by": caller,
                "remote_crew": names.get(instance_id) or instance_id,
                **open_fields(instance_id, slot, gone),
            }
        )
    return out


#: A chat whose close the hub holds until its crew settles it (``remote_kept_closes``).
STATUS_CLOSING = "closing"
#: A chat whose state on its crew is not known: open or gone.
STATUS_UNKNOWN = "unknown"

#: ``crew_state`` on a ``session_status`` answer: the crews were asked, or the
#: turn is not one that may read a crew and the rows are the hub's record alone.
CREW_STATE_READ = "read"
CREW_STATE_OWNER_TURN_ONLY = "owner_turn_only"


async def _may_read_crews(state: Any, caller: str) -> bool:
    """Whether *caller*'s running turn may read another crew: the ``crew=`` owner arm.

    The rule every crew-scoped read keeps (``authorize_remote_dispatch``, arm 8):
    a turn the owner sent, or one the owner's patrol-wake or granted-turns setting
    admits, audited as that arm audits it. Nothing is refused here: a turn that
    may not read a crew still gets its rows, without their state.
    """
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    slot = state.get_slot(caller)
    if slot is None:
        return False
    if not routes.owner_unverifiable_reason(slot):
        return True
    if await routes._patrol_wake_admitted(slot):
        routes._audit_patrol_wake(caller, "status")
        return True
    if await routes._granted_turn_admitted(state, slot, caller):
        routes._audit_granted_turn(caller, "status")
        return True
    return False


async def status_rows(state: Any, caller: str) -> tuple[list[dict[str, Any]], str]:
    """``(rows, crew_state)``: the chats *caller* opened on other crews, for ``session_status``.

    The rows are the dispatch record's chats plus the closes still kept for
    *caller*: the hub's own record, listed in any turn. Their state is read from
    each crew through the road the worker list uses (:func:`_crew_rows`), and only
    in a turn that may read a crew (:func:`_may_read_crews`); otherwise no crew is
    asked and ``crew_state`` says so.

    ``status`` is ``closing`` for a kept close, else the chat's state on its crew
    (``working``, ``idle``, ``needs_approval``, ``asking``), ``closed`` when the
    crew answered and holds no such chat, ``unknown`` when its list was not read.
    ``crew_answered`` is ``False`` when the crew did not answer the latest read, so
    the state is the last one read. Nothing the crew wrote crosses: a row is the
    key the caller already holds, the crew's id and registry name, and these words.
    """
    from kiro_crew.dashboard.handlers import remote_kept_closes

    closing = {(i, s) for _c, i, s in await remote_kept_closes.kept_closes(state, (caller,))}
    mine = sorted({(i, s) for _c, i, s in await dispatched_slots(state, (caller,))} | closing)
    if not mine:
        return [], CREW_STATE_READ
    names = await asyncio.to_thread(crew_workers._crew_names, state)
    seen: dict[str, tuple[dict[str, dict[str, object]], frozenset[str]]] = {}
    read = await _may_read_crews(state, caller)
    if read:
        wanted: dict[str, set[str]] = {}
        for _caller, instance_id, slot in await dispatched_slots(state):
            wanted.setdefault(instance_id, set()).add(slot)
        for instance_id, slot in mine:
            wanted.setdefault(instance_id, set()).add(slot)
        crews = sorted(wanted.keys() & {instance_id for instance_id, _slot in mine})
        reads = await asyncio.gather(*(_crew_rows(state, i, frozenset(wanted[i])) for i in crews))
        seen = dict(zip(crews, reads))
    unread: tuple[dict[str, dict[str, object]], frozenset[str]] = ({}, frozenset())
    out: list[dict[str, Any]] = []
    for instance_id, slot in mine:
        rows, asked = seen.get(instance_id, unread)
        row = rows.get(slot)
        if (instance_id, slot) in closing:
            status = STATUS_CLOSING
        elif row is not None:
            status = crew_workers._status(row)
        elif slot in asked:
            status = crew_workers.STATUS_CLOSED
        else:
            status = STATUS_UNKNOWN
        out.append(
            {
                "target": slot,
                "crew": instance_id,
                "crew_name": names.get(instance_id) or instance_id,
                "status": status,
                "crew_answered": read and _CACHE[state][instance_id].answered,
            }
        )
    return out, CREW_STATE_READ if read else CREW_STATE_OWNER_TURN_ONLY


async def with_crew_sessions(state: Any, result: dict[str, Any]) -> dict[str, Any]:
    """A ``session_status`` answer plus ``crew_sessions``, while the preview is on.

    Added after the roster is built, for the caller that roster verified. A fault
    here leaves the answer as it was: the local roster never fails on a crew.
    """
    from kiro_crew.remote_dispatch import remote_dispatch_enabled

    try:
        if not await asyncio.to_thread(remote_dispatch_enabled):
            return result
        caller = result.get("caller")
        if not isinstance(caller, str) or not caller:
            return result
        rows, crew_state = await status_rows(state, caller)
    except Exception:  # noqa: BLE001 - the local roster stands on its own
        logger.warning("session status: crew sessions unread", exc_info=True)
        return result
    return {**result, "crew_sessions": rows, "crew_state": crew_state}


def open_fields(instance_id: str, slot: str, gone: bool) -> dict[str, str]:
    """What the page opens a crew's chat with (its crew window), for every remote row.

    None when the chat is known *gone* from its crew, and none when either half is
    unusable. A finished task is not a gone chat: its chat may still be open there,
    so a ``closed`` bound row whose chat is not known gone still opens.
    """
    if gone or not instance_id or not _slot_ok(slot):
        return {}
    return {"remote_instance": instance_id, "remote_slot": slot}
