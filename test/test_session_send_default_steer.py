"""A ``session_send`` that names no delivery cuts in where waiting would strand it.

Two cases steer by default (``steer=None``, the route's answer to an omitted key):
the caller manages the busy target (created it, was handed it, or its work-ledger
binding names the caller), or the target is still in the turn in which it messaged
the caller, so a reply reaches the turn that asked. Every other default queues, and
``steer=False`` always queues. Driven against real slots, the real gate and the real
delivery.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- autouse fixtures apply by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
    _steerable,
)

from kiro_crew import work_ledger
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_managers
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.handlers import work_ledger_handback


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: False)
    asyncio.run(sc.refresh_manager_chain())
    session_managers.reset_cache()
    yield
    session_managers.reset_cache()


def _team(state):
    """A conductor, two workers it opened, and an unrelated session."""
    conductor = _slot(state, "chat-conductor")
    worker = _slot(state, "chat-worker")
    sibling = _slot(state, "chat-sibling")
    stranger = _slot(state, "chat-stranger")
    worker._created_by = conductor.key
    sibling._created_by = conductor.key
    return conductor, worker, sibling, stranger


def _live(slot):
    """*slot* mid-turn on a client that takes a steer."""
    _busy(slot)
    slot._acp_client = _steerable(accepted=True)
    return slot


def _send(state, caller, target, message="ORANGE", **kwargs):
    """A send the way the session_send route makes it when ``steer`` is omitted."""
    kwargs.setdefault("steer", None)
    return asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target=target.key,
            message=message,
            creator_message=True,
            **kwargs,
        )
    )


def _queued(slot) -> bool:
    return any("ORANGE" in entry.get("content", "") for entry in slot._queue)


# ── the caller manages the target ───────────────────────────────────────────


def test_the_creator_cuts_into_its_busy_worker(tmp_path):
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(worker)

    out = _send(state, conductor, worker)

    assert out["steered"] is True and out["steerDefault"] == "manager"
    worker._acp_client.steer.assert_awaited_once()
    assert not _queued(worker)


def test_a_false_steer_is_the_queue_by_name(tmp_path):
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(worker)

    out = _send(state, conductor, worker, steer=False)

    assert out["steered"] is False and "steerDefault" not in out
    worker._acp_client.steer.assert_not_awaited()
    assert _queued(worker)


def test_a_session_that_manages_nothing_still_queues(tmp_path):
    state = _make_state(tmp_path)
    _, _, _, stranger = _team(state)
    other = _live(_slot(state, "chat-other"))

    out = _send(state, stranger, other)

    assert out["steered"] is False and "steerDefault" not in out
    other._acp_client.steer.assert_not_awaited()
    assert _queued(other)


def test_the_binding_stands_in_for_the_creator(tmp_path):
    """A conductor bound to a worker someone else opened manages it all the same."""
    state = _make_state(tmp_path)
    _, _, _, stranger = _team(state)
    other = _live(_slot(state, "chat-other"))

    out = _send(state, stranger, other, caller_bound_worker=other.key)

    assert out["steered"] is True and out["steerDefault"] == "manager"


def test_a_binding_read_for_another_slot_earns_nothing(tmp_path):
    """The binding names the slot the route resolved; a send the gate resolves to a
    different slot (a title that moved in between) keeps the queue."""
    state = _make_state(tmp_path)
    _, _, _, stranger = _team(state)
    other = _live(_slot(state, "chat-other"))

    out = _send(state, stranger, other, caller_bound_worker="chat-elsewhere")

    assert out["steered"] is False and "steerDefault" not in out
    assert _queued(other)


def test_a_turn_the_person_is_driving_keeps_the_managers_default_queue(tmp_path):
    """A peer steer takes the turn's person and owner standing away for the rest of
    it, so a manager's default does not do that to a turn the person sent."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(worker)
    worker._turn_user_sent = True

    out = _send(state, conductor, worker)

    assert out["steered"] is False and "steerDefault" not in out
    assert worker._turn_user_sent is True
    assert _queued(worker)
    # It still wanted that turn, so its sender is woken if it sits.
    from kiro_crew.dashboard import queue_age

    assert queue_age.sender_unread(state, worker.key, conductor.key)[0] == 1

    worker._queue.clear()
    out = _send(state, conductor, worker, steer=True)
    assert out["steered"] is True


def test_a_worker_never_steers_its_creator_by_default(tmp_path):
    """The binding a worker could not have forged still never turns its report
    into a steer: a worker's message up keeps the queue unless it is a reply."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)

    out = _send(state, worker, conductor, caller_bound_worker=conductor.key)

    assert out["steered"] is False and out.get("toCreator") is True
    conductor._acp_client.steer.assert_not_awaited()
    assert _queued(conductor)


# ── a reply into the turn that asked ────────────────────────────────────────


def test_a_reply_cuts_into_the_creators_asking_turn(tmp_path):
    """The conductor asked mid-turn and waits in that turn: the answer must reach
    it there, or the wait it runs can never see it."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    _send(state, conductor, worker)

    out = _send(state, worker, conductor)

    assert out["steered"] is True and out["steerDefault"] == "reply"
    assert out.get("toCreator") is True
    conductor._acp_client.steer.assert_awaited_once()
    assert not _queued(conductor)


def test_a_reply_into_the_asking_turn_does_not_bring_queued_reports_in(tmp_path):
    """The ask admits one cut-in. The worker's earlier reports, queued for the
    conductor, wait for their own turn as before."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    assert _send(state, worker, conductor, steer=False)["steered"] is False
    reports = list(conductor._queue)
    _send(state, conductor, worker)

    out = _send(state, worker, conductor)

    assert out["steered"] is True and "carried" not in out
    conductor._acp_client.steer.assert_awaited_once()
    assert conductor._queue == reports and len(reports) == 1


def test_a_reply_after_the_asking_turn_ended_queues(tmp_path):
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    _send(state, conductor, worker)
    _busy(conductor)  # a new turn: the one that asked is over

    out = _send(state, worker, conductor)

    assert out["steered"] is False and "steerDefault" not in out
    conductor._acp_client.steer.assert_not_awaited()
    assert _queued(conductor)


def test_a_reply_from_a_session_that_was_not_asked_queues(tmp_path):
    state = _make_state(tmp_path)
    conductor, worker, sibling, _ = _team(state)
    _live(conductor)
    _live(worker)
    _send(state, conductor, worker)

    out = _send(state, sibling, conductor)

    assert out["steered"] is False
    assert _queued(conductor)


def test_an_explicit_steer_to_the_creator_is_still_refused(tmp_path):
    """Only the defaulted reply gets the creator opening's steer; asking for one by
    name is the refusal it always was."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    _send(state, conductor, worker)

    with pytest.raises(sc.SessionControlError):
        _send(state, worker, conductor, steer=True)
    conductor._acp_client.steer.assert_not_awaited()


def test_a_reply_between_peers_cuts_in(tmp_path):
    """Conductor to conductor: neither created the other, and the reply still
    reaches the turn that asked."""
    state = _make_state(tmp_path)
    asker = _live(_slot(state, "chat-blick-conductor"))
    peer = _live(_slot(state, "chat-fork-conductor"))
    _send(state, asker, peer, steer=False)

    out = _send(state, peer, asker)

    assert out["steered"] is True and out["steerDefault"] == "reply"


def test_a_default_reply_into_a_turn_the_person_is_driving_queues(tmp_path):
    """A peer steer takes the person's standing away for the rest of the turn, so a
    reply the sender did not ask to steer waits for the person's turn to end. Its
    entry is marked as having wanted that turn, and the ask is kept."""
    from kiro_crew.dashboard import queue_age

    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    conductor._turn_user_sent = True
    _send(state, conductor, worker)

    out = _send(state, worker, conductor)

    assert out["steered"] is False and "steerDefault" not in out
    assert out.get("toCreator") is True
    conductor._acp_client.steer.assert_not_awaited()
    assert conductor._turn_user_sent is True
    entry = next(e for e in conductor._queue if "ORANGE" in e.get("content", ""))
    assert (entry.get("meta") or {}).get(queue_age.STEER_WANTED_META) is True
    assert worker.key in conductor._awaiting_replies


def test_a_held_reply_keeps_its_ask_for_when_the_person_is_not_driving(tmp_path):
    """The owner's standing holds a peer's reply too, and the ask it kept lets the
    same reply cut in once nobody's standing is at stake."""
    state = _make_state(tmp_path)
    asker = _live(_slot(state, "chat-blick-conductor"))
    peer = _live(_slot(state, "chat-fork-conductor"))
    asker._turn_owner_sent = True
    _send(state, asker, peer, steer=False)

    held = _send(state, peer, asker)
    assert held["steered"] is False and "steerDefault" not in held
    asker._acp_client.steer.assert_not_awaited()
    assert asker._turn_owner_sent is True

    asker._turn_owner_sent = False
    out = _send(state, peer, asker)

    assert out["steered"] is True and out["steerDefault"] == "reply"
    asker._acp_client.steer.assert_awaited_once()


def test_an_idle_asker_records_nothing(tmp_path):
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(worker)

    _send(state, conductor, worker)

    assert conductor._awaiting_replies == {}


def test_one_ask_buys_one_reply_cut_in(tmp_path):
    """The reply spends the record, so a second note from the same worker in the
    same asking turn keeps the queue."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    _send(state, conductor, worker)

    first = _send(state, worker, conductor)
    assert first["steered"] is True and first["steerDefault"] == "reply"
    assert conductor._awaiting_replies == {}

    conductor._acp_client.steer.reset_mock()
    second = asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=_key(worker),
            target=conductor.key,
            message="LEMON",
            steer=None,
            creator_message=True,
        )
    )
    assert second["steered"] is False and "steerDefault" not in second
    conductor._acp_client.steer.assert_not_awaited()


def test_a_reply_that_asks_back_gets_its_answer_into_its_turn(tmp_path):
    """The worker spoke first; the conductor's answer is a reply, and it may carry a
    question of its own, so the worker's answer to that cuts in too."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    _send(state, worker, conductor, steer=False)

    asked_back = _send(state, conductor, worker)
    assert asked_back["steered"] is True and asked_back["steerDefault"] == "reply"

    answer = asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=_key(worker),
            target=conductor.key,
            message="LEMON",
            steer=None,
            creator_message=True,
        )
    )
    assert answer["steered"] is True and answer["steerDefault"] == "reply"


def test_a_reply_that_falls_back_to_the_queue_keeps_the_ask(tmp_path):
    """Only a reply that cut in answers the ask; one the turn could not take queues
    and leaves the record for the next."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    _send(state, conductor, worker)
    conductor._acp_client = _steerable(accepted=False)

    out = _send(state, worker, conductor)

    assert out["steered"] is False
    assert worker.key in conductor._awaiting_replies


def test_a_send_that_is_not_a_session_send_asks_nothing(tmp_path):
    """A create's seed, a broadcast and a workflow send never pass the opening, so
    none of them buys the target a cut-in into the sender's turn."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)

    asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=_key(conductor),
            target=worker.key,
            message="SEED",
            via="session_create",
        )
    )

    assert conductor._awaiting_replies == {}
    out = _send(state, worker, conductor)
    assert out["steered"] is False and "steerDefault" not in out


def test_a_reply_spends_only_the_ask_of_the_turn_it_answered(tmp_path, monkeypatch):
    """A later turn that asked the same session while the reply's delivery
    suspended keeps its own ask."""
    from kiro_crew.dashboard.session_control_parts import session_messaging

    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    _send(state, conductor, worker)
    asked_in = conductor._turn_generation
    real = session_messaging._take_awaited_reply

    def _after_a_new_ask(caller_key, slot, generation):
        slot._turn_generation = asked_in + 1
        slot._awaiting_replies[caller_key] = asked_in + 1
        real(caller_key, slot, generation)

    monkeypatch.setattr(session_messaging, "_take_awaited_reply", _after_a_new_ask)
    out = _send(state, worker, conductor)

    assert out["steered"] is True and out["steerDefault"] == "reply"
    assert conductor._awaiting_replies == {worker.key: asked_in + 1}


def test_a_dispatch_seed_through_the_route_asks_nothing(tmp_path):
    """work_dispatch seeds through the session_send route; its seed is no ask."""
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)

    status, payload = _route(
        state,
        conductor,
        {"target": worker.key, "message": "SEED", "steer": False, "dispatch_seed": True},
    )

    assert status == 200, payload
    assert conductor._awaiting_replies == {}
    status, payload = _route(state, conductor, {"target": worker.key, "message": "ORANGE"})
    assert status == 200 and worker.key in conductor._awaiting_replies


def test_an_ask_from_a_turn_that_ended_mid_delivery_records_nothing(tmp_path, monkeypatch):
    """The record is the generation the send was admitted under: a turn that ended
    while the delivery suspended does not credit its successor with the ask."""
    from kiro_crew.dashboard.session_control_parts import session_messaging

    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    _live(worker)
    admitted = conductor._turn_generation
    real = session_messaging._note_awaiting_reply

    def _after_a_new_turn(caller_slot, target_key, generation):
        caller_slot._turn_generation = admitted + 1
        real(caller_slot, target_key, generation)

    monkeypatch.setattr(session_messaging, "_note_awaiting_reply", _after_a_new_turn)
    _send(state, conductor, worker)

    assert conductor._awaiting_replies == {}


# ── a message that wanted the turn and sits unread wakes its sender ─────────


class _Clock:
    def __init__(self) -> None:
        self.now = 2_000_000.0

    def __call__(self) -> float:
        return self.now


def _bind(conductor, worker) -> tuple[str, str]:
    """A work-ledger item of *conductor*'s bound to *worker*: (board key, item id)."""
    from kiro_crew import session_ledger

    board = session_ledger.ledger_key(conductor.key)
    work_ledger.ensure_conductor(board, goal="reach the worker")
    item_id = str(
        work_ledger.apply_conductor_action(
            board, "create", title="the fix", acceptance={"kind": "human_approval"}
        )["item"].item_id
    )
    work_ledger.apply_conductor_action(
        board, "bind", item_id=item_id, worker_session_key=worker.key
    )
    return board, item_id


def _wake_briefs(state, board: str) -> list[str]:
    from kiro_crew.dashboard import queue_age
    from kiro_crew.probes.work_ledger import WorkLedgerProbe

    class _Ctx:
        message = json.dumps({"conductor": board})

        class job:  # noqa: N801 - the attribute the kernel reads
            id = "loop-e2e"

    probe = WorkLedgerProbe(
        worker_running=lambda _k: True,
        worker_unread=lambda w, s: queue_age.sender_unread(state, w, s),
    )
    probe.identity(_Ctx())
    return [obs.brief for obs in probe.observe(_Ctx()).observations]


@pytest.mark.parametrize("accepted", [False, True], ids=["refused", "accepted-unread"])
def test_a_default_steer_left_unread_wakes_the_conductor(tmp_path, monkeypatch, accepted):
    """Refused: the default steer falls back to the queue marked as wanting the
    turn. Accepted: it waits in the turn's pending steers until the model reaches a
    step. Either way the worker has not read it, and past the threshold the
    conductor's work-ledger watch wakes for it."""
    from kiro_crew import ledger_wake
    from kiro_crew.dashboard import queue_age

    clock = _Clock()
    monkeypatch.setattr(queue_age, "_clock", clock)
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _busy(worker)
    worker._acp_client = _steerable(accepted=accepted)
    board, item_id = _bind(conductor, worker)

    out = _send(state, conductor, worker)

    assert out["steered"] is accepted
    if not accepted:
        assert any(
            (entry.get("meta") or {}).get(queue_age.STEER_WANTED_META) for entry in worker._queue
        )
    unread = queue_age.sender_unread(state, worker.key, conductor.key)
    assert unread is not None and unread[0] == 1
    assert not any("message_unread" in b for b in _wake_briefs(state, board))

    clock.now += ledger_wake.UNREAD_WAKE_SECS
    assert any(
        b.startswith(
            f"[work-ledger wake] item={item_id} reason=message_unread "
            f"count=1 target={worker.key}"
        )
        for b in _wake_briefs(state, board)
    )


def _harness_client(monkeypatch, harness: str) -> MagicMock:
    """A running client of *harness*: ``kiro`` reports a steer read when the model
    takes it; ``claude`` reports it only when its turn ends cleanly
    (``steer_needs_loss_recovery``), and takes a peer's steer only with
    ``agent.peer_steer_loss_recovery`` on, as an install that opted in has it."""
    from types import SimpleNamespace

    from kiro_crew.acp.client_class import AcpClient
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
    from kiro_crew.dashboard import chat_delivery

    backend = {"kiro": ACP_BACKEND_KIRO, "claude": ACP_BACKEND_CLAUDE}[harness]
    client = _steerable(accepted=True)
    # The real property, read for this backend, so the test follows the named sets.
    client.steer_needs_loss_recovery = AcpClient.steer_needs_loss_recovery.fget(
        SimpleNamespace(backend=backend)
    )
    monkeypatch.setattr(
        chat_delivery.live,
        "snapshot",
        lambda: SimpleNamespace(agent=SimpleNamespace(peer_steer_loss_recovery=True)),
    )
    return client


@pytest.mark.parametrize(
    ("harness", "reason"),
    [("kiro", "message_unread"), ("claude", "message_unconfirmed")],
)
def test_a_steer_the_turn_took_wakes_its_sender_once_and_says_only_what_is_proven(
    tmp_path, monkeypatch, harness, reason
):
    """Both harnesses wake the conductor once its note has sat 600 s. On kiro a
    steer still pending is proven unread (its read is reported when the model takes
    it). On claude it is reported read only when the turn ends cleanly, so the wake
    calls it unconfirmed, never unread. One wake per message: the next tick says
    nothing. A second stuck steer wakes once more on kiro, whose read of the first
    took it off the pending list. A claude turn keeps every steer pending until it
    ends, so a second one in the same turn waits behind the first's spent wake: its
    steers wake their sender once per worker turn, and the next turn's wakes again."""
    from kiro_crew import ledger_wake
    from kiro_crew.dashboard import queue_age

    clock = _Clock()
    monkeypatch.setattr(queue_age, "_clock", clock)
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _busy(worker)
    worker._acp_client = _harness_client(monkeypatch, harness)
    board, item_id = _bind(conductor, worker)

    assert _send(state, conductor, worker)["steered"] is True
    assert not any("reason=message_" in b for b in _wake_briefs(state, board))

    clock.now += ledger_wake.UNREAD_WAKE_SECS
    first = [b for b in _wake_briefs(state, board) if "reason=message_" in b]
    # The unconfirmed wake says its turn still runs: the model most likely read it.
    turn = "turn=running " if reason == "message_unconfirmed" else ""
    assert first == [
        f"[work-ledger wake] item={item_id} reason={reason} count=1 target={worker.key} "
        f"{turn}oldest_waited_secs={int(ledger_wake.UNREAD_WAKE_SECS)}"
    ]
    assert not any("reason=message_" in b for b in _wake_briefs(state, board))

    clock.now += 30
    if harness == "kiro":
        # The model took the first steer at its next step, and kiro reports that
        # read at once, which takes it off the pending list.
        (orange,) = worker._pending_steers
        worker._pending_steers.remove(orange)
    assert _send(state, conductor, worker, message="LEMON")["steered"] is True
    clock.now += ledger_wake.UNREAD_WAKE_SECS
    again = [b for b in _wake_briefs(state, board) if "reason=message_" in b]
    if harness == "claude":
        assert again == [], "a claude turn's steers wake their sender once per worker turn"
        # The turn ends cleanly, which reports both steers read, and the next turn
        # takes a new one.
        worker._pending_steers.clear()
        _busy(worker)
        assert _send(state, conductor, worker, message="LIME")["steered"] is True
        clock.now += ledger_wake.UNREAD_WAKE_SECS
        again = [b for b in _wake_briefs(state, board) if "reason=message_" in b]
    assert len(again) == 1 and f"reason={reason} count=1 target={worker.key}" in again[0]


def test_an_unread_queued_note_beside_an_unconfirmed_steer_counts_each_apart(tmp_path, monkeypatch):
    """claude: one note the person's turn kept in the queue (proven unread) and one
    steer the turn took (unconfirmed). The wake is ``message_unread`` for the one it
    can prove and names the other apart."""
    from kiro_crew import ledger_wake
    from kiro_crew.dashboard import queue_age

    clock = _Clock()
    monkeypatch.setattr(queue_age, "_clock", clock)
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _busy(worker)
    worker._acp_client = _harness_client(monkeypatch, "claude")
    board, item_id = _bind(conductor, worker)

    assert _send(state, conductor, worker)["steered"] is True
    worker._turn_owner_sent = True  # a person now drives the turn: the next one queues
    assert _send(state, conductor, worker, message="LEMON").get("steered") is not True
    clock.now += ledger_wake.UNREAD_WAKE_SECS

    briefs = [b for b in _wake_briefs(state, board) if "reason=message_" in b]
    assert briefs == [
        f"[work-ledger wake] item={item_id} reason=message_unread count=1 unconfirmed=1 "
        f"target={worker.key} oldest_waited_secs={int(ledger_wake.UNREAD_WAKE_SECS)}"
    ]


def test_a_note_queued_after_an_unconfirmed_steer_wakes_on_its_own_wait(tmp_path, monkeypatch):
    """claude: the turn took a steer, and it stays pending until that turn ends. Its
    wake comes at 600 s. A note the person's turn queues later in the same turn is
    aged and keyed on its own stamp, not the steer's spent one: it wakes nothing
    while young and once when it has sat 600 s."""
    from kiro_crew import ledger_wake
    from kiro_crew.dashboard import queue_age

    clock = _Clock()
    monkeypatch.setattr(queue_age, "_clock", clock)
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _busy(worker)
    worker._acp_client = _harness_client(monkeypatch, "claude")
    board, item_id = _bind(conductor, worker)
    secs = int(ledger_wake.UNREAD_WAKE_SECS)

    assert _send(state, conductor, worker)["steered"] is True
    clock.now += ledger_wake.UNREAD_WAKE_SECS
    assert [b for b in _wake_briefs(state, board) if "reason=message_" in b] == [
        f"[work-ledger wake] item={item_id} reason=message_unconfirmed count=1 "
        f"target={worker.key} turn=running oldest_waited_secs={secs}"
    ]

    clock.now += 100
    worker._turn_owner_sent = True  # a person now drives the turn: the next one queues
    assert _send(state, conductor, worker, message="LEMON").get("steered") is not True
    assert not any("reason=message_" in b for b in _wake_briefs(state, board))
    clock.now += ledger_wake.UNREAD_WAKE_SECS
    assert [b for b in _wake_briefs(state, board) if "reason=message_" in b] == [
        f"[work-ledger wake] item={item_id} reason=message_unread count=1 unconfirmed=1 "
        f"target={worker.key} oldest_waited_secs={secs}"
    ]


@pytest.mark.parametrize("harness", ["kiro", "claude"])
def test_a_worker_reply_in_its_conductors_turn_never_stands_for_a_queued_note(
    tmp_path, monkeypatch, harness
):
    """``report_unread`` counts, ages and keys on the conductor's queue alone. The
    worker's reply steers into the conductor's asking turn and stays pending there
    (kiro: unread behind one long tool call; claude: unconfirmed until the turn
    ends). It is in the turn the wake would go to, so it wakes nothing however long
    it waits. A plain note the worker queues 700 s later is aged and keyed on its
    own stamp: nothing while young, one wake once it has sat 600 s."""
    from kiro_crew import ledger_wake
    from kiro_crew.dashboard import queue_age

    clock = _Clock()
    monkeypatch.setattr(queue_age, "_clock", clock)
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _busy(conductor)
    conductor._acp_client = _harness_client(monkeypatch, harness)
    _live(worker)
    board, item_id = _bind(conductor, worker)
    _send(state, conductor, worker)  # the conductor's turn asks its worker

    reply = _send(state, worker, conductor)
    assert reply["steered"] is True and reply["steerDefault"] == "reply"
    clock.now += 700
    assert not any("reason=report_unread" in b for b in _wake_briefs(state, board))

    note = _send(state, worker, conductor, message="LEMON")
    assert note["steered"] is False and note.get("toCreator") is True
    assert not any("reason=report_unread" in b for b in _wake_briefs(state, board))
    clock.now += ledger_wake.UNREAD_WAKE_SECS
    assert [b for b in _wake_briefs(state, board) if "reason=report_unread" in b] == [
        f"[work-ledger wake] item={item_id} reason=report_unread count=1 "
        f"from={worker.key} oldest_waited_secs={int(ledger_wake.UNREAD_WAKE_SECS)}"
    ]


@pytest.mark.parametrize("steer, wakes", [(None, True), (False, False)], ids=["default", "by-name"])
def test_a_worker_note_unread_behind_its_conductors_turn_wakes_the_conductor(
    tmp_path, monkeypatch, steer, wakes
):
    """A worker's note to its creator that is no reply into the turn that asked
    queues. One that names no delivery
    wanted the conductor's running turn, so once it has sat 600 s the conductor's
    own work-ledger watch wakes it, naming the worker and the age, once. A note
    queued by name chose to wait and wakes nobody."""
    from kiro_crew import ledger_wake
    from kiro_crew.dashboard import queue_age

    clock = _Clock()
    monkeypatch.setattr(queue_age, "_clock", clock)
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(conductor)
    board, item_id = _bind(conductor, worker)

    out = _send(state, worker, conductor, steer=steer)

    assert out["steered"] is False and out.get("toCreator") is True and _queued(conductor)
    assert not any("reason=report_unread" in b for b in _wake_briefs(state, board))
    clock.now += ledger_wake.UNREAD_WAKE_SECS
    briefs = [b for b in _wake_briefs(state, board) if "reason=report_unread" in b]
    assert briefs == (
        [
            f"[work-ledger wake] item={item_id} reason=report_unread count=1 "
            f"from={worker.key} oldest_waited_secs={int(ledger_wake.UNREAD_WAKE_SECS)}"
        ]
        if wakes
        else []
    )
    assert not any("reason=report_unread" in b for b in _wake_briefs(state, board))
    assert not any("reason=message_" in b for b in _wake_briefs(state, board))


# ── the route ───────────────────────────────────────────────────────────────


def _route_request(state, caller, body: dict):
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/send"
    request.method = "POST"
    request.headers = {"X-Session-Key": _key(caller)}
    request.query = {}
    request.get = lambda key, default=None: (
        True if key in ("internal_auth", "peer_verified") else default
    )

    async def _json():
        return body

    request.json = _json
    return request


def _route(state, caller, body: dict):
    resp = asyncio.run(handlers_sc.api_session_control_send(_route_request(state, caller, body)))
    return resp.status, json.loads(resp.body.decode())


def test_an_omitted_steer_reaches_the_gateway_as_the_default(tmp_path):
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(worker)

    status, payload = _route(state, conductor, {"target": worker.key, "message": "ORANGE"})

    assert status == 200, payload
    assert payload["steered"] is True and payload["steerDefault"] == "manager"


def test_a_null_steer_is_the_default_and_a_string_is_refused(tmp_path):
    state = _make_state(tmp_path)
    conductor, worker, _, _ = _team(state)
    _live(worker)

    status, payload = _route(
        state, conductor, {"target": worker.key, "message": "ORANGE", "steer": "false"}
    )
    assert status == 400 and payload["code"] == "invalid_steer"
    worker._acp_client.steer.assert_not_awaited()

    status, payload = _route(
        state, conductor, {"target": worker.key, "message": "ORANGE", "steer": None}
    )
    assert status == 200 and payload["steered"] is True


def test_the_route_reads_the_binding_for_a_worker_it_did_not_open(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    _, _, _, stranger = _team(state)
    other = _live(_slot(state, "chat-other"))
    bound_to = {}

    def _read_binding(worker_key):
        bound_to["worker"] = worker_key
        return (_key(stranger), "it_1")

    monkeypatch.setattr(work_ledger, "read_binding", _read_binding)
    monkeypatch.setattr(work_ledger_handback.session_ledger, "ledger_key", lambda key: key)

    status, payload = _route(state, stranger, {"target": other.key, "message": "ORANGE"})

    assert status == 200, payload
    assert bound_to["worker"] == other.key
    assert payload["steered"] is True and payload["steerDefault"] == "manager"


def test_the_route_reads_the_binding_of_a_worker_addressed_by_title(tmp_path, monkeypatch):
    """The binding stores the worker's key; a send by title reads it for the slot
    the title resolves to."""
    state = _make_state(tmp_path)
    _, _, _, stranger = _team(state)
    other = _live(_slot(state, "chat-other"))
    other.title = "Fix the steer default"
    other._titled = True
    bound_to = {}

    def _read_binding(worker_key):
        bound_to["worker"] = worker_key
        return (_key(stranger), "it_1")

    monkeypatch.setattr(work_ledger, "read_binding", _read_binding)
    monkeypatch.setattr(work_ledger_handback.session_ledger, "ledger_key", lambda key: key)

    status, payload = _route(
        state, stranger, {"target": "Fix the steer default", "message": "ORANGE"}
    )

    assert status == 200, payload
    assert bound_to["worker"] == other.key
    assert payload["steered"] is True and payload["steerDefault"] == "manager"


def test_a_binding_that_cannot_be_read_keeps_the_queue(tmp_path, monkeypatch):
    def _boom(_worker_key):
        raise OSError("ledger unreadable")

    monkeypatch.setattr(work_ledger, "read_binding", _boom)

    assert asyncio.run(work_ledger_handback.read_send_binding("chat-a", "chat-b")) is False
