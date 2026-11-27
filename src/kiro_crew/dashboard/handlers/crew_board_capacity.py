"""``GET /api/crew-board/capacity``: this computer's room, for the all-projects Board.

The all-projects page carries one capacity line: worker chats in use on each
computer, against the room that computer has. The worker chats are counted on the
page from the boards it already reads (each story's ``runs_on`` and ``alive``), so
this route adds only what the page cannot read anywhere else: the reading behind
``resource_status`` for THIS gateway's host, and the name its boards give this
computer, so the page can tell a story running here from one on a linked Crew.

Numbers and status words only, rebuilt from the typed probe, never its text: the
free memory, the posture and its two thresholds, and whether the macOS kernel
reports memory pressure. Crew holds no ceiling on worker chats per computer (the
execution cap counts subagents, the goal's concurrency cap counts one project's
seats), so no "N of M" is invented here: the room is the memory the probe reads.

A linked Crew's room is the word the patrol's ``[seats]`` line says for it, by the
same rule (``patrol_seats.rooms_now``): ``unreachable`` once it is not connected,
none with the remote-dispatch preview off. It is under the name its stories'
``runs_on`` carries, and read only: this route starts no crew read of its own. A
crew with no fresh reading is left out, and the page then shows its count alone.

Owner-gated like ``GET /api/crew-board``, and cookie-authed for the same reason:
a browser is its only caller.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

from aiohttp import web

from kiro_crew import resource_status
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request


class CrewRoom(TypedDict):
    """A linked Crew's room word: ``""`` has room, else what it is short of
    (``memory``, ``disk``, ``cpu``), or ``unreachable``."""

    name: str
    room: str


class CrewBoardCapacity(TypedDict):
    """The route's answer. ``available_gb`` is ``None`` when memory was not measured."""

    name: str
    available_gb: float | None
    posture: str
    tight_gb: float
    critical_gb: float
    pressure_held: bool
    crews: list[CrewRoom]


def capacity_payload(
    status: resource_status.ResourceStatus,
    name: str,
    crews: list[CrewRoom] | None = None,
) -> CrewBoardCapacity:
    """The answer for one probe *status*, with this computer's board *name*."""
    measured = status.available_gb >= 0
    return {
        "name": name,
        "available_gb": round(status.available_gb, 1) if measured else None,
        "posture": status.posture,
        "tight_gb": status.pressure_gb,
        "critical_gb": status.critical_gb,
        "pressure_held": status.memory_pressure_held,
        "crews": list(crews or []),
    }


def crew_rooms(state: Any) -> list[CrewRoom]:
    """Each linked crew's room word by the patrol's own rule, but ``unknown``. BLOCKING.

    The registry's name is the one its stories' ``runs_on`` carries.
    """
    from kiro_crew.dashboard import patrol_seats
    from kiro_crew.dashboard.handlers import remote_placement as placement

    rooms = patrol_seats.rooms_now(state, placement.remote_dispatch_settings())
    return sorted(
        (
            {"name": room.name, "room": room.word}
            for room in rooms
            if room.word != patrol_seats.UNKNOWN
        ),
        key=lambda crew: crew["name"],
    )


def _read(state: Any) -> CrewBoardCapacity:
    """One probe, the name a local story's ``runs_on`` carries, and the crews' rooms. BLOCKING."""
    from kiro_crew.relay_client.lease import default_display_name

    return capacity_payload(resource_status.probe(), default_display_name(), crew_rooms(state))


async def api_crew_board_capacity(request: web.Request) -> web.Response:
    """GET /api/crew-board/capacity -- this computer's room for worker chats."""
    denied = await require_owner_dashboard_request(request, "crew_board_capacity")
    if denied is not None:
        return denied
    payload: dict[str, Any] = dict(await asyncio.to_thread(_read, request.app["state"]))
    return web.json_response(payload)
