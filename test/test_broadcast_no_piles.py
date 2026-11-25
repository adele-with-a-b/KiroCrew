"""A broadcast never leaves a pile of stale notes on a busy worker.

Two rules, both through the real delivery (only the steer RPC is stubbed):

* A broadcast that QUEUES replaces the same sender's older unread broadcast entry on
  that target, and the queued text says how many it replaced. Nothing else is ever
  replaced: another sender's note, a person's message, a send by name, a card a
  person has open, or one already in the transcript.
* ``mode`` left out takes, per target, the default a plain ``session_send`` takes:
  the caller's own busy worker is cut into unless a person drives that turn, and
  any other target queues. A broadcast answers nothing, so it never spends the
  reply cut-in a target's asking turn is owed.
"""

from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import broadcast_supersede, queue_age
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.queue_edit_hold import EDIT_HOLD_KEY
from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES

_STEER = "kiro_crew.dashboard.chat_delivery.steer_into_running_turn"


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _setup(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    child = state.get_or_create_slot("chat-2")
    child._created_by = caller.key
    _busy(child)
    return state, caller, child


def _broadcast(state, caller, message, mode="queue", **kw):
    kw.setdefault("all_created", "targets" not in kw)
    return asyncio.run(
        sc.broadcast_to_targets(
            state,
            caller_session_key=slot_history_key(caller),
            message=message,
            mode=mode,
            **kw,
        )
    )


def _row(out):
    (row,) = out["results"]
    return row


def _contents(slot):
    return [entry["content"] for entry in slot._queue]


class TestANewerNoteReplacesTheOlderUnreadOne:
    def test_the_queue_holds_one_note_that_counts_the_ones_it_replaced(self, tmp_path):
        state, caller, child = _setup(tmp_path)
        first = _row(_broadcast(state, caller, "main moved to a1"))
        assert "replaced" not in first
        first_id = child._queue[0]["id"]

        state.broadcast_ws = MagicMock()
        second = _row(_broadcast(state, caller, "main moved to b2"))
        assert second["replaced"] == 1
        (only,) = _contents(child)
        assert "main moved to b2" in only and "main moved to a1" not in only
        assert "This replaces 1 earlier unread broadcast note from this session" in only
        # The envelope still opens the text, so the bubble drops it by position.
        assert only.startswith("[sent by session ")
        # Open dashboards drop the old card, and no composer gets its text back.
        assert ("queue_cancel", {"slot": child.key, "queue_id": first_id}) in [
            c.args for c in state.broadcast_ws.call_args_list
        ]

        third = _row(_broadcast(state, caller, "main moved to c3"))
        assert third["replaced"] == 2
        (only,) = _contents(child)
        assert "This replaces 2 earlier unread broadcast notes" in only
        assert "main moved to c3" in only

    def test_nothing_but_the_senders_own_unread_broadcast_is_replaced(self, tmp_path):
        state, caller, child = _setup(tmp_path)
        _broadcast(state, caller, "main moved to a1")
        (own,) = child._queue
        # A session_send by name from the same caller, through the real path.
        asyncio.run(
            sc.send_to_target(
                state,
                caller_session_key=slot_history_key(caller),
                target=child.key,
                message="by name: look at test_x",
                steer=False,
            )
        )

        def _plant(text, *, meta_edit=None, **flags):
            meta = copy.deepcopy(own["meta"])
            if meta_edit:
                meta_edit(meta)
            return child.queue_append(text, meta=meta, **flags)

        def _other_sender(meta):
            meta[sc.SEND_ORIGIN_META_KEY]["slot"] = "chat-9"

        def _other_tab(meta):
            meta[sc.SEND_ORIGIN_META_KEY]["tab"] = "another-tab"

        _plant("from chat-9", meta_edit=_other_sender)
        _plant("from an older occupant of this key", meta_edit=_other_tab)
        _plant("typed by the person", directive_user_origin=True)
        held_id = _plant("open in the person's editor")
        next(e for e in child._queue if e["id"] == held_id)[EDIT_HOLD_KEY] = float("inf")
        written_id = _plant("its row is already written")

        def _written(slot, entry):
            return entry.get("id") == written_id

        with patch("kiro_crew.dashboard.responsive_chat.row_already_written", new=_written):
            row = _row(_broadcast(state, caller, "main moved to b2"))

        assert row["replaced"] == 1
        kept = _contents(child)
        assert not any("main moved to a1" in text for text in kept)
        for text in (
            "by name: look at test_x",
            "from chat-9",
            "from an older occupant of this key",
            "typed by the person",
            "open in the person's editor",
            "its row is already written",
            "main moved to b2",
        ):
            assert any(text in entry for entry in kept), text

    def test_a_replaced_note_makes_room_in_a_full_queue(self, tmp_path):
        state, caller, child = _setup(tmp_path)
        _broadcast(state, caller, "main moved to a1")
        while len(child._queue) < MAX_LIVE_QUEUE_ENTRIES:
            child.queue_append("a person's note", directive_user_origin=True)
        row = _row(_broadcast(state, caller, "main moved to b2"))
        assert row["ok"] and row["replaced"] == 1
        assert len(child._queue) == MAX_LIVE_QUEUE_ENTRIES

    def test_a_note_that_cuts_in_replaces_nothing(self, tmp_path):
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED

        state, caller, child = _setup(tmp_path)
        _broadcast(state, caller, "main moved to a1")
        with patch(_STEER, new=AsyncMock(return_value=STEER_STEERED)):
            row = _row(_broadcast(state, caller, "stop, it is fixed", mode="steer"))
        assert row["steered"] is True and "replaced" not in row
        assert len(child._queue) == 1

    def test_the_replaced_count_sits_beside_the_queue_not_in_the_entry(self, tmp_path):
        """The drain unions entry meta onto the row it writes, so a count kept in
        the meta would ride into the transcript."""
        state, caller, child = _setup(tmp_path)
        _broadcast(state, caller, "one")
        _broadcast(state, caller, "two")
        (entry,) = child._queue
        assert getattr(child, broadcast_supersede.REPLACED_ATTR) == {entry["id"]: 1}
        assert not any("replace" in key for key in entry["meta"])

    def test_the_count_line_follows_the_body(self, tmp_path):
        """A drop notice quotes the entry's start, which must stay the sender's words."""
        state, caller, child = _setup(tmp_path)
        _broadcast(state, caller, "main moved to a1")
        _broadcast(state, caller, "main moved to b2")
        (only,) = _contents(child)
        assert only.index("main moved to b2") < only.index("This replaces 1 earlier")
        assert only.rstrip().endswith("only this one is current.)")


class TestTheNewerNoteTakesTheOlderOnesPlace:
    def test_it_keeps_the_oldest_ones_position(self, tmp_path):
        state, caller, child = _setup(tmp_path)
        _broadcast(state, caller, "main moved to a1")
        child.queue_append("a person's note", directive_user_origin=True)
        state.broadcast_ws = MagicMock()
        _broadcast(state, caller, "main moved to b2")
        first, second = _contents(child)
        assert "main moved to b2" in first and second == "a person's note"
        frames = [c.args for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_reorder"]
        assert frames == [
            ("queue_reorder", {"slot": child.key, "order": [e["id"] for e in child._queue]})
        ]

    def test_it_keeps_the_oldest_ones_wait(self, tmp_path, monkeypatch):
        """The unread wake and ``messages_unread`` count from the first unread
        note: a fresh stamp on every replacement would push the wake back forever."""
        now = [1000.0]
        monkeypatch.setattr(queue_age, "_clock", lambda: now[0])
        state, caller, child = _setup(tmp_path)
        _broadcast(state, caller, "main moved to a1")
        now[0] = 5000.0
        _broadcast(state, caller, "main moved to b2")
        (entry,) = child._queue
        assert child._queued_at == {entry["id"]: 1000.0}

    def test_a_stuck_wait_keeps_one_identity_so_it_wakes_its_sender_once(
        self, tmp_path, monkeypatch
    ):
        """The work-ledger unread wake is keyed on ``oldest_id``: a new id per
        replacing note would be a new wake per broadcast about the same stuck wait."""
        now = [1000.0]
        monkeypatch.setattr(queue_age, "_clock", lambda: now[0])
        state, caller, child = _setup(tmp_path)
        # A person drives the turn, so each note wants it and the wake covers it.
        child._turn_user_sent = True
        _broadcast(state, caller, "main moved to a1", mode=None)
        first_id = child._queue[0]["id"]
        sender = slot_history_key(caller)
        before = queue_age.sender_unread(state, child.key, sender)
        assert before == (1, 0.0, first_id)
        for at, sha in ((3000.0, "b2"), (5000.0, "c3")):
            now[0] = at
            _broadcast(state, caller, f"main moved to {sha}", mode=None)
            (entry,) = child._queue
            assert entry["id"] != first_id
            assert queue_age.sender_unread(state, child.key, sender) == (1, at - 1000.0, first_id)
        # Only the live entry keeps a record.
        assert child._queued_as == {entry["id"]: first_id}
        assert child._queued_at == {entry["id"]: 1000.0}

    def test_it_keeps_a_replaced_notes_wish_for_the_turn(self, tmp_path):
        state, caller, child = _setup(tmp_path)
        child._turn_user_sent = True
        _broadcast(state, caller, "main moved to a1", mode=None)
        child._turn_user_sent = False
        _broadcast(state, caller, "main moved to b2", mode="queue")
        (entry,) = child._queue
        assert entry["meta"][queue_age.STEER_WANTED_META] is True

    def test_a_failing_push_still_reports_the_note_that_landed(self, tmp_path):
        state, caller, child = _setup(tmp_path)
        _broadcast(state, caller, "main moved to a1")

        def _cancel_fails(kind, *args, **kwargs):
            if kind == "queue_cancel":
                raise RuntimeError("socket gone")

        state.broadcast_ws = MagicMock(side_effect=_cancel_fails)
        state.push_slots_update = MagicMock()
        row = _row(_broadcast(state, caller, "main moved to b2"))
        assert row["ok"] is True and row["replaced"] == 1
        (only,) = _contents(child)
        assert "main moved to b2" in only
        # The failing frame does not keep the session list from being pushed.
        assert state.push_slots_update.call_count >= 1


class TestALeftOutModeTakesTheSendDefault:
    def test_the_callers_own_busy_worker_is_cut_into(self, tmp_path):
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED

        state, caller, child = _setup(tmp_path)
        with patch(_STEER, new=AsyncMock(return_value=STEER_STEERED)) as steer:
            out = _broadcast(state, caller, "rebase before you push", mode=None)
        assert out["mode"] == "default"
        row = _row(out)
        assert row["steered"] is True and row["steerDefault"] == "manager"
        assert steer.await_count == 1

    def test_a_turn_the_person_drives_keeps_the_queue(self, tmp_path):
        state, caller, child = _setup(tmp_path)
        child._turn_user_sent = True
        with patch(_STEER, new=AsyncMock()) as steer:
            row = _row(_broadcast(state, caller, "rebase before you push", mode=None))
        assert steer.await_count == 0
        assert row["steered"] is False and row["heldForPerson"] is True
        (entry,) = child._queue
        # Wanted the turn, so the unread wake still covers it.
        assert entry["meta"][queue_age.STEER_WANTED_META] is True

    def test_a_session_the_caller_does_not_manage_queues(self, tmp_path):
        state, caller, _ = _setup(tmp_path)
        other = _busy(state.get_or_create_slot("chat-3"))
        with patch(_STEER, new=AsyncMock()) as steer:
            row = _row(_broadcast(state, caller, "fyi", mode=None, targets=[other.key]))
        assert steer.await_count == 0
        assert row["steered"] is False
        assert "steerDefault" not in row and "heldForPerson" not in row
        assert len(other._queue) == 1

    def test_a_broadcast_never_spends_the_reply_a_target_waits_for(self, tmp_path):
        """A worker that asked its conductor something and waits in that turn is
        owed one cut-in for the answer. A broadcast is not the answer."""
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED

        state, caller, child = _setup(tmp_path)
        other = _busy(state.get_or_create_slot("chat-3"))
        for asker in (child, other):
            asker._awaiting_replies = {caller.key: asker._turn_generation}
        with patch(_STEER, new=AsyncMock(return_value=STEER_STEERED)) as steer:
            out = _broadcast(state, caller, "main moved", mode=None, targets=[child.key, other.key])
        mine, theirs = out["results"]
        # Its own worker by the manager rule; the other one is not cut in as a reply.
        assert mine["steered"] is True and mine["steerDefault"] == "manager"
        assert theirs["steered"] is False and "steerDefault" not in theirs
        assert steer.await_count == 1
        assert child._awaiting_replies == {caller.key: child._turn_generation}
        assert other._awaiting_replies == {caller.key: other._turn_generation}
