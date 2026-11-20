"""POST /api/chat/slots/{slot}/attach-conversation -- continue an existing Claude conversation.

A conversation started in a terminal ``claude`` lives in
``~/.claude/projects/<folder>/<session id>.jsonl``. claude-agent-acp can load it by id
(``session/load``), and the turns a Crew chat adds are appended to the same file, so a
later ``claude --resume <id>`` in the terminal sees them. What Crew lacks is a way to
say which conversation a chat continues: this records it on a NEW chat, through the
same live-map join session transfer uses (``seed_conversation``), with the folder the
conversation ran in.

Refused unless the caller is the dashboard owner (never an app token), the chat exists, it has no conversation of its
own yet, it runs on Claude Code, the id is a UUID, and exactly one transcript with that id exists. The caller is
expected to close the terminal ``claude`` first (two writers on one transcript
interleave) and to set the chat's project folder to the returned ``cwd`` before the
first message: the adapter runs a loaded conversation in the folder it is given.

adele-with-a-b fork only.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, PROVIDER_LABEL_CLAUDE
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.handlers._shared_access import require_owner_dashboard_request
from kiro_crew.dashboard.handlers._shared_request_body import read_bounded_json
from kiro_crew.dashboard.session_transfer_layers import _slot_starts_on
from kiro_crew.dashboard.slot_ownership import slot_not_found

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import DashboardState

#: How far into a transcript to look for the folder it ran in.
_CWD_SCAN_LINES = 200


def _claude_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(base) / "projects"


def find_transcript(sid: str) -> tuple[Path | None, str]:
    """The one transcript named *sid* and the cwd it records, or (None, reason)."""
    matches = [p for p in _claude_projects_dir().glob(f"*/{sid}.jsonl") if p.is_file()]
    if not matches:
        return None, "no Claude conversation with that id"
    if len(matches) > 1:
        return None, "more than one Claude conversation with that id"
    path = matches[0]
    with path.open(encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= _CWD_SCAN_LINES:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            cwd = row.get("cwd") if isinstance(row, dict) else None
            if isinstance(cwd, str) and os.path.isabs(cwd):
                return path, cwd
    return None, "the conversation records no folder"


async def api_chat_slot_attach_conversation(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    # It reads the owner's own Claude transcripts, so the owner only: never an app token,
    # even one that owns the slot.
    if request.get("app", ""):
        return slot_not_found()
    denied = await require_owner_dashboard_request(request, "chat.slot_attach_conversation")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    raw = body.get("session_id")
    try:
        sid = str(uuid.UUID(str(raw)))
    except ValueError:
        return web.json_response({"error": "session_id must be a UUID"}, status=400)
    if str(raw).lower() != sid:
        return web.json_response({"error": "session_id must be a UUID"}, status=400)
    path, cwd_or_reason = find_transcript(sid)
    if path is None:
        return web.json_response({"error": cwd_or_reason}, status=404)
    if not await asyncio.to_thread(
        _slot_starts_on, effective_session_key(slot), ACP_BACKEND_CLAUDE
    ):
        return web.json_response({"error": "this chat does not run on Claude Code"}, status=409)
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return web.json_response({"error": "no live session manager"}, status=503)
    async with slot._lock:
        if state._slots.get(name) is not slot:
            return slot_not_found()
        key = effective_session_key(slot)
        if sessions.mapped_sid(key):
            return web.json_response(
                {"error": "this chat already has a conversation; attach to a new chat"},
                status=409,
            )
        sessions.seed_conversation(key, sid, provider=PROVIDER_LABEL_CLAUDE, cwd=cwd_or_reason)
    return web.json_response(
        {"slot": name, "session_id": sid, "cwd": cwd_or_reason, "transcript": str(path)}
    )
