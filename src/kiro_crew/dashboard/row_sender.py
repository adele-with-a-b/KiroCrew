"""Who sent a user-side transcript row: the one derivation of ``meta.sentBy``.

Every row of a role in :data:`SENDER_ROLES` gets ``meta.sentBy`` from
:func:`sent_by` inside ``_ChatSlot.append``, the one place every such row passes
through. Nothing else writes the field: each entry point already stamps a
server-only mark (``human``, ``relay``, ``turnActor``, ``cardOrigin``, the row's
role and ``injectKind``, ``nudge``), and this module only maps those marks to a
kind. ``sentBy`` and every input it reads are in
``chat_handlers.RESERVED_ROW_META_KEYS`` or are server-chosen, so neither a
sender's text nor its body meta can move the answer.

The precedence below IS the contract (``docs/system-specs/modules/history.md``,
"Row sender"). A row written before this field existed carries the same marks,
so a reader of the transcript file answers it by running the same table over
the stored row: an old row a person typed (``human: true``) still reads
``typed``.

Only ``typed`` means a person typed the row.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The row key this module owns.
SENT_BY_META_KEY = "sentBy"

#: The row key naming the one model prompt several rows were sent as
#: (``{"id", "part", "of"}``); see :func:`stamp_shared_prompt`.
PROMPT_META_KEY = "prompt"

#: Roles that stand for something sent INTO the session.
SENDER_ROLES = frozenset({"user", "inject", "nudge", "subagent"})

_APP_AUTHOR_PREFIX = "app:"


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _app_id(meta: Mapping[str, Any]) -> str:
    """The sending app's name: the app principal the gateway stamped, else ``appLabel``.

    The same order ``website/src/api/sharedCrewUi.ts`` ``appSender`` reads.
    """
    author = meta.get("author")
    if isinstance(author, Mapping) and author.get("kind") == "system":
        display = author.get("display")
        name = _str(display.get("name")) if isinstance(display, Mapping) else ""
        if name.startswith(_APP_AUTHOR_PREFIX):
            return name[len(_APP_AUTHOR_PREFIX) :]
    return _str(meta.get("appLabel")).split("/")[0]


def _with(kind: str, **fields: str) -> dict[str, str]:
    out = {"kind": kind}
    out.update((k, v) for k, v in fields.items() if v)
    return out


def sent_by(role: str, meta: Mapping[str, Any] | None) -> dict[str, str]:
    """The ``meta.sentBy`` value for a row of *role* carrying *meta*.

    First match wins; anything the table does not name is ``unknown``.
    """
    m: Mapping[str, Any] = meta if isinstance(meta, Mapping) else {}
    if role == "nudge":
        nudge = m.get("nudge")
        return _with(
            "wake", loopId=_str(nudge.get("loop_id")) if isinstance(nudge, Mapping) else ""
        )
    if role == "subagent":
        return _with("subagent_event")
    if role == "inject":
        inject_kind = m.get("injectKind")
        if inject_kind == "cron":
            return _with("cron", label=_str(m.get("cronLabel")))
        if inject_kind == "mcp_app":
            return _with("app", appId=_app_id(m))
        if inject_kind == "automation_notice":
            return _with("wake")
        return _with("gateway")
    if role != "user":
        return _with("unknown")
    relay = m.get("relay")
    if isinstance(relay, Mapping) and _str(relay.get("senderKey")):
        return _with(
            "session_send", senderKey=_str(relay.get("senderKey")), via=_str(relay.get("via"))
        )
    actor = m.get("turnActor")
    if actor == "app":
        return _with("app", appId=_app_id(m))
    if actor == "cron":
        return _with("cron")
    if actor == "crew":
        return _with("agent")
    if m.get("cardOrigin") is True:
        return _with("card")
    if m.get("human") is True:
        return _with("typed")
    return _with("unknown")


def with_sent_by(role: str, meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """*meta* with ``sentBy`` derived for *role*; a new dict, the caller's is untouched.

    A role outside :data:`SENDER_ROLES` gets *meta* back as passed.
    """
    if role not in SENDER_ROLES:
        return meta
    out = dict(meta) if isinstance(meta, dict) else {}
    out[SENT_BY_META_KEY] = sent_by(role, out)
    return out


def stamp_shared_prompt(rows: list[dict[str, Any]]) -> None:
    """Mark *rows* as the parts of ONE model prompt, in order.

    Used where several rows already on the transcript are drained into a single
    turn and no new row is written (``responsive_chat.reusable_row``), so a
    reader of the transcript can match them to the one prompt the model's own
    log holds. The id is the last row's ``meta.mid``: the row the turn anchors on.
    A run of fewer than two rows is left alone.
    """
    if len(rows) < 2:
        return
    last_meta = rows[-1].get("meta")
    prompt_id = _str(last_meta.get("mid")) if isinstance(last_meta, dict) else ""
    if not prompt_id:
        return
    for index, row in enumerate(rows, start=1):
        meta = row.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            row["meta"] = meta
        meta[PROMPT_META_KEY] = {"id": prompt_id, "part": index, "of": len(rows)}
