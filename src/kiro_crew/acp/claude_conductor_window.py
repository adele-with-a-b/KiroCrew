"""The context window a Conductor chat on the Claude backend runs with.

Claude Code compacts a session on its own against its auto-compact window (some
way under it: ``acp_frames/claude/auto-compaction-live.jsonl`` compacts at 45306
used of 100000), and reports that window as the ``size`` of every
``usage_update``, so Crew's context meter reads a percent of it. The window
comes from Claude Code's own settings (``autoCompactWindow``), which every
session on the host shares. ``session.conductor_context_window`` gives a
Conductor chat its own number, and nothing else: a worker chat, a Bot and
Claude Code in a terminal keep the window their settings name.

It travels as ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` in the session envelope
(``_meta.claudeCode.options``), in ``env`` and in the flag tier's
``settings.env``, the two places the auto-memory switch already uses. The
envelope belongs to one session; ``settings.local.json`` belongs to a folder,
which a worker in the same folder reads too. The order was measured on Claude
Code 2.1.331 through claude-agent-acp 0.109.0, against a user
``autoCompactWindow`` of 300000, by the ``size`` the running session reported:

==================================================  ========
what the session was given                          ``size``
==================================================  ========
nothing                                             300000
``options.env``, the variable at 600000             600000
``options.settings.autoCompactWindow`` 600000       600000
``options.env`` 450000 beside that 600000 setting   450000
``options.env`` 600000, user ``env`` tier 350000    350000
that, plus ``options.settings.env`` 600000          600000
==================================================  ========

So the variable outranks the setting key, a settings tier's ``env`` outranks the
process env, and the flag tier's ``env`` is read last.

A Conductor is named positively: the session's agent is one of
:func:`kiro_crew.crew_mode.conductor_agents` (the shipped conductors, the
configured ``agent.crew_mode_agent`` and its aliases). A Bot that carries such a
name is that Bot (``crew_mode.runs_crew_mode``), so a Bot's own thread and a
Bot's worker answer False.

shortcut: a chat running Crew Mode on a custom conductor that
``agent.crew_mode_agent`` does not name is known only by its slot's stamp, which
the session client does not hold; give this the stamp if such chats need the window.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name

logger = logging.getLogger(__name__)

#: Claude Code's own variable for its auto-compact window, in tokens.
CLAUDE_AUTO_COMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"


def is_conductor_session(agent: str | None, session_key: str | None) -> bool:
    """Whether the session *session_key* on *agent* is a Conductor chat. In memory."""
    if not agent:
        return False
    from kiro_crew import crew_mode, members

    if agent not in crew_mode.conductor_agents():
        return False
    return not (
        members.is_member_session_key(session_key)
        or members.is_crewmate_worker_session(session_key)
    )


def configured_conductor_window(config: Any = None) -> int:
    """``session.conductor_context_window`` in tokens; 0 when off or unreadable.

    From *config* when given, else the live config watcher's snapshot, else the
    file. An unreadable config reads as off: the session then follows Claude
    Code's own settings.
    """
    try:
        if config is None:
            from kiro_crew.config import live

            config = live.snapshot()
        if config is None:
            from kiro_crew.config.loader import KiroCrewConfig

            config = KiroCrewConfig.load()
        window = config.session.conductor_context_window
    except Exception:  # noqa: BLE001 - a session starts without the window, never fails on it
        logger.warning("could not read session.conductor_context_window", exc_info=True)
        return 0
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        return 0
    return window


def conductor_window_tokens(
    agent: str | None, session_key: str | None, backend: str | None, config: Any = None
) -> int:
    """The window Crew gives the session, in tokens; 0 for every session it gives none.

    Non-zero only for a Conductor chat (:func:`is_conductor_session`) on the Claude
    backend while the setting is on.
    """
    if not is_claude_backend_name(backend) or not is_conductor_session(agent, session_key):
        return 0
    return configured_conductor_window(config)


def apply_conductor_window(
    options: dict[str, Any], agent: str | None, session_key: str | None, backend: str | None
) -> int:
    """Give a Conductor chat's envelope *options* its context window; return the tokens sent.

    Writes the variable to ``options.env`` and to the flag tier's
    ``options.settings.env`` (copied, never mutated: the inline settings are
    shared dicts). The setting is the one number for these chats, so it replaces
    a value the agent's own ``claudeCode.env`` block named; at 0 nothing is
    written and that block's value stands.
    """
    window = conductor_window_tokens(agent, session_key, backend)
    if not window:
        return 0
    options["env"] = {**dict(options.get("env") or {}), CLAUDE_AUTO_COMPACT_WINDOW_ENV: str(window)}
    flag = dict(options.get("settings") or {})
    flag["env"] = {**dict(flag.get("env") or {}), CLAUDE_AUTO_COMPACT_WINDOW_ENV: str(window)}
    options["settings"] = flag
    return window


__all__ = [
    "CLAUDE_AUTO_COMPACT_WINDOW_ENV",
    "apply_conductor_window",
    "conductor_window_tokens",
    "configured_conductor_window",
    "is_conductor_session",
]
