"""Send all held: one press lets every held card the person counted go.

``POST /api/chat/slots/{slot}/queue/send-held`` marks each named card that still
waits for a person the way a card's "run this next" does, and never stops a
running turn: the drain sends the marked cards where they stand in the queue,
one per turn, judging each when its turn comes. On an idle slot it starts the
drain through Send now's own start. Only the owner's dashboard may press it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import queue_edit_hold, queue_send_held
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.session_control_parts import delivery_containment as dc
from kiro_crew.dashboard.slot_queue_repository import (
    RESTORED_BOOT_ACCEPTED_KEY,
    RESTORED_HELD_AGAIN_KEY,
    RESTORED_QUEUE_KEY,
    RESTORED_SEND_CHOSEN_KEY,
    durable_queue_entries,
    is_held_queue_entry,
    sanitize_restored_queue,
)

_ROUTE = "/api/chat/slots/{slot}/queue/send-held"


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch):
    """Session control on, as shipped, and every SEL write to one mock."""
    fake = MagicMock()
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _floor_monkeypatch.setattr(sc, "sel", lambda: fake)
    _floor_monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    _floor_monkeypatch.setattr("kiro_crew.sel.sel", lambda: fake)
    return fake


@pytest.fixture
def turns(monkeypatch):
    """Turns start without a model: the drain's real pick, row and pop run, and the
    spawned turn is a finished task, so the next drain can run straight after."""

    async def _run_chat(_state, _slot, _prompt, **_kwargs):
        return None

    def _spawn(_state, _slot, coro):
        coro.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    monkeypatch.setattr(cr, "_run_chat", _run_chat)
    monkeypatch.setattr(cr, "spawn_guarded_turn", _spawn)


def _held(queue_id: str, text: str) -> dict:
    return {"id": queue_id, "content": text, "meta": {}, RESTORED_QUEUE_KEY: True}


def _fresh(queue_id: str, text: str) -> dict:
    return {"id": queue_id, "content": text, "meta": {}}


def _state(tmp_path, queue: list[dict]):
    state = _make_state(tmp_path)
    state.subagents = None
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("held-chat")
    slot._queue[:] = queue
    return state, slot


def _busy(slot) -> None:
    """``running`` is derived from the task: one that is not done is a running turn."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task


def _app(state, *, user: str = "local-app", app_id: str = "") -> web.Application:
    @web.middleware
    async def _auth(request, handler):
        request["app"] = app_id
        request["user"] = user
        request["is_dashboard_user"] = app_id == ""
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post(_ROUTE, queue_send_held.api_chat_slot_queue_send_held)
    return app


async def _press(state, ids, **who):
    async with TestClient(TestServer(_app(state, **who))) as client:
        resp = await client.post(
            "/api/chat/slots/held-chat/queue/send-held", json={"queue_ids": ids}
        )
        return resp.status, await resp.json()


def _sent_rows(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "user"]


def _releases(state) -> list[dict]:
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_release"]


@pytest.mark.asyncio
async def test_only_the_named_cards_still_held_are_let_go_and_nothing_moves(tmp_path):
    state, slot = _state(
        tmp_path,
        [
            _fresh("f1", "fresh"),
            _held("h1", "first"),
            _held("h2", "second"),
            _held("h3", "held after the box opened"),
            {**_held("b1", "parked send"), RESTORED_BOOT_ACCEPTED_KEY: True},
        ],
    )
    _busy(slot)

    status, body = await _press(state, ["h2", "h1", "f1", "b1", "gone"])

    assert status == 200
    assert body == {"ok": True, "sent": ["h1", "h2"], "refused": [], "started": False}
    assert [q["id"] for q in slot._queue] == ["f1", "h1", "h2", "h3", "b1"]
    assert [q["id"] for q in slot._queue if q.get(RESTORED_SEND_CHOSEN_KEY)] == ["h1", "h2"]
    assert is_held_queue_entry(slot._queue[3])
    assert _releases(state) == [{"slot": "held-chat", "queue_ids": ["h1", "h2"]}]


@pytest.mark.asyncio
async def test_a_running_turn_is_not_stopped(tmp_path, monkeypatch):
    state, slot = _state(tmp_path, [_held("h1", "first"), _held("h2", "second")])
    _busy(slot)
    stop_turn = AsyncMock(return_value="soft")
    monkeypatch.setattr(state.sessions, "stop_turn", stop_turn)
    start = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.start_queued_card", start)

    status, body = await _press(state, ["h1", "h2"])

    assert status == 200 and body["sent"] == ["h1", "h2"] and body["started"] is False
    stop_turn.assert_not_awaited()
    start.assert_not_awaited()
    assert slot.running and slot._stop_state == "idle" and slot._stop_event_id is None


@pytest.mark.asyncio
async def test_the_turn_end_drain_sends_them_oldest_first_and_fresh_rows_keep_their_place(
    tmp_path, turns
):
    state, slot = _state(
        tmp_path,
        [
            _held("h0", "not counted"),
            _held("h1", "first"),
            _fresh("f1", "queued since the restart"),
            _held("h2", "second"),
        ],
    )
    _busy(slot)
    await _press(state, ["h1", "h2"])

    for _ in range(4):
        slot.task = None  # the running turn ended; its drain runs
        await cr._start_next_queued_turn(state, slot)

    assert _sent_rows(slot) == ["first", "queued since the restart", "second"]
    assert [q["id"] for q in slot._queue] == ["h0"]
    assert is_held_queue_entry(slot._queue[0])


@pytest.mark.asyncio
async def test_an_idle_chat_starts_the_first_one_through_send_nows_start(
    tmp_path, turns, monkeypatch
):
    from kiro_crew.dashboard import chat_handlers

    state, slot = _state(
        tmp_path, [_held("h0", "not counted"), _held("h1", "first"), _held("h2", "second")]
    )
    real_start = chat_handlers.start_queued_card
    seen: list[tuple] = []

    async def _start(state_, slot_, name, request, queue_id, **kwargs):
        seen.append((name, queue_id, kwargs))
        return await real_start(state_, slot_, name, request, queue_id, **kwargs)

    monkeypatch.setattr(chat_handlers, "start_queued_card", _start)

    status, body = await _press(state, ["h1", "h2"])

    assert status == 200 and body["started"] is True
    assert seen == [("held-chat", None, {"tool_name": "dashboard_send_held"})]
    assert _sent_rows(slot) == ["first"]
    assert [q["id"] for q in slot._queue] == ["h0", "h2"]


@pytest.mark.asyncio
async def test_a_card_open_for_editing_stays_held_and_is_named_while_the_others_go(tmp_path, turns):
    state, slot = _state(tmp_path, [_held("h1", "being edited"), _held("h2", "second")])
    slot._queue[0][queue_edit_hold.EDIT_HOLD_KEY] = queue_edit_hold._clock() + 60.0
    _busy(slot)

    status, body = await _press(state, ["h1", "h2"])

    assert status == 200
    assert body["sent"] == ["h2"]
    assert body["refused"] == [
        {"queue_id": "h1", "code": "queue_item_editing", "error": "It is open for editing."}
    ]
    assert is_held_queue_entry(slot._queue[0])
    slot._queue[0].pop(queue_edit_hold.EDIT_HOLD_KEY)
    slot.task = None
    await cr._start_next_queued_turn(state, slot)
    assert _sent_rows(slot) == ["second"]
    assert [q["id"] for q in slot._queue] == ["h1"]


@pytest.mark.asyncio
async def test_a_card_its_own_check_stops_at_its_turn_stays_held_and_the_others_go(tmp_path, turns):
    """The drain judges each card when its turn comes. A restored card that fails
    that check (here: admitted under another workspace) is not deleted: it is
    held again with its words, one notice says why, and the next card goes."""
    state, slot = _state(tmp_path, [])
    _busy(slot)
    slot.workspace = "team-a"

    async def _never_runs(*_args):  # pragma: no cover - queued, not run
        raise AssertionError("queued, not run")

    slot.enqueue_or_run_prompt("first", _never_runs, state)
    slot._queue[0].update({"id": "h1", RESTORED_QUEUE_KEY: True})
    slot._queue.append(_held("h2", "second"))
    await _press(state, ["h1", "h2"])

    slot.workspace = "team-b"
    slot.task = None
    assert await cr._start_next_queued_turn(state, slot) is True

    assert _sent_rows(slot) == ["second"]
    assert _held_texts(slot) == [("h1", "first")]
    assert RESTORED_SEND_CHOSEN_KEY not in slot._queue[0]
    notices = _notices(slot)
    assert len(notices) == 1
    assert "kept held" in notices[0] and "different workspace" in notices[0]


def _contain(slot, monkeypatch, how: str) -> None:
    """Put *slot* under a limit a restored card's admission never saw."""
    if how == "linked":
        slot.linked_session_key = "C0LINKED|1700000000.000100"
    elif how == "mirrored":
        monkeypatch.setattr(dc, "_probe_channel_mirror", lambda _state, _slot: "slack:C0MIRROR:")
    elif how == "incognito":
        slot.memory_mode = "ephemeral"
    else:
        slot._app = "some-app"


_LIMITS = ["linked", "mirrored", "incognito", "app-scoped"]


def _held_texts(slot) -> list[tuple[str, str]]:
    return [(q["id"], q["content"]) for q in slot._queue if is_held_queue_entry(q)]


def _notices(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "notice"]


@pytest.mark.parametrize("how", _LIMITS)
@pytest.mark.asyncio
async def test_a_card_the_drain_would_drop_stays_held_with_its_words(
    tmp_path, turns, monkeypatch, how
):
    """A restore strips each card's admission snapshot, so under a channel link,
    mirror or other limit the drain would delete every one at its turn. None is
    let go: each stays held, words and all, and is named back; nothing starts."""
    state, slot = _state(tmp_path, [_held("h1", "first"), _held("h2", "second")])
    _contain(slot, monkeypatch, how)

    status, body = await _press(state, ["h1", "h2"])

    assert status == 200
    assert body["sent"] == [] and body["started"] is False
    assert [(r["queue_id"], r["code"]) for r in body["refused"]] == [
        ("h1", "queue_item_contained"),
        ("h2", "queue_item_contained"),
    ]
    assert _held_texts(slot) == [("h1", "first"), ("h2", "second")]
    assert _sent_rows(slot) == [] and _notices(slot) == []
    assert _releases(state) == []
    # A later drain (a turn ending) still leaves them held.
    await cr._start_next_queued_turn(state, slot)
    assert _held_texts(slot) == [("h1", "first"), ("h2", "second")] and _notices(slot) == []


@pytest.mark.asyncio
async def test_under_a_link_only_the_cards_the_drain_would_send_go(tmp_path, turns):
    """The same test the drain runs: a card admitted in the linked chat (its stamp
    says linked) goes, the restored one beside it stays held."""
    state, slot = _state(tmp_path, [])
    _busy(slot)
    slot.linked_session_key = "C0LINKED|1700000000.000100"

    async def _never_runs(*_args):  # pragma: no cover - queued, not run
        raise AssertionError("queued, not run")

    slot.enqueue_or_run_prompt("admitted linked", _never_runs, state)
    slot._queue[0].update({"id": "h1", RESTORED_QUEUE_KEY: True})
    slot._queue.append(_held("h2", "restored"))

    status, body = await _press(state, ["h1", "h2"])

    assert body["sent"] == ["h1"]
    assert [(r["queue_id"], r["code"]) for r in body["refused"]] == [("h2", "queue_item_contained")]
    slot.task = None
    await cr._start_next_queued_turn(state, slot)
    assert _sent_rows(slot) == ["admitted linked"]
    assert _held_texts(slot) == [("h2", "restored")] and _notices(slot) == []


def _send_now_app(state) -> web.Application:
    from kiro_crew.dashboard.chat_api import turn_stop

    app = _app(state)
    app.router.add_post("/api/chat/slots/{slot}/interrupt", turn_stop.api_chat_slot_interrupt)
    return app


async def _send_now(state, queue_id: str):
    async with TestClient(TestServer(_send_now_app(state))) as client:
        resp = await client.post("/api/chat/slots/held-chat/interrupt", json={"queue_id": queue_id})
        return resp.status, await resp.json()


@pytest.mark.parametrize("how", _LIMITS)
@pytest.mark.asyncio
async def test_send_now_on_an_idle_chat_keeps_a_card_the_drain_would_drop(
    tmp_path, turns, monkeypatch, how
):
    """A card's own Send now asks the same test before it starts anything."""
    state, slot = _state(tmp_path, [_held("h1", "first"), _held("h2", "second")])
    _contain(slot, monkeypatch, how)

    status, body = await _send_now(state, "h1")

    assert status == 409 and body["code"] == "queue_item_contained"
    assert _held_texts(slot) == [("h1", "first"), ("h2", "second")]
    assert _sent_rows(slot) == [] and _notices(slot) == []


@pytest.mark.parametrize("how", _LIMITS)
@pytest.mark.asyncio
async def test_send_now_on_a_running_chat_neither_stops_the_turn_nor_marks_the_card(
    tmp_path, turns, monkeypatch, how
):
    state, slot = _state(tmp_path, [_held("h1", "first"), _held("h2", "second")])
    _contain(slot, monkeypatch, how)
    _busy(slot)
    stop_turn = AsyncMock(return_value="soft")
    monkeypatch.setattr(state.sessions, "stop_turn", stop_turn)

    status, body = await _send_now(state, "h2")

    assert status == 409 and body["code"] == "queue_item_contained"
    stop_turn.assert_not_awaited()
    assert slot.running and slot._stop_state == "idle" and slot._stop_event_id is None
    assert [q["id"] for q in slot._queue] == ["h1", "h2"]
    # The turn's own end then leaves both held, words and all.
    slot.task = None
    await cr._start_next_queued_turn(state, slot)
    assert _held_texts(slot) == [("h1", "first"), ("h2", "second")] and _notices(slot) == []


_WORDS = ["first words", "second words", "third words"]


def _restarted(tmp_path):
    """Three cards queued on a busy chat, written by the durable writer and read
    back by the restore reader into a fresh state: the restart."""
    before = _make_state(tmp_path / "before")
    before.subagents = None
    old = before.get_or_create_slot("held-chat")
    _busy(old)

    async def _never_runs(*_args):  # pragma: no cover - queued, not run
        raise AssertionError("queued, not run")

    for text in _WORDS:
        old.enqueue_or_run_prompt(text, _never_runs, before)
    saved = json.loads(json.dumps(durable_queue_entries(old._queue)))
    state, slot = _state(tmp_path / "after", sanitize_restored_queue(saved))
    assert [text for _id, text in _held_texts(slot)] == _WORDS
    return state, slot


def _late_limit(state, slot, monkeypatch, how: str) -> None:
    """A limit that comes after the press; ``probe-fails`` is one mirror-store read
    that does not answer, with nothing else changed."""
    if how == "probe-fails":

        def _unreadable(_key):
            raise OSError("store did not answer")

        monkeypatch.setattr(state.sessions, "get_mirror_link", _unreadable)
    else:
        _contain(slot, monkeypatch, how)


_LATE = {
    "linked": "the session was linked to a channel",
    "mirrored": "the session gained an outbound channel mirror",
    "incognito": "the session became incognito/temporary",
    "probe-fails": "the session's channel-mirror state could not be verified",
}


@pytest.mark.parametrize("how", sorted(_LATE))
@pytest.mark.asyncio
async def test_a_limit_after_send_all_holds_the_rest_again_with_their_words(
    tmp_path, turns, monkeypatch, how
):
    """Send all on a running chat marks all three. The first runs; while its turn
    runs the chat gains a limit. Its end must not delete the other two: they are
    held again, words and all, and one notice names the reason once."""
    state, slot = _restarted(tmp_path)
    ids = [q["id"] for q in slot._queue]
    _busy(slot)
    stop_turn = AsyncMock(return_value="soft")
    monkeypatch.setattr(state.sessions, "stop_turn", stop_turn)

    status, body = await _press(state, ids)
    assert status == 200 and body["sent"] == ids
    slot.task = None
    assert await cr._start_next_queued_turn(state, slot) is True
    _late_limit(state, slot, monkeypatch, how)
    for _ in range(2):
        await cr._start_next_queued_turn(state, slot)

    assert _sent_rows(slot) == ["first words"]
    assert [text for _id, text in _held_texts(slot)] == _WORDS[1:]
    assert not any(q.get(RESTORED_SEND_CHOSEN_KEY) for q in slot._queue)
    notices = _notices(slot)
    assert len(notices) == 1 and "kept held" in notices[0]
    assert notices[0].count(_LATE[how]) == 1
    notice_rows = [m for m in slot.messages if m.get("role") == "notice"]
    assert notice_rows[0]["meta"]["notice"] == "held_message_kept"
    stop_turn.assert_not_awaited()
    # Pressed again under the limit: nothing goes, nothing is sent twice.
    status, body = await _press(state, ids)
    assert body["sent"] == [] and [r["code"] for r in body["refused"]] == [
        "queue_item_contained",
        "queue_item_contained",
    ]
    await cr._start_next_queued_turn(state, slot)
    assert _sent_rows(slot) == ["first words"] and len(_notices(slot)) == 1


@pytest.mark.asyncio
async def test_a_link_after_send_now_holds_the_card_again(tmp_path, turns, monkeypatch):
    """Send now on a running chat stops the turn and marks its card; the chat is
    linked before that turn's end drains. The card is held again, not deleted."""
    state, slot = _restarted(tmp_path)
    ids = [q["id"] for q in slot._queue]
    _busy(slot)
    monkeypatch.setattr(state.sessions, "stop_turn", AsyncMock(return_value="soft"))

    status, _body = await _send_now(state, ids[1])
    assert status == 200 and slot._queue[0].get(RESTORED_SEND_CHOSEN_KEY)
    _contain(slot, monkeypatch, "linked")
    slot.task = None
    slot._stop_state = "idle"
    await cr._start_next_queued_turn(state, slot)

    assert _sent_rows(slot) == []
    assert [text for _id, text in _held_texts(slot)] == [_WORDS[1], _WORDS[0], _WORDS[2]]
    assert len(_notices(slot)) == 1 and _LATE["linked"] in _notices(slot)[0]


@pytest.mark.parametrize(
    "who",
    [{"user": "someone-else"}, {"app_id": "some-app"}],
    ids=["another-person", "an-app-token"],
)
@pytest.mark.asyncio
async def test_anyone_but_the_owner_is_refused_and_nothing_is_let_go(tmp_path, who):
    state, slot = _state(tmp_path, [_held("h1", "first"), _held("h2", "second")])

    status, body = await _press(state, ["h1", "h2"], **who)

    assert status == 403 and body["code"] == "owner_only"
    assert all(is_held_queue_entry(q) for q in slot._queue)
    assert _releases(state) == []


@pytest.mark.parametrize("ids", [[], "h1", [1], [""]])
@pytest.mark.asyncio
async def test_a_malformed_id_list_is_refused(tmp_path, ids):
    state, slot = _state(tmp_path, [_held("h1", "first")])

    status, body = await _press(state, ids)

    assert status == 400 and body["code"] == "invalid_queue_ids"
    assert is_held_queue_entry(slot._queue[0])


# ── The startup drain's chain: a card a limit held again waits for a press ──


def _boot_chain(tmp_path, monkeypatch, gated: tuple[str, ...]):
    """Two accepted parked sends on a chat, and turns that run as real tasks.

    Each turn's end drains the queue, as a turn's own end does, inside the context
    its task was born in, so a turn the startup drain started carries the accepted
    ids. A turn whose text is in *gated* waits for its gate first.
    """
    import asyncio

    state, slot = _state(tmp_path, [_held("r1", "accepted one"), _held("r2", "accepted two")])
    gates = {text: asyncio.Event() for text in gated}
    tasks: list = []

    async def _run_chat(_state, _slot, prompt, **_kwargs):
        if prompt in gates:
            await gates[prompt].wait()
        slot.task = None
        await cr._start_next_queued_turn(state, slot)

    def _spawn(_state, _slot, coro):
        task = asyncio.ensure_future(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(cr, "_run_chat", _run_chat)
    monkeypatch.setattr(cr, "spawn_guarded_turn", _spawn)
    return state, slot, gates, tasks


async def _boot_drain(slot, state) -> bool:
    token = cr._RESTORED_DRAIN_IDS.set({slot.key: frozenset({"r1", "r2"})})
    try:
        return await cr._start_next_queued_turn(state, slot)
    finally:
        cr._RESTORED_DRAIN_IDS.reset(token)


async def _finish(task) -> None:
    from kiro_crew.testing.wait import await_until

    await await_until(task, what="a turn of the chain")


@pytest.mark.asyncio
async def test_a_card_held_again_in_the_startup_chain_runs_only_after_a_press(
    tmp_path, monkeypatch
):
    """r1 runs from the startup drain; during it the chat is linked and the owner
    types a message. r1's end holds r2 again. During the typed message's turn the
    link goes: its end must not send r2, though r2 is one of the accepted ids that
    turn inherited. r2 waits with its words until a press sends it, once."""
    state, slot, gates, tasks = _boot_chain(
        tmp_path, monkeypatch, ("accepted one", "typed while linked")
    )
    assert await _boot_drain(slot, state) is True
    slot.linked_session_key = "C0LINKED|1700000000.000100"
    _busy(slot)

    async def _never_runs(*_args):  # pragma: no cover - queued, not run
        raise AssertionError("queued, not run")

    slot.enqueue_or_run_prompt("typed while linked", _never_runs, state)
    slot.task = None
    gates["accepted one"].set()
    await _finish(tasks[0])
    assert [text for _id, text in _held_texts(slot)] == ["accepted two"]

    slot.linked_session_key = None
    gates["typed while linked"].set()
    await _finish(tasks[1])

    assert len(tasks) == 2, "the typed message's end started nothing"
    assert _sent_rows(slot) == ["accepted one", "typed while linked"]
    assert _held_texts(slot) == [("r2", "accepted two")]
    status, body = await _press(state, ["r2"])
    await _finish(tasks[2])

    assert status == 200 and body["sent"] == ["r2"] and body["started"] is True
    assert _sent_rows(slot) == ["accepted one", "typed while linked", "accepted two"]
    assert slot._queue == []


@pytest.mark.asyncio
async def test_two_linked_drains_of_the_startup_chain_post_one_notice(
    tmp_path, monkeypatch, _quiet
):
    """Both accepted cards are judged in one pass, held again under one notice and
    one audit row; the next drain of the chain judges neither again."""
    state, slot, _gates, _tasks = _boot_chain(tmp_path, monkeypatch, ())
    slot.linked_session_key = "C0LINKED|1700000000.000100"

    assert await _boot_drain(slot, state) is False
    assert await _boot_drain(slot, state) is False

    assert _held_texts(slot) == [("r1", "accepted one"), ("r2", "accepted two")]
    assert len(_notices(slot)) == 1 and "kept held" in _notices(slot)[0]
    kept = [
        c.kwargs["metadata"]
        for c in _quiet.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "held"
    ]
    assert kept == [{"target": slot.key, "queue_ids": "r1,r2", "newly_held": "linked"}]
    assert _sent_rows(slot) == []


@pytest.mark.asyncio
async def test_a_proven_peer_note_a_late_limit_stops_is_dropped_and_its_sender_told(
    tmp_path, turns, monkeypatch
):
    """The keep is the owner's: a restored peer's note its receipt proved carries
    its sender's address, and a limit that stops it drops it and tells the sender."""
    state, slot = _state(tmp_path, [])
    told: list[dict] = []
    monkeypatch.setattr(sc, "notify_send_origin_dropped", lambda *_a, **k: told.append(k))
    slot._queue[:] = [
        {
            **_held("n1", "a peer's note"),
            RESTORED_SEND_CHOSEN_KEY: True,
            "meta": {
                sc.SESSION_SEND_QUEUED_META: True,
                sc.SEND_ORIGIN_META_KEY: {"slot": "sender", "tab": "t1"},
            },
        }
    ]
    slot.linked_session_key = "C0LINKED|1700000000.000100"

    assert await cr._start_next_queued_turn(state, slot) is False

    assert slot._queue == [] and _sent_rows(slot) == []
    assert [t["origin"] for t in told] == ["sender"]
    assert "Queued message dropped" in _notices(slot)[0]


# ── Across a restart: the owner's press is the only way out ──

_LINK = "C0LINKED|1700000000.000100"


def _parked_rows(monkeypatch):
    """An installed admission service whose store holds one parked send's row per
    work id, as a restart's reconciliation leaves it, memory ready. The startup
    scan reads the rows through ``current_work``; retiring one writes its store."""
    from dataclasses import dataclass, field, replace
    from typing import Any

    from kiro_crew import memory_startup
    from kiro_crew.dashboard.chat_turn import queue_drain
    from kiro_crew.shared_crew import admission_gate, host_hooks
    from kiro_crew.shared_crew.admission import OperationState

    @dataclass(frozen=True)
    class _Row:
        work_id: str
        session_key: str
        revision: int = 0
        extra: dict[str, Any] = field(default_factory=lambda: {admission_gate.PARKED_KEY: True})
        runtime: Any = None
        state: OperationState = OperationState.CANCELLED
        reason: Any = None

    class _Store:
        def __init__(self) -> None:
            self.rows: dict[str, _Row] = {}

        async def get(self, work_id):
            return self.rows.get(work_id)

        async def annotate(self, work, extra):
            nxt = replace(work, revision=work.revision + 1, extra={**work.extra, **extra})
            self.rows[work.work_id] = nxt
            return nxt

    class _Service:
        enabled = True

        def __init__(self) -> None:
            self._store = _Store()
            self._live: dict = {}

        async def ensure_ready(self):
            return None

        async def current_work(self, work_id):
            return await self._store.get(work_id)

        async def _cancelled_parked(self, _work_ids):
            return None

    service = _Service()

    async def _no_notes(_state):
        return None

    monkeypatch.setattr(admission_gate, "installed_service", lambda: service)
    monkeypatch.setattr(host_hooks, "deliver_owed_notes", _no_notes)
    monkeypatch.setattr(memory_startup, "memory_startup_preparing", lambda: False)
    monkeypatch.setattr(queue_drain, "_parked_queue_drain_admitted", lambda: True)
    monkeypatch.setattr(queue_drain, "_memory_preparation_pending", lambda _state: False)
    return service, _Row


def _restore(tmp_path, entries):
    """A start: the durable writer's copy read back by the restore reader."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    saved = json.loads(json.dumps(durable_queue_entries(entries)))
    state, slot = _state(tmp_path, sanitize_restored_queue(saved))
    state.memory_startup_task = None
    return state, slot, effective_session_key(slot)


async def _startup(state) -> None:
    """The real startup scan and drain, then whatever it left running."""
    import asyncio

    from kiro_crew.dashboard import slot_queue_repository

    await slot_queue_repository._drain_restored_parked_work(state)
    await asyncio.gather(*list(state._background_tasks))


def _parked_card(queue_id: str, text: str, work_id: str) -> dict:
    return {**_held(queue_id, text), "meta": {"sharedCrewWork": work_id}}


_MIRROR_PROBE = dc._probe_channel_mirror


def _mirror(monkeypatch, on: bool) -> None:
    """An outbound channel mirror: a limit that leaves the session key as it is, so
    the parked row still names this chat's session (a link would move it)."""
    mirrored = lambda _state, _slot: "slack:C0MIRROR:"  # noqa: E731
    monkeypatch.setattr(dc, "_probe_channel_mirror", mirrored if on else _MIRROR_PROBE)


@pytest.mark.asyncio
async def test_a_parked_send_a_limit_held_again_never_goes_at_a_later_start(
    tmp_path, turns, monkeypatch
):
    """A parked send r2 is held again by a mirror at the startup drain. The mirror
    goes, a later drain leaves it, the gateway restarts and its startup scan runs
    again: r2 is not sent, it waits with its words, and the owner's press sends it
    once."""
    service, row = _parked_rows(monkeypatch)
    state, slot, key = _restore(tmp_path / "first", [_parked_card("r2", "accepted two", "w-2")])
    service._store.rows["w-2"] = row("w-2", key)
    _mirror(monkeypatch, True)
    await _startup(state)
    assert _held_texts(slot) == [("r2", "accepted two")] and len(_notices(slot)) == 1
    _mirror(monkeypatch, False)
    assert await cr._start_next_queued_turn(state, slot) is False

    state, slot, _key = _restore(tmp_path / "second", slot._queue)
    await _startup(state)

    assert _sent_rows(slot) == [], "the restart sent a card a limit held again"
    assert _held_texts(slot) == [("r2", "accepted two")] and _notices(slot) == []
    status, body = await _press(state, ["r2"])
    assert status == 200 and body["sent"] == ["r2"] and body["started"] is True
    assert _sent_rows(slot) == ["accepted two"] and slot._queue == []


@pytest.mark.asyncio
async def test_a_restart_while_still_mirrored_keeps_the_card_held_with_no_new_notice(
    tmp_path, turns, monkeypatch
):
    service, row = _parked_rows(monkeypatch)
    state, slot, key = _restore(tmp_path / "first", [_parked_card("r2", "accepted two", "w-2")])
    service._store.rows["w-2"] = row("w-2", key)
    _mirror(monkeypatch, True)
    await _startup(state)
    assert len(_notices(slot)) == 1

    state, slot, _key = _restore(tmp_path / "second", slot._queue)
    await _startup(state)

    assert _sent_rows(slot) == []
    assert _held_texts(slot) == [("r2", "accepted two")] and _notices(slot) == []


async def _held_again(tmp_path, monkeypatch, *behind: dict):
    """r2 accepted by the startup drain and held again by a link, then the link goes."""
    state, slot, _gates, tasks = _boot_chain(tmp_path, monkeypatch, ())
    slot._queue[:] = [_held("r2", "accepted two"), *behind]
    slot.linked_session_key = _LINK
    assert await _boot_drain(slot, state) is False
    slot.linked_session_key = None
    assert slot._queue[0].get(RESTORED_HELD_AGAIN_KEY) is True
    return state, slot, tasks


@pytest.mark.asyncio
async def test_resume_sends_nothing_while_a_card_held_again_is_first(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import crew_pause

    state, slot, tasks = await _held_again(tmp_path, monkeypatch, _held("r3", "held three"))

    assert await crew_pause.drain_held_queue(state, slot, send_restored=True) == "held"

    assert tasks == [] and _sent_rows(slot) == []
    assert _held_texts(slot) == [("r2", "accepted two"), ("r3", "held three")]
    assert slot._queue[0].get(RESTORED_HELD_AGAIN_KEY) is True


@pytest.mark.parametrize("own_chat", [False, True], ids=["owners-chat", "apps-own-chat"])
@pytest.mark.asyncio
async def test_an_apps_send_now_on_a_card_held_again_is_refused(tmp_path, monkeypatch, own_chat):
    from kiro_crew.dashboard.chat_api import turn_stop

    state, slot, tasks = await _held_again(tmp_path, monkeypatch)
    if own_chat:
        slot._app = "some-app"
    app = _app(state, app_id="some-app")
    app.router.add_post("/api/chat/slots/{slot}/interrupt", turn_stop.api_chat_slot_interrupt)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/held-chat/interrupt", json={"queue_id": "r2"})

    assert resp.status == (409 if own_chat else 404)
    assert tasks == [] and _sent_rows(slot) == []
    assert _held_texts(slot) == [("r2", "accepted two")]
    assert slot._queue[0].get(RESTORED_HELD_AGAIN_KEY) is True
