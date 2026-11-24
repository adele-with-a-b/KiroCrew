"""The restart receipt: what one gateway start kept, cut and held, chat by chat.

The startup restore builds it in three steps:

* :func:`begin`, before the open tabs are rebuilt, starts this boot's receipt;
* the adopt pass (``chat_keeper_adopt``) reports each live keeper's outcome:
  :func:`note_kept` for a chat it attached, :func:`note_left` for one it left to
  another start of the chat (recorded as left, neither kept nor cut: that start's
  outcome is not this pass's to report), :func:`note_ended` for a keeper it ended;
* :func:`finish`, once the restore and the parked-send drain are done (or the restore
  raised), adds the chats whose running turn the restart cut and the restored queue
  cards that wait for a person, appends the whole receipt as one JSON line to
  ``<data home>/logs/`` (:data:`RECEIPTS_FILE`), and logs its one-line summary.

A kept chat whose turn was running (the keeper saw its prompt unanswered at the
detach, or the restore owes it its "interrupted" row; for an older keeper that
reports no gap, the restore found the turn in flight), or whose approval waited through the gap, gets one ``notice`` row in its
transcript, appended before its continuing turn is read. The row states only what
the keeper measured: how long no gateway was attached to this chat and, when a
permission request was open, how many waited and the longest wait. A chat whose turn was cut already
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

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "slot": self.slot,
            "title": self.title,
            "session_key": self.session_key,
            "running": self.running,
        }
        if self.detached_secs is not None:
            out["detached_secs"] = round(self.detached_secs)
        if self.waits:
            out["approvals_waited"] = self.waits
            out["approval_wait_secs"] = round(self.wait_secs)
        return out


@dataclass
class _Receipt:
    started_at: str
    kept: dict[str, _Kept] = field(default_factory=dict)
    left: dict[str, dict[str, Any]] = field(default_factory=dict)
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
        parts.append(f"The app was disconnected from this chat for {round(kept.detached_secs)} s.")
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
    if kept.running:
        args["running"] = 1
    if kept.detached_secs is not None:
        args["detached_secs"] = round(kept.detached_secs)
    if kept.waits:
        args["waits"] = kept.waits
        args["wait_secs"] = round(kept.wait_secs)
    return {"kind": ROW_KIND, **gateway_notice_meta(GATEWAY_NOTICE_RESTART_KEPT_RUNNING, **args)}


async def note_kept(state: Any, slot: Any, attach: Any) -> None:
    """Record a chat the adopt pass attached, and give it its row when it was working.

    *attach* is the state its keeper reported at the attach (``AttachState``), or
    None when there is none to read. Called after the keeper is attached and before the chat's continuing turn is
    read, so the row lands above that turn's rows. Never raises.
    """
    receipt = _RECEIPT
    if receipt is None:
        return
    try:
        detached_secs = getattr(attach, "detached_secs", None)
        kept = _Kept(
            slot=str(getattr(slot, "key", "")),
            title=_title(slot),
            session_key=_session_key(slot),
            running=_was_running(slot, attach, detached_secs),
            detached_secs=detached_secs,
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


def _was_running(slot: Any, attach: Any, detached_secs: Any) -> bool:
    """Whether the kept chat's turn ran on through the gap.

    The keeper's ``turn_at_detach``, or the restore's owed flag, which is not set
    when the transcript on disk already reads as cut (an unanswered user row). An
    older keeper reports no gap and no ``turn_at_detach``, so for it the restore's
    marker stands in: a turn marker not ended before the restart means a turn was
    in flight.
    """
    if (
        getattr(attach, "turn_at_detach", False) is True
        or getattr(slot, "_restart_interruption_owed", False) is True
    ):
        return True
    if detached_secs is not None:
        return False
    marker = getattr(slot, "_restored_turn_marker", None)
    return isinstance(marker, dict) and marker.get("cut") is not False


def note_left(slot: Any) -> None:
    """Record a kept chat the pass left to another start of it, which reports nothing here."""
    receipt = _RECEIPT
    if receipt is None:
        return
    try:
        key = str(getattr(slot, "key", ""))
        receipt.left[key] = {
            "slot": key,
            "title": _title(slot),
            "session_key": _session_key(slot),
        }
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


def _cut_and_queued(state: Any, receipt: _Receipt) -> tuple[list[dict], list[dict]]:
    """The chats whose running turn the restart cut, and the cards a person must send.

    Only the slots built by now: a crewmate thread the member-slot retry builds, or a
    channel conversation reconciled later, gets its "interrupted" row then and is not
    counted here.
    """
    from kiro_crew.dashboard.slot_queue_repository import is_held_queue_entry

    cut: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    for slot in list(getattr(state, "_slots", {}).values()):
        key = str(getattr(slot, "key", ""))
        marker = getattr(slot, "_restored_turn_marker", None)
        if (
            isinstance(marker, dict)
            # A turn the user stopped, or a refused model settled, was not cut.
            and marker.get("cut") is not False
            and key not in receipt.kept
            and key not in receipt.left
        ):
            cut.append({"slot": key, "title": _title(slot)})
        # A card the boot drain already accepted sends by itself: not a person's to send.
        held = sum(1 for entry in list(getattr(slot, "_queue", ())) if is_held_queue_entry(entry))
        if held:
            queued.append({"slot": key, "title": _title(slot), "count": held})
    return cut, queued


def summary(record: dict[str, Any]) -> str:
    """The receipt in one line, for the installer and the log."""
    kept = record.get("kept") or []
    running = sum(1 for k in kept if k.get("running"))
    queued = sum(int(q.get("count", 0)) for q in record.get("queued") or [])
    waits = sum(int(k.get("approvals_waited", 0)) for k in kept)
    cut = record.get("cut")
    parts = [
        f"{len(kept)} kept ({running} running)",
        "cut unknown" if cut is None else f"{len(cut)} cut",
    ]
    parts.append(f"{queued} queued")
    if record.get("left"):
        parts.append(f"{len(record['left'])} left to another start")
    if record.get("ended"):
        parts.append(f"{len(record['ended'])} keeper(s) ended")
    if waits:
        parts.append(f"{waits} approval(s) waited")
    line = ", ".join(parts)
    return line if record.get("complete", True) else f"{line} (the restore did not finish)"


def build(state: Any, *, complete: bool = True) -> dict[str, Any] | None:
    """This boot's receipt as the record :func:`finish` writes; None before :func:`begin`.

    *complete* is False when the restore raised: the record then
    holds what this start got to, and says so. Its ``cut`` is None (unknown), since
    the adopt pass may not have reported the chats a keeper still holds.
    """
    receipt = _RECEIPT
    if receipt is None:
        return None
    cut_found, queued = _cut_and_queued(state, receipt)
    cut: list[dict[str, Any]] | None = cut_found if complete else None
    record: dict[str, Any] = {
        "v": RECEIPT_VERSION,
        "started_at": receipt.started_at,
        "written_at": _now_iso(),
        "gateway_pid": os.getpid(),
        "complete": complete,
        "kept": [k.as_json() for k in receipt.kept.values()],
        "left": list(receipt.left.values()),
        "cut": cut,
        "queued": queued,
        "ended": list(receipt.ended),
    }
    record["summary"] = summary(record)
    return record


def receipts_path(home: Path) -> Path:
    """Where the receipts of the data home *home* are appended."""
    return Path(home) / "logs" / RECEIPTS_FILE


async def finish(state: Any, *, complete: bool = True) -> dict[str, Any] | None:
    """Write this boot's receipt and end it; the record written, or None. Never raises.

    A second call, or one before :func:`begin`, writes nothing.
    """
    global _RECEIPT
    try:
        record = build(state, complete=complete)
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
