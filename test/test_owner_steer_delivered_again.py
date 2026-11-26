"""The owner's message steered into a turn they did not send never needs retyping.

A patrol wake opens a Conductor's turn and the owner types "go" for a ``crew=`` step.
The message goes in at the next step, without the owner's rights (a steer grants no
mark), so the gate refuses the ``crew=`` call. Pinned here, through the real
``api_chat`` handler, the real running ``_run_chat`` turn, the real steer settle and
the real ``crew=`` gate (``api_session_control_create``), with only the model and the
peer crew stubbed:

* the refusal tells the agent to end the turn, because the message comes again;
* at that turn's end the message runs once more, as the owner's own turn, with the
  owner's mark, its model input led by a note that says it was already read;
* nothing comes again when nothing was refused, and a Stop, a failed turn and a
  harness that confirms a steer only at a clean end (claude) each give it once;
* every gate arm that refuses for want of the owner's or the person's rights says it;
* until the turn reads it, the owner's steer is in the durable queue a restart
  restores, in order, once, and without the mark.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
)
from test_session_control_crew import INSTANCE, StubPeer, _Registry, _request
from test_typed_message_runs_alone import _Live, _note

from kiro_crew import crew_mode
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import chat_delivery_owner_steer as owner_steer
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.guide_runs import GuideError
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.handlers.guide import _resolve_agent_caller
from kiro_crew.dashboard.handlers.remote_dispatch_routes import owner_unverifiable_reason
from kiro_crew.dashboard.handlers_instances_crew_reads import _authorize_crew_read

GO = "Q1a Go."
#: The longest a wait on a stubbed turn may take before the test fails: a bound on a
#: signal the test itself sets, never a barrier.
BOUND = 60


class _Conductor(_Live):
    """A conductor mode chat whose first turn is a real, held-open patrol wake.

    Every later turn that carries the owner's words makes the real ``crew=`` call.
    *first_end* ends the first turn cleanly or with an error; *loses_steers* is a
    harness that reports a steer read only when its turn ends cleanly (claude).
    """

    def __init__(self, tmp_path, monkeypatch, *, first_end="complete", loses_steers=False):
        super().__init__(tmp_path, monkeypatch)
        self.crew_calls: list[tuple[int, dict]] = []
        self.agent.steer_needs_loss_recovery = loses_steers
        self.agent.steers_taken_unconfirmed = frozenset()
        self.agent.held_steers_possibly_delivered = frozenset()
        self.peer = StubPeer()
        self.peer.reply("POST", "api/chat/slots", 200, {"key": "chat-77", "agent": "worker"})
        self.state.instances_manager = self.peer
        self.state.instances_registry = _Registry([SimpleNamespace(id=INSTANCE, name="lab")])
        monkeypatch.setattr(crew, "remote_dispatch_enabled", lambda: True)
        # The slot is a conductor mode chat for the gate's rule 4 (the predicate the
        # agent lock uses); its stub turns run no agent spec, so the agent is not set.
        monkeypatch.setattr(crew_mode, "slot_is_crew_mode_root", lambda slot: slot is live.slot)
        live = self

        async def _stream(message):
            slot = live.slot
            live.turns.append((message, slot._turn_owner_sent, owner_unverifiable_reason(slot)))
            if len(live.turns) == 1:
                live.entered.set()
                await asyncio.wait_for(live.release.wait(), BOUND)
                if first_end == "error":
                    raise RuntimeError("the backend went away")
            elif GO in message:
                live.crew_calls.append(await live.crew_create())
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield AcpEvent(kind=EVENT_COMPLETE)

        provider = self.state.sessions.get_or_create.return_value[0]
        provider.stream = _stream
        provider.stream_command = _stream
        self.state.sessions.record_failure = AsyncMock()  # the failed turn's own record

    async def crew_create(self) -> tuple[int, dict]:
        """The agent's ``session_create(crew="lab")``, through the real gate."""
        req = _request(self.state, _key(self.slot), body={"crew": "lab"})
        resp = await handlers_sc.api_session_control_create(req)
        return resp.status, json.loads(resp.body.decode())

    def wire(self, text: str) -> str:
        """What the agent was handed for *text*: the owner's words, marked as theirs."""
        return next(
            c.args[0] for c in reversed(self.agent.steer.await_args_list) if text in c.args[0]
        )

    def read(self, text: str) -> None:
        """The agent reads the steer: the real ``steering_consumed`` settle of its wire text."""
        cr._settle_consumed_steers(self.slot, self.wire(text), self.state)

    def owner_turns(self) -> list[tuple[str, bool, str]]:
        return [turn for turn in self.turns[1:] if GO in turn[0]]


async def _go_refused(live: _Conductor, *, read: bool = True) -> dict:
    """The owner types "go" into the patrol wake; the agent reads it and tries crew=."""
    await live.open_with("wake")
    receipt = await live.type(GO)
    assert receipt.get("steered") is True
    if read:
        live.read(GO)
        assert live.slot._pending_steers == []
    status, body = await live.crew_create()
    assert status == 403 and body["code"] == crew.OWNER_UNVERIFIABLE
    return body


# ── R1: refused for want of the owner's rights -> told, then delivered again once ──


@pytest.mark.asyncio
async def test_go_into_a_patrol_wake_is_refused_then_runs_once_as_the_owners_turn(
    tmp_path, monkeypatch
) -> None:
    live = _Conductor(tmp_path, monkeypatch)

    body = await _go_refused(live)

    assert body["error"].startswith("crew= is open only to a turn the owner sent")
    assert owner_steer.REFUSAL_NOTICE in body["error"], "the agent is told to end the turn"

    await live.finish()

    (turn,) = live.owner_turns()
    assert turn == (owner_steer.AGAIN_NOTE + GO, True, ""), "once, with the owner's mark"
    assert [status for status, _ in live.crew_calls] == [200], "and now crew= is allowed"
    assert live.peer.calls and live.peer.calls[-1]["path"] == "api/chat/slots"
    first, again = [m for m in live.slot.messages if m.get("content") == GO]
    assert (first["role"], again["role"]) == ("user", "user"), "the row keeps the text as typed"
    assert (
        again["meta"][owner_steer.OWNER_STEER_AGAIN_META] == first["meta"]["mid"]
    ), "the second row says it is the first one run again, and names it"
    assert owner_steer.OWNER_STEER_AGAIN_META not in first["meta"]
    assert live.slot.queue_depth == 0


@pytest.mark.asyncio
async def test_a_read_and_an_unread_go_keep_his_typed_order(tmp_path, monkeypatch) -> None:
    """A was read and is owed again; B was never read. A runs first, then B, each once."""
    live = _Conductor(tmp_path, monkeypatch)
    await _go_refused(live)  # GO is read, then refused
    await live.type("then this")
    assert live.slot._pending_steers == ["then this"]

    await live.finish()

    assert [prompt for prompt, _, _ in live.turns[1:]] == [
        owner_steer.AGAIN_NOTE + GO,
        "then this",
    ]
    assert [mark for _, mark, _ in live.turns[1:]] == [True, True]


@pytest.mark.asyncio
async def test_nothing_refused_means_nothing_comes_again(tmp_path, monkeypatch) -> None:
    live = _Conductor(tmp_path, monkeypatch)
    await live.open_with("wake")
    await live.type(GO)
    live.read(GO)

    await live.finish()

    assert live.owner_turns() == [] and len(live.turns) == 1
    assert live.slot.queue_depth == 0


@pytest.mark.asyncio
async def test_a_refusal_before_the_owner_typed_says_nothing_new(tmp_path, monkeypatch) -> None:
    """The wake's own crew= call, with no owner steer in the turn: today's text, no rerun."""
    live = _Conductor(tmp_path, monkeypatch)
    await live.open_with("wake")

    status, body = await live.crew_create()

    assert status == 403 and owner_steer.REFUSAL_NOTICE not in body["error"]
    await live.finish()
    assert len(live.turns) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["stop", "error"])
async def test_a_stop_or_a_failed_turn_still_brings_it_once(tmp_path, monkeypatch, end) -> None:
    live = _Conductor(tmp_path, monkeypatch, first_end="error" if end == "error" else "complete")
    await _go_refused(live)
    if end == "stop":
        live.slot._stopping = True

    await live.finish()

    (turn,) = live.owner_turns()
    assert turn[1:] == (True, "")
    assert [status for status, _ in live.crew_calls] == [200]


@pytest.mark.asyncio
async def test_an_unread_go_comes_back_by_the_requeue_alone(tmp_path, monkeypatch) -> None:
    """The turn ended before reading it: the requeue runs it as the owner's turn, and
    the refusal adds no second copy. Not read, so no note says it was."""
    live = _Conductor(tmp_path, monkeypatch)
    await _go_refused(live, read=False)

    await live.finish()

    assert live.owner_turns() == [(GO, True, "")]


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["stop", "error"])
async def test_a_harness_that_confirms_only_at_a_clean_end_brings_it_once(
    tmp_path, monkeypatch, end
) -> None:
    """claude: the steer is still pending when the turn stops or fails, so the requeue
    runs it as the owner's turn; the turn's end queues no second copy. A failed turn
    that took it in says it was read."""
    live = _Conductor(
        tmp_path,
        monkeypatch,
        first_end="error" if end == "error" else "complete",
        loses_steers=True,
    )
    await _go_refused(live, read=False)
    if end == "stop":
        live.slot._stopping = True
    else:
        live.agent.steers_taken_unconfirmed = frozenset({live.wire(GO)})

    await live.finish()

    (turn,) = live.owner_turns()
    lead = owner_steer.AGAIN_NOTE if end == "error" else ""
    assert turn == (lead + GO, True, "")


@pytest.mark.asyncio
async def test_a_hard_stop_sends_nothing_again(tmp_path, monkeypatch) -> None:
    """A hard Stop discards the queue and the turn's steers; the refusal is dropped too."""
    live = _Conductor(tmp_path, monkeypatch)
    await _go_refused(live)

    owner_steer.discard(live.slot)  # what the force-stop handler does with the steers
    await live.finish()

    assert live.owner_turns() == []


# ── every arm that refuses for want of the rights says so ──

#: The two marks a gate reads to refuse for want of the owner's or the person's rights.
_MARKS = frozenset({"_turn_owner_sent", "_turn_user_sent"})


def _mark_readers() -> set[str]:
    """Every module under ``src/kiro_crew`` whose code READS a mark (writes and the
    slot's own field declaration are not reads), found by AST, never by grep."""
    import ast
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).parent
    readers: set[str] = set()
    for path in root.rglob("*.py"):
        if "_vendor" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            reads = (
                isinstance(node, ast.Attribute)
                and node.attr in _MARKS
                and isinstance(node.ctx, ast.Load)
            ) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _MARKS
            )
            if reads:
                rel = path.relative_to(root.parent).with_suffix("")
                readers.add(".".join(rel.parts))
                break
    return readers


def test_every_reader_of_the_marks_is_a_listed_gate_or_says_why_it_is_not() -> None:
    """A new gate that refuses on a mark lands in one list or the other, or this fails."""
    gates = {site.split(":")[0] for site in owner_steer.OWNER_RIGHTS_REFUSAL_SITES}
    readers = _mark_readers()
    assert readers - gates - set(owner_steer.OTHER_MARK_READERS) == set()
    assert set(owner_steer.OTHER_MARK_READERS) - readers == set(), "no stale reason left"


@pytest.mark.parametrize("site", sorted(owner_steer.OWNER_RIGHTS_REFUSAL_SITES))
def test_every_listed_gate_completes_its_refusal_through_the_one_helper(site: str) -> None:
    import importlib
    import inspect

    module, function = site.split(":")
    source = inspect.getsource(getattr(importlib.import_module(module), function))
    assert "refused_for_owner_rights(" in source, f"{site} refuses without the notice"


@pytest.mark.asyncio
async def test_the_server_card_says_it_in_its_own_words_too(tmp_path, monkeypatch) -> None:
    """``request_server`` words the refusal itself, and the notice still rides it."""
    from kiro_crew.dashboard.handlers.template_server_request import _caller_refusal

    live = _Conductor(tmp_path, monkeypatch)
    await live.open_with("wake")
    await live.type(GO)
    request = _request(live.state, _key(live.slot))

    response = _caller_refusal(GuideError(403, "not_user_turn", "x"), request)

    body = json.loads(response.body.decode())
    assert body["code"] == "not_user_turn" and body["error"].startswith("only a message")
    assert body["error"].endswith(owner_steer.REFUSAL_NOTICE)
    assert live.slot._owner_steer_turn.refused is True, "and the message is owed again"
    await live.finish()
    assert live.owner_turns() == [(GO, True, "")], "unread, it runs once, by the requeue"


@pytest.mark.asyncio
async def test_a_user_only_tool_refused_in_that_turn_says_it_too(tmp_path, monkeypatch) -> None:
    """The guide/card/``update_self`` arm (``not_user_turn``) and the crew reads behind it."""
    live = _Conductor(tmp_path, monkeypatch)
    await live.open_with("wake")
    request = _request(live.state, _key(live.slot))

    with pytest.raises(GuideError) as before:
        await _resolve_agent_caller(request, "guide.start")
    await live.type(GO)
    live.read(GO)
    with pytest.raises(GuideError) as after:
        await _resolve_agent_caller(request, "guide.start")

    assert before.value.code == after.value.code == "not_user_turn"
    assert owner_steer.REFUSAL_NOTICE not in before.value.message
    assert after.value.message.endswith(owner_steer.REFUSAL_NOTICE)
    await live.finish()
    assert len(live.owner_turns()) == 1, "that refusal counts as well"


@pytest.mark.asyncio
async def test_a_crew_read_in_a_person_turn_that_is_not_the_owners_says_it(
    tmp_path, monkeypatch
) -> None:
    """The crew read's own owner arm (``not_owner_turn``): a dashboard user who is not
    the owner sent the turn, and the owner's steer went in."""
    live = _Conductor(tmp_path, monkeypatch)
    KiroCrewConfig.load().instances.crew_reads_enabled = True
    live.slot.task = asyncio.ensure_future(
        cr._run_chat(live.state, live.slot, "another person's request", _directive_user_origin=True)
    )
    await asyncio.wait_for(live.entered.wait(), BOUND)
    await live.type(GO)

    response = await _authorize_crew_read(_request(live.state, _key(live.slot)), "crew_search")

    body = json.loads(response.body.decode())
    assert response.status == 403 and body["code"] == "not_owner_turn"
    assert body["error"] == f"forbidden. {owner_steer.REFUSAL_NOTICE}"
    await live.finish()


# ── a gateway restart: the owner rebuilt from disk ──


async def _restarted(live: _Conductor):
    """Stop here: wait for the writes already started to land, then build a new
    gateway state over the same data home and rehydrate the chat from its file. No
    save is forced, so only what the running gateway wrote is read back."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history
    from kiro_crew.testing.wait import async_wait_until

    slot = live.slot
    await async_wait_until(
        lambda: not slot._queue_persist_inflight and not slot.queue_persist_pending,
        describe=lambda: "the queue write the steer path started",
    )
    restored = _rehydrate_slot_from_history(_make_state(live.tmp_path), slot.key)
    assert restored is not None
    return restored


@pytest.mark.asyncio
async def test_a_restart_brings_back_the_unread_go_in_order_once_and_without_the_mark(
    tmp_path, monkeypatch
) -> None:
    live = _Conductor(tmp_path, monkeypatch)
    live.tmp_path = tmp_path
    await live.open_with("note")
    await _note(live.state, live.slot, "a later note")
    await live.type(GO)
    assert live.slot._pending_steers == [GO]

    restored = await _restarted(live)

    assert [item["content"] for item in restored._queue] == [GO, "a later note"]
    assert all("_directive_owner_origin" not in item for item in restored._queue)
    live.slot._pending_steers.clear()
    live.slot._queue.clear()
    await live.finish()


@pytest.mark.asyncio
async def test_a_restart_after_the_refusal_still_runs_the_go_again_once(
    tmp_path, monkeypatch
) -> None:
    """Read, then refused: the turn's end never came, and the message is still owed."""
    live = _Conductor(tmp_path, monkeypatch)
    live.tmp_path = tmp_path
    await _go_refused(live)

    restored = await _restarted(live)

    (entry,) = restored._queue
    assert entry["content"] == GO
    first = next(m for m in live.slot.messages if m.get("content") == GO)
    assert entry["meta"][owner_steer.OWNER_STEER_AGAIN_META] == first["meta"]["mid"]
    assert "_directive_owner_origin" not in entry, "restored without the mark, as every entry"
    await live.finish()


@pytest.mark.asyncio
async def test_a_read_go_nothing_owes_never_comes_back_after_a_restart(
    tmp_path, monkeypatch
) -> None:
    """Unread it was on disk; read, the settle wrote it off at once, before any flush."""
    live = _Conductor(tmp_path, monkeypatch)
    live.tmp_path = tmp_path
    await live.open_with("wake")
    await live.type(GO)
    assert [item["content"] for item in (await _restarted(live))._queue] == [GO]

    live.read(GO)

    assert (await _restarted(live))._queue == []
    await live.finish()


@pytest.mark.asyncio
async def test_a_requeued_go_is_on_disk_once(tmp_path, monkeypatch) -> None:
    """Unread at the turn's end, it moves into the queue and leaves the steer view."""
    live = _Conductor(tmp_path, monkeypatch)
    await live.open_with("note")
    await live.type(GO)

    cr._requeue_unconsumed_steers(live.state, live.slot)

    assert [e["content"] for e in live.slot.durable_queue_entries()] == [GO]
    live.slot._queue.clear()
    await live.finish()
