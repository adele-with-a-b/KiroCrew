"""POST /api/chat/slots/{slot}/attach-conversation points a new chat at a Claude conversation."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import attach_conversation as mod


class _Sessions:
    def __init__(self, mapped: str = "") -> None:
        self.mapped = mapped
        self.seeded: list[tuple] = []

    def mapped_sid(self, key: str) -> str:
        return self.mapped

    def seed_conversation(self, key, sid, *, provider="", cwd=""):
        self.seeded.append((key, sid, provider, cwd))


class _Slot:
    def __init__(self, key: str) -> None:
        self.key = key
        self.linked_session_key = ""
        self._lock = asyncio.Lock()


class _State:
    owner_id = ""

    def __init__(self, sessions: _Sessions) -> None:
        self.sessions = sessions
        self._slots = {"chat-1": _Slot("chat-1")}


def _transcript(config_dir, sid: str, cwd: str, folder: str = "-proj") -> None:
    d = config_dir / "projects" / folder
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "summary"}, {"type": "user", "cwd": cwd, "sessionId": sid}]
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(mod, "_slot_starts_on", lambda key, backend: True)
    return tmp_path / "claude"


async def _post(state, body, headers=None, slot="chat-1"):
    app = web.Application()
    app["state"] = state
    as_owner(app)
    app.router.add_post(
        "/api/chat/slots/{slot}/attach-conversation", mod.api_chat_slot_attach_conversation
    )
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            f"/api/chat/slots/{slot}/attach-conversation", json=body, headers=headers or {}
        )
        return r.status, await r.json()


@pytest.mark.asyncio
async def test_a_new_chat_is_seeded_with_the_conversation_and_its_folder(env):
    sid = str(uuid.uuid4())
    _transcript(env, sid, "/Users/someone/project")
    sessions = _Sessions()
    status, body = await _post(_State(sessions), {"session_id": sid})
    assert status == 200, body
    assert body["cwd"] == "/Users/someone/project"
    assert sessions.seeded == [("dashboard:chat-1", sid, "claude_code", "/Users/someone/project")]


@pytest.mark.asyncio
async def test_a_chat_with_its_own_conversation_is_refused(env):
    sid = str(uuid.uuid4())
    _transcript(env, sid, "/p")
    sessions = _Sessions(mapped="other")
    status, _ = await _post(_State(sessions), {"session_id": sid})
    assert status == 409 and sessions.seeded == []


@pytest.mark.asyncio
async def test_an_app_token_is_refused_even_on_its_own_slot(env):
    sid = str(uuid.uuid4())
    _transcript(env, sid, "/p")
    sessions = _Sessions()
    status, _ = await _post(_State(sessions), {"session_id": sid}, headers={"X-Test-App": "a"})
    assert status == 404 and sessions.seeded == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["../../etc/passwd", "*", "", None, "ABC"])
async def test_a_non_uuid_id_is_refused(env, raw):
    sessions = _Sessions()
    status, _ = await _post(_State(sessions), {"session_id": raw})
    assert status == 400 and sessions.seeded == []


@pytest.mark.asyncio
async def test_a_missing_or_duplicated_transcript_is_refused(env):
    sid = str(uuid.uuid4())
    sessions = _Sessions()
    status, _ = await _post(_State(sessions), {"session_id": sid})
    assert status == 404
    _transcript(env, sid, "/p", folder="-a")
    _transcript(env, sid, "/p", folder="-b")
    status, body = await _post(_State(sessions), {"session_id": sid})
    assert status == 404 and "more than one" in body["error"]
    assert sessions.seeded == []


@pytest.mark.asyncio
async def test_a_chat_on_another_backend_is_refused(env, monkeypatch):
    sid = str(uuid.uuid4())
    _transcript(env, sid, "/p")
    monkeypatch.setattr(mod, "_slot_starts_on", lambda key, backend: False)
    sessions = _Sessions()
    status, _ = await _post(_State(sessions), {"session_id": sid})
    assert status == 409 and sessions.seeded == []
