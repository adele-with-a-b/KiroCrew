"""``crew=`` on the five session tools: the MCP half (rfc-conductor-remote-dispatch.md, wave 1).

The gateway decides everything (switch, caller, crew, target fence, redaction) in
``dashboard/handlers/remote_dispatch_routes.py``; the bodies here only carry the
call on the same ``/api/session-control/*`` route the local verb uses and render
the answer. :func:`with_crew_schema` adds the ``crew`` argument to the advertised
schemas while ``work_ledger.remote_dispatch`` is on, and leaves them untouched
while it is off.
"""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import quote

from kiro_crew import steer_refusal
from kiro_crew.mcp_tools.dashboard_client import DashboardError
from kiro_crew.mcp_tools.table import ToolContext
from kiro_crew.platform import redact_via_context as redact

#: The tools that take ``crew``.
CREW_TOOLS = (
    "session_create",
    "session_send",
    "session_read_message",
    "session_stop",
    "session_close",
)

#: The goal-conductor skill keeps its remote-dispatch procedure out of SKILL.md, so
#: a conductor learns of it only from these descriptions, which exist only while
#: the preview is on.
_READ_FIRST = (
    "Before your first crew or work_item call, read references/remote-dispatch.md "
    "in the goal-conductor skill's directory and follow it."
)

_CREW_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "Preview. Run this on a connected remote crew instead of this machine: the "
        "crew's instance id or exact name. The session lives on that crew and shows "
        "in its sidebar group. With crew, session_create takes only title, agent, "
        'agent_kind ("template" only) and model (omit agent to get the crew\'s own '
        "default), and send, read, stop and "
        "close reach only a crew session YOU opened with session_create(crew=) on "
        "that same crew; pass the key it returned as target. " + _READ_FIRST
    ),
}


_WORK_ITEM_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "Preview. One of YOUR open, unbound work items (it_<8 hex>): the crew "
        "session is created already bound to it, so skip work_ledger_record bind. "
        "Omit crew and the hub picks the connected crew with the most headroom; when "
        "none has room or none answers, the item stays unbound and you run it on "
        "this machine. Its reports reach your next work_ledger_read, and a decide reaches "
        "the worker. " + _READ_FIRST
    ),
}


#: What ``session_status`` says about its crew rows, only while the preview is on.
_STATUS_NOTE = (
    " Preview: below those, the sessions you opened on OTHER crews with "
    "session_create(crew=), each with its crew and its state there: `working`, "
    "`idle`, `needs_approval`, `asking`, `closing` (you closed it while that crew "
    "gave no answer; the hub holds the close and makes it again by itself, so wait), "
    "`closed` (that crew no longer holds it; session_close it once with crew to drop "
    "the row) or `unknown` (that crew's list was not read). A row that says the "
    "crew is not answering shows the last state read. The list is this hub's own "
    "record and comes in any turn; the state is read only in a turn that may use "
    "crew=, and the answer says when it was not. Pass crew and the row's key as "
    "target to the other session tools."
)

#: How each crew row's state reads in the ``session_status`` answer.
_CREW_STATUS_MARKS = {
    "working": "\U0001f503",
    "needs_approval": "✋",
    "asking": "❓",
    "closing": "⏳",
    "closed": "✅",
    "unknown": "❓",
}

_CREW_STATUS_NOTES = {
    "needs_approval": " (parked on a tool approval a person must answer on that crew)",
    "asking": " (a question card is open on that crew)",
    "closing": " (the hub holds your close and makes it again once that crew answers: wait)",
    "closed": " (that crew no longer holds it; session_close it with crew to drop this row)",
    "unknown": " (that crew's session list could not be read)",
}


def crew_status_lines(resp: dict[str, Any]) -> list[str]:
    """The ``session_status`` lines for the caller's sessions on other crews, if any."""
    rows = [r for r in resp.get("crew_sessions") or [] if isinstance(r, dict)]
    if not rows:
        return []
    unread = resp.get("crew_state") == "owner_turn_only"
    lines = [f"\U0001f310 {len(rows)} session(s) you opened on other crews:"]
    for row in rows:
        status = str(row.get("status") or "unknown")
        mark = _CREW_STATUS_MARKS.get(status, "\U0001f4a4")
        note = _CREW_STATUS_NOTES.get(status, "")
        if unread and status == "unknown":
            note = " (state not read in this turn)"
        elif row.get("crew_answered") is False and status not in ("closing", "unknown"):
            note += " (last state read; that crew is not answering now)"
        lines.append(
            f"  {mark} `{row.get('target', '')}` {_on(row)} (crew=`{row.get('crew', '')}`) — "
            f"{status}{note}"
        )
    if unread:
        lines.append(
            "This turn may not read another crew, so no crew was asked: the list is "
            "this hub's own record of what you opened and have not closed. Their "
            "state is read in a turn the owner sent, or one the owner's patrol "
            "settings admit."
        )
    return lines


def with_crew_schema(tools: list[dict[str, Any]], enabled: bool) -> list[dict[str, Any]]:
    """*tools* with ``crew`` added to the five schemas when *enabled*, else unchanged.

    ``session_status`` takes no ``crew``; its description gains the crew rows' note.
    """
    if not enabled:
        return tools
    out = []
    for tool in tools:
        if tool.get("name") == "session_status":
            tool = copy.deepcopy(tool)
            tool["description"] = str(tool.get("description") or "") + _STATUS_NOTE
        if tool.get("name") in CREW_TOOLS:
            tool = copy.deepcopy(tool)
            props = tool.setdefault("inputSchema", {}).setdefault("properties", {})
            props["crew"] = dict(_CREW_PROPERTY)
            if tool.get("name") == "session_create":
                props["work_item"] = dict(_WORK_ITEM_PROPERTY)
        out.append(tool)
    return out


#: Refusals of a work-item create that mean the crew is full or did not answer.
#: The item is still open and unbound, so it can run here.
_RUN_HERE_CODES = frozenset(
    {"crew_busy", "crew_none_available", "proxy_peer_not_connected", "crew_version_unknown"}
)


def _run_it_here(refused: DashboardError, args: dict[str, Any]) -> str:
    """The next step after a work-item create no crew took, else ``""``."""
    if not args.get("work_item") or refused.code not in _RUN_HERE_CODES:
        return ""
    return (
        f". {args['work_item']} is still open and unbound. "
        "Run it on this machine now: session_create without crew or work_item, "
        "work_ledger_record action=bind, then session_send the seed."
    )


def _on(resp: dict[str, Any]) -> str:
    name = resp.get("crew_name") or resp.get("crew") or ""
    return f"on crew `{name}`"


def run_crew_create(args: dict[str, Any], ctx: ToolContext) -> str:
    payload = {
        k: args[k]
        for k in ("crew", "title", "agent", "agent_kind", "model", "work_item")
        if k in args
    }
    extra = sorted(k for k in args if k not in payload and args.get(k) not in (None, ""))
    if extra:
        return f"Error: session_create with crew does not take {', '.join(extra)}."
    try:
        resp = ctx.client.post("/api/session-control/create", payload, session_key=ctx.caller_key)
    except DashboardError as refused:
        return redact(
            f"Error: could not create a session on that crew: {refused.error}"
            f"{_run_it_here(refused, args)}"
        )
    title = f" ({resp['title']})" if resp.get("title") else ""
    agent = f" bound to `{resp['agent']}`" if resp.get("agent") else ""
    note = f" The title was not set: {resp['title_error']}." if resp.get("title_error") else ""
    if resp.get("work_item_id"):
        note += f" It is bound to {resp['work_item_id']}; do not bind it again."
    if resp.get("decision_error"):
        note += (
            f" Your latest decision did not reach it ({resp['decision_error']}); "
            "record the decide again."
        )
    if resp.get("placed"):
        # Names can repeat across crews and an exact id wins over a name, so the
        # follow-up must carry the id the hub chose, not the name shown above.
        note += (
            f" The hub picked this crew by headroom; pass crew=`{resp.get('crew')}` "
            "(its id) on every follow-up call."
        )
    if "previous_branch" in resp:
        # Wave 8: a redispatch of a retired item. Two workers never share a branch.
        last = resp.get("previous_branch")
        start = f"start from `{last}` and " if last else ""
        note += (
            f" This item was retired from another crew: seed the worker to {start}"
            "push a NEW branch name."
        )
    return redact(
        f"\U0001f195 Opened `{resp.get('target')}`{title} {_on(resp)}{agent}. It is empty "
        f"and waiting in that crew's sidebar group.{note} Pass crew and this key as "
        "target to session_send, session_read_message, session_stop and session_close."
    )


def run_crew_send(args: dict[str, Any], ctx: ToolContext) -> str:
    payload: dict[str, Any] = {
        "crew": args["crew"],
        "target": args["target"],
        "message": args["message"],
    }
    # A crew send reaches only a session the caller opened there, so it is the
    # caller's own worker: it cuts in unless the caller asks for the queue.
    steer = args.get("steer") is not False
    asked = args.get("steer") is True
    if steer:
        payload["steer"] = True
    try:
        resp = ctx.client.post("/api/session-control/send", payload, session_key=ctx.caller_key)
    except DashboardError as refused:
        return redact(f"Error: could not send to that crew session: {refused.error}")
    why = ""
    if resp.get("steered") is True:
        how = "steered into its running turn"
        if resp.get("held") is True:
            why = f" {steer_refusal.held_note()}"
        why += steer_refusal.carried_note(resp.get("carried"))
        if not asked:
            why += (
                " It is your own session, so a message to it cuts in unless you pass steer=false."
            )
    elif resp.get("started") is True:
        # It was idle, so there was no turn to steer: it started one on the message.
        how = "delivered: it was idle and started a turn on it"
    elif steer and resp.get("heldForPerson") is True:
        how = "queued, not steered"
        why = " A person is driving that turn, so your message runs when that turn ends."
    elif asked or resp.get("steerRefusal"):
        # An omitted steer the crew queued with no refusal was never tried (an
        # idle worker held behind sub-agents, fair admission, a paused crew).
        how = "queued, not steered"
        why = f" {steer_refusal.describe(resp.get('steerRefusal'))}"
        why += steer_refusal.carried_note(resp.get("carried"))
    else:
        how = "queued: it runs when its current turn ends"
    return redact(f"\U0001f4e8 Message {how} to `{resp.get('target')}` {_on(resp)}.{why}")


def run_crew_read(args: dict[str, Any], ctx: ToolContext) -> str:
    if args.get("since") is not None:
        return "Error: session_read_message with crew does not take since."
    query = (
        f"target={quote(str(args['target']))}&crew={quote(str(args['crew']))}"
        f"&limit={int(args.get('limit', 20))}"
    )
    try:
        resp = ctx.client.get(f"/api/session-control/read?{query}", session_key=ctx.caller_key)
    except DashboardError as refused:
        return redact(f"Error: could not read that crew session: {refused.error}")
    state = "still working" if resp.get("running") else "idle"
    lines = [
        f"\U0001f4d6 `{resp.get('target', '')}` {_on(resp)} — {resp.get('title', '')} ({state})"
    ]
    rows = [r for r in resp.get("messages") or [] if isinstance(r, dict)]
    if not rows:
        lines.append("No messages yet.")
    for row in rows:
        lines.append(f"[{row.get('role') or '?'}] {row.get('content') or ''}")
    return redact("\n".join(lines))


def run_crew_stop(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/stop",
            {"crew": args["crew"], "target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: could not stop that crew session: {refused.error}")
    if resp.get("compacting") is True:
        return redact(
            f"\u2139\ufe0f `{resp.get('target')}` {_on(resp)} is compacting its context, so "
            "the crew did not stop it; send session_stop again to force the stop."
        )
    return redact(f"\U0001f6d1 Stop sent to `{resp.get('target')}` {_on(resp)}.")


def run_crew_close(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/close",
            {"crew": args["crew"], "target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: could not close that crew session: {refused.error}")
    if resp.get("kept") is True:
        return redact(
            f"⏳ NOT closed yet: `{resp.get('target')}` {_on(resp)} is still open "
            f"there ({resp.get('why') or 'that crew gave no answer'}). The hub kept this "
            "close and makes it again by itself once that crew answers; session_status "
            "lists the chat as `closing` until it is gone. Do not report it closed, and "
            "do not send the close again."
        )
    return redact(
        f"\U0001f5d1\ufe0f Closed `{resp.get('target')}` {_on(resp)}; that crew archives it "
        "to its own history."
    )
