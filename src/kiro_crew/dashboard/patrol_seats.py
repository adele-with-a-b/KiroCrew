"""The ``[seats]`` lines a work-ledger patrol wake ends with.

Conductors left worker seats empty while tasks with no worker waited, and filled
them only when the owner asked; a rule in the prompt was not enough. So the wake
itself states the facts, computed here at delivery, on every turn a work-ledger
watch delivers (``nudge_dashboard_fire``) and on a ``[work-ledger wake]`` steered
into a running turn:

* the stories holding a seat in the goal's ``concurrency_cap`` with no worker, and,
  while a seat is free, the marked backlog and its next pick, in the store's own
  seat count and pick order (``probes.work_ledger.seat_facts``);
* the room on this computer, ``resource_status``'s posture, and, with the
  remote-dispatch preview on, on each registered crew, read and judged the way
  placement does (``remote_placement.read_headroom`` and ``room_short``);
* the finished worker sessions still open, here or on a crew a close can reach,
  one closed item named per session, and none for a session an open item uses. A
  crew's are named only from a fresh read of its slot list (:func:`crew_slot_open`),
  never from the record a pull left while the item was still open.

Then one fixed instruction line. A board with nothing to say adds nothing, so a
quiet patrol costs no extra text, and its room is not even read.

IDS AND COUNTS ONLY. Every value is a store-minted item id, a count, a posture or
a fixed word, never an item's title or a worker's summary, so nothing a worker
reports can put a line here or forge one.

A crew never delays the wake. The line shows the last room each crew reported
(:data:`ROOM_FRESH_SECS`, else ``unknown``) and starts one bounded refresh in the
background for the next wake; a crew that is not connected reads ``unreachable``
without being asked. The Board's run line reads the same words by the same rule
(:func:`rooms_now`), and starts no refresh.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref
from typing import Any, NamedTuple, Sequence

from kiro_crew.conductor_patrol import PATROL_INTERVAL_SECS

logger = logging.getLogger(__name__)

SEATS_TAG = "[seats]"

SEATS_INSTRUCTION = (
    f"{SEATS_TAG} Dispatch every ready, independent task in a seat with no worker now, and "
    "pull the next pick into each free seat, on whichever computer has room; close finished "
    "workers; say which you did not start and why."
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

#: Per gateway state: ``{crew id: (slot keys its list named, read at)}``, and the
#: list read in flight. Only a complete list is kept; an unreadable one drops the
#: crew's reading, so its finished workers are left out rather than guessed.
_ROSTERS: "weakref.WeakKeyDictionary[Any, dict[str, tuple[frozenset[str], float]]]" = (
    weakref.WeakKeyDictionary()
)
_ROSTERING: "weakref.WeakKeyDictionary[Any, asyncio.Future[Any]]" = weakref.WeakKeyDictionary()


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


def _pullable(backlog: Sequence[str], seats_free: int | None) -> bool:
    """Whether a backlog story can be pulled now: one waits and a seat is free."""
    return bool(backlog) and (seats_free is None or seats_free > 0)


def seats_block(
    *,
    ready: Sequence[str] = (),
    backlog: Sequence[str] = (),
    seats_free: int | None = None,
    cap: int | None = None,
    finished_open: Sequence[str] = (),
    local_room: str = "",
    crews: Sequence[tuple[str, str]] = (),
) -> str:
    """The ``[seats]`` fact line and the instruction line, or ``""`` with nothing to say.

    *ready* are stories holding a seat with no worker, *backlog* the marked backlog,
    both in pick order (``probes.work_ledger.SeatFacts``); *seats_free* of *cap* is
    the goal's free seats, ``None`` with no cap. A backlog with no free seat is
    nothing to do, so it is not mentioned. *crews* is ``(name, room word)`` pairs:
    ``""`` for room, a short resource, or :data:`UNREACHABLE` / :data:`UNKNOWN`.
    Room is stated only when something can start. Anything that is not an item id
    is dropped.
    """
    ready = [i for i in ready if _ITEM_ID.fullmatch(i)]
    backlog = [i for i in backlog if _ITEM_ID.fullmatch(i)]
    finished_open = [i for i in finished_open if _ITEM_ID.fullmatch(i)]
    pullable = _pullable(backlog, seats_free)
    if not ready and not pullable and not finished_open:
        return ""
    parts = [f"{SEATS_TAG} seats with no worker: {_listed(ready)}."]
    if pullable:
        free = "no cap" if seats_free is None else f"{seats_free} of {cap} seats free"
        parts.append(f"Backlog: {len(backlog)} waiting, {free}, next pick {backlog[0]}.")
    if ready or pullable:
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


class RoomNow(NamedTuple):
    """One registered crew's room right now, as :func:`rooms_now` reads it."""

    instance_id: str
    name: str
    #: ``""`` for room, a short resource, :data:`UNREACHABLE` or :data:`UNKNOWN`.
    word: str
    #: Only a connected crew is read again (:func:`_refresh`).
    connected: bool


def rooms_now(state: Any, settings: Any) -> list[RoomNow]:
    """Each registered crew's room right now. BLOCKING; read only.

    The one rule both the ``[seats]`` line (:func:`crew_rooms`) and the Board's run
    line (``handlers/crew_board_capacity.py``) read a crew's room by, so they never
    disagree: no crew with the remote-dispatch preview off (*settings*, as
    ``remote_dispatch_settings`` reads it) or no crew manager; a crew that is not
    connected is :data:`UNREACHABLE` without being asked; else its last reading
    while :data:`ROOM_FRESH_SECS` old, else :data:`UNKNOWN`. It starts no read and
    stores nothing; only :func:`crew_rooms` starts a refresh.
    """
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes
    from kiro_crew.dashboard.handlers import remote_placement as placement

    mgr = getattr(state, "instances_manager", None)
    if mgr is None or not settings.enabled:
        return []
    try:
        rows = routes._instances(state)
    except Exception:  # noqa: BLE001 - an unreadable registry names no crew
        logger.debug("seats: crew registry unreadable", exc_info=True)
        return []
    try:
        readings = dict(_READINGS.get(state) or {})
    except TypeError:  # a state that takes no weak reference never held a reading
        readings = {}
    now = _clock()
    out: list[RoomNow] = []
    for row in rows:
        instance_id = str(getattr(row, "id", "") or "")
        if not instance_id:
            continue
        name = str(getattr(row, "name", "") or instance_id)
        if not placement._connected(mgr, instance_id):
            out.append(RoomNow(instance_id, name, UNREACHABLE, False))
            continue
        seen = readings.get(instance_id)
        fresh = seen is not None and 0 <= now - seen[1] <= ROOM_FRESH_SECS
        word = seen[0] if seen is not None and fresh else UNKNOWN
        out.append(RoomNow(instance_id, name, word, True))
    return out


async def crew_rooms(state: Any) -> list[tuple[str, str]]:
    """``(name, room word)`` per registered crew, by :func:`rooms_now`.

    Never waits on a crew: it starts :func:`_refresh` of the connected ones and returns.
    """
    from kiro_crew.dashboard.handlers import remote_placement as placement

    settings = await asyncio.to_thread(placement.remote_dispatch_settings)
    rooms = await asyncio.to_thread(rooms_now, state, settings)
    connected = [room.instance_id for room in rooms if room.connected]
    mgr = getattr(state, "instances_manager", None)
    _refresh(state, mgr, connected, settings.memory_gb, settings.disk_gb)
    return [(room.name, room.word) for room in rooms]


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
        # ``has_slot``, not ``slot_exists``: the latter answers open for every name
        # while a restore runs, and only a slot really present is a worker still open.
        present = getattr(state, "has_slot", None)
        facts = await asyncio.to_thread(
            seat_facts,
            conductor_key,
            slot_present=present if callable(present) else _no_slot,
            crew_reachable=lambda instance_id: crew_reachable(state, instance_id),
            crew_slot_open=lambda instance_id, slot: crew_slot_open(state, instance_id, slot),
        )
        if facts is None:
            return ""
        _refresh_rosters(state, list(facts.finished_crews))
        room = ""
        crews: list[tuple[str, str]] = []
        if facts.ready or _pullable(facts.backlog, facts.seats_free):
            room = await asyncio.to_thread(local_room)
            crews = await crew_rooms(state)
        return seats_block(
            ready=facts.ready,
            backlog=facts.backlog,
            seats_free=facts.seats_free,
            cap=facts.cap,
            finished_open=facts.finished_open,
            local_room=room,
            crews=crews,
        )
    except Exception:  # noqa: BLE001 - the wake goes out without the line
        logger.warning("seats: line not built for %s", conductor_key, exc_info=True)
        return ""


def _no_slot(_key: str) -> bool:
    return False


def crew_slot_open(state: Any, instance_id: str, slot: str) -> bool:
    """Whether a fresh read of *instance_id*'s slot list named *slot*. Memory read.

    Fresh is :data:`ROOM_FRESH_SECS`, as a crew's room is. No reading, an old one,
    or one that did not name *slot* answers False: a finished worker is counted
    open only when the crew itself says so now.
    """
    seen = _ROSTERS.get(state, {}).get(instance_id)
    return seen is not None and 0 <= _clock() - seen[1] <= ROOM_FRESH_SECS and slot in seen[0]


def _refresh_rosters(state: Any, instance_ids: list[str]) -> None:
    """Start one background slot-list read of *instance_ids*, unless one is running.

    The read the remote puller takes (``remote_ingest._slot_list``), bounded by
    ``remote_placement.PROBE_TIMEOUT_S`` per crew, for the next wake: like the room,
    the line never waits on a crew.
    """
    running = _ROSTERING.get(state)
    if not instance_ids or (running is not None and not running.done()):
        return
    from kiro_crew.dashboard.handlers import remote_ingest
    from kiro_crew.dashboard.handlers import remote_placement as placement

    async def _one(instance_id: str) -> None:
        try:
            listed = await asyncio.wait_for(
                remote_ingest._slot_list(state, instance_id), placement.PROBE_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 - an unread list is no reading
            listed = None
        readings = _ROSTERS.setdefault(state, {})
        if listed is None:
            readings.pop(instance_id, None)
        else:
            readings[instance_id] = (frozenset(listed), _clock())

    future = asyncio.gather(
        *(_one(instance_id) for instance_id in instance_ids), return_exceptions=True
    )
    future.add_done_callback(_settled)
    _ROSTERING[state] = future


def crew_reachable(state: Any, instance_id: str) -> bool:
    """Whether a ``session_close(crew=)`` can reach *instance_id* now. Blocking.

    That verb needs the remote-dispatch preview on and the crew registered
    (``resolve_crew``) and connected, judged as the room part judges it
    (``remote_placement._connected``). A finished worker on a crew it cannot
    reach is not named: no close could take it off the line.
    """
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes
    from kiro_crew.dashboard.handlers import remote_placement as placement

    mgr = getattr(state, "instances_manager", None)
    if mgr is None or not placement.remote_dispatch_settings().enabled:
        return False
    rows = routes._instances(state)
    registered = any(str(getattr(row, "id", "") or "") == instance_id for row in rows)
    return registered and placement._connected(mgr, instance_id)
