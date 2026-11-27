"""Crew closes no crew has settled yet: kept, stored, and made again.

``session_close(crew=)`` reaches the crew's ``DELETE api/chat/slots/{s}`` through
``remote_dispatch_routes._close``. A crew that is not connected, is being
installed, runs another version for a while, or fails the close on its own side
gives no answer that settles it, and the conductor that asked often has no later
turn that may use ``crew=``: the finished worker's chat then stays open on that
crew with nobody left to close it. Such a close is kept here
(``caller -> {(instance id, crew slot)}``), stored beside the dispatch record
(``work_ledger.write_crew_closes``, the same file shape and the same hidden
directory), and made again by the background puller (:func:`retry`, from
``remote_puller.run_once``) until the crew answers: closed, or already gone.

The dispatch record keeps the chat meanwhile, so the target fence still admits it
and ``session_status`` lists it as ``closing``
(``crew_workers_remote.status_rows``). Every change runs under
``remote_dispatch_routes.cap_lock``. The stored copy is loaded once per gateway
state; while it cannot be read nothing new is kept, and the close answers the
crew's error as it did before.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from kiro_crew import session_ledger, work_ledger
from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: ``caller slot key -> {(instance_id, peer slot key)}``, per gateway state.
_KEPT: "weakref.WeakKeyDictionary[Any, dict[str, set[tuple[str, str]]]]" = (
    weakref.WeakKeyDictionary()
)

#: The gateway states whose kept closes already hold what the data home stored.
_LOADED: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _kept(state: "DashboardState") -> dict[str, set[tuple[str, str]]]:
    kept = _KEPT.get(state)
    if kept is None:
        kept = _KEPT[state] = {}
    return kept


async def load(state: "DashboardState") -> bool:
    """Fold the stored closes into *state*'s, once per gateway state; whether it holds them.

    The dispatch record's own rules (``remote_dispatch_routes.load_record``): a slot
    key of one plain segment, no more than the cap per caller, and a store that is
    there but cannot be read is never read as empty.
    """
    if state in _LOADED:
        return True
    try:
        stored = await asyncio.to_thread(work_ledger.read_crew_closes)
    except (work_ledger.WorkLedgerError, OSError):
        logger.warning("kept crew closes unreadable; not loaded", exc_info=True)
        return False
    if state in _LOADED:
        return True
    kept = _kept(state)
    for caller, pairs in stored.items():
        held = kept.setdefault(caller, set())
        for instance_id, slot in pairs:
            if len(held) >= routes.MAX_CREW_SLOTS_PER_CALLER:
                break
            if routes._slot_ok(slot):
                held.add((instance_id, slot))
        if not held:
            kept.pop(caller, None)
    _LOADED.add(state)
    return True


async def _store(state: "DashboardState", caller: str) -> bool:
    pairs = sorted(_kept(state).get(caller, ()))
    try:
        await asyncio.to_thread(work_ledger.write_crew_closes, caller, pairs)
    except (work_ledger.WorkLedgerError, OSError):
        logger.warning("kept crew closes not stored for %s", caller, exc_info=True)
        return False
    return True


def _discard(state: "DashboardState", caller: str, pair: tuple[str, str]) -> bool:
    held = _kept(state).get(caller)
    if held is None or pair not in held:
        return False
    held.discard(pair)
    if not held:
        _kept(state).pop(caller, None)
    return True


async def keep(state: "DashboardState", caller: str, instance_id: str, slot: str) -> bool:
    """Keep *caller*'s close of *slot* on *instance_id*; whether it is kept and stored.

    ``False`` (the cap is reached, or the store cannot be read or written) keeps
    nothing: a close this hub would forget at its next restart is not promised.
    """
    if not await load(state):
        return False
    pair = (instance_id, slot)
    async with routes.cap_lock(state):
        held = _kept(state).setdefault(caller, set())
        if pair in held:
            return True
        if len(held) >= routes.MAX_CREW_SLOTS_PER_CALLER:
            return False
        held.add(pair)
        if not await _store(state, caller):
            _discard(state, caller, pair)
            return False
    from kiro_crew.dashboard.handlers import remote_puller

    remote_puller.ensure_running(state)
    return True


async def kept_or_raise(
    state: "DashboardState",
    caller: str,
    instance_id: str,
    slot: str,
    exc: routes.CrewCallError,
) -> dict[str, Any]:
    """The receipt for a close that is now kept, else *exc* raised as it was."""
    if not await keep(state, caller, instance_id, slot):
        raise exc
    return {
        "target": slot,
        "crew": instance_id,
        "closed": False,
        "kept": True,
        "why": exc.message,
        "why_code": exc.code,
    }


async def forget(state: "DashboardState", caller: str, instance_id: str, slot: str) -> None:
    """Drop a kept close whose chat is closed or gone. Run under ``cap_lock``.

    A drop that cannot be stored comes back at the next restart, is made once
    more, and is dropped when the crew answers that the chat is gone.
    """
    if await load(state) and _discard(state, caller, (instance_id, slot)):
        await _store(state, caller)


#: The refusal for a ``send`` to a chat whose close is kept.
TARGET_CLOSING = "crew_target_closing"


async def refuse_send_while_closing(
    state: "DashboardState", caller: str, instance_id: str, slot: str
) -> None:
    """Raise ``crew_target_closing`` when *caller*'s close of *slot* is kept.

    The close lands whenever its crew answers, so a message sent meanwhile would
    start work that close then cuts off. The conductor closed the chat: it reads,
    stops and closes it, and opens a new one for new work.
    """
    await load(state)
    if (instance_id, slot) in (_KEPT.get(state) or {}).get(caller, ()):
        raise routes.CrewCallError(
            409,
            TARGET_CLOSING,
            "you closed that crew session and the hub closes it once its crew answers, "
            "so it takes no more messages; open a new one with session_create",
        )


async def kept_closes(
    state: Any, callers: Iterable[str] | None = None
) -> list[tuple[str, str, str]]:
    """``(caller, instance id, crew slot)`` for each kept close, by *callers* only when given."""
    try:
        weakref.ref(state)
    except TypeError:
        return []
    await load(state)
    kept = _KEPT.get(state) or {}
    wanted = kept if callers is None else dict.fromkeys(callers)
    return sorted(
        (caller, instance_id, slot)
        for caller in wanted
        for instance_id, slot in kept.get(caller, ())
    )


async def retry(state: "DashboardState") -> list[tuple[str, str]]:
    """Make every kept close again, once. Never raises.

    Answers the ``(instance id, crew slot)`` pairs the crew settled (closed, or
    already gone). A crew that gave no answer is not asked again in this run, so one
    crew that is away costs one wait, not one per chat. A close the crew answers
    with a refusal stays kept and is made again on the next run.
    """
    from kiro_crew.dashboard.handlers import remote_ingest

    try:
        pairs = await kept_closes(state)
        # A close that lands also drops the chat from the dispatch record, which
        # is stored only once it was loaded; one that cannot be read waits.
        if not pairs or not await routes.load_record(state):
            return []
        mgr = routes._manager(state)
    except Exception:  # noqa: BLE001 - a fault here skips this run
        logger.debug("kept crew closes: nothing retried", exc_info=True)
        return []
    silent: set[str] = set()
    settled: list[tuple[str, str]] = []
    for caller, instance_id, slot in pairs:
        if instance_id in silent:
            continue
        try:
            result = await asyncio.wait_for(
                routes._close(
                    state, mgr, caller, instance_id, slot, session_ledger.ledger_key(caller)
                ),
                remote_ingest.PULL_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            silent.add(instance_id)
        except routes.CrewCallError as exc:
            if exc.code == "crew_slot_not_found":
                settled.append((instance_id, slot))
            else:
                logger.info("kept close of %s on %s refused (%s)", slot, instance_id, exc.code)
        except Exception:  # noqa: BLE001 - one close must never stop the rest
            logger.warning("kept close of %s on %s failed", slot, instance_id, exc_info=True)
        else:
            if result.get("kept"):
                silent.add(instance_id)
            else:
                settled.append((instance_id, slot))
                routes._audit(caller, "close", "success", crew=instance_id)
    return settled
