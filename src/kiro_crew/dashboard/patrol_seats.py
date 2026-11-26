"""The ``[seats]`` lines a work-ledger patrol wake ends with.

Conductors left worker seats empty while tasks with no worker waited, and filled
them only when the owner asked; a rule in the prompt was not enough. So the wake
itself states the facts, computed here at delivery, on every turn a work-ledger
watch delivers (``nudge_dashboard_fire``) and on a ``[work-ledger wake]`` steered
into a running turn:

* the open tasks no worker is on, as ``it_`` ids and a count;
* the room on this computer, ``resource_status``'s posture, and, with the
  remote-dispatch preview on, on each registered crew, read and judged the way
  placement does (``remote_placement.read_headroom`` and ``room_short``);
* the closed items whose worker session is still open, here or on its crew.

Then one fixed instruction line. A board with nothing to say adds nothing, so a
quiet patrol costs no extra text, and its room is not even read.

IDS AND COUNTS ONLY. Every value is a store-minted item id, a count, a posture or
a fixed word, never an item's title or a worker's summary, so nothing a worker
reports can put a line here or forge one.

A crew never delays the wake. The line shows the last room each crew reported
(:data:`ROOM_FRESH_SECS`, else ``unknown``) and starts one bounded refresh in the
background for the next wake; a crew that is not connected reads ``unreachable``
without being asked.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref
from typing import Any, Sequence

from kiro_crew.conductor_patrol import PATROL_INTERVAL_SECS

logger = logging.getLogger(__name__)

SEATS_TAG = "[seats]"

SEATS_INSTRUCTION = (
    f"{SEATS_TAG} Dispatch every ready, independent task with no worker now, on whichever "
    "computer has room, and close finished workers; say which you did not start and why."
)

#: Ids named per list; the count before them is always the whole number.
MAX_LISTED_IDS = 10

#: How old a crew's last reading may be and still be shown: two patrol intervals,
#: so the reading a previous cycle started stands for this one.
ROOM_FRESH_SECS = 2 * PATROL_INTERVAL_SECS

UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

_ITEM_ID = re.compile(r"it_[0-9a-f]{8}")
#: A crew name is the owner's own label for it; only these characters are shown.
_CREW_NAME_DROP = re.compile(r"[^A-Za-z0-9 ._-]")
_CREW_NAME_CHARS = 40

#: The clock readings are aged by; a test replaces it rather than ``time.monotonic``.
_clock = time.monotonic

#: Per gateway state: ``{crew id: (room word, read at)}``, and the refresh in flight.
_READINGS: "weakref.WeakKeyDictionary[Any, dict[str, tuple[str, float]]]" = (
    weakref.WeakKeyDictionary()
)
_REFRESHING: "weakref.WeakKeyDictionary[Any, asyncio.Future[Any]]" = weakref.WeakKeyDictionary()


def _listed(ids: Sequence[str]) -> str:
    if not ids:
        return "0"
    shown = ", ".join(ids[:MAX_LISTED_IDS])
    more = len(ids) - MAX_LISTED_IDS
    return f"{len(ids)} ({shown}{f', +{more} more' if more > 0 else ''})"


def _crew_name(name: str) -> str:
    return _CREW_NAME_DROP.sub("", name)[:_CREW_NAME_CHARS].strip() or "?"


def _room_phrase(word: str) -> str:
    if word == "":
        return "has room"
    if word in (UNREACHABLE, UNKNOWN):
        return word
    return f"is short on {word}"


def seats_block(
    *,
    unstarted: Sequence[str],
    finished_open: Sequence[str],
    local_room: str = "",
    crews: Sequence[tuple[str, str]] = (),
) -> str:
    """The ``[seats]`` fact line and the instruction line, or ``""`` with nothing to say.

    *crews* is ``(name, room word)`` pairs: ``""`` for room, a short resource, or
    :data:`UNREACHABLE` / :data:`UNKNOWN`. Room is stated only when a task waits.
    Ids are listed sorted, and anything that is not an item id is dropped.
    """
    unstarted = sorted(i for i in unstarted if _ITEM_ID.fullmatch(i))
    finished_open = sorted(i for i in finished_open if _ITEM_ID.fullmatch(i))
    if not unstarted and not finished_open:
        return ""
    parts = [f"{SEATS_TAG} tasks with no worker: {_listed(unstarted)}."]
    if unstarted:
        rooms = [f"this computer {(local_room or UNKNOWN).upper()}"]
        rooms += [f'crew "{_crew_name(name)}" {_room_phrase(word)}' for name, word in crews]
        parts.append(f"Room: {'; '.join(rooms)}.")
    if finished_open:
        parts.append(f"Finished workers still open: {_listed(finished_open)}.")
    return " ".join(parts) + "\n" + SEATS_INSTRUCTION


def local_room() -> str:
    """This computer's posture as ``resource_status`` reads it. Blocking."""
    from kiro_crew import resource_status

    try:
        return str(resource_status.probe().posture)
    except Exception:  # noqa: BLE001 - probe() answers unknown itself; this is a backstop
        return resource_status.POSTURE_UNKNOWN


async def crew_rooms(state: Any) -> list[tuple[str, str]]:
    """``(name, room word)`` per registered crew, from readings already taken.

    Empty with the remote-dispatch preview off or no crew manager. Never waits on a
    crew: it starts :func:`_refresh` and returns.
    """
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes
    from kiro_crew.dashboard.handlers import remote_placement as placement

    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        return []
    settings = await asyncio.to_thread(placement.remote_dispatch_settings)
    if not settings.enabled:
        return []
    try:
        rows = await asyncio.to_thread(routes._instances, state)
    except Exception:  # noqa: BLE001 - an unreadable registry names no crew
        logger.debug("seats: crew registry unreadable", exc_info=True)
        return []
    readings = _READINGS.setdefault(state, {})
    now = _clock()
    out: list[tuple[str, str]] = []
    connected: list[str] = []
    for row in rows:
        instance_id = str(getattr(row, "id", "") or "")
        if not instance_id:
            continue
        name = str(getattr(row, "name", "") or instance_id)
        if not placement._connected(mgr, instance_id):
            out.append((name, UNREACHABLE))
            continue
        connected.append(instance_id)
        seen = readings.get(instance_id)
        fresh = seen is not None and 0 <= now - seen[1] <= ROOM_FRESH_SECS
        out.append((name, seen[0] if seen is not None and fresh else UNKNOWN))
    _refresh(state, mgr, connected, settings.memory_gb, settings.disk_gb)
    return out


def _refresh(state: Any, mgr: Any, instance_ids: list[str], mem_gb: float, disk_gb: float) -> None:
    """Start one background headroom read of *instance_ids*, unless one is running.

    Each crew's read is bounded by ``remote_placement.PROBE_TIMEOUT_S``; a crew
    that gives no snapshot is stored as :data:`UNREACHABLE`.
    """
    running = _REFRESHING.get(state)
    if not instance_ids or (running is not None and not running.done()):
        return
    from kiro_crew.dashboard.handlers import remote_placement as placement

    async def _one(instance_id: str) -> None:
        payload = await placement.read_headroom(mgr, instance_id)
        word = UNREACHABLE if payload is None else placement.room_short(payload, mem_gb, disk_gb)
        _READINGS.setdefault(state, {})[instance_id] = (word, _clock())

    future = asyncio.gather(
        *(_one(instance_id) for instance_id in instance_ids), return_exceptions=True
    )
    future.add_done_callback(_settled)
    _REFRESHING[state] = future


def _settled(future: "asyncio.Future[Any]") -> None:
    """Read a finished refresh's outcome, so one cancelled at shutdown logs nothing."""
    if not future.cancelled():
        future.exception()


async def seats_text(state: Any, conductor_key: str) -> str:
    """The ``[seats]`` lines for *conductor_key*'s patrol wake, or ``""``. Never raises."""
    from kiro_crew.probes.work_ledger import seat_facts

    try:
        exists = getattr(state, "slot_exists", None)
        facts = await asyncio.to_thread(
            seat_facts, conductor_key, slot_exists=exists if callable(exists) else _no_slot
        )
        if facts is None:
            return ""
        unstarted, finished_open = facts
        if not unstarted:
            return seats_block(unstarted=(), finished_open=finished_open)
        room = await asyncio.to_thread(local_room)
        crews = await crew_rooms(state)
        return seats_block(
            unstarted=unstarted, finished_open=finished_open, local_room=room, crews=crews
        )
    except Exception:  # noqa: BLE001 - the wake goes out without the line
        logger.warning("seats: line not built for %s", conductor_key, exc_info=True)
        return ""


def _no_slot(_key: str) -> bool:
    return False
