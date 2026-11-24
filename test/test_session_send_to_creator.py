"""A session may ``session_send`` to the one session that created it, and nothing more.

Driven against real slots and the real gate (:func:`authorize_target`) and the real
delivery (:func:`send_to_target`). The creator is read off the CALLING slot's own
``_created_by``; every other verb and every other target keeps the creator fence.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from unittest.mock import MagicMock

import pytest
import source_corpus
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

from kiro_crew import crew_timeline
from kiro_crew.dashboard import relay_spans
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_managers
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.session_control_parts import target_authorization as ta
from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.members import DM_SLOT_MODE

SRC = source_corpus.src_root()
#: The two source scans stream the package; they share a tree-scan group.
_TREE_SCAN = pytest.mark.xdist_group(name="tree_scan_session_send_to_creator")

#: Every verb but ``send`` that goes through the gate, spelled out so a new one is a
#: decision here rather than an inherited admission.
OTHER_VERBS = (
    "read",
    "summary",
    "queue",
    "stop",
    "close",
    "adopt",
    "release",
    "handoff",
    "fork",
    "rename",
    "set_model",
    "set_mode",
    "set_color",
    "set_note",
    "end_wait",
    "reload",
    "status",
    "retry",
    "continue",
    "generate_title",
)


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: False)
    asyncio.run(sc.refresh_manager_chain())
    session_managers.reset_cache()
    yield
    session_managers.reset_cache()


def _team(state):
    """The person's tab, a lead it started, a conductor the lead opened, and two
    workers the conductor opened."""
    owner = _slot(state, "chat-owner")
    lead = _slot(state, "chat-lead")
    conductor = _slot(state, "chat-conductor")
    worker = _slot(state, "chat-worker")
    sibling = _slot(state, "chat-sibling")
    lead._created_by = owner.key
    conductor._created_by = lead.key
    worker._created_by = conductor.key
    sibling._created_by = conductor.key
    return owner, lead, conductor, worker, sibling


def _gate(state, caller, target: str, operation: str = "send", **kwargs):
    return sc.authorize_target(
        state, caller_session_key=_key(caller), target=target, operation=operation, **kwargs
    )


def _refusal(
    state, caller, target: str, operation: str = "send", **kwargs
) -> sc.SessionControlError:
    with pytest.raises(sc.SessionControlError) as info:
        _gate(state, caller, target, operation, **kwargs)
    return info.value


def _send(state, caller, target: str, *, creator_message: bool = True, **kwargs):
    """A send the way the session_send route makes it (``creator_message`` set)."""
    return asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target=target,
            message="done: PR 12",
            creator_message=creator_message,
            **kwargs,
        )
    )


def _calls(name: str):
    """Every call to *name* in the package source, with the file it is in.

    Streamed one file at a time (``source_corpus``), and only the files that name
    it are parsed; the AST then rules out comments and strings.
    """
    for path, text in source_corpus.iter_candidate_sources(require_any=(name,)):
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call):
                func = node.func
                if (getattr(func, "id", None) or getattr(func, "attr", None)) == name:
                    yield path, node


def _relay_senders(slot) -> list[str]:
    return [
        ((entry.get("meta") or {}).get("relay") or {}).get("senderKey", "")
        for entry in slot._queue
        if "done: PR 12" in entry.get("content", "")
    ]


# ── the one admission ───────────────────────────────────────────────────────


def test_a_worker_sends_to_its_creator_by_key_and_lands_tagged(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)

    out = _send(state, worker, conductor.key)

    # A busy creator queues it, exactly as any session_send to a busy target does.
    assert out["started"] is False
    assert out["steered"] is False
    assert _relay_senders(conductor) == [worker.key]
    entry = next(e for e in conductor._queue if "done: PR 12" in e.get("content", ""))
    assert entry["content"].startswith(f"[sent by session {worker.key} via session_send]")


@pytest.mark.parametrize("direction", ["up to the creator", "down to a worker"])
def test_the_text_lands_as_any_session_send_does(tmp_path, direction):
    """The stored text and its relay record are exactly a plain send's either way,
    so every surface that reads a delivered row (bubble, queue card, pinned card,
    crew timeline, drain rebuild) shows it as it shows any other send."""
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    sender, target = (
        (worker, conductor) if direction == "up to the creator" else (conductor, worker)
    )
    _busy(target)

    _send(state, sender, target.key)

    (entry,) = target._queue
    envelope = f"[sent by session {sender.key} via session_send]\n\n"
    assert entry["content"] == envelope + "done: PR 12"
    relay = entry["meta"]["relay"]
    assert relay == {
        "senderKey": sender.key,
        "via": "session_send",
        relay_spans.ENVELOPE_KEY: relay_spans.leading_span(envelope),
    }
    rebuilt = relay_spans.relay_for(entry["content"], [relay], sc._SEND_PROVENANCE)
    assert rebuilt is not None
    assert rebuilt[relay_spans.ENVELOPE_KEY] == relay_spans.leading_span(envelope)
    assert crew_timeline._SEED_RE.match(entry["content"])


def test_a_worker_sends_to_its_creator_by_exact_title(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    conductor.title = "Fork Conductor"
    _busy(conductor)

    assert _gate(state, worker, "Fork Conductor", creator_message=True) is conductor
    _send(state, worker, "Fork Conductor")

    assert _relay_senders(conductor) == [worker.key]


# ── every other verb on the creator ─────────────────────────────────────────


@pytest.mark.parametrize("operation", OTHER_VERBS)
def test_every_other_verb_on_the_creator_is_refused(tmp_path, operation):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)

    exc = _refusal(state, worker, conductor.key, operation)

    assert exc.code == "not_creator"
    assert exc.message == ta.CREATOR_MESSAGE_ONLY


def test_the_admission_lifts_only_for_a_send_that_asks_for_it(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)

    # A send that does not ask for it keeps the fence, so no other caller of the
    # gate inherits the reach by spelling its operation "send".
    assert _refusal(state, worker, conductor.key).code == "not_creator"
    # And the flag lifts nothing for any other verb.
    for operation in OTHER_VERBS:
        exc = _refusal(state, worker, conductor.key, operation, creator_message=True)
        assert exc.code == "not_creator", operation


def test_a_real_stop_on_the_creator_is_refused(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)

    with pytest.raises(sc.SessionControlError) as info:
        asyncio.run(sc.stop_target(state, caller_session_key=_key(worker), target=conductor.key))

    assert info.value.code == "not_creator"
    assert conductor.running


def test_a_steer_to_the_creator_is_refused_and_never_injected(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)
    conductor._acp_client = _steerable(accepted=True)

    with pytest.raises(sc.SessionControlError) as info:
        _send(state, worker, conductor.key, steer=True)

    assert info.value.code == "not_creator"
    assert info.value.message == ta.CREATOR_MESSAGE_ONLY
    conductor._acp_client.steer.assert_not_awaited()
    assert not conductor._queue


@pytest.mark.parametrize("mode", ["queue", "steer"])
def test_a_broadcast_does_not_reach_the_creator(tmp_path, mode):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)
    conductor._acp_client = _steerable(accepted=True)

    # The real verb, naming the creator: its per-target delivery never asks for
    # the opening, so the creator is refused like any other session.
    out = asyncio.run(
        sc.broadcast_to_targets(
            state,
            caller_session_key=_key(worker),
            message="done: PR 12",
            mode=mode,
            targets=[conductor.key],
        )
    )

    ((row,),) = [out["results"]]
    assert row["code"] == "not_creator", row
    assert not conductor._queue
    conductor._acp_client.steer.assert_not_awaited()


# ── every other target ──────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["sibling", "owner", "lead"])
def test_a_worker_still_cannot_send_to_anyone_else(tmp_path, who):
    state = _make_state(tmp_path)
    owner, lead, _, worker, sibling = _team(state)
    target = {"sibling": sibling, "owner": owner, "lead": lead}[who]
    _busy(target)

    with pytest.raises(sc.SessionControlError) as info:
        _send(state, worker, target.key)

    assert info.value.code == "not_creator"
    assert info.value.message != ta.CREATOR_MESSAGE_ONLY
    assert not target._queue


def test_the_creator_is_the_calling_slots_own_never_one_it_names(tmp_path):
    state = _make_state(tmp_path)
    _, lead, conductor, worker, _ = _team(state)
    other = _slot(state, "chat-other-conductor")
    other._created_by = lead.key
    stranger = _slot(state, "chat-stranger")
    stranger._created_by = other.key

    # The stranger's own slot names `other`; naming `conductor`, whose worker it is
    # not, reaches nothing.
    assert _refusal(state, stranger, conductor.key, creator_message=True).code == "not_creator"
    assert _gate(state, stranger, other.key, creator_message=True) is other
    assert _gate(state, worker, conductor.key, creator_message=True) is conductor


def test_an_unverified_creator_reaches_nothing(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    worker._created_by = sc.UNVERIFIED_CREATOR

    assert _refusal(state, worker, conductor.key, creator_message=True).code == "not_creator"


def test_a_fenced_caller_with_a_blank_creator_reaches_nothing(tmp_path):
    state = _make_state(tmp_path)
    owner, _, conductor, _, _ = _team(state)
    bot_chat = _slot(state, "chat-bot")
    assert bot_chat._created_by == ""

    # Fenced by a verified member admission, the HTTP gate's carried verdict.
    for target in (owner, conductor):
        exc = _refusal(
            state, bot_chat, target.key, creator_message=True, precomputed_ownership_fenced=True
        )
        assert exc.code == "not_creator"


def test_a_handoff_does_not_change_who_the_creator_is(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    new_manager = _slot(state, "chat-new-manager")
    new_manager._created_by = conductor.key
    # The conductor hands its worker to another session it manages.
    result = asyncio.run(
        sc.handoff_target(
            state, caller_session_key=_key(conductor), target=worker.key, to=new_manager.key
        )
    )

    assert result["manager"] == new_manager.key
    assert session_managers.holder(worker) == new_manager.key
    assert worker._created_by == conductor.key
    assert _gate(state, worker, conductor.key, creator_message=True) is conductor
    assert _refusal(state, worker, new_manager.key, creator_message=True).code == "not_creator"
    assert _refusal(state, worker, conductor.key, "stop").code == "not_creator"


def test_a_steer_by_a_worker_that_manages_its_creator_is_not_marked_a_report(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    # Another road to the creator: a handoff made the worker its creator's manager,
    # so the ordinary fence admits the steer. It is not the opening's plain send.
    session_managers.write({conductor.key: (worker.key, conductor._tab_id)})
    _busy(conductor)
    conductor._acp_client = _steerable(accepted=True)

    out = _send(state, worker, conductor.key, steer=True)

    assert out["steered"] is True
    conductor._acp_client.steer.assert_awaited()
    assert "toCreator" not in out


# ── the nearby callers keep their reach ─────────────────────────────────────


def test_a_scheduled_runs_child_still_cannot_reach_the_scheduled_run(tmp_path):
    state = _make_state(tmp_path)
    cron_tab = _slot(state, "cron-nightly")
    child = _slot(state, "chat-cron-child")
    child._created_by = cron_tab.key

    assert _refusal(state, child, cron_tab.key, creator_message=True).code == "unattended_target"


def test_a_child_of_a_channel_session_still_cannot_reach_it(tmp_path):
    state = _make_state(tmp_path)
    dm = _slot(state, "chat-owner-dm")
    dm.linked_session_key = "slack:T1:D1"
    child = _slot(state, "chat-dm-child")
    child._created_by = dm.key

    exc = _refusal(state, child, dm.key, creator_message=True)
    assert exc.code == "linked_session_target"


def test_an_app_scoped_child_still_cannot_reach_its_creator(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    worker._app = "work-items"

    assert _refusal(state, worker, conductor.key, creator_message=True).code == "app_scoped_caller"


def test_a_child_in_another_workspace_still_cannot_reach_its_creator(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    worker.workspace = "elsewhere"

    assert _refusal(state, worker, conductor.key, creator_message=True).code == "workspace_mismatch"


def test_a_cron_stamp_names_no_creator(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    cron_tab = _slot(state, "cron-nightly")
    worker._created_by = "cron:nightly"

    for target in (conductor, cron_tab):
        assert not ta._is_callers_creator(worker, target)
        assert _refusal(state, worker, target.key, creator_message=True).code in (
            "not_creator",
            "unattended_target",
        )


@pytest.mark.parametrize("stamp", ["cron:nightly", sc.UNVERIFIED_CREATOR])
def test_no_slot_can_take_a_key_that_a_placeholder_stamp_would_match(tmp_path, stamp):
    """The placeholders hold a ``:``, which a slot key never keeps."""
    state = _make_state(tmp_path)
    worker = _slot(state, "chat-worker")
    worker._created_by = stamp
    lookalike = _slot(state, stamp)

    assert ":" not in lookalike.key
    assert not ta._is_callers_creator(worker, lookalike)


def test_a_creator_mirrored_to_a_channel_is_not_reachable(tmp_path, _floor_monkeypatch):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _floor_monkeypatch.setattr(
        sc, "_has_channel_mirror", lambda _state, slot, **_kw: slot.key == conductor.key
    )

    assert _refusal(state, worker, conductor.key, creator_message=True).code == "mirrored_target"


def test_a_bot_thread_worker_reaches_its_bot_thread_and_nothing_else(tmp_path):
    state = _make_state(tmp_path)
    owner, _, conductor, _, _ = _team(state)
    bot = _slot(state, "member-victor", mode=DM_SLOT_MODE)
    worker = _slot(state, "chat-bot-worker")
    worker._created_by = bot.key

    assert _gate(state, worker, bot.key, creator_message=True) is bot
    assert _refusal(state, worker, bot.key, "read").code == "not_creator"
    for target in (owner, conductor):
        assert _refusal(state, worker, target.key, creator_message=True).code == "not_creator"


# ── the opening is asked for, and only by the session_send route ───────────


def test_a_send_that_does_not_ask_keeps_the_fence(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)

    with pytest.raises(sc.SessionControlError) as info:
        _send(state, worker, conductor.key, creator_message=False)

    assert info.value.code == "not_creator"
    assert not conductor._queue


@_TREE_SCAN
def test_only_the_session_send_route_asks_for_the_creator_opening():
    askers = [
        (path.relative_to(SRC).as_posix(), node)
        for path, node in _calls("send_to_target")
        if any(k.arg == "creator_message" for k in node.keywords)
    ]
    assert [where for where, _ in askers] == ["dashboard/handlers/session_control.py"]
    # And there it is the plain send, not the create's seed.
    ((_, call),) = askers
    assert not any(k.arg == "via" for k in call.keywords)


def _gate_verbs() -> tuple[set[str], set[str], list[str]]:
    """The verbs the gate is asked about, the wrappers that pass one on, and any
    call this scan cannot read.

    A wrapper is a function that takes ``operation`` and hands it to the gate, or
    to another wrapper, as ``operation=operation``; found to a fixpoint, so a verb
    that reaches the gate through one is counted at the wrapper's call. A nested
    function reads its enclosing function's ``operation``. A function that hands
    on a LOCAL ``operation`` counts the strings it assigns to it.
    """
    # Only the files that name a gate are parsed, and only their trees are kept;
    # the corpus itself is streamed one file at a time (``source_corpus``).
    trees: dict[pathlib.Path, ast.AST] = {}

    def params_of(fn) -> set[str]:
        return {a.arg for a in fn.args.args + fn.args.posonlyargs + fn.args.kwonlyargs}

    def gate_calls(names):
        """``(path, call, enclosing functions innermost first)`` for each call to *names*."""
        for path, text in source_corpus.iter_candidate_sources(require_any=tuple(names)):
            if path not in trees:
                trees[path] = ast.parse(text)
            stack: list = []

            def visit(node):
                is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                if is_fn:
                    stack.insert(0, node)
                if isinstance(node, ast.Call):
                    func = node.func
                    if (getattr(func, "id", None) or getattr(func, "attr", None)) in names:
                        found.append((path, node, list(stack)))
                for child in ast.iter_child_nodes(node):
                    visit(child)
                if is_fn:
                    stack.pop(0)

            found: list = []
            visit(trees[path])
            yield from found

    def operation_arg(call):
        return next((k.value for k in call.keywords if k.arg == "operation"), None)

    def forwards(call) -> bool:
        arg = operation_arg(call)
        return isinstance(arg, ast.Name) and arg.id == "operation"

    def owner_of_operation(enclosing):
        """The function whose parameter ``operation`` is, or None for a local."""
        return next((fn for fn in enclosing if "operation" in params_of(fn)), None)

    gates = {"authorize_target"}
    while True:
        found_wrappers = {
            owner.name
            for _, call, enclosing in gate_calls(gates)
            if forwards(call) and (owner := owner_of_operation(enclosing)) is not None
        }
        if found_wrappers <= gates:
            break
        gates |= found_wrappers

    verbs: set[str] = set()
    unread: list[str] = []
    for path, call, enclosing in gate_calls(gates):
        arg = operation_arg(call)
        where = f"{path.relative_to(SRC).as_posix()}:{call.lineno}"
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            verbs.add(arg.value)
        elif forwards(call) and owner_of_operation(enclosing) is not None:
            continue  # a wrapper: its own callers are counted
        elif forwards(call) and enclosing:
            assigned = [
                n.value.value
                for n in ast.walk(enclosing[-1])
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "operation" for t in n.targets)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            ]
            if assigned:
                verbs.update(assigned)
            else:
                unread.append(where)
        else:
            unread.append(where)
    return verbs, gates, unread


@_TREE_SCAN
def test_every_verb_the_gate_is_asked_about_is_listed_here():
    """``OTHER_VERBS`` is checked against the source, so a new verb is a decision."""
    verbs, gates, unread = _gate_verbs()

    assert {"authorize_created_target", "_continue_turn_of_target"} <= gates, gates
    assert not unread, f"a gate call whose verb this scan cannot read: {unread}"
    assert {"send", "stop", "generate_title"} <= verbs, verbs
    assert verbs <= set(OTHER_VERBS) | {"send"}, verbs - set(OTHER_VERBS) - {"send"}


# ── the route and the tool reply ────────────────────────────────────────────


def _route_request(state, caller, body: dict):
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/send"
    request.method = "POST"
    request.headers = {"X-Session-Key": _key(caller)}
    request.query = {}
    # The secret and the kernel peer attestation, as an internal caller arrives.
    request.get = lambda key, default=None: (
        True if key in ("internal_auth", "peer_verified") else default
    )

    async def _json():
        return body

    request.json = _json
    return request


def test_the_send_route_reaches_the_creator_and_says_so(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)
    body = {"target": conductor.key, "message": "done: PR 12"}

    resp = asyncio.run(handlers_sc.api_session_control_send(_route_request(state, worker, body)))

    assert resp.status == 200, resp.body
    payload = json.loads(resp.body.decode())
    assert payload["toCreator"] is True and payload["started"] is False
    assert _relay_senders(conductor) == [worker.key]


def test_the_send_route_refuses_a_steer_to_the_creator(tmp_path):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    _busy(conductor)
    conductor._acp_client = _steerable(accepted=True)
    body = {"target": conductor.key, "message": "done: PR 12", "steer": True}

    resp = asyncio.run(handlers_sc.api_session_control_send(_route_request(state, worker, body)))

    assert resp.status == 403
    assert json.loads(resp.body.decode())["code"] == "not_creator"
    conductor._acp_client.steer.assert_not_awaited()


def test_a_send_to_anyone_else_carries_no_creator_mark(tmp_path):
    state = _make_state(tmp_path)
    owner, lead, _, _, _ = _team(state)
    _busy(lead)

    assert "toCreator" not in _send(state, owner, lead.key)


@pytest.mark.parametrize("started", [True, False])
def test_the_tool_reply_never_points_a_worker_at_a_refused_read(started):
    resp = {"ok": True, "target": "chat-c", "started": started, "steered": False, "toCreator": True}
    dash = InMemoryDashboardClient({"POST /api/session-control/send": resp})

    out = TABLE.call(
        "session_send",
        {"target": "chat-c", "message": "done"},
        ToolContext(dash, Caller.strict("dashboard:chat-w")),
    )

    assert "session that created you" in out
    assert "session_read_message" not in out


# ── a full queue ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["creator", "child"])
def test_a_send_to_a_full_queue_is_refused(tmp_path, who):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    sender, target = (worker, conductor) if who == "creator" else (conductor, worker)
    _busy(target)
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        target.queue_append(f"waiting {n}")

    with pytest.raises(sc.SessionControlError) as info:
        _send(state, sender, target.key)

    assert info.value.code == "queue_full"
    assert len(target._queue) == MAX_LIVE_QUEUE_ENTRIES


def test_a_send_to_an_idle_session_with_a_full_queue_still_starts_its_turn(
    tmp_path, _floor_monkeypatch
):
    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        conductor.queue_append(f"waiting {n}")
    ran: list[str] = []

    async def _fake_run_chat(_state, _slot, prompt):
        ran.append(prompt)

    _floor_monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    async def _drive():
        out = await sc.send_to_target(
            state,
            caller_session_key=_key(worker),
            target=conductor.key,
            message="done: PR 12",
            creator_message=True,
        )
        await asyncio.gather(*list(state._background_tasks))
        return out

    out = asyncio.run(_drive())

    # Idle: the send is the turn that drains the queue, so it is never refused.
    assert out["started"] is True and out["toCreator"] is True
    assert len(ran) == 1 and ran[0].endswith("done: PR 12")


def test_a_send_to_a_paused_session_with_a_full_queue_is_refused(tmp_path, _floor_monkeypatch):
    from kiro_crew import crew_pause

    state = _make_state(tmp_path)
    _, _, conductor, worker, _ = _team(state)
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        conductor.queue_append(f"waiting {n}")
    # Idle, but a pause holds it: the send would wait, so the bound applies.
    _floor_monkeypatch.setattr(crew_pause, "holds_slot", lambda slot: slot is conductor)

    with pytest.raises(sc.SessionControlError) as info:
        _send(state, worker, conductor.key)

    assert info.value.code == "queue_full"
    assert len(conductor._queue) == MAX_LIVE_QUEUE_ENTRIES
