"""A kept card and a restart: only the owner's press sends it.

A send accepted while its chat was busy is parked work: a row in the admission
store and a card in the chat's queue line. The start after a restart resumes
it. Whatever that start did with the card (sent it, kept it under a limit, never
started it), no later start may send it by itself, also when the write that
would have said so never landed.

Every start here runs the real admission store, reopened from its file, the
queue line read back by the restore reader, and the gateway's own reconcile
listener.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_queue_send_held import (
    _held_texts,
    _mirror,
    _notices,
    _press,
    _sent_rows,
    _state,
)
from test_queue_send_held import _quiet as _held_quiet
from test_queue_send_held import turns as _held_turns
from test_shared_crew_queued_admission import BOB
from test_shared_crew_turn_outcome import _limits, _service

import kiro_crew
from kiro_crew import memory_startup
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.slot_persistence.metadata_codec import _read_queued_prompts
from kiro_crew.dashboard.slot_queue_repository import (
    RESTORED_SEND_CHOSEN_KEY,
    drain_restored_parked_sends,
    durable_queue_entries,
    sanitize_restored_queue,
)
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import host_hooks
from kiro_crew.shared_crew.admission import OperationKind

_quiet = _held_quiet
turns = _held_turns

_CARD = [("r1", "accepted one")]


async def _parked(tmp_path) -> list[dict]:
    """The run that parked the send: its row in the store, its card in the queue
    line as the gateway saved it. The store is closed with the row still queued."""
    (tmp_path / "store").mkdir()
    service = _service(tmp_path / "store")
    _state_before, slot = _state(tmp_path / "before", [])
    admission = await service.admit(
        budget_key=BOB.id,
        session_key=effective_session_key(slot),
        operation=OperationKind.CHAT_TURN,
        idempotency_key="kept-card-0001",
        park=True,
    )
    line = durable_queue_entries(
        [
            {
                "id": "r1",
                "content": "accepted one",
                "meta": {entry.QUEUED_WORK_META_KEY: admission.work.work_id},
            }
        ]
    )
    service._store.tasks.close()
    return json.loads(json.dumps(line))


async def _scan(state) -> None:
    """The real startup scan and drain, then whatever it left running."""
    await drain_restored_parked_sends(state)
    await asyncio.gather(*list(state._background_tasks))


@pytest.fixture
def start(tmp_path, monkeypatch):
    """A gateway start over a queue line; the start before it has stopped."""
    opened: list = []
    monkeypatch.setattr(memory_startup, "memory_startup_preparing", lambda: False)
    monkeypatch.setattr(cr, "_parked_queue_drain_admitted", lambda: True)

    async def _start(name: str, line: list[dict], *, store_writes_fail: bool = False, **cfg):
        for stopped in opened:
            stopped._store.tasks.close()
        service = _service(tmp_path / "store", **cfg)
        opened.append(service)
        monkeypatch.setattr(ag, "_SERVICE", service)
        monkeypatch.setattr(ag, "current_service", AsyncMock(return_value=service))
        if store_writes_fail:

            async def _refused(*_args, **_kwargs):
                raise OSError("the store refused the write")

            # Reconciliation ends rows through ``advance``; this is every write after it.
            monkeypatch.setattr(service._store, "annotate", _refused)
        state, slot = _state(tmp_path / name, sanitize_restored_queue(json.loads(json.dumps(line))))
        state.memory_startup_task = None
        service.set_reconcile_listener(host_hooks.reconcile_note(state))
        host_hooks.mark_sessions_restored(state)
        await _scan(state)
        return service, state, slot

    yield _start
    for service in opened:
        service._store.tasks.close()


@pytest.mark.asyncio
async def test_the_start_after_the_park_resumes_the_send_once(tmp_path, turns, start):
    line = await _parked(tmp_path)

    _service_one, _state_one, slot = await start("first", line)

    assert _sent_rows(slot) == ["accepted one"] and slot._queue == []


@pytest.mark.asyncio
async def test_a_card_a_start_sent_is_not_sent_again_when_its_pop_was_never_saved(
    tmp_path, turns, start
):
    """The start sends the card and the gateway stops before the queue line is
    saved without it. The next start reads the same line: it must not send the
    card a second time. It cannot tell a sent card from a kept one, so the card
    waits with its words."""
    line = await _parked(tmp_path)
    _service_one, _state_one, slot = await start("first", line)
    assert _sent_rows(slot) == ["accepted one"]

    _service_two, _state_two, slot = await start("second", line)

    assert _sent_rows(slot) == [], "the restart sent the card a second time"
    assert _held_texts(slot) == _CARD and _notices(slot) == []


@pytest.mark.asyncio
async def test_a_card_a_limit_kept_waits_for_a_press_when_no_write_after_the_keep_landed(
    tmp_path, turns, monkeypatch, start
):
    """A mirror keeps the card at the start that would have resumed it. Every
    store write after the keep is refused and the queue line is never saved
    again. The mirror goes and the gateway restarts: the card is not sent, it
    waits with its words, and the owner's press sends it once."""
    line = await _parked(tmp_path)
    _mirror(monkeypatch, True)
    _service_one, _state_one, slot = await start("first", line, store_writes_fail=True)
    assert _sent_rows(slot) == [] and _held_texts(slot) == _CARD and len(_notices(slot)) == 1
    _mirror(monkeypatch, False)

    _service_two, state, slot = await start("second", line)

    assert _sent_rows(slot) == [], "the restart sent a card a limit had kept"
    assert _held_texts(slot) == _CARD and _notices(slot) == []
    status, body = await _press(state, ["r1"])
    assert status == 200 and body["sent"] == ["r1"] and body["started"] is True
    assert _sent_rows(slot) == ["accepted one"] and slot._queue == []


@pytest.mark.asyncio
async def test_a_restart_while_still_mirrored_keeps_the_card_with_no_new_notice(
    tmp_path, turns, monkeypatch, start
):
    line = await _parked(tmp_path)
    _mirror(monkeypatch, True)
    _service_one, _state_one, slot = await start("first", line)
    assert len(_notices(slot)) == 1

    _service_two, _state_two, slot = await start("second", line)

    assert _sent_rows(slot) == []
    assert _held_texts(slot) == _CARD and _notices(slot) == []


@pytest.mark.asyncio
async def test_a_card_the_startup_drain_never_started_stays_kept_at_the_next_start(
    tmp_path, turns, monkeypatch, start
):
    """The start accepts the card and its drain is refused: the card is shown as
    kept. The next start must not send it by itself."""
    line = await _parked(tmp_path)
    monkeypatch.setattr(cr, "_parked_queue_drain_admitted", lambda: False)
    _service_one, _state_one, slot = await start("first", line)
    assert _sent_rows(slot) == [] and _held_texts(slot) == _CARD
    monkeypatch.setattr(cr, "_parked_queue_drain_admitted", lambda: True)

    _service_two, _state_two, slot = await start("second", line)

    assert _sent_rows(slot) == [], "the restart sent a card the start before had kept"
    assert _held_texts(slot) == _CARD


@pytest.mark.asyncio
async def test_a_start_with_admission_off_ends_the_cards_resume(tmp_path, turns, start):
    """With admission off the start resumes nothing and the card waits for a
    person. That run's own save of the queue line drops the card's link to its
    parked row, so the next start, admission on again, does not send it."""
    line = await _parked(tmp_path)
    _service_off, _state_off, slot = await start("off", line, admission_enabled=False)
    assert _sent_rows(slot) == [] and _held_texts(slot) == _CARD
    saved = slot.durable_queue_entries()

    _service_on, state, slot = await start("on", saved)

    assert _sent_rows(slot) == [], "the restart sent a card a run with admission off had kept"
    assert _held_texts(slot) == _CARD
    status, body = await _press(state, ["r1"])
    assert status == 200 and body["sent"] == ["r1"]
    assert _sent_rows(slot) == ["accepted one"] and slot._queue == []


@pytest.mark.asyncio
async def test_admission_coming_on_later_in_a_start_resumes_nothing(tmp_path, turns, start):
    """The start found admission off, so the card waits for a person. Admission
    comes on in the same run and a late pass scans again: nothing is sent."""
    line = await _parked(tmp_path)
    service, state, slot = await start("off", line, admission_enabled=False)

    await service.set_limits(_limits())
    await _scan(state)

    assert _sent_rows(slot) == [], "a late pass sent a card the start had kept"
    assert _held_texts(slot) == _CARD


def _restored(tmp_path, name: str, line: list[dict]):
    """The slot the metadata reader builds from a saved queue line."""
    _restored_state, slot = _state(tmp_path / name, [])
    _read_queued_prompts(
        SimpleNamespace(
            slot=slot, meta={"queued_prompts": line}, purpose=SimpleNamespace(name=slot.key)
        )
    )
    return slot


def test_a_restored_card_owes_one_save_that_drops_its_link(tmp_path):
    """The link is spent by the restore: the next flush writes the card without
    it. A card with no link owes no save, as before."""
    linked = durable_queue_entries(
        [{"id": "r1", "content": "accepted one", "meta": {entry.QUEUED_WORK_META_KEY: "w-1"}}]
    )
    plain = durable_queue_entries([{"id": "p1", "content": "typed", "meta": {"sendId": "s-1"}}])

    slot = _restored(tmp_path, "linked", linked)
    assert slot.queue_persist_pending is True
    assert [saved.get("meta") for saved in slot.durable_queue_entries()] == [{}]
    # In this run the card keeps its link: the startup scan reads it.
    assert slot._queue[0]["meta"] == {entry.QUEUED_WORK_META_KEY: "w-1"}

    assert _restored(tmp_path, "plain", plain).queue_persist_pending is False


# ── One owner for "does this restored card wait for a person" ──

#: Every function that may read the "run this next" mark, and why.
_CHOSEN_READERS = {
    # The predicate's owner.
    ("dashboard/slot_queue_repository.py", "restored_card_waits"),
    # Send now's own claim on the head it marked a moment ago.
    ("dashboard/chat_api/turn_stop.py", "_still_ours"),
    # The card's fields as the page gets them.
    ("dashboard/chat_api/slot_detail.py", "api_chat_slot_detail"),
}


class _ChosenReads(ast.NodeVisitor):
    """The innermost functions of one file that read the chosen mark."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.readers: set[str] = set()

    def _function(self, node: ast.AST) -> None:
        self.stack.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    @staticmethod
    def _is_mark(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id == "RESTORED_SEND_CHOSEN_KEY") or (
            isinstance(node, ast.Constant) and node.value == RESTORED_SEND_CHOSEN_KEY
        )

    def _read(self) -> None:
        self.readers.add(self.stack[-1] if self.stack else "<module>")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
            if self._is_mark(node.args[0]):
                self._read()
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and self._is_mark(node.slice):
            self._read()
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if self._is_mark(node.left) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            self._read()
        self.generic_visit(node)


def test_one_predicate_decides_whether_a_restored_card_waits():
    """A second copy of the predicate reads the chosen mark. Ask
    ``slot_queue_repository.restored_card_waits`` instead of adding a reader."""
    root = Path(kiro_crew.__file__).parent
    readers: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "RESTORED_SEND_CHOSEN_KEY" not in source and RESTORED_SEND_CHOSEN_KEY not in source:
            continue
        visitor = _ChosenReads()
        visitor.visit(ast.parse(source))
        relative = path.relative_to(root).as_posix()
        readers.update((relative, name) for name in visitor.readers)

    assert readers - _CHOSEN_READERS == set(), "a second copy of restored_card_waits"
    assert _CHOSEN_READERS - readers == set(), "the list names a reader that is gone"
