"""The session head of a claude-agent-acp chat: its fixed blocks as system text.

A new dashboard chat on the default agent sends ``[AGENT SYSTEM PROMPT]`` and the
dashboard ``[CRITICAL RULES]`` as ACP's ``systemPrompt`` in ``append`` mode,
after Claude Code's own preset, instead of inside its first user message. The
adapter holds that text as the session's system prompt, so a compaction cannot
summarize it away and a restored transcript does not have to carry it.

It is the claude side of :mod:`kiro_crew.session_head` and adds no seam of its
own: the blocks are rendered by the functions that render them for the first
message, composed by ``session_head.head_prompt``, recorded under its keys, and
left out of a message only by its byte-equal check (``session_head.holds``).
The claude client's members that put the head on the wire and record it are in
``kiro_crew.acp.client_claude_head``.

A claude chat carries no ``[TOOL NAMES]`` block, so its head holds none: a head
never adds text the chat would not have had.

Sent only to an adapter whose ``initialize`` answer advertises
``sessionCapabilities.systemPrompt``. Any other adapter is sent nothing and
records nothing, so every block rides in the message as before.

The adapter compares the ``systemPrompt`` of a ``session/load`` with the one a
live session started under and replaces the session's Claude Code process when a
byte differs, which restarts the query mid-chat. On claude-agent-acp 0.109.0 the
replaced process, and a session restored on a new adapter, still read the head
the session was created with, whatever the load carries. So a new render on a
restore could cost a restart and change nothing: a restore sends the head the
session was started with, read back from this module's record. A session with no
record is sent none, and the resumed turn then carries the blocks itself.

The record sits in the data home, which an agent's shell can write, and its text
becomes system text. So each session's entry is signed with a key derived from
the gateway's trust root, as the session-token mapping is
(:mod:`kiro_crew.session_token_sig`): an entry that does not verify is no
record. With no trust root there is no head at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiro_crew import session_head

logger = logging.getLogger(__name__)

#: The session capability an adapter advertises when it applies a client's
#: ``systemPrompt`` on ``session/new`` and on a restore.
SYSTEM_PROMPT_CAPABILITY = "systemPrompt"
#: The one mode sent. ``override`` would replace Claude Code's own preset.
APPEND_MODE = "append"

#: The record's folder under the data home: ``sids/<session id>`` names the
#: digest of the head that session's last start or restore carried, with its
#: signature; ``heads/<digest>.json`` holds the blocks. Chats share a head, so
#: the blocks are stored once per wording.
RECORD_DIR = "claude-session-heads"
#: Session entries kept; past it the oldest are removed, and a removed session's
#: next restore sends no head (its resumed turn carries the blocks).
MAX_SESSION_RECORDS = 2000
#: A head file larger than this is not a head.
MAX_HEAD_FILE_BYTES = 1_048_576

#: Domain-separation label of the record's signing subkey, different from every
#: other protocol anchored on the same trust root. Versioned: a new label makes
#: every existing entry unverifiable, which reads as "no record".
_SUBKEY_DOMAIN = b"kirocrew.claude_session_head.v1"

_SID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")

Blocks = tuple[tuple[str, str], ...]


def advertised(initialize_result: object) -> bool:
    """Whether the adapter's ``initialize`` answer advertises ``systemPrompt``."""
    if not isinstance(initialize_result, Mapping):
        return False
    capabilities = initialize_result.get("agentCapabilities")
    session = capabilities.get("sessionCapabilities") if isinstance(capabilities, Mapping) else None
    return isinstance(session, Mapping) and isinstance(
        session.get(SYSTEM_PROMPT_CAPABILITY), Mapping
    )


def applies(agent: str | None, session_key: str | None) -> bool:
    """Whether a chat of *agent* under *session_key* is one a head is for.

    The default agent on a dashboard key, the two conditions
    ``session_head.head_blocks`` sets for the same reason: a custom agent keeps
    its own prompt, and a cron, channel or sub-agent start may not send every
    block, so a head there would add one.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.context_assembly.sections import _resolve_runtime_source

    if (agent or session_head.HEAD_AGENT) != session_head.HEAD_AGENT:
        return False
    if not session_key or _resolve_runtime_source(session_key) != "dashboard":
        return False
    return bool(ctx._agent_includes_crew_context(agent))


def head_blocks(agent: str | None, session_key: str | None) -> Blocks:
    """The ``(record key, block)`` pairs of a new claude chat's head, or ``()``.

    Each block is folded as ``build_message`` folds the message, so the model
    reads the bytes it read when the block rode there. ``()`` where
    :func:`applies` is false. Reads the prompt file: call it off the event loop.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.context_assembly import agent_spec as _agent_spec

    if not applies(agent, session_key):
        return ()
    assert session_key is not None
    try:
        contract = _agent_spec._render_contract(
            agent,
            _agent_spec.claude_default_prompt(_agent_spec._read_prompt_file(ctx._prompt_path())),
            session_key,
            cap_figure=_agent_spec._live_cap_figure,
            computer_use=_agent_spec._live_computer_use_gate,
            browser=_agent_spec._live_browser_gate,
            substitute_bot_name=session_head._substitute_bot_name,
        )
    except Exception:
        # No head is the safe direction: every block then rides in the message.
        logger.warning("claude session head not rendered for %s", session_key, exc_info=True)
        return ()
    rules = ctx._critical_rules_for(session_key, None)
    if not (contract and rules):
        return ()
    return (
        (session_head.AGENT_PROMPT_KEY, session_head.fold(contract)),
        (session_head.RULES_KEY, session_head.fold(rules)),
    )


def head_text(blocks: Blocks) -> str:
    """The system text that carries *blocks*, which are folded already."""
    return session_head.head_prompt((*blocks, (session_head.TOOL_NAMES_KEY, "")))


def system_prompt(blocks: Blocks) -> dict[str, Any]:
    """The ``systemPrompt`` field of a session request that carries *blocks*."""
    return {"mode": APPEND_MODE, "content": [{"type": "text", "text": head_text(blocks)}]}


def in_system_prompt(provider: Any, key: str, text: str) -> bool:
    """Whether *provider*'s live session holds *text* as its head's *key* block
    in the harness's system prompt, where a compaction and a restore keep it.

    The message builder's question before it re-sends a block after a
    compaction or on a resumed turn. False for no provider, for every provider
    that does not say so (``LLMProvider.session_head_in_system_prompt``; a
    provider double without the attribute reads as one), and for any text that
    differs from the recorded block by a byte.
    """
    return getattr(provider, "session_head_in_system_prompt", False) is True and session_head.holds(
        provider.native_context_documents, key, text
    )


# ── The record ────────────────────────────────────────────────────────────────


def _valid(blocks: object) -> Blocks:
    """*blocks* when it is exactly a head's two blocks, else ``()``."""
    if not isinstance(blocks, (list, tuple)) or len(blocks) != 2:
        return ()
    pairs = tuple(
        (key, text)
        for entry in blocks
        if isinstance(entry, (list, tuple)) and len(entry) == 2
        for key, text in [entry]
        if isinstance(key, str) and isinstance(text, str) and text
    )
    keys = tuple(key for key, _ in pairs)
    return pairs if keys == (session_head.AGENT_PROMPT_KEY, session_head.RULES_KEY) else ()


def _digest(blocks: Blocks) -> str:
    return hashlib.sha256(head_text(blocks).encode("utf-8", "surrogatepass")).hexdigest()


def _record_root() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / RECORD_DIR


def signing_key() -> bytes | None:
    """This record's signing subkey, or None with no trust root. Blocking."""
    from kiro_crew.session_pid_sig import _load_hmac_key

    root = _load_hmac_key()
    return None if root is None else hmac.new(root, _SUBKEY_DOMAIN, hashlib.sha256).digest()


def _signature(key: bytes, session_id: str, digest: str) -> str:
    """MAC over the session id and the head's digest: an entry copied to another
    session's name, or pointed at another head, does not verify."""
    return hmac.new(key, f"{session_id}:{digest}".encode(), hashlib.sha256).hexdigest()


def _read_head_file(path: Path) -> str | None:
    """*path*'s text when it is a regular file of a head's size, never through a
    symlink and never blocking on a pipe planted at its name."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HEAD_FILE_BYTES:
            return None
        with os.fdopen(os.dup(fd), "rb") as handle:
            return handle.read(MAX_HEAD_FILE_BYTES + 1).decode("utf-8", "surrogatepass")
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def keep(session_id: object, blocks: Blocks) -> bool:
    """Record that *session_id*'s session carries *blocks*. Blocking; never raises.

    False when nothing was recorded (no trust root, an unusable id, a failed
    write). That costs the session's next restore its head, and the resumed
    turn then carries the blocks: nothing is lost.
    """
    from kiro_crew.atomic_write import atomic_write

    if not blocks or not isinstance(session_id, str) or not _SID.fullmatch(session_id):
        return False
    try:
        key = signing_key()
        if key is None:
            return False
        root = _record_root()
        digest = _digest(blocks)
        heads, sids = root / "heads", root / "sids"
        heads.mkdir(parents=True, exist_ok=True)
        sids.mkdir(parents=True, exist_ok=True)
        head_file = heads / f"{digest}.json"
        text = json.dumps(blocks)
        # Written again when what sits at the name is not this head's text.
        if _read_head_file(head_file) != text:
            atomic_write(head_file, text, restrict_to_owner=True)
        atomic_write(
            sids / session_id,
            f"{digest}:{_signature(key, session_id, digest)}",
            restrict_to_owner=True,
        )
        _prune(root)
        return True
    except Exception:
        logger.warning("claude session head record not kept for %s", session_id, exc_info=True)
        return False


def kept(session_id: object) -> Blocks:
    """The blocks *session_id*'s session carries, or ``()``. Blocking; never raises.

    ``()`` for a session with no entry, for an entry whose signature does not
    verify, and for a head file that does not read back as the head its name
    digests: a restore then sends no head.
    """
    from kiro_crew.session_pid_sig import _read_regular_nofollow

    if not isinstance(session_id, str) or not _SID.fullmatch(session_id):
        return ()
    try:
        root = _record_root()
        entry = _read_regular_nofollow(root / "sids" / session_id)
        digest, _, signature = (entry or "").strip().partition(":")
        if not _DIGEST.fullmatch(digest):
            return ()
        key = signing_key()
        if key is None or not hmac.compare_digest(signature, _signature(key, session_id, digest)):
            return ()
        text = _read_head_file(root / "heads" / f"{digest}.json")
        blocks = _valid(json.loads(text)) if text else ()
        return blocks if blocks and _digest(blocks) == digest else ()
    except Exception:
        logger.warning("claude session head record unreadable for %s", session_id, exc_info=True)
        return ()


def forget(session_id: object) -> None:
    """Remove *session_id*'s entry: its session now runs without a head. Blocking."""
    if isinstance(session_id, str) and _SID.fullmatch(session_id):
        try:
            (_record_root() / "sids" / session_id).unlink(missing_ok=True)
        except OSError:
            logger.warning("claude session head record not removed for %s", session_id)


def _prune(root: Path) -> None:
    """Keep the newest :data:`MAX_SESSION_RECORDS` session entries and their heads."""
    entries = sorted(
        (entry for entry in (root / "sids").iterdir() if entry.is_file()),
        key=lambda entry: entry.stat().st_mtime,
    )
    excess = len(entries) - MAX_SESSION_RECORDS
    if excess <= 0:
        return
    for entry in entries[:excess]:
        entry.unlink(missing_ok=True)
    named = {entry.read_text(encoding="utf-8").partition(":")[0] for entry in entries[excess:]}
    for head_file in (root / "heads").iterdir():
        if head_file.stem not in named:
            head_file.unlink(missing_ok=True)


def _prepared(
    agent: str | None, session_key: str | None, restored: str | None
) -> tuple[Blocks, Blocks]:
    """``(a new session's head, the head *restored*'s restore sends)``. Blocking.

    Both ``()`` with no trust root: a head whose record cannot be signed could
    not be sent again on a restore, so none is sent at all.
    """
    if not applies(agent, session_key) or signing_key() is None:
        return (), ()
    return head_blocks(agent, session_key), kept(restored) if restored else ()
