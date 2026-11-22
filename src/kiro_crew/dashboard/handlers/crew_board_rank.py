"""A person's drag on the Crew board: one backlog item's ``plan.rank``.

The dashboard owner orders the backlog; the conductor dispatches its top row next
(``work_plan_vocab.next_pick``). This is the third writer of an item's plan, and it
writes exactly one key: ``rank``. Everything else in the plan stays the conductor's.

Recorded as every ledger write is: the board lock, the cache commit, one
``work/recorded`` entry (actor ``person``, action ``rank``, the committed
``plan``), and the commit undone when that entry does not land.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from kiro_crew import work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard.handlers.work_ledger import (
    _WIDEST_EVENT_TEXT,
    _WIDEST_HEX_ID,
    _WIDEST_STAMP,
    _audit,
    _board_header,
    _board_lock,
    _current_item_and_board,
    _drain_before_cancelling,
    _entry_probe_with_baseline,
    _entry_with_baseline,
    _mark_recorded,
    _refuse_if_dirty,
    _refuse_unrecorded,
    _steer_unit,
)
from kiro_crew.work_ledger import WorkLedgerError
from kiro_crew.work_plan_vocab import merge_plan, rank_value

RANK_CREW_LOG_OFF = "crew_log_off"
RANK_NO_UNIT = "no_crew_log_unit"
RANK_NOT_RECORDED = "not_recorded"
RANK_TOO_LARGE = "item_too_large"

_OPERATION = "crew_board.rank"


async def record_person_rank(
    state: Any, board_key: str, item_id: str, rank: Any
) -> tuple[dict[str, Any] | None, str, str]:
    """Write *rank* into *item_id*'s plan on *board_key*'s board, as the person did.

    Returns ``(plan, "", "")`` once recorded, or ``(None, code, message)``: a store
    refusal keeps its own code (``unknown_item``, ``item_closed``, ``invalid_value``).
    """
    if not crew_log_emit.enabled():
        _audit(board_key, _OPERATION, "denied", resources=item_id, error=RANK_CREW_LOG_OFF)
        return None, RANK_CREW_LOG_OFF, "the crew log is off, so the board takes no writes"
    unit = await asyncio.to_thread(_steer_unit, state, (), board_key)
    if not unit:
        _audit(board_key, _OPERATION, "denied", resources=item_id, error=RANK_NO_UNIT)
        return None, RANK_NO_UNIT, "this board has no crew-log unit to record the move in"
    async with _board_lock(board_key):
        if await _refuse_if_dirty(board_key, board_key, _OPERATION) is not None:
            return None, RANK_NOT_RECORDED, "the board needs a rebuild before it takes writes"
        probe_item, probe_board = await asyncio.to_thread(
            _current_item_and_board, board_key, item_id
        )
        probe, invalid = _entry_probe_with_baseline(
            board_key,
            probe_item,
            probe_board,
            actor="person",
            by=board_key,
            action="rank",
            item_id=item_id,
            generation=_WIDEST_HEX_ID,
            # The plan as the write would store it, so an item the rank pushes past
            # the entry's limit is refused here rather than undone after its commit.
            plan=merge_plan(getattr(probe_item, "plan", None), {"rank": rank_value(rank) or 0}),
            event=_WIDEST_EVENT_TEXT,
            event_kind="plan",
            event_id=_WIDEST_HEX_ID,
            event_ts=_WIDEST_STAMP,
        )
        if invalid is not None or probe is None or not crew_log_emit.work_entry_fits(probe):
            _audit(board_key, _OPERATION, "denied", resources=item_id, error=RANK_TOO_LARGE)
            return None, RANK_TOO_LARGE, "this item's record is too large to move"

        async def _commit_and_record() -> tuple[dict[str, Any] | None, str, str]:
            snapshot = await asyncio.to_thread(
                functools.partial(work_ledger.snapshot_for_write, board_key, item_id=item_id)
            )
            try:
                result = await asyncio.to_thread(
                    work_ledger.apply_person_rank, board_key, item_id, rank
                )
            except WorkLedgerError as exc:
                _audit(board_key, _OPERATION, "denied", resources=item_id, error=exc.code)
                return None, exc.code, str(exc)
            item, event = result["item"], result["event"]
            board = await asyncio.to_thread(work_ledger.read_conductor, board_key)
            established, board = await _board_header(board_key, board)
            landed = established and await asyncio.to_thread(
                crew_log_emit.on_work_recorded,
                unit,
                _entry_with_baseline(
                    board_key,
                    item,
                    board,
                    actor="person",
                    by=board_key,
                    action="rank",
                    item_id=item.item_id,
                    generation=getattr(board, "generation", None) or None,
                    plan=item.plan,
                    event=event.text,
                    event_kind="plan",
                    event_id=event.id or None,
                    event_ts=event.ts or None,
                ),
            )
            if not landed:
                await _refuse_unrecorded(board_key, _OPERATION, item.item_id, board_key, snapshot)
                return None, RANK_NOT_RECORDED, "the move was not recorded; it was undone"
            await asyncio.to_thread(_mark_recorded, board_key, item.item_id)
            _audit(board_key, _OPERATION, "ok", resources=item.item_id)
            return dict(item.plan or {}), "", ""

        return await _drain_before_cancelling(_commit_and_record())
