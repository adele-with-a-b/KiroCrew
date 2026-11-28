"""A dashboard turn's part in the record of what its session's context holds.

``kiro_crew.held_blocks`` is the record; this module is the three points where a
dashboard turn touches it, kept out of ``chat_turn/prompt_assembly`` so each rule
is stated once (the names carry ``held_`` because the chat runner binds them):

* :func:`held_open_for_turn`, before the build: which chats have a record at all, and
  when the record forgets;
* :func:`held_note_prompt`, after the build: what the prompt carries;
* :func:`held_settle`, at the turn's end: whether the harness holds that prompt.

:func:`stopped_prompt_kept` is the fact the last one rests on, and the turn's
re-injection settle reads it too.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import held_blocks
from kiro_crew.agent_sdk import TURN_STOP_REASON_CANCELLED as STOP_REASON_CANCELLED
from kiro_crew.conductor_step import is_conductor_slot
from kiro_crew.essential_delivery import RECEIPT_RESETTING_COMMANDS

#: A dashboard turn's locals (``chat_turn.turn_locals._ChatTurn``). Only the chat
#: runner imports a ``chat_turn`` owner, so the type is not named here.
_ChatTurn = Any


def held_open_for_turn(
    turn: _ChatTurn,
    *,
    context_is_new: bool,
    provider_has_history: bool,
    needs_reinjection: bool,
    wake_text: str = "",
) -> str:
    """Open the session's record for this build, or drop it; return the wake lines owed.

    A record is kept for a Conductor chat on a harness whose compactions Crew sees
    (``reports_compaction is True``, the send-once rule of harness-parity H6). Any
    other chat has none, so its text is what it has always been.

    The record forgets when this turn's context cannot hold the earlier copies: it
    starts with no history, or it re-sends after a compaction. A wake the fire had
    already shortened against the context that is gone then gets its lines back:
    they are returned here, for the build to put before the request.

    *wake_text* is the turn's own text. When a fire started the turn with it, the
    ledger snapshot that fire composed is recorded here, and a context that cannot
    hold the earlier one gets the whole block back with the lines, set apart.
    """
    key = turn.session_key
    if not (
        is_conductor_slot(turn.slot, key)
        and getattr(turn.client, "reports_compaction", False) is True
    ):
        held_blocks.drop(key)
        return ""
    fresh = (context_is_new and not provider_has_history) or needs_reinjection
    owed = held_blocks.owed_wake_lines(key) if fresh and turn._directive_self_wake else ""
    held_blocks.begin_turn(key, fresh=fresh)
    if turn._directive_self_wake:
        snapshot = held_blocks.fired_snapshot(key, wake_text, fresh=fresh)
        owed = "\n\n".join(part for part in (owed, snapshot) if part)
    return owed


def held_forget_on_command(turn: _ChatTurn) -> None:
    """A slash command that empties or rewrites the conversation ends the record."""
    if turn.first_word in RECEIPT_RESETTING_COMMANDS:
        held_blocks.drop(turn.session_key)


def held_note_prompt(turn: _ChatTurn, *texts: str) -> None:
    """Record the fixed wake lines the built prompt carries."""
    held_blocks.note_wake(turn.session_key, "\n".join(text for text in texts if text))


def stopped_prompt_kept(turn: _ChatTurn) -> bool:
    """Whether the harness holds the prompt of this stopped turn.

    The claude contract. A prompt Claude Code started is written to its
    conversation at once, and a stop adds an interruption row after it; the next
    prompt is answered with both in view. Kiro CLI is the opposite (it writes a
    prompt only once the model answered it;
    ``turn_stats._native_session_lacks_the_prompt``), so this is asked of the
    provider's own identity and is true nowhere else.

    A prompt Claude Code had not started is not kept. While Claude Code is busy,
    for one with a reply it began on its own, a new prompt waits in its queue; a
    stop then answers the prompt ``cancelled`` and removes it from the queue, so
    it never reaches the conversation. The adapter's answer tells the two apart:
    it carries the turn's token counts, zero or not, only for a prompt Claude Code
    started (``AcpEvent.answer_reported_usage``). An ending without them, an
    ending Crew wrote itself included, counts as not kept: the prompt's text is
    owed again.

    A conversation this turn is about to discard holds nothing.
    """
    return (
        getattr(turn, "_stop_reason", "") == STOP_REASON_CANCELLED
        and getattr(turn, "_saw_terminal_event", False) is True
        and getattr(turn, "_terminal_synthetic", True) is False
        and getattr(turn, "_terminal_reported_usage", False) is True
        and getattr(turn, "needs_conversation_discard", True) is False
        and getattr(getattr(turn, "client", None), "is_claude_backend", False) is True
    )


def held_prompt_kept(turn: _ChatTurn) -> bool:
    """Whether the harness holds this turn's prompt: it landed, or was stopped and kept."""
    return bool(getattr(turn, "_turn_landed", False)) or stopped_prompt_kept(turn)


def held_settle(turn: _ChatTurn) -> None:
    """Keep or roll back what this turn's build recorded. Never raises."""
    try:
        held_blocks.settle_turn(turn.session_key, kept=held_prompt_kept(turn))
    except Exception:  # noqa: BLE001 - a record must never cost the turn its exit
        held_blocks.drop(turn.session_key)
