"""The "Not now" control on a reply ask: it leaves ``needs_you`` until the agent asks again."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import needs_you_aside, slot_projection
from kiro_crew.dashboard.slot_persistence import metadata_codec as mc
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history_schema import HUMAN_TURN_META_KEY, SLOT_OWNED_META_KEYS
from kiro_crew.testing.clock import ManualClock

NOW = 1_800_000_000.0
KEY = "chat-1-100"


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    ManualClock(NOW).install(monkeypatch, slot_projection)


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    created = asyncio.new_event_loop()
    yield created
    created.close()


def _person(text: str, at: float) -> dict:
    return {"role": "user", "content": text, "ts": _stamp(at), "meta": {HUMAN_TURN_META_KEY: True}}


def _reply(text: str, at: float, mid: str) -> dict:
    return {"role": "assistant", "content": text, "ts": _stamp(at), "meta": {"mid": mid}}


def _asking(*more: dict) -> _ChatSlot:
    slot = _ChatSlot(KEY)
    slot.messages.extend(
        [
            _person("go", NOW - 90),
            _reply("Tests run.\n[Q3a] Which tide source wins?", NOW - 60, "m-1"),
        ]
    )
    slot.messages.extend(more)
    return slot


# ── the projection ──


def test_a_reply_ask_names_its_row_and_set_aside_clears_needs_you():
    slot = _asking()
    row = slot.to_dict()
    assert row["needs_you"]["kinds"] == ["labelled"]
    assert row["reply_ask"] == {
        "row_id": "m-1",
        "first_words": "Q3a Which tide source wins?",
        "set_aside": False,
    }
    slot._needs_you_set_aside = "m-1"
    row = slot.to_dict()
    assert row["needs_you"] is None
    assert row["reply_ask"]["set_aside"] is True


def test_a_new_ask_on_a_newer_reply_lights_the_chat_again():
    slot = _asking()
    slot._needs_you_set_aside = "m-1"
    slot.messages.append(_person("and the docs?", NOW - 40))
    slot.messages.append(_reply("Docs pass is green.\n[Q4a] Merge Docs pass now?", NOW - 30, "m-2"))
    row = slot.to_dict()
    assert row["needs_you"]["first_words"] == "Q4a Merge Docs pass now?"
    assert row["reply_ask"] == {
        "row_id": "m-2",
        "first_words": "Q4a Merge Docs pass now?",
        "set_aside": False,
    }


def test_the_same_reply_named_again_by_a_newer_row_is_a_new_ask():
    # The newest reply re-asking the SAME label is another row, so it lights.
    slot = _asking(
        _person("go", NOW - 40), _reply("[Q3a] Which tide source wins?", NOW - 30, "m-2")
    )
    slot._needs_you_set_aside = "m-1"
    assert slot.to_dict()["needs_you"]["row_id"] == "m-2"


def test_an_options_ask_is_set_aside_the_same_way():
    slot = _ChatSlot(KEY)
    slot.messages.extend(
        [_person("go", NOW - 90), _reply("Merge?\n[OPTIONS: Merge | Hold]", NOW - 60, "m-o")]
    )
    slot._needs_you_set_aside = "m-o"
    row = slot.to_dict()
    assert row["needs_you"] is None
    assert row["reply_ask"]["set_aside"] is True


def test_approvals_and_question_cards_are_never_set_aside(loop: asyncio.AbstractEventLoop):
    slot = _ChatSlot(KEY)
    slot.messages.append(_person("Run it.", NOW - 200))
    row = {
        "role": "permission",
        "content": "Bash",
        "cls": json.dumps({"request_id": "r1", "tool_input": "ls"}),
        "ts": _stamp(NOW - 120),
        "meta": {"mid": "m-r1"},
    }
    slot.messages.append(row)
    slot.register_approval("r1", loop.create_future(), row)
    slot._question_pending = {"card-1": {"ts": NOW - 100, "questions": [{"question": "Which?"}]}}
    slot._needs_you_set_aside = "m-r1"
    projected = slot.to_dict()
    assert projected["needs_you"]["kinds"] == ["approval", "question"]
    assert projected["needs_you"]["count"] == 2
    assert projected["reply_ask"] is None


def test_a_reply_that_asks_someone_else_has_no_reply_ask():
    slot = _ChatSlot(KEY)
    relay = {"role": "user", "content": "[From chat-2] Review it.", "ts": _stamp(NOW - 30)}
    slot.messages.extend([relay, _reply("[Q1a] Approve?", NOW - 20, "m-x")])
    assert slot.to_dict()["reply_ask"] is None


# ── a restart ──


def test_the_mark_survives_a_restart_and_bring_back_clears_it_on_disk():
    assert needs_you_aside.META_KEY in SLOT_OWNED_META_KEYS
    slot = _asking()
    slot._needs_you_set_aside = "m-1"
    folds = mc.SaveFolds(memory_mode="persistent")
    line = json.loads(json.dumps(mc.encode(slot, folds=folds)))
    assert line[needs_you_aside.META_KEY] == "m-1"
    restarted = _asking()
    field = mc._BY_KEY[needs_you_aside.META_KEY]
    assert field.read is not None
    field.read(mc._Read(state=None, slot=restarted, meta=line, purpose=None, applied=None))
    assert restarted.to_dict()["needs_you"] is None
    restarted._needs_you_set_aside = ""
    assert needs_you_aside.META_KEY not in mc.encode(restarted, folds=folds)
    assert mc.encode(restarted, merge=True, folds=folds)[needs_you_aside.META_KEY] == ""


def test_a_malformed_mark_on_the_line_is_ignored():
    field = mc._BY_KEY[needs_you_aside.META_KEY]
    assert field.read is not None
    for bad in (7, "", "x" * (needs_you_aside.ROW_ID_MAX + 1), ["m-1"]):
        slot = _asking()
        meta = {needs_you_aside.META_KEY: bad}
        field.read(mc._Read(state=None, slot=slot, meta=meta, purpose=None, applied=None))
        assert slot._needs_you_set_aside == ""


# ── the route ──


def _app(state: MagicMock, *, internal_auth: bool = False, app_caller: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _identity(request: web.Request, handler):
        request["app"] = app_caller
        request["user"] = "local-app"
        request["internal_auth"] = internal_auth
        request["is_dashboard_user"] = app_caller == "" and not internal_auth
        return await handler(request)

    app.middlewares.append(_identity)
    app.router.add_patch(
        "/api/chat/slots/{slot}/set-aside", needs_you_aside.api_chat_slot_set_aside
    )
    return app


def _state(slot: _ChatSlot) -> MagicMock:
    state = MagicMock()
    state._slots = {slot.key: slot}
    return state


def _committing_save() -> AsyncMock:
    async def save(*args, **kwargs):
        kwargs["after_commit_under_lock"]()
        return True

    return AsyncMock(side_effect=save)


async def _press(state: MagicMock, payload: object, save: AsyncMock, **app_kwargs):
    fences = (
        patch("kiro_crew.dashboard.chat_folders.refuse_unattributable_caller", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.member_slot_write_refused", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.deny_app_slot_access", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.app_owns_transcript", return_value=True),
        patch("kiro_crew.dashboard.chat_folders._effective_request_app", return_value=""),
        patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", save),
    )
    with fences[0], fences[1], fences[2], fences[3], fences[4], fences[5]:
        async with TestClient(TestServer(_app(state, **app_kwargs))) as client:
            resp = await client.patch(f"/api/chat/slots/{KEY}/set-aside", json=payload)
            return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_the_owners_press_sets_it_aside_persists_and_publishes():
    slot = _asking()
    state = _state(slot)
    save = _committing_save()
    status, body = await _press(state, {"row_id": "m-1", "set_aside": True}, save)
    assert (status, body) == (200, {"ok": True, "set_aside": True, "changed": True})
    assert slot._needs_you_set_aside == "m-1"
    assert save.await_count == 1
    state.push_slots_update.assert_called_once_with()
    assert slot.to_dict()["needs_you"] is None
    # Bring back.
    status, body = await _press(state, {"row_id": "m-1", "set_aside": False}, save)
    assert (status, body) == (200, {"ok": True, "set_aside": False, "changed": True})
    assert slot._needs_you_set_aside == ""
    assert slot.to_dict()["needs_you"]["row_id"] == "m-1"


@pytest.mark.asyncio
async def test_the_press_writes_no_row_and_sends_nothing():
    slot = _asking()
    before = [dict(message) for message in slot.messages]
    state = _state(slot)
    await _press(state, {"row_id": "m-1", "set_aside": True}, _committing_save())
    assert slot.messages == before
    assert not slot._queue
    assert slot.task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_kwargs", "who"), [({"internal_auth": True}, "agent"), ({"app_caller": "an-app"}, "app")]
)
async def test_a_caller_that_is_not_the_owner_is_refused(app_kwargs: dict, who: str):
    slot = _asking()
    save = _committing_save()
    status, body = await _press(
        _state(slot), {"row_id": "m-1", "set_aside": True}, save, **app_kwargs
    )
    assert status == 403, who
    assert body["code"] == "set_aside_user_only"
    assert slot._needs_you_set_aside == ""
    save.assert_not_called()


@pytest.mark.asyncio
async def test_a_press_on_an_ask_a_newer_reply_replaced_is_refused():
    slot = _asking(_person("go", NOW - 40), _reply("[Q4a] Merge now?", NOW - 30, "m-2"))
    state = _state(slot)
    save = _committing_save()
    status, body = await _press(state, {"row_id": "m-1", "set_aside": True}, save)
    assert status == 409
    assert body["code"] == "not_waiting"
    assert slot._needs_you_set_aside == ""
    save.assert_not_called()
    state.push_slots_update.assert_not_called()


@pytest.mark.asyncio
async def test_a_press_while_a_turn_runs_is_refused():
    slot = _asking()
    slot.task = asyncio.get_running_loop().create_future()
    status, body = await _press(
        _state(slot), {"row_id": "m-1", "set_aside": True}, _committing_save()
    )
    slot.task.cancel()
    assert (status, body["code"]) == (409, "not_waiting")
    assert slot._needs_you_set_aside == ""


@pytest.mark.asyncio
async def test_an_approval_row_is_refused():
    slot = _ChatSlot(KEY)
    slot.messages.append(_person("Run it.", NOW - 200))
    row = {
        "role": "permission",
        "content": "Bash",
        "cls": json.dumps({"request_id": "r1", "tool_input": "ls"}),
        "ts": _stamp(NOW - 120),
        "meta": {"mid": "m-r1"},
    }
    slot.messages.append(row)
    slot.register_approval("r1", asyncio.get_running_loop().create_future(), row)
    status, body = await _press(
        _state(slot), {"row_id": "m-r1", "set_aside": True}, _committing_save()
    )
    assert (status, body["code"]) == (409, "not_waiting")
    assert slot.to_dict()["needs_you"]["kinds"] == ["approval"]


@pytest.mark.asyncio
async def test_a_failed_save_puts_the_mark_back():
    slot = _asking()
    state = _state(slot)
    status, body = await _press(
        state, {"row_id": "m-1", "set_aside": True}, AsyncMock(side_effect=OSError("disk full"))
    )
    assert (status, body["code"]) == (503, "set_aside_save_failed")
    assert slot._needs_you_set_aside == ""
    assert slot.to_dict()["needs_you"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"row_id": "", "set_aside": True},
        {"row_id": "m-1", "set_aside": "true"},
        {"set_aside": True},
    ],
)
async def test_a_malformed_body_is_400(payload: dict):
    slot = _asking()
    status, _body = await _press(_state(slot), payload, _committing_save())
    assert status == 400
    assert slot._needs_you_set_aside == ""
