"""A sender learns how long its queued message has waited, and is woken when it sits.

``queue_age`` keeps each queue entry's enqueue instant beside the queue and answers
"how many, and how old is the oldest" for one sender; the ledger read shows it on the
worker's row, and the work-ledger probe wakes the conductor once a message that
wanted the running turn has sat unread past ``ledger_wake.UNREAD_WAKE_SECS``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import ledger_wake, work_ledger
from kiro_crew.dashboard import queue_age
from kiro_crew.dashboard.session_control_parts.delivery_containment import (
    SEND_ORIGIN_META_KEY,
)

CONDUCTOR = "chat-conductor-q"
WORKER = "chat-worker-q"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(queue_age, "_clock", fake)
    return fake


def _entry(entry_id: str, sender: str = "", *, wanted: bool = False) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if sender:
        meta[SEND_ORIGIN_META_KEY] = {"slot": sender, "tab": "tab-1"}
    if wanted:
        meta[queue_age.STEER_WANTED_META] = True
    return {"id": entry_id, "content": "x", "meta": meta}


def _slot(*entries: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(_queue=list(entries), _queued_at={})


def _queue(slot: SimpleNamespace, entry: dict[str, Any]) -> None:
    slot._queue.append(entry)
    queue_age.note_queued(slot, entry["id"])


class TestTheAge:
    def test_the_oldest_stamped_entry_gives_the_age(self, clock):
        slot = _slot()
        _queue(slot, _entry("q1", f"dashboard_{CONDUCTOR}"))
        clock.now += 300
        _queue(slot, _entry("q2", CONDUCTOR))
        clock.now += 60
        found = queue_age.waits(slot, sender=CONDUCTOR)
        assert found == queue_age.QueueWait(2, 360.0, "q1")
        assert queue_age.oldest_field(slot) == {"queue_oldest_secs": 360}

    def test_a_stamp_is_dropped_once_its_entry_has_left_the_queue(self, clock):
        slot = _slot()
        _queue(slot, _entry("q1"))
        slot._queue.clear()
        _queue(slot, _entry("q2"))
        assert set(slot._queued_at) == {"q2"}

    def test_a_restored_entry_counts_but_gives_no_age(self, clock):
        slot = _slot(_entry("old", CONDUCTOR))
        found = queue_age.waits(slot, sender=CONDUCTOR)
        assert found == queue_age.QueueWait(1, None, "")
        assert queue_age.oldest_field(slot) == {}
        assert queue_age.unread_row(slot, CONDUCTOR) == {"count": 1}

    def test_another_senders_and_the_persons_entries_are_not_this_senders(self, clock):
        slot = _slot()
        _queue(slot, _entry("person"))
        _queue(slot, _entry("peer", "chat-someone-else"))
        assert queue_age.waits(slot, sender=CONDUCTOR) is None
        assert queue_age.waits(slot).count == 2
        assert queue_age.unread_row(slot, CONDUCTOR) is None

    def test_only_a_message_that_wanted_the_turn_is_steer_wanted(self, clock):
        slot = _slot()
        _queue(slot, _entry("by-name", CONDUCTOR))
        clock.now += 10
        _queue(slot, _entry("wanted", CONDUCTOR, wanted=True))
        clock.now += 5
        assert queue_age.waits(slot, sender=CONDUCTOR, steer_wanted=True) == (
            queue_age.QueueWait(1, 5.0, "wanted")
        )
        assert queue_age.unread_row(slot, CONDUCTOR) == {"count": 2, "oldest_secs": 15}

    def test_a_slot_that_cannot_hold_the_record_keeps_its_queue(self):
        class _Frozen:
            __slots__ = ("_queue",)

        slot = _Frozen()
        slot._queue = [_entry("q1")]
        queue_age.note_queued(slot, "q1")
        assert queue_age.waits(slot) == queue_age.QueueWait(1, None, "")


def _origin(sender: str) -> dict[str, Any]:
    return {SEND_ORIGIN_META_KEY: {"slot": sender, "tab": "tab-1"}}


class TestTheAcceptedSteer:
    """A steer the running turn accepted waits unread until the model's next step."""

    def _steered(self, clock, *senders: str) -> SimpleNamespace:
        slot = SimpleNamespace(
            _queue=[], _queued_at={}, _pending_steers=[], _steer_admissions={}, _steered_at={}
        )
        for n, sender in enumerate(senders):
            message = f"steer-{n}"
            slot._pending_steers.append(message)
            if sender:
                slot._steer_admissions[message] = _origin(sender)
            queue_age.note_steered(slot, message)
            clock.now += 10
        return slot

    def test_it_counts_for_its_sender_and_gives_its_age(self, clock):
        slot = self._steered(clock, CONDUCTOR, "chat-someone-else", "")
        clock.now += 30
        found = queue_age.waits(slot, sender=CONDUCTOR, steer_wanted=True, steers=True)
        assert found is not None and (found.count, found.oldest_secs) == (1, 60.0)
        assert found.oldest_id.startswith("steer@")
        assert queue_age.unread_row(slot, CONDUCTOR) == {"count": 1, "oldest_secs": 60}

    def test_a_harness_that_reports_reading_only_at_turn_end_is_not_counted(self, clock):
        """Its pending list holds steers the model already read, so none counts;
        the send instant is still kept, so a requeued one is aged from it."""
        slot = self._steered(clock, CONDUCTOR)
        assert queue_age.unread_row(slot, CONDUCTOR) == {"count": 1, "oldest_secs": 10}
        queue_age.note_steered(slot, "steer-0", read_reported=False)
        assert queue_age.unread_row(slot, CONDUCTOR) is None

        sent_at = slot._steered_at["steer-0"][0]
        clock.now += 500
        _queue(slot, _entry("requeued", CONDUCTOR, wanted=True))
        queue_age.carry_steered(slot, "steer-0", "requeued")
        assert slot._queued_at["requeued"] == sent_at

    def test_the_queue_age_alone_leaves_it_out(self, clock):
        slot = self._steered(clock, CONDUCTOR)
        assert queue_age.waits(slot, sender=CONDUCTOR) is None
        assert queue_age.oldest_field(slot) == {}

    def test_once_read_it_no_longer_counts_and_its_stamp_goes_on_the_next_write(self, clock):
        slot = self._steered(clock, CONDUCTOR)
        slot._pending_steers.clear()
        assert queue_age.unread_row(slot, CONDUCTOR) is None
        slot._pending_steers.append("next")
        queue_age.note_steered(slot, "next")
        assert set(slot._steered_at) == {"next"}

    def test_a_requeued_one_keeps_the_instant_it_was_sent(self, clock):
        slot = self._steered(clock, CONDUCTOR)
        sent_at = slot._steered_at["steer-0"][0]
        clock.now += 500
        _queue(slot, _entry("requeued", CONDUCTOR, wanted=True))
        queue_age.carry_steered(slot, "steer-0", "requeued")
        assert slot._queued_at["requeued"] == sent_at
        assert "steer-0" not in slot._steered_at

    def test_only_a_peers_requeued_steer_is_marked_as_wanting_the_turn(self):
        assert queue_age.requeued_steer_meta(_origin(CONDUCTOR)) == {
            queue_age.STEER_WANTED_META: True
        }
        assert queue_age.requeued_steer_meta({}) == {}
        assert queue_age.requeued_steer_meta(None) == {}


class TestTheRequeueAndTheDrain:
    def test_the_turn_ending_requeues_a_peers_steer_marked_and_aged(
        self, tmp_path, monkeypatch, clock
    ):
        from unittest.mock import MagicMock

        from chat_test_helpers import _make_state

        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot._pending_steers = ["peer text"]
        slot._steer_admissions = {"peer text": _origin(CONDUCTOR)}
        queue_age.note_steered(slot, "peer text")
        clock.now += 700

        _requeue_unconsumed_steers(state, slot)

        head = slot._queue[0]
        assert head["meta"][queue_age.STEER_WANTED_META] is True
        assert queue_age.sender_unread(state, "test", CONDUCTOR) == (
            1,
            700.0,
            head["id"],
            0,
            (1, 700.0, head["id"]),
        )

    @pytest.mark.asyncio
    async def test_the_drained_row_does_not_carry_the_mark(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from chat_runner_coverage_helpers import _slot as _bare_slot
        from chat_runner_coverage_helpers import _state as _bare_state

        from kiro_crew.dashboard import chat_runner

        state, slot = _bare_state(tmp_path), _bare_slot()
        state.broadcast_ws = MagicMock()
        slot.queue_append("NOTE", meta={queue_age.STEER_WANTED_META: True})

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        rows = [m for m in slot.messages if m.get("content") == "NOTE"]
        assert rows and queue_age.STEER_WANTED_META not in (rows[0].get("meta") or {})


class TestTheResolver:
    def test_it_reads_the_worker_under_either_spelling(self, clock):
        slot = _slot()
        _queue(slot, _entry("q1", CONDUCTOR, wanted=True))
        clock.now += 42
        table = SimpleNamespace(get_slot={f"dashboard_{WORKER}": slot}.get)
        assert queue_age.sender_unread(table, WORKER, CONDUCTOR) == (
            1,
            42.0,
            "q1",
            0,
            (1, 42.0, "q1"),
        )

    def test_the_queued_part_counts_ages_and_keys_on_the_queue_alone(self, clock):
        """Beside a note queued 5 s ago: the sender's steer unread for 700 s, one the
        turn took unconfirmed, and one a failed turn settled. The whole answer counts
        all but the taken steer as unread and ages on the oldest of those; the
        queued part is the note alone, which is all the conductor's own queue may
        wake it for."""
        slot = SimpleNamespace(
            _queue=[], _queued_at={}, _pending_steers=[], _steer_admissions={}, _steered_at={}
        )
        for message, reported in (("unread", True), ("taken", False)):
            slot._pending_steers.append(message)
            slot._steer_admissions[message] = _origin(CONDUCTOR)
            queue_age.note_steered(slot, message, read_reported=reported)
        queue_age.note_unconfirmed(slot, "settled", _origin(CONDUCTOR))
        clock.now += 695
        _queue(slot, _entry("q1", CONDUCTOR, wanted=True))
        clock.now += 5
        table = SimpleNamespace(get_slot={WORKER: slot}.get)

        count, oldest_secs, oldest_id, unconfirmed, queued = queue_age.sender_unread(
            table, WORKER, CONDUCTOR
        )
        assert (count, oldest_secs, unconfirmed) == (3, 700.0, 1)
        assert oldest_id.startswith(ledger_wake.UNCONFIRMED_WAIT_PREFIX)
        assert queued == (1, 5.0, "q1")

    def test_an_unconfirmed_steer_sets_the_wait_only_when_nothing_is_proven_unread(self, clock):
        """A claude turn holds the steer it took until it ends. Alone, it is the
        wait; beside a note queued later in that turn, the note's own stamp and id
        are, so the note is never keyed on the steer's spent wake."""
        slot = SimpleNamespace(_queue=[], _queued_at={}, _pending_steers=["taken"], _steered_at={})
        slot._steer_admissions = {"taken": _origin(CONDUCTOR)}
        queue_age.note_steered(slot, "taken", read_reported=False)
        sent = clock.now
        clock.now += 700
        table = SimpleNamespace(get_slot={WORKER: slot}.get)
        assert queue_age.sender_unread(table, WORKER, CONDUCTOR) == (
            0,
            700.0,
            f"steer@{sent!r}",
            1,
            None,
        )
        _queue(slot, _entry("q1", CONDUCTOR, wanted=True))
        clock.now += 5
        assert queue_age.sender_unread(table, WORKER, CONDUCTOR) == (
            1,
            5.0,
            "q1",
            1,
            (1, 5.0, "q1"),
        )

    def test_nothing_to_read_answers_none(self):
        table = SimpleNamespace(get_slot={}.get)
        assert queue_age.sender_unread(table, WORKER, CONDUCTOR) is None
        assert queue_age.sender_unread(None, WORKER, CONDUCTOR) is None
        assert queue_age.sender_unread(table, WORKER, "") is None

    def test_a_slot_read_that_raises_answers_none(self):
        def _boom(_key: str) -> None:
            raise RuntimeError("slot table torn")

        assert queue_age.sender_unread(SimpleNamespace(get_slot=_boom), WORKER, CONDUCTOR) is None


class TestThePolicy:
    def test_the_threshold_is_inclusive_and_an_unmeasured_age_never_wakes(self):
        assert not ledger_wake.is_unread_overdue(None)
        assert not ledger_wake.is_unread_overdue(ledger_wake.UNREAD_WAKE_SECS - 1)
        assert ledger_wake.is_unread_overdue(ledger_wake.UNREAD_WAKE_SECS)

    def test_the_brief_is_structural(self):
        assert ledger_wake.unread_wake_brief(
            item_id="it_1", target=WORKER, count=3, waited_secs=725.9
        ) == (
            f"[work-ledger wake] item=it_1 reason=message_unread count=3 target={WORKER} "
            "oldest_waited_secs=725"
        )

    def test_the_brief_never_calls_an_unconfirmed_message_unread(self):
        """With nothing proven unread the text is the one unconfirmed brief's, the
        same writer a failed turn's note wakes through."""
        live = ledger_wake.unread_wake_brief(
            item_id="it_1", target=WORKER, count=0, unconfirmed=2, waited_secs=600
        )
        assert live == (
            f"[work-ledger wake] item=it_1 reason=message_unconfirmed count=2 target={WORKER} "
            "oldest_waited_secs=600"
        )
        assert live == ledger_wake.unconfirmed_wake_brief(
            item_id="it_1", count=2, target=WORKER, waited_secs=600
        )
        assert ledger_wake.unread_wake_brief(
            item_id="it_1", target=WORKER, count=1, unconfirmed=2, waited_secs=600
        ) == (
            f"[work-ledger wake] item=it_1 reason=message_unread count=1 unconfirmed=2 "
            f"target={WORKER} oldest_waited_secs=600"
        )


def _bound_item() -> str:
    work_ledger.ensure_conductor(CONDUCTOR, goal="reach the worker")
    item_id = str(
        work_ledger.apply_conductor_action(
            CONDUCTOR, "create", title="the fix", acceptance={"kind": "human_approval"}
        )["item"].item_id
    )
    work_ledger.apply_conductor_action(
        CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER
    )
    return item_id


class _Ctx:
    message = json.dumps({"conductor": CONDUCTOR})

    class job:  # noqa: N801 - the attribute the kernel reads
        id = "loop-1"


def _briefs(probe) -> list[str]:
    return [obs.brief for obs in probe.observe(_Ctx()).observations]


def _in_queue(count: int, oldest_secs: float | None, oldest_id: str) -> tuple:
    """A resolver answer whose every message is a queue entry, so its queued part
    is the whole of it."""
    return (count, oldest_secs, oldest_id, 0, (count, oldest_secs, oldest_id))


class TestTheWake:
    def _probe(self, answers: dict[str, Any]):
        from kiro_crew.probes.work_ledger import WorkLedgerProbe

        seen: list[tuple[str, str]] = []

        def _unread(reader: str, sender: str):
            seen.append((reader, sender))
            # "now": the conductor's message in its worker's queue; "report": the
            # worker's note in the conductor's own.
            return answers.get("now") if reader == WORKER else answers.get("report")

        probe = WorkLedgerProbe(worker_running=lambda _k: True, worker_unread=_unread)
        probe.identity(_Ctx())
        return probe, seen

    def test_a_message_past_the_threshold_wakes_once_and_the_next_one_wakes_again(self):
        item_id = _bound_item()
        answers: dict[str, Any] = {"now": _in_queue(2, ledger_wake.UNREAD_WAKE_SECS + 5, "q1")}
        probe, seen = self._probe(answers)
        brief = f"[work-ledger wake] item={item_id} reason=message_unread count=2 target={WORKER}"
        assert any(b.startswith(brief) for b in _briefs(probe))
        assert seen == [(WORKER, CONDUCTOR), (CONDUCTOR, WORKER)]
        assert not any("message_unread" in b for b in _briefs(probe))
        answers["now"] = _in_queue(1, ledger_wake.UNREAD_WAKE_SECS, "q2")
        assert any(f"message_unread count=1 target={WORKER}" in b for b in _briefs(probe))

    def test_a_worker_note_unread_in_the_conductors_queue_wakes_it_once(self):
        """The other direction, through the same resolver with the keys swapped,
        reading only the answer's queued part. A steer only unconfirmed went into
        the conductor's turn and wakes nothing; a queued note past the threshold
        wakes once, naming the worker, and the next stuck one wakes again. Neither
        direction's record spends the other's."""
        item_id = _bound_item()
        secs = ledger_wake.UNREAD_WAKE_SECS
        answers: dict[str, Any] = {"report": (0, secs, "steer@1.0", 1, None)}
        probe, _ = self._probe(answers)
        assert not any("report_unread" in b for b in _briefs(probe))
        answers["report"] = _in_queue(1, secs - 1, "n1")
        assert not any("report_unread" in b for b in _briefs(probe))
        answers["report"] = _in_queue(1, secs + 5, "n1")
        assert [b for b in _briefs(probe) if "report_unread" in b] == [
            f"[work-ledger wake] item={item_id} reason=report_unread count=1 from={WORKER} "
            f"oldest_waited_secs={int(secs) + 5}"
        ]
        assert not any("report_unread" in b for b in _briefs(probe))
        answers["now"] = _in_queue(1, secs, "n1")
        assert any("message_unread" in b for b in _briefs(probe))
        answers["report"] = _in_queue(2, secs * 2, "n2")
        assert any(f"report_unread count=2 from={WORKER}" in b for b in _briefs(probe))

    def test_only_the_conductors_queue_counts_ages_and_keys_a_report_wake(self):
        """A steer of the worker's is in the conductor's turn already, where the wake
        would go: unread behind one long tool call (kiro), taken unconfirmed until the
        turn ends (claude), or settled unconfirmed by a failed turn. Beside a note
        queued seconds ago it neither counts, ages nor keys the wake, and alone it
        wakes nothing. The note wakes once on its own count, wait and id."""
        item_id = _bound_item()
        secs = ledger_wake.UNREAD_WAKE_SECS
        settled = f"{ledger_wake.UNCONFIRMED_WAIT_PREFIX}1.0#1"
        young = (1, 5.0, "n1")
        answers: dict[str, Any] = {}
        probe, _ = self._probe(answers)
        for mixed in (
            (1, 700.0, "steer@1.0", 1, young),
            (2, secs, "steer@1.0", 0, young),
            (2, secs, settled, 0, young),
            (1, secs, "steer@1.0", 0, None),
            (1, secs, settled, 0, None),
        ):
            answers["report"] = mixed
            assert not any("report_unread" in b for b in _briefs(probe)), mixed
        answers["report"] = (3, secs + 100, "steer@1.0", 1, (1, secs, "n1"))
        assert [b for b in _briefs(probe) if "report_unread" in b] == [
            f"[work-ledger wake] item={item_id} reason=report_unread count=1 from={WORKER} "
            f"oldest_waited_secs={int(secs)}"
        ]
        answers["report"] = (3, secs + 200, "steer@2.0", 1, (1, secs + 100, "n1"))
        assert not any("report_unread" in b for b in _briefs(probe)), "keyed on the note"

    def test_a_younger_message_or_none_wakes_nothing(self):
        _bound_item()
        answers: dict[str, Any] = {"now": _in_queue(1, ledger_wake.UNREAD_WAKE_SECS - 1, "q1")}
        probe, _ = self._probe(answers)
        assert not any("message_unread" in b for b in _briefs(probe))
        answers["now"] = _in_queue(1, None, "")
        assert not any("message_unread" in b for b in _briefs(probe))
        answers["now"] = None
        assert not any("message_unread" in b for b in _briefs(probe))

    def test_a_probe_with_no_resolver_never_wakes_for_it(self):
        from kiro_crew.probes.work_ledger import WorkLedgerProbe

        _bound_item()
        probe = WorkLedgerProbe(worker_running=lambda _k: True)
        probe.identity(_Ctx())
        assert not any("message_unread" in b for b in _briefs(probe))

    def test_a_resolver_that_raises_costs_only_this_wake(self):
        from kiro_crew.probes.work_ledger import WorkLedgerProbe

        _bound_item()

        def _boom(_w: str, _s: str):
            raise RuntimeError("queue torn")

        probe = WorkLedgerProbe(worker_running=lambda _k: True, worker_unread=_boom)
        probe.identity(_Ctx())
        assert probe.observe(_Ctx()).fetch_ok is not False

    def test_the_service_passes_the_gateways_resolver_through(self):
        from kiro_crew.autonudge import AutoNudgeService

        svc = AutoNudgeService(worker_unread=lambda w, s: _in_queue(1, 1.0, f"{w}>{s}"))
        assert svc._worker_unread(WORKER, CONDUCTOR) == _in_queue(1, 1.0, f"{WORKER}>{CONDUCTOR}")
        assert svc._worker_unread(WORKER, "") is None
        assert AutoNudgeService()._worker_unread(WORKER, CONDUCTOR) is None


class TestTheLedgerRow:
    def _row(self, slots: dict[str, Any]) -> dict[str, Any]:
        from kiro_crew.dashboard.handlers import work_ledger_read

        item_id = _bound_item()
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        state = SimpleNamespace(get_slot=slots.get)
        return work_ledger_read._row_with_flags(
            state, item, conductor_alive=True, patrolled=True, board=CONDUCTOR
        )

    def test_the_row_says_how_many_of_the_conductors_messages_wait(self, clock):
        slot = _slot()
        _queue(slot, _entry("q1", f"dashboard_{CONDUCTOR}"))
        _queue(slot, _entry("other", "chat-someone-else"))
        clock.now += 90
        row = self._row({WORKER: slot})
        assert row["messages_unread"] == {"count": 1, "oldest_secs": 90}

    def test_an_empty_queue_reads_none(self):
        assert self._row({WORKER: _slot()})["messages_unread"] is None

    def test_a_gone_worker_reads_none(self):
        assert self._row({})["messages_unread"] is None

    def test_the_compact_read_carries_it(self):
        from kiro_crew.dashboard.handlers import work_ledger_read

        assert "messages_unread" in work_ledger_read._COMPACT_ROW_FIELDS


class TestTheToolText:
    @pytest.mark.parametrize(
        ("secs", "words"),
        [
            (40, ", the oldest waiting 40s"),
            (300, ", the oldest waiting 5m"),
            (7500, ", the oldest waiting 2h 5m"),
            (None, ""),
            (True, ""),
            (-1, ""),
        ],
    )
    def test_the_oldest_wait_reads_as_words(self, secs, words):
        from kiro_crew.mcp_dashboard_session_runs import _oldest_wait

        assert _oldest_wait({"queue_oldest_secs": secs}) == words
