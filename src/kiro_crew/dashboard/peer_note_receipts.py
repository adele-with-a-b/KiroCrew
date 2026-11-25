"""Proof, kept where a sandboxed process cannot write, that a session queued a note.

``session_send`` to a busy session queues the message. The queue persists to the
session's metadata line, an ordinary writable file, so a restart hands every
entry back stripped of its sender, its actor and its containment snapshot
(``slot_queue_repository.sanitize_restored_queue``) and holds it as a card for a
person. That rule stays. What it cost was a peer's note: nobody is at that card,
so the note sat undelivered and the session that sent it waited.

This module keeps a receipt for each such note in one small JSON file under the
data home's ``crew-log`` root, keyed by the queue entry's id:

    {"<entry id>": {"target": "<slot key>", "target_tab": "<tab id>",
                    "sha256": "<of the note's text>", "sender": "<slot key>",
                    "sender_tab": "<tab id>", "relay": {...}, "at": <epoch>}}

The ``crew-log`` root is hidden from every sandboxed process and fenced from the
agent's own file tools (``sandbox_leaves``, ``security.paths``), which is the
whole point: the queue file can say anything, and only the gateway can write
what proves it. ``session_managers`` keeps its record there on the same terms.

WRITTEN when the queue takes a ``session_send`` entry (:func:`note_appended`,
called by the queue's own append, so every queue arm is covered).

REMOVED, before anything else happens, when the entry leaves the queue through
the queue's own pop, remove or edit, and when a drain consumes it
(:func:`forget_entries`). The write is synchronous, so a note whose turn may
have started has no receipt by the time that turn exists. After a crash in that
window the entry can still be on the metadata line; it comes back with no
receipt and waits for a person, as every restored card did before this module.
A note is never sent twice on this proof.

READ after a restart (:func:`deliver_restored`). A restored entry is proven only
when a receipt this process did not write names the same entry id, the same
target key and tab, and the same text digest. A proven entry gets the mark a
person's "run this next" gives (``RESTORED_SEND_CHOSEN_KEY``), so every drain
treats it as that click and nothing in the drain changes: it keeps its restored
provenance, carries no actor and no directive authority, and is judged against
the slot's current containment before it runs. Its ``meta`` is rebuilt from the
receipt, so nothing the file added rides along and the sender label is the one
the gateway recorded.

A receipt whose target is open and whose entry is missing or changed means the
note was not delivered: the sender's chat gets one fixed notice and the receipt
is dropped. The notice names the target and quotes none of the note, because the
only copy of that text is the file's.

What a writer of the queue file gains: nothing it could not already do. It can
drop or change a note (the note then waits or is lost, as before, and the sender
is now told), and it can leave a real note alone, which then runs once, in the
session it was sent to, with the text its sender wrote.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

FILENAME = "queued_peer_notes.json"

#: The most receipts kept. Past it the oldest go, and their notes wait for a
#: person after a restart, which is what every note did before receipts.
MAX_RECORDS = 256
#: A receipt for a session that never reopens is dropped at this age on load.
MAX_AGE_S = 14 * 24 * 3600
#: The largest sender label a receipt carries; a note with a larger one gets none.
_MAX_RELAY_BYTES = 4096
#: The largest file read back; a bigger one is read as damaged, so no note is proven.
_MAX_FILE_BYTES = 1024 * 1024

_FIELDS = ("target", "target_tab", "sha256", "sender", "sender_tab")

_lock = threading.Lock()
#: ``(path, records)`` for the file last read or written. Keyed by path so a test
#: that moves the data home reads that home's file.
_cache: "tuple[Path, dict[str, dict[str, Any]]] | None" = None
#: Ids the file held when this process first read it: receipts a PREVIOUS process
#: wrote, still to be judged against the restored queues. A receipt this process
#: writes is never in it, so a live note is never mistaken for a lost one.
_prior: "set[str]" = set()


def _path() -> Path:
    return data_home() / "crew-log" / FILENAME


def _digest(content: Any) -> str:
    if not isinstance(content, str) or not content:
        return ""
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


def _valid(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and all(isinstance(record.get(f), str) and record[f] for f in _FIELDS)
        and isinstance(record.get("at"), (int, float))
        and not isinstance(record.get("at"), bool)
        and (record.get("relay") is None or isinstance(record.get("relay"), dict))
    )


def _read(path: Path) -> "dict[str, dict[str, Any]]":
    """The file's valid, unexpired receipts. Anything unreadable proves nothing."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            logger.warning("peer note receipts: %s is too large; no note is proven", path)
            return {}
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("peer note receipts: %s is unreadable; no note is proven", path)
        return {}
    if not isinstance(data, dict):
        return {}
    oldest = time.time() - MAX_AGE_S
    return {
        str(entry_id): record
        for entry_id, record in data.items()
        if _valid(record) and record["at"] >= oldest
    }


def _records() -> "dict[str, dict[str, Any]]":
    """The live receipts, read from disk on the first call. Call under ``_lock``."""
    global _cache
    path = _path()
    if _cache is None or _cache[0] != path:
        records = _read(path)
        _cache = (path, records)
        _prior.clear()
        _prior.update(records)
    return _cache[1]


def reset_cache() -> None:
    """Forget what was read, so the next read comes from disk (a restart, in tests)."""
    global _cache
    with _lock:
        _cache = None
        _prior.clear()


def _apply(changes: "dict[str, dict[str, Any] | None]") -> None:
    """Apply *changes* (``id -> receipt``, or ``None`` to drop) and write the file.

    Blocking, and meant to be: a caller dropping a receipt needs it gone from the
    disk before the note's turn starts. A failed write keeps a new receipt out of
    memory (its note waits for a person after a restart, as it always did) and
    keeps a dropped one dropped in memory, with the failure logged.
    """
    with _lock:
        records = _records()
        before = dict(records)
        for entry_id, record in changes.items():
            if record is None:
                records.pop(entry_id, None)
                _prior.discard(entry_id)
            else:
                records[entry_id] = record
        while len(records) > MAX_RECORDS:
            dropped = next(iter(records))
            del records[dropped]
            _prior.discard(dropped)
        if records == before:
            return
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps(records, indent=1, sort_keys=True) + "\n")
        except OSError:
            logger.warning("peer note receipts: could not write %s", path, exc_info=True)
            for entry_id, record in changes.items():
                if record is not None:
                    records.pop(entry_id, None)


def note_appended(owner: Any, item: Any) -> None:
    """Keep a receipt for *item* when it is a ``session_send`` note *owner* queued.

    Only an entry carrying ``session_send``'s own queue mark and a whole sender
    stamp (slot key and tab), on a slot with a tab identity of its own: those four
    are what the restore checks, and a receipt missing one could not be matched.
    Never raises: a note that gets no receipt is still queued, and waits for a
    person after a restart as it did before.
    """
    try:
        meta = item.get("meta") if isinstance(item, dict) else None
        if not isinstance(meta, dict) or item.get("kind"):
            return
        # Local import: session_control reaches the queue repository through state.
        from kiro_crew.dashboard.session_control import (
            SESSION_SEND_QUEUED_META,
            send_origin_slot,
            send_origin_tab,
        )

        if meta.get(SESSION_SEND_QUEUED_META) is not True:
            return
        entry_id = item.get("id")
        record: dict[str, Any] = {
            "target": str(getattr(owner, "key", "") or ""),
            "target_tab": str(getattr(owner, "_tab_id", "") or ""),
            "sha256": _digest(item.get("content")),
            "sender": send_origin_slot(meta),
            "sender_tab": send_origin_tab(meta),
            "at": time.time(),
        }
        if not (isinstance(entry_id, str) and entry_id and all(record[f] for f in _FIELDS)):
            return
        relay = meta.get("relay")
        if isinstance(relay, dict) and relay:
            encoded = json.dumps(relay)
            if len(encoded) > _MAX_RELAY_BYTES:
                return
            record["relay"] = json.loads(encoded)
        _apply({entry_id: record})
    except Exception:
        logger.warning("peer note receipts: no receipt kept for a queued note", exc_info=True)


def forget_entries(items: Any) -> None:
    """Drop the receipts of queue entries that just left their queue.

    Called before the caller does anything with them, and synchronous: when it
    returns, no receipt on disk names an entry whose turn is about to start. An
    entry with no receipt costs one dictionary lookup and no I/O. Never raises.
    """
    try:
        ids = [
            item["id"]
            for item in items or ()
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        if not ids:
            return
        with _lock:
            records = _records()
            held = [entry_id for entry_id in ids if entry_id in records]
        if held:
            _apply(dict.fromkeys(held))
    except Exception:
        logger.warning("peer note receipts: could not drop a receipt", exc_info=True)


def _prior_records() -> "dict[str, dict[str, Any]]":
    with _lock:
        records = _records()
        return {entry_id: dict(records[entry_id]) for entry_id in _prior if entry_id in records}


def _tell_sender(state: Any, record: "dict[str, Any]") -> None:
    """One fixed notice in the sender's chat. Quotes nothing: the only copy of the
    note's text is the queue file's, and the receipt holds a digest."""
    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        _send_origin_recipient,
    )

    # The same resolver the drop notices use: no notice for a closed sender, or
    # for a key whose occupant is another tab than the one that sent the note.
    sender = _send_origin_recipient(state, record["sender"], record["sender_tab"], record["target"])
    if sender is None:
        return
    try:
        sender.append(
            "notice",
            f"⚠️ A message you sent to {record['target']} was still waiting in its queue "
            "when Kiro Crew restarted, and it was not delivered. Send it again if it "
            "still matters.",
            "msg msg-info",
        )
    except Exception:  # pragma: no cover - the report must not block the restore
        logger.warning("peer note receipts: could not tell %s", record["sender"], exc_info=True)


async def deliver_restored(state: Any) -> None:
    """After a restore: send the restored notes a receipt proves, report the rest.

    Safe to call again (the boot calls it once more for crewmate threads built
    late): a receipt is judged once its target is open, and a receipt this process
    wrote is never judged.
    """
    records = await asyncio.to_thread(_prior_records)
    if not records:
        return
    from kiro_crew.dashboard.session_control import SESSION_SEND_QUEUED_META
    from kiro_crew.dashboard.slot_queue_repository import (
        RESTORED_QUEUE_KEY,
        RESTORED_SEND_CHOSEN_KEY,
        _drain_after_memory_task_publication,
    )

    proven: set[str] = set()
    keys: list[str] = []
    for slot in list(state._slots.values()):
        tab = str(getattr(slot, "_tab_id", "") or "")
        for entry in list(getattr(slot, "_queue", None) or ()):
            entry_id = entry.get("id") if isinstance(entry, dict) else None
            if not isinstance(entry_id, str):
                continue
            record = records.get(entry_id)
            # One entry per receipt: a second copy of the line waits for a person.
            if record is None or entry_id in proven or not entry.get(RESTORED_QUEUE_KEY):
                continue
            if (
                record["target"] != slot.key
                or record["target_tab"] != tab
                or record["sha256"] != _digest(entry.get("content"))
            ):
                continue
            proven.add(entry_id)
            # Rebuilt, not filtered: whatever else the line carried is dropped, and
            # the sender label is the gateway's own record of it.
            entry["meta"] = {
                SESSION_SEND_QUEUED_META: True,
                **({"relay": dict(record["relay"])} if record.get("relay") else {}),
            }
            entry[RESTORED_SEND_CHOSEN_KEY] = True
            if slot.key not in keys:
                keys.append(slot.key)
    lost = [
        entry_id
        for entry_id, record in records.items()
        # A target that is not open yet is judged when it is.
        if entry_id not in proven and record["target"] in state._slots
    ]
    for entry_id in lost:
        _tell_sender(state, records[entry_id])
    with _lock:
        # Judged: a proven note keeps its receipt until it leaves the queue.
        _prior.difference_update(proven)
    if lost:
        await asyncio.to_thread(_apply, dict.fromkeys(lost))
    if not keys:
        return
    state.push_slots_update()
    from kiro_crew.dashboard.chat_runner import _drain_parked_queues
    from kiro_crew.memory_startup import memory_startup_preparing

    # An idle target starts its first proven note now; a target whose turn outlived
    # the restart takes them at that turn's end, like any chosen card.
    memory_task = state.memory_startup_task
    if (memory_task is None and memory_startup_preparing()) or (
        memory_task is not None and not memory_task.done()
    ):
        task = asyncio.create_task(_drain_after_memory_task_publication(state, keys))
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    else:
        await _drain_parked_queues(state, keys)
