"""``GET /api/chat/slots/{slot}/sids``: who may read a chat's ids and title, and what they get."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from slot_ownership_checkpoint_helpers import (  # noqa: F401 -- fixtures are requested by name
    _NOT_FOUND,
    APP,
    _as,
    sel_spy_fixture,
    state_fixture,
)

from kiro_crew.dashboard.handlers.slot_sids import api_chat_slot_sids
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.state_parts.slot_session import NEW_SESSION_TITLE
from kiro_crew.session_map import SessionMap

#: The manifest read under the scope loader, so the loader primes the cache the
#: own-slot revocation check reads, as it does in production.
_EVENTS = "kiro_crew.dashboard.ws_event_scope._read_declared_events"
_CACHE = "kiro_crew.dashboard.ws_event_scope._declared_cache"


@pytest.fixture()
def smap(state, tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        state.sessions._session_map = SessionMap()
        yield state.sessions._session_map


def _client(state, caller: str | None) -> TestClient:
    app = _make_app(state)
    app.router.add_get("/api/chat/slots/{slot}/sids", api_chat_slot_sids)
    app.middlewares.insert(0, _as(caller))
    return TestClient(TestServer(app))


async def _get(state, caller: str | None, slot: str, events=(True, frozenset())):
    with patch(_EVENTS, return_value=events), patch.dict(_CACHE, clear=True):
        async with _client(state, caller) as client:
            resp = await client.get(f"/api/chat/slots/{slot}/sids")
            return resp.status, await resp.json()


def _user_chat(state, smap) -> None:
    slot = state.get_or_create_slot("u1", origin=SlotOrigin.USER)
    slot.title, slot._titled = "Blick", True
    smap.set("dashboard:u1", "s1", provider="claude_code")
    smap.clear_sid("dashboard:u1")
    smap.set("dashboard:u1", "s2", provider="claude_code")


@pytest.mark.asyncio
async def test_the_person_reads_the_ids_and_title(state, smap, sel_spy) -> None:
    _user_chat(state, smap)
    status, body = await _get(state, "", "u1")
    assert status == 200
    assert body["key"] == "dashboard:u1"
    assert body["title"] == "Blick"
    assert (body["sid"], body["provider"]) == ("s2", "claude_code")
    assert [h["sid"] for h in body["history"]] == ["s1"]
    assert (body["cap"], body["full"]) == (32, False)


@pytest.mark.asyncio
async def test_an_untitled_chat_reads_as_new_session(state, smap, sel_spy) -> None:
    state.get_or_create_slot("u2", origin=SlotOrigin.USER)
    status, body = await _get(state, "", "u2")
    assert (status, body["title"], body["history"]) == (200, NEW_SESSION_TITLE, [])


@pytest.mark.asyncio
async def test_a_renamed_title_is_redacted_as_in_the_slot_list(state, smap, sel_spy) -> None:
    """A stub redactor proves the route runs the slot list's, not its patterns."""
    slot = state.get_or_create_slot("u3", origin=SlotOrigin.USER)
    slot.title, slot._titled = "deploy SECRET", True
    with patch(
        "kiro_crew.dashboard.chat_utils_display._redact_for_display",
        lambda text: text.replace("SECRET", "<RED>"),
    ):
        status, body = await _get(state, "", "u3")
        listed = slot.to_dict()["title"]
    assert (status, body["title"], listed) == (200, "deploy <RED>", "deploy <RED>")


@pytest.mark.asyncio
async def test_an_app_with_slots_user_reads_a_person_chat(state, smap, sel_spy) -> None:
    _user_chat(state, smap)
    status, body = await _get(state, APP, "u1", (True, frozenset({"slots:user"})))
    assert (status, body["title"], body["sid"]) == (200, "Blick", "s2")
    assert sel_spy.denials() == []


@pytest.mark.asyncio
async def test_an_app_without_the_scope_gets_the_uniform_404(state, smap, sel_spy) -> None:
    _user_chat(state, smap)
    assert await _get(state, APP, "u1") == (404, _NOT_FOUND)
    assert len(sel_spy.denials()) == 1
    assert await _get(state, APP, "absent", (True, frozenset({"slots:all"}))) == (404, _NOT_FOUND)
    assert len(sel_spy.denials()) == 1


@pytest.mark.asyncio
async def test_a_disabled_app_reads_nothing_even_its_own(state, smap, sel_spy) -> None:
    state.get_or_create_slot("mine", app=APP)
    status, _ = await _get(state, APP, "mine")
    assert status == 200
    assert await _get(state, APP, "mine", (False, frozenset())) == (404, _NOT_FOUND)


@pytest.mark.asyncio
async def test_another_apps_chat_needs_its_opt_in(state, smap, sel_spy) -> None:
    state.get_or_create_slot("theirs", app="other-app")
    scope = (True, frozenset({"slots:app:other-app"}))
    assert await _get(state, APP, "theirs", scope) == (404, _NOT_FOUND)


@pytest.mark.asyncio
async def test_an_apps_slot_linked_to_a_person_conversation_is_refused(
    state, smap, sel_spy
) -> None:
    """Its ids would be the linked conversation's, which no slot scope grants."""
    state.get_or_create_slot("mine-linked", app=APP).linked_session_key = "slack:1.2"
    smap.set("slack:1.2", "person-sid", provider="claude_code")
    for scope in (frozenset(), frozenset({"slots:user", "slots:all"})):
        assert await _get(state, APP, "mine-linked", (True, scope)) == (404, _NOT_FOUND)
    assert [row.get("error") for row in sel_spy.denials()] == [
        "app does not own the session this slot is linked to"
    ] * 2
