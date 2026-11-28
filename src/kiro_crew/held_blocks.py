"""The fixed text a session's model context already holds.

A Conductor chat is woken many times inside one model context, and each wake is
composed with the same fixed text: the how-to lines of a work-ledger wake, the patrol
instruction, the ``[seats]`` instruction and a ``[seats]`` fact line that may not
have moved; a resumed turn is composed with the critical rules, the response
preferences and the reply-format rules. The harness replays its own conversation,
so a second copy adds nothing. This module is the record of what one session's
context holds, and the one place that takes a held block out of a wake.

Who opens a record: the dashboard turn's build (``chat_turn/prompt_assembly``), for a
Conductor chat on a harness whose compactions Crew sees
(``LLMProvider.reports_compaction``). A session with no record is told nothing here
and every function answers "not held", so any other chat and any other harness keep
the text they have always had, byte for byte.

What ends a record's knowledge, each one a point where the context it describes is
gone:

* a compaction: one Crew ran or saw inside a turn ends in
  ``SessionManager.mark_needs_reinjection``, which calls :func:`drop`; one that only
  set the re-send flag (a compact that timed out, ended with no status or completed
  late) ends the record at the next build, as the turn that re-sends does;
* a context that starts with no history (a new conversation, a reset, a context-cap
  trim) or a turn that re-sends after a compaction: the build passes ``fresh=True``;
* ``/clear`` and ``/compact``: the build calls :func:`drop`;
* a gateway restart: the record is in memory only.

Every one of those errs toward sending a block again, never toward leaving one out.

A turn records what its prompt carries when it is built, and the turn's end settles
it: kept when the harness holds the prompt, rolled back when it does not
(:func:`settle_turn`). A wake steered into a running turn is held apart until the
turn reads it (:func:`steer_read`): a later steer into the same turn leaves its
lines out, and the turn's end forgets every steer it never read, because that text
then waits as a queue card or was dropped.

The wake blocks are matched as whole lines against the owners' own constants, so a
line a worker or a person wrote is never taken out unless it is that constant.

Three blocks are not fixed text, and the record keeps the last content sent of each,
so they go out again when it changes: the ``[seats]`` fact line, the project and
runtime lines of a turn (``PROJECT``, ``RUNTIME``; ``context_assembly``), and the
session-ledger snapshot a fired wake's body opens with (``LEDGER``). The snapshot is
kept row by row. A context's first wake carries it whole; a later one carries nothing
when no row differs, and otherwise the rows that do under
``LEDGER_CHANGES_HEADER``, which says in its own words that every other row stands.

The snapshot is never looked for in a text. The fire hands over the body it composed
(:func:`lean_snapshot`), which opens with the snapshot or has none, and what the wake
then carries waits in the record under the wake's own text (:func:`keep_fired`) until
the build of the turn that text starts (:func:`fired_snapshot`). So a block that only looks like a
snapshot, in a monitor's evidence or in a message someone pasted, is neither
shortened nor recorded, and a steer, a queue card and a run of queued messages never
carry one. A wake that started no turn changes nothing: the next one is measured
against the same record.
"""

from __future__ import annotations

import hashlib
import itertools
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field

#: Sessions with a record at once; the oldest is dropped, and a dropped session's
#: next wake carries every block again.
MAX_SESSIONS = 256
#: Steers one session's running turn may hold unread. Past it the oldest is
#: forgotten, so its lines go out again with the next steer.
MAX_UNREAD_STEERS = 32

#: Record names of the blocks a turn start carries.
CRITICAL_RULES = "critical-rules"
RESPONSE_PREFERENCES = "response-preferences"
REPLY_FORMAT_RULES = "reply-format-rules"

#: Record names of a wake's lines. ``SEATS`` is the fact line: its value is the last
#: one sent, so a line with the same numbers is not sent twice.
WAKE_SUFFIX = "wake-suffix"
WAKE_READ_POINTER = "wake-read-pointer"
PATROL = "patrol"
SEATS = "seats"
SEATS_INSTRUCTION = "seats-instruction"

#: Record names of the two rail lines a turn carries: the value is the last one sent,
#: so a line goes out again only when its content changes.
PROJECT = "project"
RUNTIME = "runtime"

#: The session-ledger snapshot a fired wake's body opens with
#: (``session_ledger.render_snapshot``). ``LEDGER`` says the context holds one, and
#: each row it was sent has a place under ``_LEDGER_ROW``: its label for ``goal``,
#: ``phase``, ``next`` and each ``artifact <key>``, so a new value takes the old
#: one's place; its own text for a ``tried`` row, which is an entry in a list, and
#: for a row cut inside its label.
LEDGER = "ledger"
_LEDGER_ROW = LEDGER + "/"
_LEDGER_BY_TEXT = _LEDGER_ROW + ":"
_LEDGER_ONE_ROW = frozenset({"goal", "phase", "next"})
_LEDGER_ARTIFACT = "artifact "
#: What a later wake carries in place of a snapshot whose rows the context holds:
#: this line, the rows that are new or differ, and one line for each labelled row
#: the context holds and the snapshot does not have. The line says what a reader of it alone must know:
#: that it is a part, that the rest stands, and where the whole is.
LEDGER_CHANGES_HEADER = (
    "[work ledger — changed rows only; every row of the last snapshot that is not "
    "named here still stands; session_ledger_read shows it whole]"
)
#: Opens the line for a row that is gone. A snapshot cut at its ceiling cannot tell
#: a removed row from one pushed past the cut, so it says only what it knows.
LEDGER_ROW_REMOVED = "removed: "
LEDGER_ROW_CUT_OFF = "no longer shown, the snapshot is cut at its size limit: "
#: Fired wakes one session may have waiting for their turn's build. Past it the
#: oldest is forgotten, and with it what the record knew of the snapshot.
MAX_FIRED_SNAPSHOTS = 4

#: Lines of which a text keeps the newest copy; of any other the first.
_KEEP_NEWEST = frozenset({SEATS, SEATS_INSTRUCTION})
#: Blocks with more than one wording (the reply-format rules of a typed turn and of
#: a wake differ by a paragraph): the context holds every wording it was sent, so a
#: chat that alternates between the two is not sent one of them at every switch.
_EVERY_WORDING = frozenset({CRITICAL_RULES, RESPONSE_PREFERENCES, REPLY_FORMAT_RULES})


@dataclass(frozen=True)
class FiredSnapshot:
    """What the snapshot of one fired wake leaves a context holding.

    *sent* is what the wake's text carries (place to row, ``""`` for a row said
    gone, ``LEDGER`` with a whole block), *whole* the whole block the same way, and
    *owed* the whole block's text when the wake carries less than it.
    """

    sent: Mapping[str, str]
    whole: Mapping[str, str]
    owed: str = ""


@dataclass
class _Record:
    blocks: dict[str, str] = field(default_factory=dict)
    turn_undo: dict[str, str | None] = field(default_factory=dict)
    unread_steers: OrderedDict[str, dict[str, str]] = field(default_factory=OrderedDict)
    left_out: tuple[str, ...] = ()
    #: The open turn's number, unique in the process; see :func:`turn_of`.
    turn: int = 0
    #: Fired wakes whose turn is not built yet, by the wake's text.
    fired: OrderedDict[str, FiredSnapshot] = field(default_factory=OrderedDict)
    #: False until a build opens the record: a fire on a session with none keeps
    #: only *fired* here, and every reader takes the session as having no record.
    opened: bool = True


_turns = itertools.count(1)
_lock = threading.Lock()
_records: OrderedDict[str, _Record] = OrderedDict()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _place(name: str, value: str) -> str:
    """Where *name*'s digest *value* is kept: one place per wording, or one in all."""
    return f"{name}/{value}" if name in _EVERY_WORDING else name


def _record(key: object) -> _Record | None:
    """*key*'s record; the caller holds the lock. A key that is no string has none."""
    record = _records.get(key) if isinstance(key, str) and key else None
    return record if record is not None and record.opened else None


def begin_turn(key: str, *, fresh: bool) -> None:
    """Open *key*'s record for the turn being built; *fresh* forgets what it held.

    *fresh* is a context that cannot hold the earlier copies: one that starts with
    no history, or a turn that re-sends after a compaction.
    """
    if not isinstance(key, str) or not key:
        return
    with _lock:
        record = _records.pop(key, None)
        if record is None:
            record = _Record()
        _records[key] = record
        record.opened = True
        if fresh:
            record.blocks.clear()
        record.unread_steers.clear()
        record.turn_undo.clear()
        record.left_out = ()
        record.turn = next(_turns)
        while len(_records) > MAX_SESSIONS:
            _records.popitem(last=False)


def drop(key: str) -> None:
    """*key*'s context is gone, or the chat has no record: every block goes out again."""
    with _lock:
        if isinstance(key, str):
            _records.pop(key, None)


def tracked(key: str) -> bool:
    """Whether *key* has a record, which is what turns any saving on."""
    with _lock:
        return _record(key) is not None


def turn_of(key: str) -> int:
    """The number of the turn *key*'s record is open for; 0 with no record.

    A build reads it before its first blocking step and hands it to :func:`note`.
    A build runs on a worker and outlives a turn that was stopped while it ran;
    what it carries was never sent, so its late :func:`note` must not land in the
    record of the turn that came after.
    """
    with _lock:
        record = _record(key)
        return record.turn if record is not None else 0


def holds(key: str, name: str, text: str) -> bool:
    """Whether *key*'s context holds exactly *text* as its block *name*."""
    if not key or not text:
        return False
    with _lock:
        record = _record(key)
        value = _digest(text)
        return record is not None and record.blocks.get(_place(name, value)) == value


def note(key: str, blocks: Mapping[str, str], *, turn: int | None = None) -> None:
    """Record that the prompt being built for *key* carries *blocks* (name to text).

    *turn* is the :func:`turn_of` the build read when it started: a build whose
    turn is over by now records nothing. :func:`settle_turn` undoes the writes
    when the harness did not keep the prompt.

    An empty text forgets its block.
    """
    if not key or not blocks:
        return
    with _lock:
        record = _record(key)
        if record is None or (turn is not None and record.turn != turn):
            return
        _note(record, blocks)


def _note(record: _Record, blocks: Mapping[str, str]) -> None:
    """Write *blocks* to *record*, undoably; the caller holds the lock.

    A whole ledger snapshot (``LEDGER``) takes the place of every row held. Rows
    without it change the snapshot the context holds, and are not taken by a record
    that holds none: its next wake is whole.
    """
    whole = LEDGER in blocks
    if whole:
        for place in [p for p in record.blocks if p.startswith(_LEDGER_ROW) and p not in blocks]:
            record.turn_undo.setdefault(place, record.blocks.pop(place))
    for name, text in blocks.items():
        if name.startswith(_LEDGER_ROW) and not whole and LEDGER not in record.blocks:
            continue
        value = _digest(text) if text else None
        place = _place(name, value or "")
        if record.blocks.get(place) == value:
            continue
        record.turn_undo.setdefault(place, record.blocks.get(place))
        if value is None:
            del record.blocks[place]
        else:
            record.blocks[place] = value


def settle_turn(key: str, *, kept: bool) -> None:
    """The turn on *key* ended; *kept* says the harness holds the prompt it sent.

    A steer the turn never read is forgotten either way.
    """
    if not key:
        return
    with _lock:
        record = _record(key)
        if record is None:
            return
        if not kept:
            for name, prior in record.turn_undo.items():
                if prior is None:
                    record.blocks.pop(name, None)
                else:
                    record.blocks[name] = prior
        record.turn_undo.clear()
        record.unread_steers.clear()


def seats_silent(key: str) -> None:
    """The board has no seat fact now, so the next fact line is news whatever it says."""
    with _lock:
        record = _record(key)
        if record is not None:
            record.blocks.pop(SEATS, None)


def _fixed_lines() -> dict[str, str]:
    """Each fixed wake line the gateway writes, to its record name."""
    # Imported here: these owners import the dashboard and the session layers,
    # and the session layer imports this module.
    from kiro_crew import conductor_patrol, ledger_wake
    from kiro_crew.dashboard import patrol_seats
    from kiro_crew.probes import work_ledger

    lines = {
        work_ledger.WAKE_SUFFIX: WAKE_SUFFIX,
        ledger_wake.WAKE_READ_POINTER: WAKE_READ_POINTER,
        patrol_seats.SEATS_INSTRUCTION: SEATS_INSTRUCTION,
    }
    lines.update(dict.fromkeys(conductor_patrol.SHIPPED_PATROL_MESSAGES, PATROL))
    return lines


def _row_place(row: str) -> str:
    """Where the snapshot row *row* is kept: by its label, or by its own text."""
    label, colon, _value = row.partition(": ")
    if colon and (label in _LEDGER_ONE_ROW or label.startswith(_LEDGER_ARTIFACT)):
        return _LEDGER_ROW + label
    return _LEDGER_BY_TEXT + _digest(row)


def _ledger_changes(
    block: list[str], held: Mapping[str, str]
) -> tuple[list[str], dict[str, str]] | None:
    """The whole snapshot *block* (its lines) as what differs from the rows *held*.

    Returns the lines to send, none when nothing differs, and what they leave a
    context holding (place to row, ``""`` for a row said gone). ``None`` when the
    changes would be no shorter than the block, which then goes out whole.
    """
    from kiro_crew.session_ledger import snapshot_is_cut

    rows = block[1:]
    places = {_row_place(row) for row in rows}
    changed = [row for row in rows if held.get(_row_place(row)) != _digest(row)]
    absent = [
        place
        for place in held
        if place.startswith(_LEDGER_ROW)
        and not place.startswith(_LEDGER_BY_TEXT)
        and place not in places
    ]
    if not changed and not absent:
        return [], {}
    gone = LEDGER_ROW_CUT_OFF if snapshot_is_cut("\n".join(block)) else LEDGER_ROW_REMOVED
    lines = [
        LEDGER_CHANGES_HEADER,
        *changed,
        *(gone + place[len(_LEDGER_ROW) :] for place in absent),
    ]
    if len("\n".join(lines)) >= len("\n".join(block)):
        return None
    sent = {_row_place(row): row for row in changed}
    sent.update(dict.fromkeys(absent, ""))
    return lines, sent


def lean_snapshot(key: str, body: str) -> tuple[str, FiredSnapshot | None]:
    """A fired wake's *body* with the ledger snapshot it opens with as *key* needs it.

    *body* is what ``compose_nudge_body`` returned: the snapshot, an empty line and
    the loop's message, or the message alone. The snapshot is read at that place
    and nowhere else. Whole for a context the record does not say holds one;
    otherwise the rows that differ, or nothing when none does.

    Returns the body to send and what its snapshot leaves a context holding, for
    :func:`keep_fired` once the wake starts a turn; ``None`` when the body has no
    snapshot. Nothing is written here, so a wake that starts no turn changes nothing.
    """
    from kiro_crew.session_ledger import SNAPSHOT_HEADER

    block, gap, message = body.partition("\n\n")
    lines = block.split("\n")
    if lines[0] != SNAPSHOT_HEADER or not gap:
        return body, None
    whole = {LEDGER: lines[0], **{_row_place(row): row for row in lines[1:]}}
    with _lock:
        record = _record(key)
        held = dict(record.blocks) if record is not None else {}
    changes = _ledger_changes(lines, held) if LEDGER in held else None
    if changes is None:
        return body, FiredSnapshot(sent=whole, whole=whole)
    rows, sent = changes
    lean = "\n".join(rows) + gap + message if rows else message
    return lean, FiredSnapshot(sent=sent, whole=whole, owed=block)


def keep_fired(key: str, text: str, snapshot: FiredSnapshot) -> None:
    """The wake *text* is starting a turn: keep *snapshot* for that turn's build.

    *snapshot* is what :func:`lean_snapshot` returned for the body inside *text*.
    A session with no record gets one that is not open: it holds this and nothing
    else, until a build opens it or drops it.
    """
    if not isinstance(key, str) or not key or not text:
        return
    with _lock:
        record = _records.get(key)
        if record is None:
            record = _records[key] = _Record(opened=False)
            while len(_records) > MAX_SESSIONS:
                _records.popitem(last=False)
        record.fired.pop(text, None)
        record.fired[text] = snapshot
        while len(record.fired) > MAX_FIRED_SNAPSHOTS:
            _, lost = record.fired.popitem(last=False)
            if lost.owed:
                # Its wake carried less than the whole and may yet reach the
                # context unrecorded.
                _forget_ledger(record)


def _forget_ledger(record: _Record) -> None:
    """*record* stops vouching for any snapshot row: the next wake carries it whole."""
    for place in [p for p in record.blocks if p == LEDGER or p.startswith(_LEDGER_ROW)]:
        del record.blocks[place]
        record.turn_undo.pop(place, None)


def fired_snapshot(key: str, text: str, *, fresh: bool) -> str:
    """Record the snapshot a fired wake brings the turn being built for *key*.

    Called by the build of a turn a fire started, after :func:`begin_turn`, with
    the turn's text: the wake as the fire sent it, whatever the build put around
    it. *fresh* is a context that holds nothing: it is recorded as holding the
    whole snapshot, and the whole block is returned when the wake carried less, for
    the build to send ahead of the request. Otherwise what the wake carries is
    recorded and ``""`` returned. :func:`settle_turn` undoes it when the harness
    did not keep the prompt.

    A text no fire answers for records nothing. Should it carry a snapshot header
    all the same, the record forgets every row, which sends the next one whole.
    """
    from kiro_crew.session_ledger import SNAPSHOT_HEADER

    if not key or not text:
        return ""
    with _lock:
        record = _record(key)
        if record is None:
            return ""
        wake = next((sent for sent in reversed(record.fired) if sent in text), None)
        if wake is None:
            if SNAPSHOT_HEADER in text or LEDGER_CHANGES_HEADER in text:
                _forget_ledger(record)
            return ""
        snapshot = record.fired.pop(wake)
        _note(record, snapshot.whole if fresh else snapshot.sent)
    return snapshot.owed if fresh else ""


def _lean_each(
    texts: list[str], held: Mapping[str, str]
) -> tuple[list[str], dict[str, str], list[str]]:
    """*texts*, read as one prompt, with one copy of each fixed line and no *held* one.

    Returns the texts, the fixed lines they still carry (name to line) and the
    lines left out because *held* has them, in their order. A text with nothing to
    take out comes back as the same string.
    """
    from kiro_crew.dashboard.patrol_seats import SEATS_TAG

    fixed = _fixed_lines()
    split = [text.split("\n") for text in texts]
    names = [
        [fixed.get(line) or (SEATS if line.startswith(SEATS_TAG + " ") else "") for line in lines]
        for lines in split
    ]
    newest = {
        name: (part, at)
        for part, part_names in enumerate(names)
        for at, name in enumerate(part_names)
        if name in _KEEP_NEWEST
    }
    out: list[str] = []
    carried: dict[str, str] = {}
    left_out: list[str] = []
    seen: set[str] = set()
    for part, (text, lines) in enumerate(zip(texts, split, strict=True)):
        kept: list[str] = []
        removed = False
        gap = False
        for at, (line, name) in enumerate(zip(lines, names[part], strict=True)):
            if name:
                repeat = newest[name] != (part, at) if name in _KEEP_NEWEST else name in seen
                seen.add(name)
                if repeat or held.get(name) == _digest(line):
                    if not repeat:
                        left_out.append(line)
                    removed = gap = True
                    continue
                carried[name] = line
            elif gap and not line and (not kept or not kept[-1]):
                # The blank line that set a removed block apart.
                continue
            if line:
                gap = False
            kept.append(line)
        if not removed:
            out.append(text)
            continue
        while kept and not kept[-1]:
            kept.pop()
        out.append("\n".join(kept))
    return out, carried, left_out


def _lean(text: str, held: Mapping[str, str]) -> tuple[str, dict[str, str], list[str]]:
    lean, carried, left_out = _lean_each([text], held)
    return lean[0], carried, left_out


def lean_wake(key: str, text: str, *, starts_turn: bool = False) -> str:
    """The wake *text* without the fixed lines *key*'s context already holds.

    *starts_turn* is the fire's own turn start: the lines left out are kept for
    :func:`owed_wake_lines`, because only that turn's build learns whether its
    context is a new one. Without it the text is a steer, and the lines of the
    steers the running turn has not read yet count as held too. A session with no
    record gets *text* back unchanged.
    """
    if not key or not text:
        return text
    with _lock:
        record = _record(key)
        if record is None:
            return text
        held = dict(record.blocks)
        if not starts_turn:
            for unread in record.unread_steers.values():
                held.update(unread)
    lean, _carried, left_out = _lean(text, held)
    if starts_turn:
        with _lock:
            record = _record(key)
            if record is not None:
                record.left_out = tuple(left_out)
    return lean


def owed_wake_lines(key: str) -> str:
    """The lines the last turn-starting wake left out, taken once; ``""`` if none.

    Read by a wake turn whose context turned out to be new: the lines were left
    out against a context that is gone, so the turn sends them after all.
    """
    with _lock:
        record = _record(key)
        if record is None or not record.left_out:
            return ""
        owed, record.left_out = record.left_out, ()
    return "\n".join(owed)


def note_wake(key: str, text: str) -> None:
    """Record the fixed wake lines the prompt being built for *key* carries."""
    if key and text and tracked(key):
        note(key, _lean(text, {})[1])


def steer_sent(key: str, text: str) -> None:
    """The harness accepted the wake *text* as a steer into *key*'s running turn."""
    if not key or not text:
        return
    carried = {name: _digest(line) for name, line in _lean(text, {})[1].items()}
    with _lock:
        record = _record(key)
        if record is None or not carried:
            return
        record.unread_steers[_digest(text)] = carried
        while len(record.unread_steers) > MAX_UNREAD_STEERS:
            record.unread_steers.popitem(last=False)


def steer_read(key: str, text: str) -> None:
    """*key*'s turn read the steered wake *text*: its fixed lines are in the context."""
    if not key or not text:
        return
    carried = {name: _digest(line) for name, line in _lean(text, {})[1].items()}
    with _lock:
        record = _record(key)
        if record is None:
            return
        record.unread_steers.pop(_digest(text), None)
        for name, value in carried.items():
            # Settled as read, so the turn's own undo must not take it back.
            record.turn_undo.pop(name, None)
            record.blocks[name] = value


def one_copy(texts: list[str]) -> list[str]:
    """*texts*, about to be joined as one prompt, with one copy of each fixed wake line.

    Wakes that waited together as queue cards were each written for a turn of
    their own. Read as one message they need each how-to line once and the newest
    ``[seats]`` lines only. A text with nothing to take out is the same string.
    """
    return _lean_each(texts, {})[0] if len(texts) > 1 else texts
