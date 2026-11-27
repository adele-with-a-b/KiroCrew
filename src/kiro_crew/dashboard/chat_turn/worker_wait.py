"""The turn-end half of the reports the gateway files for a bound worker (``work_ledger_waits``).

A turn that ended normally hands its reply to the ledger side, which files a
``question`` report when the reply leaves one for the person and the session is a
bound crew worker. Read here: the reply, and whether a person typing in this chat
started the turn (they are looking at the reply, so nothing is filed), and the calls a
safety policy blocked in it (``_refusal_reasons``, redacted when the chat showed them).

A turn that ended on an error hands the error instead (``_note_model_road_down``): the
ledger side files a ``blocked`` report when it says the chat's model account cannot be
used, and nothing for any other error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.history import HUMAN_TURN_META_KEY

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import STOP_REASON_END_TURN, logger
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn


def _worker_person_turn(turn: _ChatTurn) -> bool:
    """Whether the row this turn answers is one a person typed (``HUMAN_TURN_META_KEY``)."""
    row = turn._current_message
    meta = row.get("meta") if isinstance(row, dict) else None
    return isinstance(meta, dict) and bool(meta.get(HUMAN_TURN_META_KEY))


def _note_worker_question(turn: _ChatTurn) -> None:
    """Hand a normally ended turn's reply to ``work_ledger.note_turn_end_question``.

    A cancelled, refused or leaked turn hands nothing. Never raises: a turn's end
    must not fail on the ledger.
    """
    try:
        if turn._stop_reason != STOP_REASON_END_TURN or turn._noticed_leak:
            return
        text = turn.assistant_text
        if not text:
            return
        from kiro_crew.dashboard.handlers import work_ledger

        refused = [f"{title} — {why}" for title, why in turn._refusal_reasons or []]
        work_ledger.note_turn_end_question(
            turn.state,
            turn.slot.key,
            text,
            person_turn=_worker_person_turn(turn),
            refused=refused,
        )
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("worker wait: turn-end check failed", exc_info=True)


def _note_model_road_down(turn: _ChatTurn, exc: BaseException) -> None:
    """Hand a turn that ENDED on *exc* to ``work_ledger.note_model_road_down``.

    Called by the arms that end a turn on an error, never by one that retries,
    resets or replays it, so a failure a retry absorbed files nothing. An error that
    is not the model road (``llm_helpers.model_road_down_cause`` answers ``""``) hands
    nothing, and the ledger side files nothing for a session that is not a bound
    worker. Never raises: a turn's end must not fail on the ledger.
    """
    try:
        from kiro_crew.llm_helpers import model_road_down_cause

        cause = model_road_down_cause(exc)
        if not cause:
            return
        from kiro_crew.dashboard.handlers import work_ledger

        work_ledger.note_model_road_down(turn.state, turn.slot.key, cause)
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("model road: turn-end check failed", exc_info=True)
