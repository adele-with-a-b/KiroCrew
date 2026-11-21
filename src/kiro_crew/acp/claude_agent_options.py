"""Per-agent Claude Code options from a user-level agent spec's ``claudeCode`` block.

A spec under the user's agents directory may carry::

    "claudeCode": {
        "autoMemoryDirectory": "/abs/path/to/memory",
        "env": {"NAME": "value"},
        "strictMcpConfig": true,
        "tools": ["Read", "Bash"]
    }

``autoMemoryDirectory`` rides the flag tier (``options.settings``), the tier a
``claude --settings`` flag uses, so it wins over the user's and the project's
settings files. ``env`` is spread over the adapter's process env
(``options.env``). ``strictMcpConfig`` can only be turned on: it keeps the
user's, the project's and plugins' servers out, so the session runs the
servers its spec names and nothing else. ``tools`` names the built-in tools
the session keeps (``options.tools``); it only narrows, so a list Crew already
set (``"tools": []``, a worker's or a Side Chat's) is intersected, never widened.

Read from the USER scope only: a checked-out repository's ``.kiro/agents``
spec never reaches this, so a repo cannot redirect memory writes or set the
session's env. Nothing here loosens Crew's permission surface; Crew's own
switches applied after this (the auto-memory off switch, the read-only Side
Chat shape) still win.

A Bot (a crewmate) runs Crew's stock conductor and worker specs, so the block is
also read from the Bot's own file under ``<kiro home>/mates/agents/`` for both
its DM thread and its workers (:func:`crewmate_claude_options`). Every Bot's
session gets ``strictMcpConfig``, and one whose file names no
``autoMemoryDirectory`` gets Claude Code's auto-memory turned off, so no Bot
reads the home folder's memory index or the user's own MCP servers (fork only).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MAX_ENV = 16


def spec_claude_options(agent: str | None) -> dict[str, Any]:
    """The validated ``claudeCode`` block of *agent*'s user-level spec, or ``{}``."""
    if not agent:
        return {}
    from kiro_crew.agent import _read_spec_capped, session_agent_spec_path

    try:
        path = session_agent_spec_path(agent)
    except ValueError:  # an ambiguous or unpinned name: no options, as for no spec
        return {}
    if path is None or path.suffix != ".json":
        return {}
    return _validated(_read_spec_capped(path), agent)


def crewmate_claude_options(mate_id: str) -> dict[str, Any]:
    """The validated ``claudeCode`` block of Bot *mate_id*'s own file, or ``{}``."""
    if not mate_id:
        return {}
    from kiro_crew.agent import _read_spec_capped
    from kiro_crew.mate_config import mate_config_path

    try:
        path = mate_config_path(mate_id)
    except ValueError:
        return {}
    if not path.is_file():
        return {}
    return _validated(_read_spec_capped(path), "Bot " + mate_id)


def _validated(spec: Any, agent: str) -> dict[str, Any]:
    """The checked entries of *spec*'s ``claudeCode`` block; *agent* names it in a warning."""
    block = spec.get("claudeCode") if isinstance(spec, dict) else None
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    memory = block.get("autoMemoryDirectory")
    if isinstance(memory, str) and memory:
        p = Path(memory)
        if p.is_absolute() and ".." not in p.parts:
            out["autoMemoryDirectory"] = str(p)
        else:
            logger.warning(
                "agent %r: claudeCode.autoMemoryDirectory must be an absolute path", agent
            )
    env = block.get("env")
    if isinstance(env, dict):
        kept = {
            k: v
            for k, v in env.items()
            if isinstance(k, str) and isinstance(v, str) and _ENV_NAME.match(k)
        }
        if len(kept) != len(env) or len(kept) > _MAX_ENV:
            logger.warning(
                "agent %r: claudeCode.env entries dropped (NAME must be A-Z0-9_, values strings, at most %d)",
                agent,
                _MAX_ENV,
            )
        if kept and len(kept) <= _MAX_ENV:
            out["env"] = kept
    if block.get("strictMcpConfig") is True:
        out["strictMcpConfig"] = True
    tools = block.get("tools")
    if isinstance(tools, list) and all(isinstance(t, str) and t for t in tools):
        out["tools"] = list(dict.fromkeys(tools))
    return out


def apply_spec_claude_options(options: dict[str, Any], agent: str | None) -> None:
    """Merge *agent*'s ``claudeCode`` block into a claude session's ``options``."""
    _merge(options, spec_claude_options(agent))


def apply_crewmate_claude_options(options: dict[str, Any], session_key: str | None) -> bool:
    """Merge the Bot's own block into *options* when *session_key* is a Bot's session.

    A Bot's DM thread or worker (``crewmate_harness.crewmate_for_session``) always
    gets ``strictMcpConfig``. Returns True when the session is a Bot's and its
    file names no ``autoMemoryDirectory``: the caller then turns auto-memory off,
    as for an incognito session, so the Bot does not read the home folder's index.
    """
    if not session_key:
        return False
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.crewmate_harness import crewmate_for_session

        mate_id = crewmate_for_session(KiroCrewConfig.load(), session_key)
    except Exception:  # noqa: BLE001 - an unreadable config names no Bot; the session still starts
        logger.warning("could not tell whether session %r is a Bot's", session_key, exc_info=True)
        return False
    if not mate_id:
        return False
    extra = crewmate_claude_options(mate_id)
    _merge(options, extra)
    options["strictMcpConfig"] = True
    return "autoMemoryDirectory" not in extra


def _merge(options: dict[str, Any], extra: dict[str, Any]) -> None:
    if not extra:
        return
    if "env" in extra:
        options["env"] = {**extra["env"], **dict(options.get("env") or {})}
    if "autoMemoryDirectory" in extra:
        flag = dict(options.get("settings") or {})
        flag["autoMemoryDirectory"] = extra["autoMemoryDirectory"]
        options["settings"] = flag
    if extra.get("strictMcpConfig"):
        options["strictMcpConfig"] = True
    if "tools" in extra:
        current = options.get("tools")
        keep = extra["tools"]
        options["tools"] = [t for t in current if t in keep] if isinstance(current, list) else keep


__all__ = [
    "apply_crewmate_claude_options",
    "apply_spec_claude_options",
    "crewmate_claude_options",
    "spec_claude_options",
]
