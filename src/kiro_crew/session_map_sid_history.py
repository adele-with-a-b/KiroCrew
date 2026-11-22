"""The past native session ids of one ``session_map.json`` row, oldest first.

A chat moves to a new native session on a context-cap trim, a reset, a provider
switch, a poisoned-conversation discard or a stale repair. The row's ``sid`` then
names only the newest one, and ``discarded_sid`` only the last one emptied in
place, so an app that keyed data on an older id (a question asked before the move)
loses it. :data:`SID_HISTORY_FIELD` keeps every id that left ``sid``, capped at
:data:`SID_HISTORY_CAP`, so the chat reads as one conversation across moves.

Each entry is ``{"sid", "provider", "until"}``: the id, the row's provider while
it was current, and the UTC time it stopped being current. The current ``sid`` is
never listed. A row written before the field existed reads its ``discarded_sid``
as its one past id, with an empty provider and time; the first real push folds it
in, so nothing is rewritten on load.

:func:`record_departure` is the one writer, called by ``session_map`` wherever a
non-empty ``sid`` is replaced or emptied. Each function here whose first parameter
is ``self`` is a ``SessionMap`` method, bound by name. ``sid_record`` takes the map
lock through ``_guarded``, so a row's ``sid``, provider and list read as one unit;
it reaches the row only through ``_resolve_alias``.

Instance fields written: none.

Instance fields read: none.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.session_map_store import _guarded
from kiro_crew.validation import bounded_session_id

if TYPE_CHECKING:
    from kiro_crew.session_map import SessionMap

#: The row field holding the past ids. An ON-DISK name: renaming it drops every
#: chat's history on the next read.
SID_HISTORY_FIELD = "sid_history"

#: Set on a row once the cap has dropped an entry, so a reader can tell "these are
#: all its ids" from "older ids were lost". Also on disk.
SID_HISTORY_FULL_FIELD = "sid_history_full"

#: How many past ids a row keeps. Past it, the oldest is dropped. About 3.5 KB per
#: row at worst, and every map write rewrites every row.
SID_HISTORY_CAP = 32

#: The longest provider label an entry keeps; a longer one read off disk reads "".
_PROVIDER_MAX = 64
_UNTIL_MAX = 40


def _now() -> str:
    """The UTC time an id stops being current, in the shape entries store."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: object, limit: int) -> str:
    return value if isinstance(value, str) and len(value) <= limit else ""


def _clean(raw: object) -> list[dict[str, str]]:
    """The well-formed entries of a stored list, in order, each id once."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = bounded_session_id(item.get("sid"))
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(
            {
                "sid": sid,
                "provider": _text(item.get("provider"), _PROVIDER_MAX),
                "until": _text(item.get("until"), _UNTIL_MAX),
            }
        )
    return out


def _stored(entry: dict) -> list[dict[str, str]]:
    """*entry*'s past ids as stored, the legacy ``discarded_sid`` folded in."""
    if SID_HISTORY_FIELD in entry:
        return _clean(entry.get(SID_HISTORY_FIELD))
    legacy = bounded_session_id(entry.get("discarded_sid"))
    return [{"sid": legacy, "provider": "", "until": ""}] if legacy else []


def record_departure(entry: dict, sid: object) -> None:
    """Record that *sid* is leaving *entry*'s ``sid`` field.

    Call it BEFORE the field changes, so ``provider`` still names the backend the
    id ran on. An id already listed keeps its first place; an id that fails
    ``bounded_session_id`` is not recorded. Pure: the caller saves.
    """
    leaving = bounded_session_id(sid)
    if not leaving:
        return
    history = _stored(entry)
    if all(item["sid"] != leaving for item in history):
        history.append(
            {
                "sid": leaving,
                "provider": _text(entry.get("provider"), _PROVIDER_MAX),
                "until": _now(),
            }
        )
    if len(history) > SID_HISTORY_CAP:
        del history[: len(history) - SID_HISTORY_CAP]
        entry[SID_HISTORY_FULL_FIELD] = True
    entry[SID_HISTORY_FIELD] = history


def past_sids(entry: dict) -> tuple[str, list[dict[str, str]], bool]:
    """``(current sid, past entries, full)`` for *entry*. Pure; copies."""
    current = bounded_session_id(entry.get("sid")) or ""
    history = [item for item in _stored(entry) if item["sid"] != current]
    return current, history, entry.get(SID_HISTORY_FULL_FIELD) is True


@_guarded
def sid_record(self: SessionMap, key: str) -> dict[str, Any]:
    """Everything the map knows about *key*'s ids, read as one unit.

    ``{"sid", "provider", "history", "cap", "full"}``; an unknown key answers an
    empty record. Alias-folded like ``mapped_sid``; no disk, no mutation.
    """
    entry = self._resolve_alias(key)[1]
    if not entry:
        return {"sid": "", "provider": "", "history": [], "cap": SID_HISTORY_CAP, "full": False}
    current, history, full = past_sids(entry)
    return {
        "sid": current,
        "provider": _text(entry.get("provider"), _PROVIDER_MAX),
        "history": history,
        "cap": SID_HISTORY_CAP,
        "full": full,
    }


def sid_history(self: SessionMap, key: str) -> list[dict[str, str]]:
    """*key*'s past ids, oldest first, the current one not listed."""
    history: list[dict[str, str]] = self.sid_record(key)["history"]
    return history


def conversation_sids(self: SessionMap, key: str) -> list[str]:
    """Every id *key*'s conversation ran on, oldest first, the current one last."""
    record = self.sid_record(key)
    sids = [item["sid"] for item in record["history"]]
    return sids + [record["sid"]] if record["sid"] else sids
