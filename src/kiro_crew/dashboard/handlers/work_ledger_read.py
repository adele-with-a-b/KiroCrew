"""The conductor's read of its own board: ``GET /api/work-ledger``.

The query options and filters, the compact row, each item's event tail, and the flags
a row derives at read time -- orphaned, stale, acceptance concrete, unpatrolled and
pending overdue -- from the dashboard's own slot table and the wake gate's resolvers;
and whether a slot is a worker still holding an open item.

Served through :mod:`kiro_crew.dashboard.handlers.work_ledger`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, NamedTuple

from aiohttp import web

from kiro_crew import conductor_patrol, ledger_wake, session_ledger, work_ledger
from kiro_crew.dashboard import queue_age, session_control
from kiro_crew.dashboard.handlers.crew_board_flight import BoardAnswer, answer_of, shared_board
from kiro_crew.dashboard.handlers.work_ledger_caller import (
    _caller_key,
    _code_channel_caller,
    _contained_channel_caller,
    _own_ledger,
)
from kiro_crew.dashboard.handlers.work_ledger_recording import _board_lock, _refuse_if_dirty
from kiro_crew.dashboard.handlers.work_ledger_refusals import (
    _audit,
    _refuse_400,
    _refuse_403,
    _validation_code,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.validation import WORK_LEDGER_READ_SCHEMA, ValidationError, validate_tool_args
from kiro_crew.work_plan_vocab import next_pick, show_step

logger = logging.getLogger("kiro_crew.dashboard.handlers.work_ledger")


# ── conductor half ────────────────────────────────────────────────────────


async def api_work_ledger_get(request: web.Request) -> web.Response:
    """GET /api/work-ledger — the ledger this session owns.

    The conductor record, every item with all its fields, the derived
    ``orphaned`` / ``stale`` / ``acceptance_concrete`` flags, each item's newest
    events, and a ready-to-pipe ``accept_batch`` built from ``acceptance`` ALONE —
    never from a worker's claimed ``pr``, which is surfaced beside the item instead.

    ``accept_batch`` holds only the items whose bar is concrete, so an item still
    carrying a ``"TBD"`` pull request number is absent from it; ``acceptance_concrete``
    on the item row is why. Each entry carries the item's ``status`` so the conductor
    can apply its own "``done`` only" filter without a second lookup — the filter stays
    the conductor's to apply.

    With no query the answer is the whole board. Every parameter NARROWS it:
    ``events=<n>`` shortens each tail, ``item_id`` / ``state`` / ``since`` select
    rows (``accept_batch`` stays built from the WHOLE board, because its contract
    is the whole bar), and ``compact=true`` returns the status columns alone, the
    derived flags among them, with no events, acceptance or batch. The route does
    not size the reply: the MCP tool that carries it to a model fits it to that
    runtime's cut (``mcp_work._fit_ledger``), measured on the text it delivers.
    """
    key, refusal = await _caller_key(request, "work_ledger_read")
    if refusal is not None:
        return refusal
    assert key is not None
    options, bad = _read_options(request)
    if bad is not None:
        return bad
    assert options is not None
    published_only = _code_channel_caller(request)
    remote_on = await asyncio.to_thread(_remote_dispatch_on)
    if remote_on:
        # Remote dispatch (preview): pull each open remote item's peer copy and file
        # what is new BEFORE the snapshot below, outside its lock (the pull is network).
        from kiro_crew.dashboard.handlers import remote_ingest, remote_puller

        remote_puller.ensure_running(request.app["state"])
        await remote_ingest.ingest_board(request.app["state"], key)
    state: DashboardState = request.app["state"]
    # Single flight (see crew_board_flight). The tool behind this route gives up after
    # ten seconds and its caller asks again, so the same read can arrive while one is
    # being built. Reads of one board with the same arguments share ONE build, and only
    # a build that started after each of them arrived, so no answer is older than its
    # request; nothing is kept afterwards. The gate, the validation and the remote pull
    # above ran for this caller, and the channel re-check and the audit line below run
    # for it too, shared build or not.
    answer = await shared_board(
        _READ_FLIGHT + key,
        _shared_by(options, published_only, remote_on),
        lambda: _build_read(state, key, options, published_only, remote_on),
    )
    if answer.status != 200:
        return _json_bytes(answer)
    if why := _contained_channel_caller(request, request.headers.get("X-Session-Key", "")):
        # Same post-await re-check as ``work_brief``. This payload is larger: every
        # item's acceptance bar plus worker-authored prose for the whole fleet.
        _audit(
            request.headers.get("X-Session-Key", "") or "anonymous",
            "work_ledger_read",
            "denied",
            resources="channel_agent_block_post_read",
        )
        return _refuse_403(
            "channel_session",
            "This session stopped being a private surface while the ledger was being "
            f"read, so it is no longer one to return it to: {why}.",
        )
    _audit(key, "work_ledger_read", "ok", resources=f"{answer.items} item(s)")
    return _json_bytes(answer)


#: Names this route's builds in :mod:`crew_board_flight`, whose Crew board builds are
#: keyed by the bare ledger key: the two reads of one board never share an answer.
_READ_FLIGHT = "work-ledger-read:"


def _shared_by(options: dict[str, Any], published_only: bool, remote_on: bool) -> str:
    """The arguments of :func:`_build_read` as text: reads share a build when it is equal.

    The schema's options are text, integers and booleans. ``since`` is the one the route
    holds parsed (:func:`_read_options`), so it goes in as its ISO text, wall time and
    offset: two stamps give one text only when they are the same stamp. A value of any
    other type raises instead of being folded to a text that might not tell two apart.
    """
    return json.dumps(
        [options, published_only, remote_on], sort_keys=True, default=datetime.isoformat
    )


def _json_bytes(answer: BoardAnswer) -> web.Response:
    """*answer* as the response ``web.json_response`` gives for the same body."""
    return web.Response(
        body=answer.body, status=answer.status, content_type="application/json", charset="utf-8"
    )


async def _build_read(
    state: DashboardState, key: str, options: dict[str, Any], published_only: bool, remote_on: bool
) -> BoardAnswer:
    """One build of the read's answer: the snapshot under the board lock, then the rows.

    Takes the gateway state and plain values, never the request: reads with the same
    arguments may share the build (:func:`api_work_ledger_get`), so nothing in it may
    depend on which of them started it.
    """
    # A code channel's conductor reads the published-fields projection alone, so
    # there is no event tail to read for it.
    compact = published_only or bool(options.get("compact", False))
    tail = int(options.get("events", _MAX_EVENT_TAIL))
    # Under the board lock for the same reason the brief read is: a write's
    # cache commit and its record append are two steps, and a read between them
    # would publish a mutation the record may yet roll back. The header and the
    # items are read under one hold, so the response is one snapshot.
    async with _board_lock(key):
        record, lrefusal = await _own_ledger(key, "work_ledger_read")
        if lrefusal is not None:
            return answer_of(lrefusal)
        dirty = await _refuse_if_dirty(key, key, "work_ledger_read")
        if dirty is not None:
            return answer_of(dirty)
        # One executor hop for the whole snapshot (:func:`_snapshot`): this hold is
        # the one every write and the Crew board's build wait on, so it must not
        # grow with the board.
        items, shown, event_tails = await asyncio.to_thread(
            _snapshot, key, options, None if compact else tail
        )
    assert record is not None

    # Liveness is read straight off the dashboard's own slot table rather than
    # over HTTP: this handler runs in the process that owns it. ``orphaned`` asks
    # whether the CONDUCTOR's slot is still open and ``stale`` whether the
    # WORKER's is — the conjunction with the staleness window is what keeps a
    # worker in a thirty-minute build from being flagged. Derived for the compact
    # read too: a patrol that reads only the status columns must still see a dead
    # worker.
    conductor_alive = _slot_open(state, key)
    # One read for the whole board: an open item whose conductor holds no active
    # work-ledger watch has nobody reading its reports, and the flag puts that in
    # front of the conductor's next turn.
    patrolled = conductor_patrol.has_active_patrol(state, key)
    # Everything else the rows and the header read off disk, in one hop as well.
    side = await asyncio.to_thread(_side_reads, state, key, shown, remote_on, not published_only)
    rows: list[dict[str, Any]] = []
    for item in shown:
        row = _row_with_flags(
            state,
            item,
            conductor_alive=conductor_alive,
            patrolled=patrolled,
            report_undelivered=side.undelivered.get(item.item_id),
            board=key,
            remote_live=side.remote_live.get(item.item_id),
        )
        show_step(row, record.releases)
        if item.item_id in side.remote_rows:
            row.update(side.remote_rows[item.item_id])
        if remote_on and item.worker_session_key:
            row["runs_on"] = _runs_on(item, side.crew_names)
        if compact:
            compact_row = {name: row[name] for name in _COMPACT_ROW_FIELDS}
            if item.plan:
                # The board's plan is how a patrol picks what to pull next.
                compact_row["plan"] = row["plan"]
            if "runs_on" in row:
                compact_row["runs_on"] = row["runs_on"]
            if item.worker_instance_id or item.retired_bindings:
                from kiro_crew.dashboard.handlers.remote_ingest import (
                    REMOTE_ROW_FIELDS,
                    RETIRED_ROW_FIELDS,
                )

                compact_row.update(
                    {
                        name: row[name]
                        for name in REMOTE_ROW_FIELDS + RETIRED_ROW_FIELDS
                        if name in row
                    }
                )
            rows.append(compact_row)
            continue
        events = event_tails[item.item_id]
        row["events"] = [event.to_dict() for event in events]
        rows.append(row)

    if published_only:
        published = _published_payload(
            record, [(item, bool(row.get("stale"))) for item, row in zip(shown, rows)]
        )
        return answer_of(web.json_response(published), len(rows))
    # ``goal_items_used`` is derived, never stored: the goal's item count, read
    # off the header. The compact read carries it so a round report can state
    # the goal's total from a patrol that reads only the status columns.
    # ``max_depth`` is the conducting depth cap in force right now (never stored),
    # so a conductor can see how many levels may still dispatch below its own.
    # ``next_pick`` is derived, never stored: the top of the backlog by rank, over
    # the whole board (never the filtered rows), which is the item to dispatch
    # next when a seat frees (``work_plan_vocab.next_pick``).
    conductor = dict(
        record.to_dict(),
        goal_items_used=record.goal_items_used,
        max_depth=side.depth_cap,
        next_pick=next_pick(items),
    )
    payload: dict[str, Any] = {"conductor": conductor, "items": rows}
    if compact:
        payload["compact"] = True
    else:
        # The whole board, never the filtered rows.
        payload["accept_batch"] = work_ledger.accept_batch(items)
    return answer_of(web.json_response(payload), len(rows))


#: What a code channel's ``accept_eval`` answer may carry per item: which item, its
#: verdict, and why one was skipped. Never the bar, the evidence its check printed,
#: or the row facts beside it, since the answer is posted into the channel.
_CHANNEL_ANSWER_FIELDS: tuple[str, ...] = ("id", "verdict", "why")

#: Why a code channel's done item was not evaluated by the gateway.
_UNPLACED_WHY = (
    "its bar is read relative to a working directory (a pull request with no repo, "
    "or a relative file path); record the repo or an absolute path, then ask again"
)


def _bar_is_placed(accept: Any) -> bool:
    """Whether *accept* names everything it checks without a working directory."""
    if not isinstance(accept, dict):
        return True  # the evaluator's own error
    kind = accept.get("kind")
    if kind in ("pr_checks", "pr_merged"):
        return bool(accept.get("repo"))
    if kind == "file":
        path = accept.get("path")
        return not isinstance(path, str) or os.path.isabs(path)
    return True


async def api_work_ledger_accept_eval(request: web.Request) -> web.Response:
    """GET /api/work-ledger/accept-eval — a code channel's conductor's ``accept_eval``.

    That conductor reads published fields only (a derived ``state``, no ``status``
    and no bars), so an evaluation of its own read would call open work "already
    done". The gateway evaluates the stored bars instead, through the tool's own
    evaluator (``mcp_work._accept_eval``: the same selection, path screen and kinds),
    and answers with :data:`_CHANNEL_ANSWER_FIELDS` alone. ``item_id`` narrows it to
    one item. Any other caller evaluates its own full read and is refused here.
    """
    key, refusal = await _caller_key(request, "work_ledger_accept_eval")
    if refusal is not None:
        return refusal
    assert key is not None
    if not _code_channel_caller(request):
        return _refuse_403(
            "not_code_channel",
            "Only a Slack code channel's conductor is evaluated here; call accept_eval, "
            "which evaluates your own ledger read.",
        )
    item_id = request.query.get("item_id")
    if set(request.query) - {"item_id"} or (item_id is not None and not 0 < len(item_id) <= 64):
        return _refuse_400(work_ledger.CODE_INVALID_VALUE, "expected only item_id", "item_id")
    async with _board_lock(key):
        _record, lrefusal = await _own_ledger(key, "work_ledger_accept_eval")
        if lrefusal is not None:
            return lrefusal
        dirty = await _refuse_if_dirty(key, key, "work_ledger_accept_eval")
        if dirty is not None:
            return dirty
        items = await asyncio.to_thread(work_ledger.list_work_items, key)
    from kiro_crew import mcp_work

    batch = work_ledger.accept_batch(items)
    # The gateway's working directory is not the channel's worktree, so a done
    # item whose bar leans on one (a PR with no repo, a relative file path) is
    # skipped with that reason rather than judged against the wrong directory.
    placed = [entry for entry in batch["items"] if _bar_is_placed(entry.get("accept"))]
    unplaced = [
        entry["id"]
        for entry in batch["items"]
        if entry not in placed and entry.get("status") == "done"
    ]
    board = {
        "items": [item.to_dict() for item in items if item.item_id not in unplaced],
        "accept_batch": {"items": placed},
    }
    args = {} if item_id is None else {"item_id": item_id}
    if item_id is not None and item_id in unplaced:
        document: dict[str, Any] = {"results": []}
    else:
        # Off the loop: the evaluator runs ``gh`` and stats files.
        document = json.loads(await asyncio.to_thread(mcp_work._accept_eval, args, board))
    document.setdefault("skipped", []).extend(
        {"id": unplaced_id, "why": _UNPLACED_WHY}
        for unplaced_id in unplaced
        if item_id is None or unplaced_id == item_id
    )
    if why := _contained_channel_caller(request, request.headers.get("X-Session-Key", "")):
        _audit(key, "work_ledger_accept_eval", "denied", resources="channel_agent_block_post_read")
        return _refuse_403(
            "channel_session",
            f"This session's audience changed while its ledger was being evaluated: {why}.",
        )
    answer: dict[str, Any] = {
        name: [
            {field: row[field] for field in _CHANNEL_ANSWER_FIELDS if field in row}
            for row in document.get(name) or []
            if isinstance(row, dict)
        ]
        for name in ("results", "skipped")
    }
    if "error" in document:
        answer["error"] = "the evaluator could not read the ledger's bars"
    for name in ("truncated", "omitted"):
        if name in document:
            answer[name] = document[name]
    answer["published_fields_only"] = True
    _audit(key, "work_ledger_accept_eval", "ok", resources=f"{len(answer['results'])} result(s)")
    return web.json_response(answer)


def _row_with_flags(
    state: DashboardState,
    item: work_ledger.WorkItem,
    *,
    conductor_alive: bool,
    patrolled: bool,
    report_undelivered: dict[str, str] | None = None,
    board: str = "",
    remote_live: tuple[bool, bool] | None = None,
) -> dict[str, Any]:
    """One item as a read row, with the flags the store derives at read time.

    A function rather than four lines inlined per caller because the read route and
    the wake snapshot both publish these flags and must publish the SAME ones: a
    snapshot that said a worker was alive while the route called it stale would be a
    second opinion on the one question a conductor acts on.

    ``report_undelivered`` is ``{"tool", "at"}`` while the worker's last
    ``work_report`` / ``work_brief`` call failed before reaching the ledger (the
    harness could not resolve it) and none has got through since, else ``None``
    (:func:`kiro_crew.work_ledger.read_undelivered`, read by the caller
    off the event loop). It is what tells "couldn't send its report" apart from a
    worker that went quiet.

    Liveness is read straight off the dashboard's own slot table rather than over
    HTTP: every caller runs in the process that owns it. ``orphaned`` asks whether
    the CONDUCTOR's slot is still open and ``stale`` whether the WORKER's is -- the
    conjunction with the staleness window is what keeps a worker in a thirty-minute
    build from being flagged.

    ``acceptance_concrete`` says why an item is (or is not) in ``accept_batch``.
    Without it a conductor sees an item it dispatched simply missing from the batch
    and has no way to tell "bar not filled in yet" from "the read dropped it".

    ``unpatrolled`` marks an open item whose conductor holds no active work-ledger
    watch -- nobody is reading its reports -- and ``patrolled`` is that one read for
    the whole board, passed in rather than re-read per item.

    ``pending_overdue`` marks an item whose acceptance has read ``pending`` past the
    budget, so the conductor stops polling it and asks the user.

    ``messages_unread`` is ``{"count", "oldest_secs"}`` while messages *board* sent
    the worker are still queued unread (``queue_age.unread_row``), else ``None``.
    """
    row = item.to_dict()
    row["orphaned"] = work_ledger.is_orphaned(item, conductor_slot_exists=conductor_alive)
    running, closed, looping, held = work_ledger.item_liveness(
        board,
        item,
        running=lambda k: _slot_running(state, k),
        closed=lambda k: _slot_closed(state, k),
        looping=_slot_looping,
        held=_slot_loop_held,
        remote=remote_live if item.worker_instance_id else None,
    )
    row["stale"] = work_ledger.is_stale(
        item,
        worker_running=running,
        worker_closed=closed,
        worker_looping=looping,
        worker_loop_held=held,
        worker_idle_since=_item_idle_since(state, item),
    )
    row["acceptance_concrete"] = work_ledger.is_acceptance_concrete(item.acceptance)
    row["unpatrolled"] = item.state == "open" and not patrolled
    row["pending_overdue"] = work_ledger.is_pending_overdue(item)
    row["report_undelivered"] = report_undelivered
    # What this board's conductor sent the worker that still sits in its queue, so a
    # patrol sees "my correction has waited 40 minutes" instead of a quiet worker.
    # A remote worker's queue is on its peer, so it reads as nothing here.
    row["messages_unread"] = (
        None
        if item.worker_instance_id
        else queue_age.unread_row(_find_slot(state, item.worker_session_key or ""), board)
    )
    return row


#: Why a code channel's conductor reads less than a dashboard conductor does.
_PUBLISHED_NOTE = (
    "This session is a Slack code channel, and what it reads is posted there, so "
    "the ledger answers with the published fields only: the first line of the goal, "
    "and each item's title, state, the first line of its worker's report, its "
    "branch, its PR and CI state. Acceptance bars, decisions and event tails stay "
    "in the dashboard."
)


def _published_payload(
    record: work_ledger.ConductorRecord | None,
    items: list[tuple[work_ledger.WorkItem, bool]],
) -> dict[str, Any]:
    """A ledger read (or a record reply) as a code channel's conductor may see it.

    Built from ``session_control.published_item_fields`` and nothing else, so the
    rule for what reaches a code channel lives in ONE place. The conductor header
    keeps its counters and the goal's first line (``session_control.published_goal``).
    """
    conductor: dict[str, Any] = {}
    if record is not None:
        conductor = {
            "round": record.round,
            "depth": record.depth,
            "goal_items_used": record.goal_items_used,
            "goal": session_control.published_goal(record.goal),
        }
    return {
        "conductor": conductor,
        "items": [
            session_control.published_item_fields(item, stale=stale) for item, stale in items
        ],
        "compact": True,
        "published_fields_only": True,
        "note": _PUBLISHED_NOTE,
    }


#: The fields a ``compact=true`` row carries: what a patrol cycle reads to decide
#: who moves next, the derived flags included, and nothing that is a
#: document in its own right (``acceptance``, ``artifacts``, the events).
_COMPACT_ROW_FIELDS: tuple[str, ...] = (
    "item_id",
    "title",
    "state",
    "created_at",
    "status",
    "reason",
    "summary",
    "decision",
    "verdict",
    "pr",
    "worker_session_key",
    "last_report_at",
    "orphaned",
    "stale",
    "acceptance_concrete",
    "unpatrolled",
    "pending_overdue",
    "report_undelivered",
    "messages_unread",
)

#: Query-string spellings of ``compact``. ``true`` / ``false`` is what the tool
#: layer sends.
_QUERY_TRUE = frozenset({"1", "true", "yes", "on"})
_QUERY_FALSE = frozenset({"0", "false", "no", "off"})


def _read_options(
    request: web.Request,
) -> tuple[dict[str, Any], None] | tuple[None, web.Response]:
    """The read's query parameters, validated against ``WORK_LEDGER_READ_SCHEMA``.

    A query string carries only text, so ``events`` and ``compact`` are coerced to
    the schema's types first; the rest is the schema's own check, the same one the
    tool layer applies. An unknown or empty parameter is refused rather than
    ignored: a filter that silently did not apply would hand back the whole board
    as if it were the narrowed one.
    """
    raw: dict[str, Any] = {}
    for name in request.query:
        value = request.query.get(name, "")
        if value == "":
            return None, _refuse_400(
                work_ledger.CODE_INVALID_VALUE, f"{name}: expected a value", name
            )
        if name == "events":
            try:
                raw[name] = int(value)
            except ValueError:
                return None, _refuse_400(
                    work_ledger.CODE_INVALID_VALUE, "events: expected an integer", "events"
                )
        elif name == "compact":
            lowered = value.strip().lower()
            if lowered not in _QUERY_TRUE | _QUERY_FALSE:
                return None, _refuse_400(
                    work_ledger.CODE_INVALID_VALUE, "compact: expected true or false", "compact"
                )
            raw[name] = lowered in _QUERY_TRUE
        else:
            raw[name] = value
    try:
        cleaned = validate_tool_args(raw, WORK_LEDGER_READ_SCHEMA)
    except ValidationError as exc:
        # An unknown key is a wrong VALUE for the query; the store-code mapping
        # reads the field name and would call a stray ``status=`` an
        # ``invalid_status``.
        unknown = "unknown field" in (exc.message or "")
        code = work_ledger.CODE_INVALID_VALUE if unknown else _validation_code(exc)
        return None, _refuse_400(code, str(exc), exc.field)
    since = cleaned.get("since")
    if since is not None:
        moment = work_ledger.parse_stamp(since)
        if moment is None:
            return None, _refuse_400(
                work_ledger.CODE_INVALID_VALUE, "since: expected an ISO-8601 timestamp", "since"
            )
        cleaned["since"] = moment
    return cleaned, None


def _passes_filters(item: work_ledger.WorkItem, options: dict[str, Any]) -> bool:
    """Whether *item* survives the read's ``item_id`` / ``state`` / ``since`` filters.

    ``since`` is judged on the newest of ``created_at``, ``last_report_at`` and
    ``closed_at``, so it answers "what moved since my last cycle" from the item's
    own fields, without reading any event log.
    """
    item_id = options.get("item_id")
    if item_id is not None and item.item_id != item_id:
        return False
    state = options.get("state")
    if state is not None and item.state != state:
        return False
    since = options.get("since")
    if since is not None:
        stamps = [
            parsed
            for parsed in (
                work_ledger.parse_stamp(value)
                for value in (item.created_at, item.last_report_at, item.closed_at)
                if value
            )
            if parsed is not None
        ]
        if not stamps or max(stamps) < since:
            return False
    return True


#: Events returned per item. The log is append-only and capped at 200 per item,
#: so an unbounded slice would eventually be the largest thing in a conductor's
#: context — the opposite of what a patrol cycle needs. Newest kept, oldest
#: dropped, which is the slice ``read_events``' own ``limit`` already applies.
_MAX_EVENT_TAIL = 20


def _remote_dispatch_on() -> bool:
    """``work_ledger.remote_dispatch``, read on every call. Blocking; fails closed."""
    from kiro_crew.remote_dispatch import remote_dispatch_enabled

    return remote_dispatch_enabled()


#: ``runs_on`` for a worker on this machine.
RUNS_ON_LOCAL = "local"


def _crew_names(state: DashboardState) -> dict[str, str]:
    """Each registered crew's display name by instance id. BLOCKING; never raises."""
    try:
        from kiro_crew.dashboard.handlers_instances_gate import _registry

        return {
            str(getattr(row, "id", "")): str(getattr(row, "name", "") or "")
            for row in _registry(state).list()
        }
    except Exception:  # noqa: BLE001 - a name is a label; the read must not fail on it
        logger.debug("crew registry unreadable for runs_on", exc_info=True)
        return {}


def _runs_on(item: work_ledger.WorkItem, crew_names: dict[str, str]) -> str:
    """Which machine a bound item's worker runs on: a crew's name, else this one."""
    crew = str(item.worker_instance_id or "")
    if not crew:
        return RUNS_ON_LOCAL
    return crew_names.get(crew) or crew


def _remote_row_fields(key: str, item: work_ledger.WorkItem) -> dict[str, Any]:
    from kiro_crew.dashboard.handlers.remote_ingest import remote_row_fields

    return remote_row_fields(key, item)


def _tail_events(key: str, item_id: str, limit: int) -> list[work_ledger.WorkEvent]:
    """The newest *limit* events for one item. ``limit`` is keyword-only downstream."""
    return work_ledger.read_events(key, item_id, limit=limit)


def _snapshot(key: str, options: dict[str, Any], tail: int | None) -> tuple[
    list[work_ledger.WorkItem],
    list[work_ledger.WorkItem],
    dict[str, list[work_ledger.WorkEvent]],
]:
    """The board's items, the ones this read shows, and each shown item's event tail.

    BLOCKING, and the whole of what the read takes under the board lock. It is one
    executor hop however large the board is: the handler waits for each hop on the
    gateway's loop, so a hop per item made the read as slow as the board is large
    times the loop is busy. *tail* is ``None`` for a read that carries no events.
    """
    items = work_ledger.list_work_items(key)
    shown = [item for item in items if _passes_filters(item, options)]
    if tail is None:
        return items, shown, {}
    return items, shown, {item.item_id: _tail_events(key, item.item_id, tail) for item in shown}


class _SideReads(NamedTuple):
    """What a read's rows and header take off disk beside the board's snapshot."""

    undelivered: dict[str, dict[str, str] | None]
    remote_live: dict[str, tuple[bool, bool]]
    #: The extra row fields of each item that is, or was, bound to a remote worker.
    remote_rows: dict[str, dict[str, Any]]
    crew_names: dict[str, str]
    #: The conducting depth cap in force; ``0`` when the read does not carry it.
    depth_cap: int


def _side_reads(
    state: DashboardState,
    key: str,
    shown: list[work_ledger.WorkItem],
    remote_on: bool,
    with_depth_cap: bool,
) -> _SideReads:
    """Every file read the rows of *shown* and the header need, in one go. BLOCKING.

    One executor hop for the same reason as :func:`_snapshot`. The crew registry is
    read only when a row names a crew, and the depth cap (it consults the config
    file) only for a read that answers with it.
    """
    names_a_crew = remote_on and any(
        item.worker_session_key and item.worker_instance_id for item in shown
    )
    return _SideReads(
        undelivered={item.item_id: work_ledger.read_undelivered(key, item) for item in shown},
        remote_live=work_ledger.remote_liveness_map(key, shown),
        remote_rows={
            item.item_id: _remote_row_fields(key, item)
            for item in shown
            if item.worker_instance_id or item.retired_bindings
        },
        crew_names=_crew_names(state) if names_a_crew else {},
        depth_cap=work_ledger.max_depth() if with_depth_cap else 0,
    )


def _slot_running(state: DashboardState, key: str) -> bool:
    """Whether *key*'s slot has a TURN IN FLIGHT — not merely an open tab.

    ``stale`` is the conjunction "quiet past the window AND not running AND the
    worker's last word still left the move with it", and the running half means the
    worker is doing something (a thirty-minute build), not that its session exists. An idle worker whose tab is still open but which
    stopped without reporting is exactly the case the flag exists to surface, and
    testing slot EXISTENCE here would never flag it. ``orphaned`` keeps the
    existence test, because a conductor's absence is what that flag means.
    """
    slot = _find_slot(state, key)
    return bool(getattr(slot, "running", False)) if slot is not None else False


def _slot_closed(state: DashboardState, key: str) -> bool:
    """Whether *key*'s slot is GONE, read the way the work-ledger wake gate reads it.

    ``is_stale`` skips its window for a worker that reported and then closed, and the
    badge must agree with the gate that wakes the conductor: one resolver,
    :func:`ledger_wake.worker_closed`, answers both. It counts a slot being built, an
    unrestored key and any key during a restore as open, so the badge cannot report a
    live worker as gone, and anything unreadable answers False.
    """
    return ledger_wake.worker_closed(state, key)


def _slot_looping(key: str) -> bool:
    """Whether *key*'s session holds an active loop, read the way the wake gate reads it.

    One resolver, :func:`ledger_wake.worker_looping`, answers both the badge and the
    stall wake, so a worker waiting inside its own monitor loop reads the same on
    both. No auto-nudge service, or anything unreadable, answers False.
    """
    from kiro_crew.autonudge import get_instance

    return ledger_wake.worker_looping(get_instance(), key)


def _slot_loop_held(key: str) -> bool:
    """Whether *key*'s session holds an active loop that cannot fire, as the wake gate reads it."""
    from kiro_crew.autonudge import get_instance

    return ledger_wake.worker_loop_held(get_instance(), key)


def _item_idle_since(state: DashboardState, item: work_ledger.WorkItem) -> float | None:
    """When *item*'s local worker went idle with nothing to wake it, as the idle wake reads it.

    One resolver, :func:`slot_retention.worker_idle_since`, answers both the Board's
    Quiet reading and the ``worker_idle`` wake, so the two cannot disagree. A remote
    worker's slot is on its peer, so it answers None and keeps the window.
    """
    if item.worker_instance_id or not item.worker_session_key:
        return None
    from kiro_crew.dashboard import slot_retention

    return slot_retention.worker_idle_since(state, item.worker_session_key)


def worker_holds_open_item(slot_key: str) -> bool:
    """Whether *slot_key* is a worker bound to an OPEN item that names it back. BLOCKING.

    For the slot-retention paths (the open-tab restore budget and the idle
    sweep), which must not leave a worker without its tab: the wake gate reads a
    worker with no slot as closed. The key is the slot's own name, resolved by
    the server, never by a caller. Both spellings are asked, because a binding
    is filed under the ledger's folded key. Any read that fails answers True: an
    unknown binding must not cost a worker its tab.
    """
    try:
        for candidate in dict.fromkeys((slot_key, session_ledger.ledger_key(slot_key))):
            binding = work_ledger.read_binding(candidate, strict=True)
            if binding is None:
                continue
            item = work_ledger.read_work_item(*binding, strict=True)
            if item is not None and not item.is_terminal and item.worker_session_key == candidate:
                return True
    except Exception:  # noqa: BLE001 - unreadable binding means keep the tab
        logger.debug("work-ledger binding read failed for %s", slot_key, exc_info=True)
        return True
    return False


def _find_slot(state: DashboardState, key: str):
    if not key:
        return None
    for candidate in (key, f"dashboard_{key}"):
        try:
            slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover - a slot-table read must not fail a read
            logger.debug("slot lookup failed for %s", candidate, exc_info=True)
            continue
        if slot is not None:
            return slot
    return None


def _slot_open(state: DashboardState, key: str) -> bool:
    """Whether *key* still names an open slot. A blank key is never alive."""
    if not key:
        return False
    try:
        return state.get_slot(key) is not None or state.get_slot(f"dashboard_{key}") is not None
    except Exception:  # pragma: no cover - a slot-table read must not fail a read
        logger.debug("slot liveness read failed for %s", key, exc_info=True)
        return False
