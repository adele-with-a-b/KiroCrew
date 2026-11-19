"""Second live views of an app slot's own tabs (``live_hub_views`` and the
``/api/browser/live/views/*`` routes), for ``BrowserView``'s ``tabId``.

The rule: a view shows one of its slot's OWN tabs (``LiveHub.targets_of``, still in
the browser's ledger), never another app's, another chat's or the person's own; it
owns no tab and no turn; it is for looking only (visibility, acks and open in
browser take its key, take over, input and reopen do not); it ends with its slot's
chat, when its app is turned off, and on close. The routes sit behind the
``app_browser_sdk`` preview and the owner's approval of the app's ``view`` verb, and
answer 403 ``app_disabled`` while the app is off.

Each guarantee and the one-line change that turns its test red:
- own tabs only: drop ``target_id not in parent.targets`` in ``start_view``;
- look only: pass ``views=True`` on the claim route;
- ends with the chat: drop the ``stop_view`` loop in ``browser_ops._close_tabs_of``;
- ends with the app: drop ``_end_views`` in ``end_app_browser_views``;
- preview and verb: drop ``_view_refusal`` in the open route.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from browser_live_helpers import FakeRelay
from test_browser_live_hub import FakeStream, Wire, _settled

from kiro_crew.apps import browser_sdk
from kiro_crew.apps import manager as apps_manager
from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import live_hub_views as views
from kiro_crew.dashboard.handlers import browser_live as live_mod
from kiro_crew.dashboard.handlers import browser_ops
from kiro_crew.dashboard.routes import browser_live as route_slice

pytestmark = pytest.mark.asyncio

SLOT, OTHER_APP, CHAT = "bvtest-1", "other-1", "chat-1"


class LedgerRelay(FakeRelay):
    """A relay whose tab ledger lists *tabs*."""

    def __init__(self, tabs: list[str]) -> None:
        super().__init__()
        self.tabs = tabs
        self.closed = False

    def agent_targets(self) -> list[str]:
        return list(self.tabs)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(
        live_mod, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
    )
    _floor_monkeypatch.setattr(lh, "LiveStream", FakeStream)
    lh.reset_live_hub()
    yield
    lh.reset_live_hub()


@pytest.fixture
def grant(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Preview on, both apps on, ``view`` approved for ``bvtest`` only."""
    g: dict[str, Any] = {"preview": True, "on": {"bvtest": True, "other": True}, "verbs": ("view",)}
    monkeypatch.setattr(browser_sdk, "preview_on", lambda cfg=None: g["preview"])
    monkeypatch.setattr(
        browser_sdk,
        "app_browser_verbs",
        lambda app: g["verbs"] if g["preview"] and app == "bvtest" and g["on"][app] else (),
    )
    monkeypatch.setattr(apps_manager, "is_app_enabled", lambda name: g["on"].get(name, False))
    return g


def _state() -> SimpleNamespace:
    wire = Wire()
    return SimpleNamespace(
        owner_id="",
        _slots={
            SLOT: SimpleNamespace(_app="bvtest"),
            OTHER_APP: SimpleNamespace(_app="other"),
            CHAT: SimpleNamespace(_app=""),
        },
        deliver_ws_owners=wire.publish,
        deliver_ws_owners_bytes=wire.send_frame,
    )


def _app() -> web.Application:
    @web.middleware
    async def owner(request: web.Request, handler: Any) -> Any:
        request["user"], request["app"] = "local-app", ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = _state()
    route_slice.register(app)
    return app


async def _browsing() -> LedgerRelay:
    """Each slot browses its own tabs; the relay lists them all, plus one nobody owns."""
    hub = lh.get_live_hub()
    relay = LedgerRelay(["a", "b", "x", "c", "loose"])
    for key, tabs in ((SLOT, ["a", "b"]), (OTHER_APP, ["x"]), (CHAT, ["c"])):
        for t in tabs:
            hub.bind_target(key, t)
        await hub.start_stream(
            key, relay, tabs[-1], browser="edge", headless=False, stream_factory=FakeStream
        )
    return relay


async def _post(client: TestClient, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await client.post(path, json=body)
    return resp.status, await resp.json()


async def _open(client: TestClient, tab: str, key: str = SLOT) -> tuple[int, dict[str, Any]]:
    return await _post(client, "/api/browser/live/views/open", {"session_key": key, "tab_id": tab})


# ── the hub ─────────────────────────────────────────────────────────────────


async def test_a_view_streams_the_slots_tab_and_owns_nothing() -> None:
    await _browsing()
    hub = lh.get_live_hub()
    stream = await views.start_view(hub, SLOT, f"{SLOT}.view1", "a", stream_factory=FakeStream)
    assert isinstance(stream, FakeStream) and stream.target_id == "a"
    view = hub.get(f"{SLOT}.view1")
    assert view is not None and view.view_of == SLOT and view.turn_id and not view.targets
    assert hub.owner_of_target("a") == SLOT and hub.get(SLOT).stream is not None
    hub.turn_started(SLOT, "t1")
    hub.turn_ended(SLOT)
    assert hub.get(f"{SLOT}.view1").state == lh.STATE_LIVE, "the slot's turn ending leaves it"


@pytest.mark.parametrize(
    ("parent", "key", "tab", "code"),
    [
        ("nobody-1", "nobody-1.view1", "a", "unknown_session"),
        (SLOT, SLOT, "a", "unknown_session"),
        (SLOT, f"{SLOT}.view1", "x", "not_agent_tab"),  # another app's tab
        (SLOT, f"{SLOT}.view1", "c", "not_agent_tab"),  # another chat's tab
        (SLOT, f"{SLOT}.view1", "loose", "not_agent_tab"),  # nobody's (the person's own)
        (SLOT, f"{SLOT}.view1", "gone", "not_agent_tab"),  # owned, closed since
        (SLOT, CHAT, "a", "key_taken"),
    ],
)
async def test_a_view_shows_only_one_of_the_slots_own_open_tabs(
    parent: str, key: str, tab: str, code: str
) -> None:
    await _browsing()
    hub = lh.get_live_hub()
    hub.bind_target(SLOT, "gone")
    hub.bind_target(SLOT, "a")
    with pytest.raises(LookupError, match=code):
        await views.start_view(hub, parent, key, tab, stream_factory=FakeStream)


async def test_stop_view_folds_it_and_frees_its_key() -> None:
    await _browsing()
    hub = lh.get_live_hub()
    stream = await views.start_view(hub, SLOT, f"{SLOT}.view1", "a", stream_factory=FakeStream)
    assert await views.stop_view(hub, f"{SLOT}.view1")
    assert hub.get(f"{SLOT}.view1").state == lh.STATE_DONE and views.views_of(hub, SLOT) == []
    assert hub.get(f"{SLOT}.view1").stream is None and stream is not None
    assert not await views.stop_view(hub, f"{SLOT}.view1")
    assert views.free_view_key(hub, SLOT) == f"{SLOT}.view1"
    assert not await views.stop_view(hub, SLOT), "a slot is never stopped as a view"


# ── the routes ──────────────────────────────────────────────────────────────


async def test_open_shows_the_slots_own_tab_and_the_view_is_for_looking_only(grant) -> None:
    await _browsing()
    async with TestClient(TestServer(_app())) as client:
        status, body = await _open(client, "a")
        assert status == 200 and body["view_key"] == f"{SLOT}.view1"
        view = body["view_key"]
        seen = {"session_key": view, "visible": True, "client": "tab-1"}
        assert (await _post(client, "/api/browser/live/visibility", seen))[0] == 200
        assert (await _post(client, "/api/browser/live/ack", {"session_key": view, "seq": 1}))[
            0
        ] == 200
        for path, extra in (
            ("/api/browser/input/claim", {}),
            ("/api/browser/input/event", {"event": {"type": "key", "action": "down", "key": "a"}}),
            ("/api/browser/live/reopen", {"turn_id": "t"}),
        ):
            status, body = await _post(client, path, {"session_key": view, **extra})
            assert (status, body["code"]) == (404, "unknown_session"), path
    assert not lh.get_live_hub().in_control(view)


@pytest.mark.parametrize("tab", ["x", "c", "loose", "nope"])
async def test_open_refuses_any_tab_that_is_not_the_slots_own(grant, tab: str) -> None:
    await _browsing()
    async with TestClient(TestServer(_app())) as client:
        status, body = await _open(client, tab)
    assert (status, body["code"]) == (404, "not_agent_tab")
    assert views.views_of(lh.get_live_hub(), SLOT) == []


async def test_open_needs_an_app_slot_the_preview_and_the_view_verb(grant) -> None:
    await _browsing()
    async with TestClient(TestServer(_app())) as client:
        assert (await _open(client, "c", key=CHAT))[1]["code"] == "not_app_slot"
        # Another app's slot, its own tab: that app holds no approved ``view``.
        assert (await _open(client, "x", key=OTHER_APP))[0] == 403
        grant["verbs"] = ()
        assert (await _open(client, "a"))[0:1] == (403,)
        grant["verbs"], grant["preview"] = ("view",), False
        status, body = await _open(client, "a")
        assert (status, body["code"]) == (404, "preview_off")
        status, body = await _post(client, "/api/browser/live/views/tabs", {"session_key": SLOT})
        assert (status, body["code"]) == (404, "preview_off")
    assert views.views_of(lh.get_live_hub(), SLOT) == []


async def test_an_app_turned_off_refuses_views_and_a_views_route_ends_it(grant) -> None:
    await _browsing()
    hub = lh.get_live_hub()
    async with TestClient(TestServer(_app())) as client:
        view = (await _open(client, "a"))[1]["view_key"]
        grant["on"]["bvtest"] = False
        # Turned off with no gateway action (the CLI): a view's own route ends it.
        status, body = await _post(client, "/api/browser/live/ack", {"session_key": view, "seq": 1})
        assert (status, body["code"]) == (403, "app_disabled")
        assert hub.get(view).state == lh.STATE_DONE
        status, body = await _open(client, "a")
        assert (status, body["code"]) == (403, "app_disabled")


async def test_turning_the_app_off_ends_every_view_of_its_slots(grant) -> None:
    await _browsing()
    hub = lh.get_live_hub()
    async with TestClient(TestServer(_app())) as client:
        keys = [(await _open(client, t))[1]["view_key"] for t in ("a", "b")]
        grant["on"]["bvtest"] = False
        await live_mod.end_app_browser_views(client.app["state"], "bvtest")
    assert [hub.get(k).state for k in keys] == [lh.STATE_DONE] * 2
    assert views.views_of(hub, SLOT) == []


async def test_close_ends_a_view_and_the_cap_holds(grant) -> None:
    await _browsing()
    async with TestClient(TestServer(_app())) as client:
        keys = [(await _open(client, "a"))[1]["view_key"] for _ in range(views.VIEW_CAP)]
        assert len(set(keys)) == views.VIEW_CAP
        status, body = await _open(client, "a")
        assert (status, body["code"]) == (409, "view_cap")
        close = "/api/browser/live/views/close"
        assert (await _post(client, close, {"session_key": keys[0]}))[1]["closed"] is True
        assert (await _post(client, close, {"session_key": keys[0]}))[0] == 404
        assert (await _post(client, close, {"session_key": SLOT}))[1]["closed"] is False
        assert (await _open(client, "b"))[1]["view_key"] == keys[0]


async def test_tabs_lists_the_slots_tabs_through_the_one_browser_door(
    grant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _browsing()
    asked: list[tuple[str, str, dict[str, Any]]] = []

    async def run_browser_op(key: str, op: str, args: dict[str, Any], **_: Any) -> Any:
        asked.append((key, op, args))
        tabs = [{"target_id": "a", "url": "https://a/", "title": "A"}, {"target_id": "b"}]
        return {"ok": True, "result": {"ok": True, "tabs": tabs}}, 200

    monkeypatch.setattr(browser_ops, "run_browser_op", run_browser_op)
    async with TestClient(TestServer(_app())) as client:
        status, body = await _post(client, "/api/browser/live/views/tabs", {"session_key": SLOT})
    assert asked == [(f"dashboard:{SLOT}", "tabs", {"action": "list"})]
    assert status == 200 and body["tabs"] == [
        {"tab_id": "a", "url": "https://a/", "title": "A", "current": False},
        {"tab_id": "b", "url": "", "title": "", "current": True},
    ]


async def test_the_chats_close_ends_and_forgets_its_views(
    grant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _browsing()
    hub = lh.get_live_hub()
    async with TestClient(TestServer(_app())) as client:
        view = (await _open(client, "a"))[1]["view_key"]
        stream = hub.get(view).stream
        monkeypatch.setattr(browser_ops._live_handlers(), "_current_relay", lambda: None)
        await browser_ops._close_tabs_of(SimpleNamespace(_slots={}), SLOT)
    await _settled(hub)
    assert ("stop", None) in stream.calls, "its stream stops with the chat"
    assert hub.get(view) is None and views.views_of(hub, SLOT) == []


# ── review round 1: reservation, failure cleanup, idle reaping, address ──────


async def test_two_opens_at_once_get_two_keys(grant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The key is reserved before the stream attaches: two opens interleaved at
    ``stream.start()`` (the first await) are given two different keys."""
    import asyncio

    await _browsing()
    hub = lh.get_live_hub()
    # A key folded by an earlier close: the state the race starts from.
    await views.start_view(hub, SLOT, f"{SLOT}.view1", "a", stream_factory=FakeStream)
    await views.stop_view(hub, f"{SLOT}.view1")
    gate, attaching = asyncio.Event(), asyncio.Event()
    started = 0

    class HeldStream(FakeStream):
        async def start(self) -> None:
            nonlocal started
            started += 1
            if started == 2:
                attaching.set()
            await gate.wait()

    monkeypatch.setattr(lh, "LiveStream", HeldStream)
    async with TestClient(TestServer(_app())) as client:
        first = asyncio.ensure_future(_open(client, "a"))
        second = asyncio.ensure_future(_open(client, "b"))
        await asyncio.wait_for(attaching.wait(), 30)  # both inside stream.start()
        gate.set()
        answers = await asyncio.wait_for(asyncio.gather(first, second), 30)
    keys = {body["view_key"] for status, body in answers if status == 200}
    assert keys == {f"{SLOT}.view1", f"{SLOT}.view2"}


async def test_an_open_whose_stream_fails_frees_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    await _browsing()
    hub = lh.get_live_hub()

    class FailingStream(FakeStream):
        async def start(self) -> None:
            raise RuntimeError("target closed")

    with pytest.raises(RuntimeError):
        await views.start_view(hub, SLOT, f"{SLOT}.view1", "a", stream_factory=FailingStream)
    assert views.views_of(hub, SLOT) == [] and views.free_view_key(hub, SLOT) == f"{SLOT}.view1"


async def test_a_view_nobody_looks_at_is_reaped_at_the_next_open(grant) -> None:
    """A reloaded page runs no cleanup: its view is ended once idle, so reloads
    never fill the cap; a view that was looked at lately is kept."""
    await _browsing()
    hub = lh.get_live_hub()
    now = [1000.0]
    hub._clock = lambda: now[0]
    async with TestClient(TestServer(_app())) as client:
        left = (await _open(client, "a"))[1]["view_key"]
        kept = (await _open(client, "b"))[1]["view_key"]
        now[0] += views.VIEW_IDLE_S + 1
        seen = {"session_key": kept, "visible": True, "client": "tab-1"}
        assert (await _post(client, "/api/browser/live/visibility", seen))[0] == 200
        again = (await _open(client, "a"))[1]["view_key"]
    assert hub.get(kept).state == lh.STATE_LIVE
    assert again == left and hub.get(left).state == lh.STATE_LIVE
    assert sorted(views.views_of(hub, SLOT)) == sorted([left, kept])


async def test_a_view_shows_its_tabs_address_and_follows_the_agent_on_it() -> None:
    relay = await _browsing()
    hub = lh.get_live_hub()
    relay.answers["Target.getTargetInfo"] = {
        "targetInfo": {"url": "https://a.example/", "title": "A"}
    }
    await views.start_view(hub, SLOT, f"{SLOT}.view1", "a", stream_factory=FakeStream)
    view = hub.get(f"{SLOT}.view1")
    assert (view.url, view.title) == ("https://a.example/", "A"), "a tab the agent is not on"
    # The agent switches to that tab and moves it: the view follows.
    hub.bind_target(SLOT, "a")
    hub.page_changed(SLOT, url="https://a.example/next", title="Next")
    assert (view.url, view.title) == ("https://a.example/next", "Next")
    # The agent's other tab moving leaves the view alone.
    hub.bind_target(SLOT, "b")
    hub.page_changed(SLOT, url="https://b.example/", title="B")
    assert view.url == "https://a.example/next"
