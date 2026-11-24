"""A chat's Trust or Read mode survives a gateway restart, and only the gateway records it.

A restart here is two dashboard states over one data home, as in
``test_chat_trust_restart_notice.py``: the first boots through the gateway's
restore seam, gets its mode through the picker's own route and stops the way a
stopping gateway does (or dies after a flush); the second boots through the same
seam. What comes back is what was held: the mode, a conductor card's narrower
Trust, and whether the chat follows its creator. A revoke is on disk before its
request answers. A chat that left the open set, a chat made after boot and a
record outside the sealed directory get nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_chat_trust_restart_notice import (  # noqa: F401
    KEY,
    RECORD_FILE,
    _boot,
    _booted_with_mode,
    _notes_for,
    _pick,
    _rows_about_mode,
    _settle,
    home_fixture,
)

from kiro_crew.dashboard import chat_persistence, chat_trust_restart
from kiro_crew.dashboard.approval_posture import trust_carries
from kiro_crew.dashboard.chat_utils import effective_session_key


async def _picked(home: Path, mode: str):
    """A booted gateway whose chat got *mode* through ``POST /api/chat/mode``."""
    state = await _boot(home)
    await _pick(state, KEY, mode)
    return state


def _recorded(home: Path) -> list[dict]:
    path = home / RECORD_FILE
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["chats"]


class TestTheModeComesBack:
    @pytest.mark.asyncio
    async def test_a_trusted_chat_is_trusted_again_after_a_restart(self, home: Path) -> None:
        before = await _picked(home, "trust")
        chat_persistence.save_all_slots_to_history(before)

        after = await _boot(home)
        try:
            slot = after.get_slot(KEY)
            assert slot._trust is True
            # What a Trust pick writes for the spawn gate and each subagent.
            after.sessions.set_approval_policy.assert_any_call(effective_session_key(slot), "auto")
            # Back as it was, so the owner is owed no notice.
            assert _notes_for(after) == [] and _rows_about_mode(after) == []
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_it_survives_every_later_restart_too(self, home: Path) -> None:
        state = await _picked(home, "trust")
        for _ in range(2):
            chat_persistence.save_all_slots_to_history(state)
            state = await _boot(home)
            assert state.get_slot(KEY)._trust is True

    @pytest.mark.asyncio
    async def test_a_crash_after_the_periodic_flush_keeps_it(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        # No shutdown save: the process dies after a flush tick.
        before._flush_dirty_slots()

        after = await _boot(home)
        assert after.get_slot(KEY)._trust is True

    @pytest.mark.asyncio
    async def test_read_mode_comes_back_as_read(self, home: Path) -> None:
        before = await _picked(home, "trust_reads")
        chat_persistence.save_all_slots_to_history(before)

        slot = (await _boot(home)).get_slot(KEY)
        assert slot._trust_reads is True and slot._trust is False

    @pytest.mark.asyncio
    async def test_a_card_trust_comes_back_no_wider(self, home: Path) -> None:
        # "Trust this crew member" covers the worker and its subagents, not the
        # sessions it opens; a restart must not turn it into a full Trust.
        before = await _booted_with_mode(home, "trust")
        slot = before.get_slot(KEY)
        slot._trust_card_scoped = True
        slot._posture_inherited = False
        chat_persistence.save_all_slots_to_history(before)

        slot = (await _boot(home)).get_slot(KEY)
        assert slot._trust is True and slot._trust_card_scoped is True
        assert trust_carries(slot) is False
        # A mode a person set on the chat stays its own, not its creator's to change.
        assert slot._posture_inherited is False


class TestAnExplicitStopEndsIt:
    @pytest.mark.parametrize("how", ["kirocrew stop", "ctrl-c", "desktop quit"])
    @pytest.mark.asyncio
    async def test_the_next_boot_restores_nothing_and_tells_nobody(
        self, home: Path, how: str
    ) -> None:
        from kiro_crew import kept_trust

        before = await _picked(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        assert _recorded(home)
        # The user ended Kiro Crew: the stop leaves its mark before the gateway goes.
        assert kept_trust.mark_owner_stop(how) is True

        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is False
            # Ended on purpose, so there is nothing to tell the owner.
            assert _notes_for(after) == [] and _rows_about_mode(after) == []
            assert _recorded(home) == []
        finally:
            await _settle(after)

        # And the boot after that is an ordinary restart again.
        await _pick(after, KEY, "trust")
        assert (await _boot(home)).get_slot(KEY)._trust is True


class TestOnlyWhatWasHeldComesBack:
    @pytest.mark.asyncio
    async def test_a_revoke_is_on_disk_before_a_crash(self, home: Path) -> None:
        before = await _picked(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        assert [c["key"] for c in _recorded(home)] == [KEY]
        # Trust taken away, then the process dies before any flush or save.
        await _pick(before, KEY, "normal")
        assert _recorded(home) == []

        after = await _boot(home)
        assert after.get_slot(KEY)._trust is False
        # Taken away on purpose, so there is nothing to tell the owner.
        assert _notes_for(after) == []

    @pytest.mark.asyncio
    async def test_a_chat_that_left_the_open_set_is_not_recorded(self, home: Path) -> None:
        before = await _picked(home, "trust")
        before._flush_dirty_slots()
        assert _recorded(home)
        # Closed: no longer an open chat, so the next write drops it.
        before._slots.pop(KEY)
        chat_persistence.save_all_slots_to_history(before)
        assert _recorded(home) == []

    @pytest.mark.asyncio
    async def test_a_chat_made_after_boot_is_never_trusted(self, home: Path) -> None:
        # The record names a key no open tab holds; the boot gives it to nobody,
        # and a chat later opened under that key starts asking.
        (home / RECORD_FILE).parent.mkdir(exist_ok=True)
        (home / RECORD_FILE).write_text(
            json.dumps({"version": 2, "chats": [{"key": "chat-9-later", "mode": "trust"}]}),
            encoding="utf-8",
        )
        state = await _boot(home)
        assert state.get_slot("chat-9-later") is None
        assert state.get_or_create_slot("chat-9-later")._trust is False

    @pytest.mark.asyncio
    async def test_the_older_writable_record_grants_nothing(self, home: Path) -> None:
        # An older build's record sat at the data-home root, where any sandboxed
        # shell can write. It still gets the owner a notice, never the mode.
        (home / chat_trust_restart.LEGACY_RECORD_FILE).write_text(
            json.dumps({"version": 1, "chats": [{"key": KEY, "mode": "trust"}]}),
            encoding="utf-8",
        )
        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is False
            assert len(_notes_for(after)) == 1
            assert not (home / chat_trust_restart.LEGACY_RECORD_FILE).exists()
        finally:
            await _settle(after)

    @pytest.mark.skipif(sys.platform == "win32", reason="a directory symlink needs privilege")
    @pytest.mark.asyncio
    async def test_a_link_in_place_of_the_sealed_directory_is_not_read(
        self, home: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        (elsewhere / "chats.json").write_text(
            json.dumps({"version": 2, "chats": [{"key": KEY, "mode": "trust"}]}),
            encoding="utf-8",
        )
        (home / RECORD_FILE).parent.symlink_to(elsewhere, target_is_directory=True)
        assert (await _boot(home)).get_slot(KEY)._trust is False


def test_the_record_is_sealed_from_the_agent() -> None:
    from kiro_crew import kept_trust, sandbox
    from kiro_crew.security import is_sensitive_write_path

    assert kept_trust.KEPT_DIR in sandbox._CREW_READONLY_LEAVES
    assert is_sensitive_write_path(f"~/.kiro/crew/{RECORD_FILE}") is True
