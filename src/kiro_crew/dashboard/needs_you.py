"""Whether a chat waits on its owner: each sidebar row's ``needs_you`` summary.

``SlotProjection.to_dict`` puts it on every row, beside ``pending_approval``,
``needs_input`` and ``has_options``, so it rides the same ``slots`` frames and
diffs. ``None`` means nothing in the chat waits on the owner. Otherwise it is
``{count, oldest_ts, first_words, row_id, kinds}``: how many asks are open; when
the oldest began, in epoch seconds; that ask's first words, display-clean,
redacted and at most :data:`FIRST_WORDS_MAX` characters (``""`` when the ask has
none); the ``meta.mid`` of the transcript row holding it, ``None`` when no row
does; and which of :data:`KINDS` are open, in that order.

What counts, and what clears it:

* ``approval`` -- each tool approval still pending: the slot's own futures,
  matched to their permission rows, and the coordinator records the state holds
  for the slot. Deciding one removes it. A Crew Mode worker's approval is
  decided only on its conductor's approval card, so while that conductor is a
  row of the list the approval counts on the conductor's row instead
  (:func:`settle_crew_rows`).
* ``question`` -- each question card still open (``_question_pending``, the
  record ``needs_input`` reads); its words are the card's first question.
  Answering or withdrawing it removes it.
* ``options`` / ``labelled`` -- the newest finished reply ends in an
  ``[OPTIONS: ...]`` line, or has lines that open, after list, heading or
  emphasis marks, with a question label: ``Q``, digits and one lower-case
  letter in square brackets, like ``[Q7a]``. Each distinct label is one ask; an
  options line is one ask when the reply has no label (with labels, the options
  answer them). Lines in a code block or a quote never count, and only the
  newest reply is read (the newest-reply-only decision in docs/decisions).
  They count while no turn runs, the last turn was not interrupted and the
  reply is inside the options cutoff, and only when a PERSON started the turn
  that wrote it: the newest
  ``user`` row before the reply carries ``history.HUMAN_TURN_META_KEY``. Input
  rows the gateway writes itself (a nudge, an inject, a sub-agent event) carry
  that person's turn on. A reply to another session's send, to an app or to any
  other unmarked row asks the owner nothing, and a Crew Mode worker never asks
  the person (its questions go to its conductor), so on a worker's row these
  two kinds never count. The owner's next message is a newer row, so it clears
  them.

Nothing is stored: the field is derived on each projection from the
transcript and the in-memory approval and question state the row already
reads, so after a gateway restart it says what the restored chat still asks.
A reply's labels and words are read inside the row summary (``slot_row_memo``),
memoized on the transcript tail, so an unchanged chat costs a compare; who
started its turn is read only while its ask is open, from the roles and markers
of at most :data:`STARTER_SCAN_ROWS` rows.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from kiro_crew.constants import _FenceWalk
from kiro_crew.history_schema import transcript_sort_key
from kiro_crew.preview_text import speech_preview

logger = logging.getLogger(__name__)

#: The row field.
FIELD = "needs_you"

KIND_APPROVAL = "approval"
KIND_QUESTION = "question"
KIND_OPTIONS = "options"
KIND_LABELLED = "labelled"
#: Every kind, in the order a row lists them.
KINDS = (KIND_APPROVAL, KIND_QUESTION, KIND_OPTIONS, KIND_LABELLED)

#: The most characters ``first_words`` holds, its "…" included.
FIRST_WORDS_MAX = 80

#: How many rows older than a reply are read to find the row that started its
#: turn. A reply whose starter is further back is not read as the owner's.
STARTER_SCAN_ROWS = 200

_QUOTE_RE = re.compile(r" {0,3}>")
_LABEL_RE = re.compile(
    r" {0,3}(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)?(?:#{1,6}[ \t]+)?"
    r"(?:(?P<em>\*\*|__|\*|_)[ \t]*)?\[(?P<label>Q\d{1,9}[a-z])\](?!\()"
)
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


class Ask(NamedTuple):
    """One open ask, or several of one reply that share its time and words."""

    kinds: tuple[str, ...]
    #: How many asks this stands for (``count`` would shadow ``tuple.count``).
    number: int
    ts: float
    words: str
    row_id: str | None


class ReplyAsk(NamedTuple):
    """What the newest reply asks, read with the row summary."""

    #: The reply row's stamp.
    ts: str
    #: Its distinct question labels, in order.
    labels: tuple[str, ...]
    #: Whether it ends in an ``[OPTIONS:]`` line.
    options: bool
    #: Display-clean and redacted.
    words: str
    row_id: str | None
    #: How many rows of the summarized list follow the reply.
    back: int


def question_labels(text: str) -> list[tuple[str, str]]:
    """Each distinct question label opening a line of *text*, with that line as words.

    The words are the line from the label on, the label without its brackets
    and any emphasis that opened before it kept, so markdown stripping closes it.
    """
    if "[Q" not in text:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    # The control-tag strip's own walker: a fence line, the code inside and
    # everything after a fence it cannot read are code.
    fence = _FenceWalk()
    for line in text.splitlines():
        was_inside = fence.inside
        fence.feed(line)
        if was_inside or fence.inside or _QUOTE_RE.match(line):
            continue
        match = _LABEL_RE.match(line)
        if match is None or match.group("label") in seen:
            continue
        label = match.group("label")
        seen.add(label)
        rest = line[match.end() :].strip()
        out.append((label, f"{match.group('em') or ''}{label} {rest}".rstrip()))
    return out


def options_words(body: str) -> str:
    """The words an ``[OPTIONS:]`` ask is shown by: *body* is the reply without that line.

    Its last paragraph, from the last sentence there that asks a question, or
    the whole paragraph when none does.
    """
    paragraphs = [part for part in _PARAGRAPH_RE.split(body) if part.strip()]
    if not paragraphs:
        return ""
    sentences = _SENTENCE_END_RE.split(" ".join(paragraphs[-1].split()))
    asked = [index for index, sentence in enumerate(sentences) if sentence.endswith("?")]
    return " ".join(sentences[asked[-1] if asked else 0 :])


def clean_words(text: str, redact: Callable[[str], str]) -> str:
    """*text* as one display line: markdown stripped, then redacted, then cut.

    The roster's one-line quote (``speech_preview``), cut one short so its "…"
    keeps the whole at :data:`FIRST_WORDS_MAX`.
    """
    return speech_preview(text, redact, FIRST_WORDS_MAX - 1)


def reply_ask_of(
    message: Mapping[str, Any],
    text: str,
    *,
    back: int,
    options: bool,
    redact: Callable[[str], str],
    strip_options: Callable[[str], str],
) -> ReplyAsk | None:
    """What reply *message* (its *text*, *back* rows from the end) asks, or ``None``."""
    labels = question_labels(text)
    if not labels and not options:
        return None
    # circular import: the state facade imports the row summary, which imports this.
    from kiro_crew.dashboard.state import row_mid

    raw = labels[0][1] if labels else options_words(strip_options(text))
    return ReplyAsk(
        ts=str(message.get("ts") or ""),
        labels=tuple(label for label, _ in labels),
        options=options,
        words=clean_words(raw, redact),
        row_id=row_mid(message),
        back=back,
    )


def started_by_person(
    rows: Sequence[Mapping[str, Any]],
    reply: ReplyAsk,
    is_system_notice: Callable[[str, dict], bool],
) -> bool:
    """Whether a person started the turn that wrote *reply*, read from the rows before it.

    Walking back from the reply, the first ``user`` row decides, read with the
    lesson gate's classifier (``memory_origin_gate.classify_turn``): a person's
    row (``HUMAN_TURN_META_KEY``) is ``True``, any other (another session's
    send, an app's, a wake) is ``False``. Every other row carries the turn on.
    No such row within :data:`STARTER_SCAN_ROWS` is ``False``.
    """
    # Imported here: the gate loads the memory store's modules, which a row
    # needs only once its reply asks something.
    from kiro_crew.memory_origin_gate import TURN_HUMAN, classify_turn

    newest = len(rows) - 1 - reply.back
    for index in range(newest - 1, max(newest - 1 - STARTER_SCAN_ROWS, -1), -1):
        message = rows[index]
        meta = message.get("meta")
        if message.get("role") == "user" and not is_system_notice(
            "user", meta if isinstance(meta, dict) else {}
        ):
            return classify_turn(message) == TURN_HUMAN
    return False


def ask_epoch(stamp: Any) -> float | None:
    """A transcript stamp as epoch seconds, ``None`` when it does not parse."""
    if not isinstance(stamp, str) or not stamp:
        return None
    bucket, seconds = transcript_sort_key(stamp)
    return float(seconds) if bucket == 0 else None


def _tool_words(tool: Any, purpose: Any, redact: Callable[[str], str]) -> str:
    # Each part redacted alone, as the approval card redacts it: a pattern held to
    # a line's start (``key=value``) misses a credential the join moves off it.
    tool_text = redact(str(tool or "").strip())
    purpose_text = redact(str(purpose or "").strip())
    if tool_text and purpose_text:
        return f"{tool_text} · {purpose_text}"
    return tool_text or purpose_text


def pending_permission_rows(
    slot: Any,
    rows: Sequence[Mapping[str, Any]],
    parse_cls_meta: Callable[[str], dict | None],
) -> list[tuple[Mapping[str, Any], dict, str, str]]:
    """The permission rows of the slot's own live approvals, newest first.

    ``(row, meta, request_id, request_mid)``, one per request. A row counts only
    while its future is open and its delivery is the one the future was raised
    for, so a stale unresolved row from an earlier turn describes nothing.
    """
    futures = slot._approval_futures
    live = sum(1 for future in futures.values() if not future.done())
    found: list[tuple[Mapping[str, Any], dict, str, str]] = []
    if not live:
        return found
    seen: set[str] = set()
    for message in reversed(rows):
        if message.get("role") != "permission":
            continue
        meta = parse_cls_meta(message.get("cls") or "") or {}
        if meta.get("resolved"):
            continue
        request_id = meta.get("approval_id", meta.get("request_id", ""))
        if not isinstance(request_id, str) or request_id in seen:
            continue
        future = futures.get(request_id)
        if future is None or future.done():
            continue
        request_mid = slot.approval_instance(request_id, message)
        if not request_mid:
            continue
        seen.add(request_id)
        found.append((message, meta, request_id, request_mid))
        if len(found) >= live:
            break
    return found


def approval_asks(
    slot: Any,
    permission_rows: Sequence[tuple[Mapping[str, Any], dict, str, str]],
    coordinator_pending: Iterable[Mapping[str, Any]],
    *,
    fallback_ts: float,
    redact: Callable[[str], str],
) -> list[Ask]:
    """One ask per pending approval of *slot*: its own futures, then coordinator records.

    A live future with no permission row still counts, at *fallback_ts* and with
    no words, so an approval is never left out.
    """
    asks = [
        Ask(
            (KIND_APPROVAL,),
            1,
            _or(ask_epoch(message.get("ts")), fallback_ts),
            clean_words(
                _tool_words(message.get("content"), meta.get("tool_purpose"), redact), redact
            ),
            request_mid,
        )
        for message, meta, _request_id, request_mid in permission_rows
    ]
    live = sum(1 for future in slot._approval_futures.values() if not future.done())
    asks.extend(Ask((KIND_APPROVAL,), 1, fallback_ts, "", None) for _ in range(live - len(asks)))
    for record in coordinator_pending:
        asks.append(
            Ask(
                (KIND_APPROVAL,),
                1,
                _or(_record_epoch(record.get("ts")), fallback_ts),
                clean_words(
                    _tool_words(record.get("tool"), record.get("tool_purpose"), redact), redact
                ),
                None,
            )
        )
    return asks


def question_asks(slot: Any, *, fallback_ts: float, redact: Callable[[str], str]) -> list[Ask]:
    """One ask per open question card of *slot*, worded by its first question.

    Any truthy record set counts, as ``needs_input`` counts it; one that is not a
    mapping of records is one card with no words.
    """
    pending = getattr(slot, "_question_pending", None)
    if not pending:
        return []
    asks: list[Ask] = []
    for record in pending.values() if isinstance(pending, dict) else (None,):
        questions = record.get("questions") if isinstance(record, dict) else None
        first = questions[0] if isinstance(questions, list) and questions else {}
        text = (
            (first.get("question") or first.get("header") or "") if isinstance(first, dict) else ""
        )
        ts = _record_epoch(record.get("ts")) if isinstance(record, dict) else None
        asks.append(
            Ask((KIND_QUESTION,), 1, _or(ts, fallback_ts), clean_words(str(text), redact), None)
        )
    return asks


def reply_asks(
    rows: Sequence[Mapping[str, Any]],
    reply: ReplyAsk | None,
    *,
    open_now: bool,
    is_system_notice: Callable[[str, dict], bool],
) -> list[Ask]:
    """The newest reply's asks, while still open and only when a person is the one it asks.

    *rows* are the ones the summary that found *reply* read. Who started the
    turn is read only for an open ask, outside the memoized summary, so a turn
    with a long row before its reply does not push the summary past its bound.
    """
    if reply is None or not open_now:
        return []
    ts = ask_epoch(reply.ts)
    if ts is None or not started_by_person(rows, reply, is_system_notice):
        return []
    kinds = tuple(
        kind
        for kind, present in ((KIND_OPTIONS, reply.options), (KIND_LABELLED, bool(reply.labels)))
        if present
    )
    return [Ask(kinds, len(reply.labels) or 1, ts, reply.words, reply.row_id)]


def needs_you_of(asks: Sequence[Ask]) -> dict[str, Any] | None:
    """The row's ``needs_you`` value for *asks*; ``None`` when there are none.

    The oldest ask names the words and row; of asks that began at the same
    instant, the first listed does.
    """
    if not asks:
        return None
    oldest = min(asks, key=lambda ask: ask.ts)
    return {
        "count": sum(ask.number for ask in asks),
        "oldest_ts": oldest.ts,
        "first_words": oldest.words,
        "row_id": oldest.row_id,
        "kinds": [kind for kind in KINDS if any(kind in ask.kinds for ask in asks)],
    }


def _asks_of_value(value: Any) -> list[Ask]:
    """A ``needs_you`` value back as one ask, so more asks can be added to it."""
    if not isinstance(value, dict):
        return []
    return [
        Ask(
            tuple(value["kinds"]),
            int(value["count"]),
            float(value["oldest_ts"]),
            str(value["first_words"]),
            value["row_id"],
        )
    ]


def settle_crew_rows(state: Any, rows: list[dict[str, Any]]) -> None:
    """Apply the Crew Mode half of the rule to a whole stamped list, in place.

    Runs after ``crew_mode.stamp_rows``. A worker's row (``crew_root``) keeps
    only its approvals and question cards; while its conductor is a row of the
    list, its approvals move onto the conductor's row, the one card they can be
    decided on. A fault costs this step only: every row keeps its own value.
    """
    try:
        _settle_crew_rows(state, rows)
    except Exception:
        logger.warning("needs_you not settled for Crew Mode workers", exc_info=True)


def _settle_crew_rows(state: Any, rows: list[dict[str, Any]]) -> None:
    # circular imports: the state facade and slot_projection import this module,
    # and crew_mode is loaded here as late as slots_frame loads it.
    from kiro_crew import crew_mode
    from kiro_crew.dashboard.slot_projection import summary_rows
    from kiro_crew.dashboard.state import _redact, parse_cls_meta

    roots = {row.get("key"): row for row in rows if row.get(crew_mode.ROOT_FIELD) is True}
    slots = getattr(state, "_slots", None) or {}
    for row in rows:
        root = row.get(crew_mode.WORKER_FIELD)
        if not root or row.get(FIELD) is None:
            continue
        slot = slots.get(row.get("key"))
        if slot is None:
            continue
        fallback = _or(ask_epoch(row.get("last_activity_ts")), 0.0)
        coordinator = getattr(slot, "_coordinator_approvals", None)
        approvals = approval_asks(
            slot,
            pending_permission_rows(slot, summary_rows(slot), parse_cls_meta),
            coordinator(slot.key) if coordinator is not None else (),
            fallback_ts=fallback,
            redact=_redact,
        )
        target = roots.get(root)
        if target is not None and target is not row and approvals:
            # The permission row is the worker's, not in the conductor's transcript.
            moved = [ask._replace(row_id=None) for ask in approvals]
            target[FIELD] = needs_you_of([*_asks_of_value(target.get(FIELD)), *moved])
            approvals = []
        row[FIELD] = needs_you_of(
            [*approvals, *question_asks(slot, fallback_ts=fallback, redact=_redact)]
        )


def _record_epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _or(value: float | None, fallback: float) -> float:
    return fallback if value is None else value
