"""The claude-agent-acp client's side of its session head.

``kiro_crew.claude_session_head`` renders a chat's head, says which chats get one
and keeps the signed record. The three members here carry it on the wire: they
render and read back before the process starts, add ``systemPrompt`` to the
session request, and record what the live session holds. ``AcpClient`` reaches
the last two through hooks whose default is what every other backend does
(nothing sent, nothing recorded), and ``ClaudeAcpClient`` binds the functions here.

Each function here whose first parameter is ``self`` is a ``ClaudeAcpClient`` method:
the class binds it by name, so it reads and writes the session's own state on the
instance, and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none but the four head fields the class declares.

Instance fields read: ``_agent``, ``_fork_request``, ``_resume_session_id``,
``_resumed``, ``_session_id``, ``_session_key``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew import claude_session_head as _head

if TYPE_CHECKING:
    from kiro_crew.acp.client import ClaudeAcpClient


async def _prepare_session_head(self: ClaudeAcpClient) -> None:
    """Render a new session's head and read back the one a restore must send.

    Off the loop, before the process starts: both read files, and the request
    that carries the head is built in memory at a site shared with kiro-cli. A
    fork is restored under its source's head, which is what the forked
    transcript was written under.
    """
    fork = self._fork_request
    restored = self._resume_session_id or (fork[0] if fork else None)
    self._session_head_sent = ()
    self._session_head_held = {}
    self._session_head_new, self._session_head_kept = await asyncio.to_thread(
        _head._prepared, self._agent, self._session_key, restored
    )


def _harness_session_params(self: ClaudeAcpClient, *, restoring: bool) -> dict[str, Any]:
    """``systemPrompt`` for this session's request, or nothing.

    A new session sends the head rendered for it; a restore sends the recorded
    one and never a new render. In memory: ``_prepare_session_head`` did the
    reads. The last request built is the one the session exists under, so it
    decides what ``_keep_session_head`` records.
    """
    blocks: _head.Blocks = ()
    if _head.advertised(self.initialize_result):
        blocks = getattr(self, "_session_head_kept" if restoring else "_session_head_new", ())
    self._session_head_sent = blocks
    return {"systemPrompt": _head.system_prompt(blocks)} if blocks else {}


async def _keep_session_head(self: ClaudeAcpClient, *, adopted: bool = False) -> None:
    """Record the head the live session holds, for the message builder (in
    memory) and for the session's next restore (on disk).

    A session is held to carry a head only when its entry was written, so a
    message never leaves out a block a later restore could not bring back. A
    restore that carried no head removes the entry: the session now runs
    without one. *adopted*: the session is live in a process a previous gateway
    started and no request was sent, so the entry says what it holds.
    """
    session_id = self._session_id
    if adopted:
        blocks = await asyncio.to_thread(_head.kept, session_id)
    else:
        blocks = getattr(self, "_session_head_sent", ())
        if blocks and not await asyncio.to_thread(_head.keep, session_id, blocks):
            blocks = ()
        if not blocks and self._resumed:
            await asyncio.to_thread(_head.forget, session_id)
    self._session_head_held = dict(blocks)
