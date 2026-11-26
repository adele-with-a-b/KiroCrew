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
        self.task: asyncio.Future[None] | None = None


_CACHE: "weakref.WeakKeyDictionary[Any, dict[str, _Crew]]" = weakref.WeakKeyDictionary()


async def _read(state: Any, instance_id: str, crew: _Crew, wanted: frozenset[str]) -> None:
    try:
        got = await read_peer_slots(state, instance_id)
    except PeerSlotsUnavailable as exc:
        logger.info("crew workers: %s's session list unread (%s)", instance_id, exc.code)
    except Exception:
        logger.debug("crew workers: %s's session list unread", instance_id, exc_info=True)
    else:
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


def open_fields(instance_id: str, slot: str, gone: bool) -> dict[str, str]:
    """What the page opens a crew's chat with (its crew window), for every remote row.

    None when the chat is known *gone* from its crew, and none when either half is
    unusable. A finished task is not a gone chat: its chat may still be open there,
    so a ``closed`` bound row whose chat is not known gone still opens.
    """
    if gone or not instance_id or not _slot_ok(slot):
        return {}
    return {"remote_instance": instance_id, "remote_slot": slot}
