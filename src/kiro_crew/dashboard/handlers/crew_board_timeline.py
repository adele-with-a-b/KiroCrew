"""The Crew board's Timeline read: each item's ledger events over the last hours.

``GET /api/crew-board`` sends each item's newest 20 events, enough for its row but
not for a picture of the last 6, 12 or 24 hours. This route sends the events
themselves, oldest first, for the window the page asks for, and nothing else: the
page joins them to the board read it already holds for each item's title, state and
facts, so no derived flag is worked out twice.

What a line carries is its ``ts``, ``kind`` and report ``status``, plus a verdict's
value, and ``by: "person"`` on a person's rank drag (the line's only record of who
wrote it is its text, read through the writer's own test). Never its ``text``: a ``bind`` line's text is a worker session key, and a
report's is the worker's summary, which the board read already sends redacted. So
there is nothing here to mask or redact.

Owner-gated and cookie-authenticated like the board read, for the same reasons
(``work_ledger_board``'s module docstring).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web

from kiro_crew import session_ledger, work_ledger
from kiro_crew.dashboard.conditional_get import conditional_json_response
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.work_ledger import (
    _audit,
    _board_lock,
    _own_ledger,
    _refuse_if_dirty,
)
from kiro_crew.work_ledger_plan import is_person_rank_text

#: The windows the page offers, in hours. Anything else is refused, so a caller
#: cannot ask the gateway to read a month of logs.
WINDOW_HOURS: tuple[int, ...] = (6, 12, 24)
DEFAULT_WINDOW_HOURS = 12


def _now() -> datetime:
    """The read's clock, aware. One seam, so a test pins it."""
    return datetime.now().astimezone()


def _line(event: work_ledger.WorkEvent) -> dict[str, Any]:
    """One event as the Timeline needs it: never its text, a verdict's value only,
    and ``by: "person"`` on a person's drag, which is no move of the conductor's."""
    line: dict[str, Any] = {"ts": event.ts, "kind": event.kind, "status": event.status}
    if event.kind == "verdict" and event.text in work_ledger.VERDICTS:
        line["verdict"] = event.text
    if event.kind == "plan" and is_person_rank_text(event.text):
        line["by"] = "person"
    return line


#: Kinds that say nothing about who has the work, so they open no stretch: the page
#: runs the stretch before one straight through it (``AFTER`` in
#: ``website/src/lib/boardTimeline.ts``). A plan step or a rank drag is bookkeeping.
CARRIES_THROUGH: frozenset[str] = frozenset({"plan"})


def window_events(
    events: list[work_ledger.WorkEvent], since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """The lines from *since* to *until*, oldest first, led by the newest line
    before *since* that opens a stretch: what the item was doing when the window
    opens. Never a ``plan`` line, which would leave a story bound before the window
    and planned since drawing an empty lane. A line whose stamp does not parse is
    skipped: it cannot be placed on a time axis."""
    placed: list[tuple[datetime, work_ledger.WorkEvent]] = []
    for event in events:
        at = work_ledger.parse_stamp(event.ts)
        if at is not None and at <= until:
            placed.append((at, event))
    placed.sort(key=lambda pair: pair[0])
    before = [event for at, event in placed if at < since and event.kind not in CARRIES_THROUGH]
    inside = [event for at, event in placed if at >= since]
    return [_line(event) for event in before[-1:] + inside]


def _overlaps(item: work_ledger.WorkItem, since: datetime) -> bool:
    """Whether *item* was still open at some point inside the window."""
    if not item.closed_at:
        return True
    closed = work_ledger.parse_stamp(item.closed_at)
    return closed is None or closed >= since


def _read_logs(key: str, item_ids: list[str]) -> dict[str, list[work_ledger.WorkEvent]]:
    """Each item's whole event log, oldest first. BLOCKING."""
    return {item_id: work_ledger.read_events(key, item_id) for item_id in item_ids}


def _hours(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return DEFAULT_WINDOW_HOURS
    return int(text) if text.isdigit() and int(text) in WINDOW_HOURS else None


async def api_crew_board_timeline(request: web.Request) -> web.Response:
    """GET /api/crew-board/timeline?conductor=KEY&hours=12 -- the Timeline's events."""
    denied = await require_owner_dashboard_request(request, "crew_board_timeline")
    if denied is not None:
        return denied
    caller = str(request.get("user") or "unknown")
    requested = str(request.query.get("conductor") or "").strip()
    if not requested:
        return web.json_response(
            {
                "error": "a conductor session key is required (?conductor=KEY)",
                "code": "missing_conductor",
            },
            status=400,
        )
    hours = _hours(str(request.query.get("hours") or ""))
    if hours is None:
        return web.json_response(
            {"error": "hours must be one of 6, 12 or 24", "code": "invalid_hours"},
            status=400,
        )
    key = session_ledger.ledger_key(requested)
    until = _now()
    since = until - timedelta(hours=hours)
    try:
        async with _board_lock(key):
            _record, refusal = await _own_ledger(key, "crew_board_timeline", caller)
            if refusal is not None:
                return refusal
            dirty = await _refuse_if_dirty(key, caller, "crew_board_timeline")
            if dirty is not None:
                return dirty
            items = await asyncio.to_thread(work_ledger.list_work_items, key)
    except work_ledger.WorkLedgerError as exc:
        if exc.code != work_ledger.CODE_INVALID_VALUE:
            raise
        return web.json_response(
            {
                "error": "that conductor session key cannot name a work ledger",
                "code": "invalid_conductor",
            },
            status=400,
        )
    # The logs are read after the board hold is let go: the hold is for the ownership
    # check and the item list, and a write replaces a log whole (an atomic rename), so
    # each log read here is one the writer finished. Holding it across every shown
    # item's whole log would stall the board's own writes for as long as they take.
    shown = [item for item in items if _overlaps(item, since)]
    logs = await asyncio.to_thread(_read_logs, key, [item.item_id for item in shown])
    events = {item_id: window_events(log, since, until) for item_id, log in logs.items()}
    payload = {
        "read_at": until.isoformat(timespec="seconds"),
        "since": since.isoformat(timespec="seconds"),
        "hours": hours,
        "events": {item_id: lines for item_id, lines in events.items() if lines},
    }
    _audit(caller, "crew_board_timeline_read", "ok", resources=f"conductor={key} hours={hours}")
    return await conditional_json_response(request, json.dumps(payload).encode("utf-8"))
