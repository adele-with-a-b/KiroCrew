"""The restart receipt: what one gateway start kept, cut and held, chat by chat.

The startup restore builds it in three steps:

* :func:`begin`, before the open tabs are rebuilt, starts this boot's receipt;
* the adopt pass (``chat_keeper_adopt``) reports each live keeper's outcome:
  :func:`note_kept` for a chat it attached, :func:`note_left` for one another start
  of the chat holds, :func:`note_ended` for a keeper it ended;
* :func:`finish`, once the restore and the parked-send drain are done, adds the chats
  whose running turn was cut and the restored queue cards that wait for a person,
  appends the whole receipt as one JSON line to ``<data home>/logs/`` (:data:`RECEIPTS_FILE`),
  and logs its one-line summary.

A kept chat whose turn was running (the keeper saw its prompt unanswered at the
detach, or the restore owes it its "interrupted" row), or whose approval waited
through the gap, gets one ``notice`` row in its transcript, appended before its
continuing turn is read. The row states only what the keeper measured: how long no gateway was attached
(Crew's own tools could not answer in that time) and, when a permission request
was open, how many waited and the longest wait. A chat whose turn was cut already
gets its "interrupted" row (``slot_persistence.turn_marker``), and a held queue card
already shows in its chat, so neither gets a second row here.

Process state for this gateway's one restore, like ``chat_keeper_adopt``'s kept set.
Every function here is best effort: a receipt must never fail a boot or an adopt.
Design: ``docs/system-specs/modules/runtime-ownership.md``, "The restart receipt".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("kiro_crew.dashboard.restart_receipt")

#: The receipts of past starts, one JSON line each, under ``<data home>/logs/``.
RECEIPTS_FILE = "restart-receipts.jsonl"
#: One receipt is well under a few KB; the live file plus its ``.1`` generation
#: hold hundreds of starts.
RECEIPTS_MAX_BYTES = 1024 * 1024
#: The record's shape version.
RECEIPT_VERSION = 1
#: The transcript row's ``meta["kind"]``.
ROW_KIND = "restart_receipt"
#: A chat title is the user's own text; the record keeps a bounded copy.
_TITLE_MAX_CHARS = 120


@dataclass
class _Kept:
    slot: str
    title: str
    session_key: str
    running: bool
    detached_secs: float | None = None
    waits: int = 0
    wait_secs: float = 0.0
    via: str = "adopt"

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "slot": self.slot,
            "title": self.title,
            "session_key": self.session_key,
            "running": self.running,
            "via": self.via,
        }
        if self.detached_secs is not None:
            out["tools_down_secs"] = round(self.detached_secs)
        if self.waits:
            out["approvals_waited"] = self.waits
            out["approval_wait_secs"] = round(self.wait_secs)
        return out


@dataclass
class _Receipt:
    started_at: str
    kept: dict[str, _Kept] = field(default_factory=dict)
    ended: list[dict[str, Any]] = field(default_factory=list)


_RECEIPT: _Receipt | None = None


def begin() -> None:
    """Start this boot's receipt; any earlier one is dropped."""
    global _RECEIPT
    _RECEIPT = _Receipt(started_at=_now_iso())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _title(slot: Any) -> str:
    try:
        return str(getattr(slot, "display_title", "") or "")[:_TITLE_MAX_CHARS]
    except Exception:
        return ""


def _session_key(slot: Any) -> str:
    try:
        from kiro_crew.dashboard.chat_utils_slots import effective_session_key

        return str(effective_session_key(slot))
    except Exception:
        return ""


def row_text(kept: _Kept) -> str:
    """The row's English content, the catalog's words: what channel mirrors and older frontends read."""
    parts = ["The app restarted. This chat kept running."]
    if kept.detached_secs is not None:
        parts.append(f"The app's own tools were offline for {round(kept.detached_secs)} s.")
    if kept.waits == 1:
        parts.append(f"1 tool call waited {round(kept.wait_secs)} s for approval.")
    elif kept.waits:
        parts.append(
            f"{kept.waits} tool calls waited up to {round(kept.wait_secs)} s for approval."
        )
    return " ".join(parts)


def row_meta(kept: _Kept) -> dict[str, object]:
    """The row's ``meta``: its token and only the numbers the keeper measured."""
    from kiro_crew.dashboard.gateway_notice import (
        GATEWAY_NOTICE_RESTART_KEPT_RUNNING,
        gateway_notice_meta,
    )

    args: dict[str, int] = {}
    if kept.detached_secs is not None:
        args["down_secs"] = round(kept.detached_secs)
    if kept.waits:
        args["waits"] = kept.waits
        args["wait_secs"] = round(kept.wait_secs)
    return {"kind": ROW_KIND, **gateway_notice_meta(GATEWAY_NOTICE_RESTART_KEPT_RUNNING, **args)}


async def note_kept(state: Any, slot: Any, provider: Any) -> None:
    """Record a chat the adopt pass attached, and give it its row when it was working.

    Called after the keeper is attached and before the chat's continuing turn is
    read, so the row lands above that turn's rows. Never raises.
    """
    receipt = _RECEIPT
    if receipt is None:
        return
    try:
        from kiro_crew.acp.client_keeper import keeper_process

        client = getattr(provider, "_client", None)
        process = keeper_process(client) if client is not None else None
        attach = getattr(process, "state", None)
        kept = _Kept(
            slot=str(getattr(slot, "key", "")),
            title=_title(slot),
            session_key=_session_key(slot),
            # The keeper's own word first: the restore owes no row when the
            # transcript on disk already reads as cut (an unanswered user row).
            running=getattr(attach, "turn_at_detach", False) is True
            or getattr(slot, "_restart_interruption_owed", False) is True,
            detached_secs=getattr(attach, "detached_secs", None),
            waits=int(getattr(attach, "permission_waits", 0) or 0),
            wait_secs=float(getattr(attach, "permission_wait_secs", 0.0) or 0.0),
        )
        receipt.kept[kept.slot] = kept
        if not (kept.running or kept.waits):
            return
        from kiro_crew.dashboard.state import append_and_surface, load_window_off_loop

        await load_window_off_loop(slot)
        append_and_surface(
            state, slot, "notice", row_text(kept), "msg msg-info", meta=row_meta(kept)
        )
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning(
            "restart receipt: could not note kept chat %s", getattr(slot, "key", "?"), exc_info=True
        )


def note_left(slot: Any) -> None:
    """Record a kept chat that another start of it holds (its numbers are that start's)."""
    receipt = _RECEIPT
    if receipt is None:
        return
    try:
        key = str(getattr(slot, "key", ""))
        receipt.kept[key] = _Kept(
            slot=key,
            title=_title(slot),
            session_key=_session_key(slot),
            running=getattr(slot, "_restart_interruption_owed", False) is True,
            via="another start",
        )
    except Exception:  # noqa: BLE001 - see module docstring
        logger.warning("restart receipt: could not note a held chat", exc_info=True)


def note_ended(session_key: str, slot: Any, why: str) -> None:
    """Record a keeper the adopt pass ended, and why; *slot* is None when none was restored."""
    receipt = _RECEIPT
    if receipt is None:
        return
    entry: dict[str, Any] = {"session_key": str(session_key), "why": str(why)}
    if slot is not None:
        entry["slot"] = str(getattr(slot, "key", ""))
        entry["title"] = _title(slot)
    receipt.ended.append(entry)


def _cut_and_queued(state: Any, kept: dict[str, _Kept]) -> tuple[list[dict], list[dict]]:
    """The chats whose running turn did not carry on, and the cards a person must send."""
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    cut: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    for slot in list(getattr(state, "_slots", {}).values()):
        key = str(getattr(slot, "key", ""))
        if getattr(slot, "_restored_turn_marker", None) and key not in kept:
            cut.append({"slot": key, "title": _title(slot)})
        held = sum(
            1 for entry in list(getattr(slot, "_queue", ())) if entry.get(RESTORED_QUEUE_KEY)
        )
        if held:
            queued.append({"slot": key, "title": _title(slot), "count": held})
    return cut, queued


def summary(record: dict[str, Any]) -> str:
    """The receipt in one line, for the installer and the log."""
    kept = record.get("kept") or []
    running = sum(1 for k in kept if k.get("running"))
    queued = sum(int(q.get("count", 0)) for q in record.get("queued") or [])
    waits = sum(int(k.get("approvals_waited", 0)) for k in kept)
    parts = [f"{len(kept)} kept ({running} running)", f"{len(record.get('cut') or [])} cut"]
    parts.append(f"{queued} queued")
    if record.get("ended"):
        parts.append(f"{len(record['ended'])} keeper(s) ended")
    if waits:
        parts.append(f"{waits} approval(s) waited")
    return ", ".join(parts)


def build(state: Any) -> dict[str, Any] | None:
    """This boot's receipt as the record :func:`finish` writes; None before :func:`begin`."""
    receipt = _RECEIPT
    if receipt is None:
        return None
    cut, queued = _cut_and_queued(state, receipt.kept)
    record: dict[str, Any] = {
        "v": RECEIPT_VERSION,
        "started_at": receipt.started_at,
        "written_at": _now_iso(),
        "gateway_pid": os.getpid(),
        "kept": [k.as_json() for k in receipt.kept.values()],
        "cut": cut,
        "queued": queued,
        "ended": list(receipt.ended),
    }
    record["summary"] = summary(record)
    return record


def receipts_path(home: Path) -> Path:
    """Where the receipts of the data home *home* are appended."""
    return Path(home) / "logs" / RECEIPTS_FILE


async def finish(state: Any) -> dict[str, Any] | None:
    """Write this boot's receipt and end it; the record written, or None. Never raises."""
    global _RECEIPT
    try:
        record = build(state)
    except Exception:  # noqa: BLE001 - see module docstring
        logger.warning("restart receipt: could not be built", exc_info=True)
        record = None
    _RECEIPT = None
    if record is None:
        return None
    try:
        from kiro_crew.config.paths import data_home

        await asyncio.to_thread(_append, receipts_path(data_home()), record)
    except Exception:  # noqa: BLE001 - see module docstring
        logger.warning("restart receipt: could not be written", exc_info=True)
    logger.info("restart receipt: %s", record["summary"])
    return record


def _append(path: Path, record: dict[str, Any]) -> None:
    from kiro_crew.jsonl_util import rotate_jsonl_at
    from kiro_crew.owner_only_files import ensure_directory, owner_only_opener_for

    ensure_directory(path.parent)
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":"), default=str)
    rotate_jsonl_at(path, RECEIPTS_MAX_BYTES)
    with open(path, "a", encoding="utf-8", opener=owner_only_opener_for(path)) as fh:
        fh.write(line + "\n")
