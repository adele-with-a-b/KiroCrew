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

#: Lines of which a text keeps the newest copy; of any other the first.
_KEEP_NEWEST = frozenset({SEATS, SEATS_INSTRUCTION})
#: Blocks with more than one wording (the reply-format rules of a typed turn and of
#: a wake differ by a paragraph): the context holds every wording it was sent, so a
#: chat that alternates between the two is not sent one of them at every switch.
_EVERY_WORDING = frozenset({CRITICAL_RULES, RESPONSE_PREFERENCES, REPLY_FORMAT_RULES})


@dataclass
class _Record:
    blocks: dict[str, str] = field(default_factory=dict)
    turn_undo: dict[str, str | None] = field(default_factory=dict)
    unread_steers: OrderedDict[str, dict[str, str]] = field(default_factory=OrderedDict)
    left_out: tuple[str, ...] = ()
    #: The open turn's number, unique in the process; see :func:`turn_of`.
    turn: int = 0


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
    return _records.get(key) if isinstance(key, str) and key else None


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
    """
    if not key or not blocks:
        return
    with _lock:
        record = _record(key)
        if record is None or (turn is not None and record.turn != turn):
            return
        for name, text in blocks.items():
            value = _digest(text)
            place = _place(name, value)
            if record.blocks.get(place) == value:
                continue
            record.turn_undo.setdefault(place, record.blocks.get(place))
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
