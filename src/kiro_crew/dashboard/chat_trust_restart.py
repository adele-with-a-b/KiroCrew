"""Keeping a chat's approval mode across a gateway exit the user did not choose.

The rule is ``kiro_crew.kept_trust``'s (kirodotdev/KiroCrew#12275): a chat's Trust
and Read modes (``_ChatSlot._trust`` / ``._trust_reads``) stay on across every exit
the user did not choose -- the in-place re-exec behind the dashboard Restart button
and an applied update, ``kirocrew restart``, a service manager's stop or restart, a
crash relaunch -- and only an explicit stop (``kirocrew stop``, the desktop Quit,
Ctrl+C) clears them.

* :func:`record` writes which open chats hold a mode to :data:`RECORD_FILE` in the
  sealed ``kept-trust`` directory. The periodic flush, the shutdown save and every
  approval-mode change (``apply_approval_mode``) call it, off the event loop, so a
  revoke is on disk before its request returns and a crash loses at most one flush
  interval of any other change. It writes nothing until :func:`restore` ran, so a
  flush during the open-tab restore cannot erase the record before it is used.
* :func:`arm` runs at boot before the open-tab restore. It reads the previous
  process's record, or nothing when that process ended on an explicit stop
  (``kept_trust.take_owner_stop``).
* :func:`restore` runs after the open-tab restore and gives each recorded chat it
  rebuilt the mode it held, capped by the approval-modes policy as every mode is
  (the slot's own setters refuse a denied mode), and audits each one. Then this
  process may write.
* :func:`report` then posts one bell note and one chat row for each recorded chat
  that still came back without its mode (the policy denies it now).

The record is a grant, so only the gateway may write it: the directory is sealed
read-only in the OS sandbox and write-fenced at the file-tool gate, so a sandboxed
shell cannot plant an entry for its own chat and wait for a restart. The record an
older build wrote, :data:`LEGACY_RECORD_FILE` at the writable data-home root, is
never read as a grant: it feeds the notice only.

What comes back is exactly what was held: Trust or Read, whether a Trust was a
conductor card's ("Trust this crew member", ``_trust_card_scoped``), and whether
the chat follows its creator (``_posture_inherited``). An app-owned chat
(``_app``) is never recorded: its mode is the app's to set, and the app sets it
again. An incognito or temporary chat does not come back, so it is not recorded
either. A closed or deleted chat leaves the open set, so the next write drops it.
A chat created after boot is never looked up in the record.

At report time a chat is skipped when a live auto-approve grant reaches it (a
declared one boot armed again before the restore), and a crewmate's conductor
chat or worker is skipped when its Profile (``agents.<name>.approval_mode``) seeds
the recorded mode back at its next turn (``crewmate_approval.seed_for_turn``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import kept_trust
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: The record of which chats hold an approval mode, inside ``kept_trust.KEPT_DIR``.
RECORD_FILE = kept_trust.CHATS_FILE

#: Where an older build wrote its notice-only record: the writable data-home root.
#: Read once for the notice, then deleted; never a grant.
LEGACY_RECORD_FILE = "chat_trust_held.json"

_RECORD_VERSION = 2
_LEGACY_RECORD_VERSION = 1

#: One key and a few flags per trusted chat is far below this. The bound keeps a
#: file this module did not write from turning the boot read into an unbounded one.
_MAX_RECORD_BYTES = 256 * 1024

#: The per-state writer :func:`arm` attaches; its absence is what keeps
#: :func:`record` from writing before the previous record was read.
_WRITER_ATTR = "_chat_trust_record"

#: The chat row's ``meta["kind"]``, a wire value the dashboard keys its localized
#: card on (``TrustResetNoticeCard.tsx``). The card's one button sends the row's
#: ``meta["approval_mode"]`` through the picker's own route, so the person, not
#: this module, turns the mode back on. The row text stays the fallback for
#: readers that cannot localize.
ROW_KIND = "trust_reset_notice"

#: The modes a restart carries over, with the name the chat's approval picker shows.
_MODE_LABELS = {"trust": "Trust", "trust_reads": "Read"}

#: The SEL caller of a restored mode, so the audit trail tells it from a person's pick.
_RESTORE_CALLER = "gateway:restart_restore"


@dataclass(frozen=True, order=True)
class HeldMode:
    """One open chat that held an approval mode, and how it held it."""

    key: str
    mode: str
    #: A conductor card's Trust: it covers the chat and its subagents, not the
    #: sessions it opens (``approval_posture.trust_carries``).
    card_scoped: bool = False
    #: The chat follows its creator's next mode change (``approval_inheritance``).
    inherited: bool = False


@dataclass(frozen=True)
class Armed:
    """What :func:`arm` read: the modes to restore, and the notice-only entries."""

    restore: tuple[HeldMode, ...] = ()
    #: From :data:`LEGACY_RECORD_FILE`, which any sandboxed process could write.
    notice: tuple[HeldMode, ...] = ()

    def every_chat(self) -> tuple[HeldMode, ...]:
        """Each recorded chat once, for :func:`report`; the sealed record wins."""
        keys = {entry.key for entry in self.restore}
        return self.restore + tuple(entry for entry in self.notice if entry.key not in keys)


@dataclass
class _Writer:
    lock: threading.Lock = field(default_factory=threading.Lock)
    # What this process last wrote. ``None`` until the first write, so that write
    # always lands, even an empty set that removes the previous process's record.
    written: frozenset[HeldMode] | None = None
    # Set by :func:`restore`. Until then a write would record the chats as the
    # open-tab restore rebuilds them, without their modes, and erase the record.
    ready: bool = False


def record_path() -> Path:
    """The record's path under the data home."""
    return kept_trust.kept_path(RECORD_FILE)


def _held_mode(slot: Any) -> str:
    if getattr(slot, "_trust", False) is True:
        return "trust"
    if getattr(slot, "_trust_reads", False) is True:
        return "trust_reads"
    return ""


def _held_now(state: Any) -> frozenset[HeldMode]:
    """The open chats that hold a mode now, among those a restart restores."""
    building = getattr(state, "_slots_under_construction", ())
    held: set[HeldMode] = set()
    for key, slot in list(getattr(state, "_slots", {}).items()):
        # The same set ``open_slots.json`` keeps: an incognito or temporary chat
        # does not come back, so there is nothing to give a mode to.
        if getattr(slot, "memory_mode", "persistent") != "persistent" or key in building:
            continue
        # An app sets its own chat's mode again after a restart (module docstring).
        if getattr(slot, "_app", ""):
            continue
        mode = _held_mode(slot)
        if mode:
            held.add(
                HeldMode(
                    key,
                    mode,
                    card_scoped=mode == "trust"
                    and getattr(slot, "_trust_card_scoped", False) is True,
                    inherited=getattr(slot, "_posture_inherited", False) is True,
                )
            )
    return frozenset(held)


def record(state: Any) -> None:
    """Write which open chats hold an approval mode, when that set changed.

    Writes nothing before :func:`restore` ran on *state*, so this process cannot
    overwrite the previous process's record before its modes are given back. Never
    raises: a lost write costs the owner a mode, never a grant they took away,
    because a revoke's write is retried by every later flush.
    """
    writer = getattr(state, _WRITER_ATTR, None)
    if not isinstance(writer, _Writer) or not writer.ready:
        return
    try:
        # Sampled under the lock, so the flush thread, the shutdown thread and a
        # mode change cannot land an older sample after a newer one.
        with writer.lock:
            held = _held_now(state)
            if held == writer.written:
                return
            path = record_path()
            if path.parent.is_symlink():
                # The sealed directory replaced by a link: write nothing through it.
                logger.warning("Not recording approval modes: %s is a link", path.parent)
                return
            if held:
                path.parent.mkdir(mode=0o700, exist_ok=True)
                chats = [
                    {
                        "key": h.key,
                        "mode": h.mode,
                        "card_scoped": h.card_scoped,
                        "inherited": h.inherited,
                    }
                    for h in sorted(held)
                ]
                payload = json.dumps({"version": _RECORD_VERSION, "chats": chats})
                atomic_write(path, payload, mode=0o600)
            else:
                path.unlink(missing_ok=True)
            writer.written = held
    except Exception:
        logger.warning("Could not record the chats holding an approval mode", exc_info=True)


def _read_record(path: Path) -> str | None:
    """The record's text, or ``None`` when there is no regular file to read.

    Opened by descriptor, non-blocking and without following a link, and read only
    when ``fstat`` says it is a regular file under the bound: a FIFO planted at the
    path would otherwise block the boot read forever. Windows has neither flag, so
    a link is refused there by the ``is_symlink`` check before the open.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES:
            return None
        return os.read(fd, _MAX_RECORD_BYTES).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def _parse(text: str, version: int) -> tuple[HeldMode, ...]:
    try:
        data = json.loads(text)
    except ValueError:
        return ()
    if not isinstance(data, dict) or data.get("version") != version:
        return ()
    chats = data.get("chats")
    if not isinstance(chats, list):
        return ()
    held: dict[str, HeldMode] = {}
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        key, mode = chat.get("key"), chat.get("mode")
        if not (isinstance(key, str) and key and mode in _MODE_LABELS) or key in held:
            continue
        held[key] = HeldMode(
            key,
            mode,
            card_scoped=mode == "trust" and chat.get("card_scoped") is True,
            inherited=chat.get("inherited") is True,
        )
    return tuple(held.values())


def _take_legacy() -> tuple[HeldMode, ...]:
    """The older build's notice record, deleted once read whatever it holds."""
    path = config_dir() / LEGACY_RECORD_FILE
    try:
        text = _read_record(path)
        return _parse(text, _LEGACY_RECORD_VERSION) if text is not None else ()
    finally:
        path.unlink(missing_ok=True)


def arm(state: Any) -> Armed:
    """Read the previous process's record. Nothing, after an explicit stop.

    Run once per boot, off the event loop, before the open-tab restore. The record
    stays on disk until :func:`restore` lets this process write its own, so a crash
    before the restore finishes loses nothing. After an explicit stop
    (``kept_trust.take_owner_stop``) the record is already gone and no notice is
    owed: the person chose to end the modes. Never raises.
    """
    restore: tuple[HeldMode, ...] = ()
    notice: tuple[HeldMode, ...] = ()
    owner_stop = kept_trust.take_owner_stop()
    if not owner_stop:
        try:
            text = _read_record(record_path())
            if text is not None:
                restore = _parse(text, _RECORD_VERSION)
        except Exception:
            logger.warning("Could not read the chat approval-mode record", exc_info=True)
    try:
        notice = _take_legacy()
    except Exception:
        logger.debug("Could not take the older chat approval-mode record", exc_info=True)
    if owner_stop:
        notice = ()
    setattr(state, _WRITER_ATTR, _Writer())
    return Armed(restore=restore, notice=notice)


def _apply(slot: Any, entry: HeldMode) -> bool:
    """Give a just-restored *slot* the mode *entry* recorded. True when it took.

    The ``_trust`` / ``_trust_reads`` setters refuse a mode the approval-modes
    policy denies, so a restore never holds more than a person's pick could.
    """
    if (
        getattr(slot, "_trust", False)
        or getattr(slot, "_trust_reads", False)
        or getattr(slot, "_trust_scope", "")
    ):
        # Something already gave this chat a posture; a restore never replaces one.
        return False
    if entry.mode == "trust":
        slot._trust = True
        if slot._trust is not True:
            return False
        slot._trust_card_scoped = entry.card_scoped
    else:
        slot._trust_reads = True
        if slot._trust_reads is not True:
            return False
    slot._posture_inherited = entry.inherited
    return True


def _yolo_reaches(state: Any, slot: Any) -> bool:
    try:
        return state.is_yolo_active() is True and state.yolo_reaches(slot) is True
    except Exception:
        return False


def _settle_restored(state: Any, restored: list[tuple[HeldMode, Any]]) -> None:
    """The restore's blocking half: session policies and the audit.

    Writes ``auto`` where ``apply_approval_mode`` would: a restored Trust that
    reaches subagents, and every chat a kept YOLO grant
    (``safety_grants._restore_prior_grant``, restored earlier in boot) reaches, so
    the subagents those chats spawn inherit it as they did before the exit. Never
    writes ``""``: a chat that held nothing has the default already, and another
    owner (an app, a crewmate seed) may have set its policy since.
    """
    from kiro_crew.dashboard.approval_posture import trust_reaches_subagents
    from kiro_crew.dashboard.chat_utils import effective_session_key

    sessions = getattr(state, "sessions", None)
    if sessions is not None:
        for slot in list(getattr(state, "_slots", {}).values()):
            try:
                if trust_reaches_subagents(slot) or _yolo_reaches(state, slot):
                    sessions.set_approval_policy(effective_session_key(slot), "auto")
            except Exception:
                logger.warning(
                    "Could not settle the session policy of chat %s", slot.key, exc_info=True
                )
    for entry, slot in restored:
        try:
            from kiro_crew.sel import sel

            sel().log_api_access(
                caller=_RESTORE_CALLER,
                operation=f"mode_change:{entry.mode}",
                outcome="restored",
                resources=slot.key,
            )
        except Exception:
            logger.warning("Could not audit the restored mode of chat %s", entry.key, exc_info=True)


async def restore(state: Any, held: tuple[HeldMode, ...]) -> None:
    """Give each recorded chat the restore rebuilt the mode it held. Never raises.

    Run after the open-tab restore and before any turn is drained, so a restored
    chat's first tool call already runs under its mode. The policy verdict is made
    final first, off the loop, so a ceiling still installing is not read as a deny.
    Only then may this process write its own record (:func:`record`).
    """
    try:
        if held:
            from kiro_crew.approval_mode_policy import ensure_resolved

            await asyncio.to_thread(ensure_resolved)
        restored: list[tuple[HeldMode, Any]] = []
        for entry in held:
            slot = state.get_slot(entry.key)
            if slot is None or str(getattr(slot, "_app", "") or ""):
                continue
            if getattr(slot, "memory_mode", "persistent") != "persistent":
                continue
            if _apply(slot, entry):
                restored.append((entry, slot))
        await asyncio.to_thread(_settle_restored, state, restored)
        if restored and hasattr(state, "push_slots_update"):
            state.push_slots_update()
    except Exception:
        logger.warning("Could not restore the chats' approval modes", exc_info=True)
    finally:
        writer = getattr(state, _WRITER_ATTR, None)
        if not isinstance(writer, _Writer):
            writer = _Writer()
            setattr(state, _WRITER_ATTR, writer)
        writer.ready = True
    await asyncio.to_thread(record, state)


def note_title(mode: str) -> str:
    """The bell note's title for a chat that came back from a restart without *mode*."""
    return f"{_MODE_LABELS[mode]} mode was turned off by a restart"


def note_body(mode: str, chat_title: str) -> str:
    """The bell note's body: what ended, what it means for running work, the remedy."""
    label = _MODE_LABELS[mode]
    return (
        f'Kiro Crew restarted and "{chat_title}" came back without {label} mode, so its '
        f"tools ask for approval again. A loop or task "
        f"left running in it waits for that approval. Turn {label} mode back on in the "
        "chat if you still want it."
    )


def row_text(mode: str) -> str:
    """The system row shown in the chat itself."""
    label = _MODE_LABELS[mode]
    return (
        f"{label} mode was turned off because Kiro Crew restarted, so tools ask for "
        f"approval again. Turn {label} mode back on if you still want it."
    )


def _covers(granted: str, mode: str) -> bool:
    """Whether a chat holding *granted* still has *mode*: Trust covers Read."""
    return granted == mode or (granted == "trust" and mode in _MODE_LABELS)


def _auto_approved(state: Any, slot: Any) -> bool:
    """Whether a live auto-approve grant reaches a dashboard turn in *slot*.

    The same test the tool gate makes (``chat_turn.tool_approval``). Boot arms a
    declared grant (``agent.dangerously_skip_permissions``, ``gateway --approval
    yolo``) again before the restore, so a chat it reaches asks for nothing. The
    note would then be false, and its remedy harmful: Trust picked on one chat
    ends a declared grant for every other chat (``chat_api.slot_approval``).
    Never raises; an unanswerable check counts as no grant, so the note is posted.
    """
    try:
        return state.is_yolo_active() is True and state.yolo_reaches(slot) is True
    except Exception:
        logger.debug("Could not read the auto-approve grant for a restored chat", exc_info=True)
        return False


def _owed(state: Any, slot: Any, mode: str) -> bool:
    """Whether restored *slot* asks for approval where its recorded *mode* did not."""
    return not _covers(_held_mode(slot), mode) and not _auto_approved(state, slot)


def _profile_modes(slots: list[Any]) -> dict[str, str]:
    """The mode each crewmate chat in *slots* takes from its Profile, by slot key.

    The mode ``crewmate_approval.seed_for_turn`` gives a crewmate's conductor chat
    or worker at its next turn: ``agents.<name>.approval_mode``, an unset one read
    as ``MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE``, clamped to what the policy allows.
    A chat no crewmate owns is absent. MAY BLOCK (the config, each session's
    record, a policy install): call it off the event loop. Never raises.
    """
    modes: dict[str, str] = {}
    try:
        from kiro_crew.approval_mode_policy import clamp_session_mode, ensure_resolved
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.sections import MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
        from kiro_crew.dashboard.chat_utils import slot_history_key
        from kiro_crew.dashboard.crewmate_approval import crewmate_name_for
        from kiro_crew.execution_context import read_session_execution

        config = KiroCrewConfig.load()
        for slot in slots:
            try:
                execution = read_session_execution(slot_history_key(slot))
                name = crewmate_name_for(config, execution)
            except Exception:  # noqa: BLE001 - an unreadable record is no crewmate's
                continue
            if not name:
                continue
            if not modes:
                # A final policy answer before the clamp, as the seed waits for one.
                ensure_resolved()
            mode = getattr(config.agents.get(name), "approval_mode", "") or ""
            modes[slot.key] = clamp_session_mode(mode or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE)
    except Exception:
        logger.debug("Could not read the crewmate Profile modes of restored chats", exc_info=True)
    return modes


async def report(state: Any, held: tuple[HeldMode, ...]) -> None:
    """Tell the owner about each recorded chat that came back without its mode.

    One ``agent`` bell note per chat, whose ``meta.slot`` gives the detail panel
    its open-chat action, and one system row in the chat. A chat that is not open,
    that holds the mode again, that a live auto-approve grant reaches, or that is
    a crewmate's chat its Profile seeds the mode back into at its next turn, is
    owed nothing, so a chat :func:`restore` gave its mode back is never told. This
    grants nothing. Never raises: boot must not fail over a notice.
    """
    if not held:
        return
    from kiro_crew.dashboard.state import load_window_off_loop

    lost: list[tuple[HeldMode, Any]] = []
    for entry in held:
        slot = state.get_slot(entry.key)
        if slot is not None and _owed(state, slot, entry.mode):
            lost.append((entry, slot))
    if not lost:
        return
    profile_modes = await asyncio.to_thread(_profile_modes, [slot for _, slot in lost])
    for entry, slot in lost:
        try:
            if state.get_slot(entry.key) is not slot or not _owed(state, slot, entry.mode):
                continue
            # The seed runs once per live slot, so only an unseeded chat gets it back.
            if not getattr(slot, "_member_approval_seeded", True) and _covers(
                profile_modes.get(slot.key, ""), entry.mode
            ):
                continue
            state.notify(
                "agent",
                note_title(entry.mode),
                note_body(entry.mode, slot.display_title),
                meta={"slot": slot.key, "approval_mode": entry.mode},
            )
            # A restored chat's window may still be on disk; appending reads it, so
            # the read happens off the loop first.
            await load_window_off_loop(slot)
            if state.get_slot(entry.key) is slot:
                slot.append(
                    "system",
                    row_text(entry.mode),
                    "msg msg-info",
                    meta={"kind": ROW_KIND, "approval_mode": entry.mode},
                )
        except Exception:
            logger.warning(
                "Could not report the restart-ended approval mode of chat %s",
                entry.key,
                exc_info=True,
            )
