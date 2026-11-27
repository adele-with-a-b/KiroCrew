"""A bound worker that stops on the person without reporting it: the gateway reports it.

A worker's ``work_report`` is the only thing that wakes its conductor, and a worker
can stop on the person without one. It can end its turn on a question written in its
chat (a ``[Q2a]`` line, an ``[OPTIONS: ...]`` line), park on a tool approval, or put
a question card up. Each of those sits in the worker's own chat, which nobody
watches: its item keeps reading ``progress``, the conductor's ``work-ledger`` watch
stays asleep, and a patrol that reads the chat's last few rows can scroll past it.

So the gateway files the ``question`` report the worker did not, through the same
transaction a worker's own report takes (``work_ledger_worker._record_report``):
the item moves to ``question`` with a person ``reason``, the conductor wakes, and
the Board's "needs you" reads it, with nothing downstream knowing the difference.
The report says in its own words and in its :data:`WAIT_ARTIFACT` pointer (a key a
worker's own report cannot write) that the gateway wrote it and where the question
is. The worker's own ``artifacts`` and ``discussion`` ride along unchanged.

It is written only when no other report already holds the conductor: a ``question``,
``blocked``, ``reply`` or ``done`` the worker filed and nobody has answered is left
standing,
and so is a gateway report of the same question (the worker asking again), so a
repeat never wakes the conductor twice. A turn that answered a person typing in the
worker's own chat writes nothing: that person is looking at the question.

A turn can also end with no reply at all, because the worker's model account cannot
be used: it is signed out, it could not mint credentials, or its credentials were
refused (``llm_helpers.model_road_down_cause``). The chat is left on an error row and
its item on its last status, so the conductor learns nothing until the item goes
stale. The gateway files a ``blocked`` report for that turn through the same
transaction (:func:`record_model_road_down`): no ``reason``, because the item waits on
a service and not on a person, fixed words that start ``Gateway:``, and the
:data:`MODEL_ROAD_ARTIFACT` pointer naming the cause. It is written only while no
report holds the conductor (:func:`_road_mark_owed`), so one report stands for the
whole outage: the turns that die after it file nothing until the conductor has
answered it.

What counts as a question in text agrees with the Ref Links finder
(``ref_labels.DEF`` / ``HEAD`` in agent-definitions, Q labels only) and with the chip
bar's ``[OPTIONS: ...]`` reader (``member_waiting.chip_bar_options``).

Served through :mod:`kiro_crew.dashboard.handlers.work_ledger`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any

from kiro_crew import work_ledger
from kiro_crew.credential_errors import (
    MODEL_ROAD_CREDENTIALS_REFUSED,
    MODEL_ROAD_CREDENTIALS_UNAVAILABLE,
    MODEL_ROAD_SIGNED_OUT,
)
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.resolve import unit_for_session_key
from kiro_crew.dashboard.handlers.work_ledger_recording import _acting_unit
from kiro_crew.dashboard.handlers.work_ledger_worker import (
    GATEWAY_ARTIFACT_KEYS,
    MODEL_ROAD_ARTIFACT,
    REFUSED_ARTIFACT,
    WAIT_ARTIFACT,
    _last_report_id,
    _record_report,
    record_person_steer_result,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.work_ledger")

#: The audit operation of a report the gateway files for a worker.
WAIT_OPERATION = "work_report.gateway"

#: Where a worker can stop on the person: the values :data:`WAIT_ARTIFACT` carries.
WAIT_IN_TEXT = "a question in its reply"
WAIT_ON_APPROVAL = "a tool approval"
WAIT_ON_CARD = "a question card"

#: The ``steer`` text written when a person answers a worker's question card. Fixed
#: words, like ``work_ledger_worker.APPROVAL_ANSWERED_TEXT``.
CARD_ANSWERED_TEXT = "The person answered a question card in this crew member's chat."

#: Fixed words for an approval wait: nothing from the tool call reaches the ledger.
_APPROVAL_SUMMARY = "A tool approval is waiting for a person in its chat."

_SUMMARY_PREFIX = "Gateway: it stopped on {where} and filed no work_report. "

#: Fixed words for a turn that ended because the model road is down, by cause: the
#: provider's own text can name an account or a host, so none of it reaches the ledger.
_ROAD_DOWN_WORDS = {
    MODEL_ROAD_SIGNED_OUT: "its model account is signed out",
    MODEL_ROAD_CREDENTIALS_REFUSED: "its model account's credentials were refused",
    MODEL_ROAD_CREDENTIALS_UNAVAILABLE: "its model account could not mint credentials",
}
_ROAD_DOWN_SUMMARY = (
    "Gateway: this chat's turn ended because {words}, and it filed no work_report. "
    "The work waits on the model road, not on a person."
)

#: A Q-label definition line, as the Ref Links finder reads one (``ref_labels.DEF``,
#: narrowed to Q labels): optional list marker, heading marks and bold, then the label.
#: Matched on a line already stripped, so no two runs of blanks sit side by side: a
#: leading ``\s*`` ahead of the optional marker and a second ``\s*`` behind it would
#: try every split of a long blank line.
_Q_LINE = re.compile(r"(?:[-*+]|\d+[.)])?\s*(?:#{1,6}\s+)?(?:\*\*)?\[(Q\d+[a-z])\]\**:?\s*(.*)")
#: A heading whose first word is a Q label (``ref_labels.HEAD``), on a stripped line.
_Q_HEAD = re.compile(r"#{1,6}\s+(Q\d+[a-z])\b[:.]?\s*(.*)")
_FENCE = re.compile(r"```|~~~")


def asked_in_text(text: str) -> str:
    """The question *text* (a reply) leaves for the person, as one line, or ``""``.

    Every Q-label line outside a code fence, label kept, joined with a space; else
    the reply's ``[OPTIONS: ...]`` choices, when the chip bar would offer them. Pure.
    """
    from kiro_crew.dashboard.member_waiting import chip_bar_options

    questions: list[str] = []
    fenced = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or "Q" not in line:
            continue
        match = _Q_LINE.match(line) or _Q_HEAD.match(line)
        if match:
            questions.append(f"[{match.group(1)}] {match.group(2).strip()}".strip())
    if questions:
        return " ".join(questions)
    options = chip_bar_options(text or "") if "[OPTION" in (text or "") else []
    return f"Choices: {' | '.join(options)}" if options else ""


def _summary(where: str, question: str) -> str:
    text = _SUMMARY_PREFIX.format(where=where) + " ".join(question.split())
    cap = work_ledger.MAX_SUMMARY_CHARS
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _bound_item_of(slot_key: str) -> tuple[str, str, str]:
    """``(board, item id, worker key)`` *slot_key* is bound to, or blanks. BLOCKING.

    The worker's own binding names the item, the way ``work_report`` resolves it.
    Anything unreadable answers no item, so a failure writes nothing.
    """
    from kiro_crew import session_ledger

    try:
        for candidate in dict.fromkeys((slot_key, session_ledger.ledger_key(slot_key))):
            binding = work_ledger.read_binding(candidate)
            if binding is not None:
                return binding[0], binding[1], candidate
    except Exception:  # noqa: BLE001 - an unreadable binding records nothing
        logger.debug("worker wait: binding read failed for %s", slot_key, exc_info=True)
    return "", "", ""


def _read_item(board: str, item_id: str) -> work_ledger.WorkItem | None:
    """*item_id* of *board* as it stands, or ``None``. BLOCKING."""
    return work_ledger.read_work_item(board, item_id)


def _own_artifacts(item: work_ledger.WorkItem) -> dict[str, str]:
    """*item*'s artifacts without the keys only the gateway writes."""
    return {k: v for k, v in (item.artifacts or {}).items() if k not in GATEWAY_ARTIFACT_KEYS}


def _with_gateway_keys(own: dict[str, str], gateway: dict[str, str]) -> dict[str, str]:
    """*own* plus *gateway*, within ``MAX_ARTIFACT_KEYS``: the gateway's keys keep a seat."""
    room = max(0, work_ledger.MAX_ARTIFACT_KEYS - len(gateway))
    return {**dict(list(own.items())[:room]), **gateway}


def _report_seen(item: work_ledger.WorkItem) -> tuple[Any, ...]:
    """What tells *item*'s last report apart from a later one: its stamp and fields."""
    return (item.last_report_at, item.status, item.artifacts or {}, item.discussion)


def _failed_since_report(events: list[dict[str, Any]]) -> bool:
    """Whether a ``fail`` verdict follows the newest ``report`` on *events*.

    A verdict stays on the item across a later ``done``, so the item alone cannot
    tell a ``done`` the conductor failed from a fresh one it has yet to check.
    """
    last_report = max((n for n, e in enumerate(events) if e.get("kind") == "report"), default=None)
    return last_report is not None and any(
        e.get("kind") == "verdict" and e.get("text") == "fail" for e in events[last_report + 1 :]
    )


def _workers_move(item: work_ledger.WorkItem, events: list[dict[str, Any]]) -> bool:
    """Whether *item*'s last report leaves the next move with the worker.

    ``work_ledger._worker_owns_next_move``, read off the event tail where its stamps
    tie: a ``done`` counts only when its ``fail`` verdict came after it
    (:func:`_failed_since_report`), and a waiting report is left to
    ``answered_since_report``, which orders a same-second ``decide`` by the tail. A
    ``reply`` always counts: it waits on nobody, and a follow-up writes no answer.
    """
    if item.status == "done":
        return item.verdict == "fail" and _failed_since_report(events)
    if item.status in work_ledger.WAITING_STATUSES:
        return False
    return item.status == "reply" or work_ledger._worker_owns_next_move(item)


def _stopped_unreported(
    worker_key: str, summary: str, seen: tuple[Any, ...]
) -> Callable[[work_ledger.WorkItem, list[dict[str, Any]]], bool]:
    """Whether a gateway report of *summary* is owed on an item, as it stands.

    Open, still bound to *worker_key*, its last report still the one *seen* when the
    report was built (:func:`_report_seen`: no report landed in between), and no
    report holds the conductor: the next move is the worker's
    (:func:`_workers_move`), or a person answered the last report since. A
    ``question`` the gateway filed is replaced only by another wait; the same wait
    is never filed twice.
    """
    from kiro_crew.dashboard.handlers.work_ledger_board import answered_since_report

    def owed(item: work_ledger.WorkItem, events: list[dict[str, Any]]) -> bool:
        if item.is_terminal or item.worker_session_key != worker_key:
            return False
        if _report_seen(item) != seen:
            return False
        if _workers_move(item, events) or answered_since_report(item, events):
            return True
        filed_by_gateway = WAIT_ARTIFACT in (item.artifacts or {})
        return filed_by_gateway and item.status == "question" and item.summary != summary

    return owed


def _refused_text(refused: list[str]) -> str:
    text = "; ".join(" ".join(r.split()) for r in refused if r and r.strip())
    cap = work_ledger.MAX_ARTIFACT_VALUE_CHARS
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _road_mark_owed(
    worker_key: str, seen: tuple[Any, ...]
) -> Callable[[work_ledger.WorkItem, list[dict[str, Any]]], bool]:
    """Whether a model-road ``blocked`` report is owed on an item, as it stands.

    The first half of :func:`_stopped_unreported`, and only that: open, still bound
    to *worker_key*, its last report the one *seen*, and no report holding the
    conductor. So a report the worker filed and nobody answered is left standing, a
    question the gateway filed for the person is never replaced by a wait on no
    person, and the gateway's own model-road report stands until the conductor
    answers it: one per outage, however many turns die in it.
    """
    from kiro_crew.dashboard.handlers.work_ledger_board import answered_since_report

    def owed(item: work_ledger.WorkItem, events: list[dict[str, Any]]) -> bool:
        if item.is_terminal or item.worker_session_key != worker_key:
            return False
        if _report_seen(item) != seen:
            return False
        return _workers_move(item, events) or answered_since_report(item, events)

    return owed


async def _file_for_worker(
    state: Any,
    slot_key: str,
    fields: dict[str, Any],
    gateway: dict[str, str],
    owed: Callable[
        [str, str, tuple[Any, ...]], Callable[[work_ledger.WorkItem, list[dict[str, Any]]], bool]
    ],
) -> str:
    """File the report *fields* on the item worker *slot_key* is bound to.

    The one write both gateway reports take. *fields* are the report's ``status``,
    ``summary`` and, for a wait on a person, ``reason``; *gateway* are the artifact
    keys only the gateway writes, set beside the worker's own artifacts, with its
    discussion kept. *owed*, given the worker key, the cleaned summary and the report
    last seen, answers the ``only_while`` asked under the board lock. Returns the item
    id, or ``""`` when the session is not a bound worker, no report is owed or the
    write was refused. Raises what the ledger raises: each caller swallows it.
    """
    board, item_id, worker_key = await asyncio.to_thread(_bound_item_of, slot_key)
    if not item_id:
        return ""
    unit, refusal = await asyncio.to_thread(
        _acting_unit, state, worker_key, WAIT_OPERATION, item_id, board
    )
    if refusal is not None or not unit:
        return ""
    own_unit = unit_for_session_key(getattr(state, "sessions", None), worker_key)
    # Read again once when a report landed between the read and the board lock:
    # the fields are built from the item as read, and the newer report decides.
    for _attempt in range(2):
        item = await asyncio.to_thread(_read_item, board, item_id)
        if item is None:
            return ""
        seen = _report_seen(item)
        cleaned = crew_log_emit.safe_work_fields(
            {**fields, "artifacts": _with_gateway_keys(_own_artifacts(item), gateway)}
        )
        response = await _record_report(
            state,
            worker_key,
            board,
            item_id,
            unit,
            own_unit,
            cleaned,
            item.discussion,
            operation=WAIT_OPERATION,
            only_while=owed(worker_key, cleaned["summary"], seen),
        )
        if response.status == 200:
            return item_id
        now = await asyncio.to_thread(_read_item, board, item_id)
        if now is None or _report_seen(now) == seen:
            return ""
    return ""


async def record_worker_wait(
    state: Any, slot_key: str, *, where: str, question: str, refused: list[str] | None = None
) -> str:
    """File a ``question`` report for worker *slot_key*, which stopped on the person.

    *where* is one of the ``WAIT_*`` values, *question* the words the person must
    answer, *refused* the tool calls a policy blocked on the way there
    (``REFUSED_ARTIFACT``). The worker's own artifacts and discussion are kept.
    Returns the item id, or ``""`` when the session is not a bound worker, no report
    is owed (:func:`_stopped_unreported`) or the write was refused. Never raises.
    """
    try:
        gateway = {WAIT_ARTIFACT: where}
        if refused_text := _refused_text(refused or []):
            gateway[REFUSED_ARTIFACT] = refused_text
        fields = {
            "status": "question",
            "reason": "approval" if where == WAIT_ON_APPROVAL else "needs_human",
            "summary": _summary(where, question),
        }
        return await _file_for_worker(state, slot_key, fields, gateway, _stopped_unreported)
    except Exception:  # noqa: BLE001 - a turn must never fail on the ledger
        logger.warning("worker wait: the gateway report was not written", exc_info=True)
        return ""


async def record_model_road_down(state: Any, slot_key: str, *, cause: str) -> str:
    """File a ``blocked`` report for worker *slot_key*, whose turn the model road ended.

    *cause* is a ``credential_errors.MODEL_ROAD_*`` value and rides as the
    :data:`MODEL_ROAD_ARTIFACT` artifact; the summary is fixed words for it. No
    ``reason`` is set: the item waits on a service, not on a person, so it wakes the
    conductor and holds nothing for the person. Returns the item id, or ``""`` when
    the session is not a bound worker, *cause* is not one of those values, no report
    is owed (:func:`_road_mark_owed`) or the write was refused. Never raises.
    """
    try:
        words = _ROAD_DOWN_WORDS.get(cause)
        if not words:
            return ""
        fields = {"status": "blocked", "summary": _ROAD_DOWN_SUMMARY.format(words=words)}
        return await _file_for_worker(
            state,
            slot_key,
            fields,
            {MODEL_ROAD_ARTIFACT: cause},
            lambda worker_key, _summary_text, seen: _road_mark_owed(worker_key, seen),
        )
    except Exception:  # noqa: BLE001 - a turn must never fail on the ledger
        logger.warning("model road: the gateway report was not written", exc_info=True)
        return ""


def _schedule(state: Any, coro: Any) -> None:
    """Run *coro* off the caller's path, held by the state's task set. Never raises."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
        tasks = getattr(state, "_background_tasks", None)
        if isinstance(tasks, set):
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except Exception:  # noqa: BLE001 - no loop, or a stub state: record nothing
        coro.close()
        logger.debug("worker wait: could not schedule the ledger write", exc_info=True)


def note_turn_end_question(
    state: Any,
    slot_key: str,
    text: str,
    *,
    person_turn: bool,
    refused: list[str] | None = None,
) -> None:
    """A turn of *slot_key* ended on *text*: file its question when it left one.

    *person_turn* is a turn that answered a person typing in this chat, who is
    looking at the reply already, so nothing is filed for it. *refused* are the
    turn's policy-blocked calls, filed beside the question. Cheap when the reply
    holds no question: the binding is read only after the text matched.
    """
    if person_turn:
        return
    question = asked_in_text(text)
    if question:
        _schedule(
            state,
            record_worker_wait(
                state, slot_key, where=WAIT_IN_TEXT, question=question, refused=refused
            ),
        )


def note_model_road_down(state: Any, slot_key: str, cause: str) -> None:
    """A turn of *slot_key* ended because its model account cannot be used (*cause*)."""
    _schedule(state, record_model_road_down(state, slot_key, cause=cause))


def note_approval_waiting(state: Any, slot_key: str) -> None:
    """A tool approval in *slot_key*'s chat now waits for a person."""
    _schedule(
        state,
        record_worker_wait(state, slot_key, where=WAIT_ON_APPROVAL, question=_APPROVAL_SUMMARY),
    )


def note_question_card(state: Any, slot_key: str, questions: list[dict[str, Any]]) -> None:
    """A question card in *slot_key*'s chat now waits for a person."""
    words = " ".join(str(q.get("question") or "") for q in questions if isinstance(q, dict))
    _schedule(
        state,
        record_worker_wait(state, slot_key, where=WAIT_ON_CARD, question=words or "A question."),
    )


def _waits_on_card(
    worker_key: str, report_id: str
) -> Callable[[work_ledger.WorkItem, list[dict[str, Any]]], bool]:
    """The item still waits on the question card the gateway filed as report *report_id*."""
    from kiro_crew.dashboard.handlers.work_ledger_board import awaits_person

    def waits(item: work_ledger.WorkItem, events: list[dict[str, Any]]) -> bool:
        return (
            not item.is_terminal
            and item.worker_session_key == worker_key
            and (item.artifacts or {}).get(WAIT_ARTIFACT) == WAIT_ON_CARD
            and bool(report_id)
            and _last_report_id(events) == report_id
            and awaits_person(item, events)
        )

    return waits


async def record_question_card_answered(state: Any, slot_key: str) -> str:
    """Record that a person answered the question card worker *slot_key* waited on.

    The answer clears the item's "needs you" the way an approval answer does
    (``work_ledger_worker.record_approval_answered``): one person ``steer``, written
    only while the gateway's card report is the item's newest. Returns the item id,
    or ``""``. Never raises.
    """
    try:
        board, item_id, worker_key = await asyncio.to_thread(_bound_item_of, slot_key)
        if not item_id:
            return ""
        events = [
            e.to_dict() for e in await asyncio.to_thread(work_ledger.read_events, board, item_id)
        ]
        report_id = _last_report_id(events)
        landed, _why = await record_person_steer_result(
            state,
            board,
            item_id,
            CARD_ANSWERED_TEXT,
            only_while=_waits_on_card(worker_key, report_id),
            id_salt=report_id,
        )
        return landed
    except Exception:  # noqa: BLE001 - an answer must not fail on the ledger
        logger.warning("question card answer: the ledger event was not written", exc_info=True)
        return ""


def note_question_card_answered(state: Any, slot_key: str) -> None:
    """Schedule :func:`record_question_card_answered` off the caller's path."""
    _schedule(state, record_question_card_answered(state, slot_key))
