"""Per-agent Claude Code options from a user-level agent spec's ``claudeCode`` block.

A spec under the user's agents directory may carry::

    "claudeCode": {
        "autoMemoryDirectory": "/abs/path/to/memory",
        "env": {"NAME": "value"},
        "strictMcpConfig": true
    }

``autoMemoryDirectory`` rides the flag tier (``options.settings``), the tier a
``claude --settings`` flag uses, so it wins over the user's and the project's
settings files. ``env`` is spread over the adapter's process env
(``options.env``). ``strictMcpConfig`` can only be turned on: it keeps the
user's, the project's and plugins' servers out, so the session runs the
servers its spec names and nothing else.

Read from the USER scope only: a checked-out repository's ``.kiro/agents``
spec never reaches this, so a repo cannot redirect memory writes or set the
session's env. Nothing here loosens Crew's permission surface; Crew's own
switches applied after this (the auto-memory off switch, the read-only Side
Chat shape) still win.
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
    from kiro_crew.agent_parts.spec_lookup import _read_spec_capped, session_agent_spec_path

    try:
        path = session_agent_spec_path(agent)
    except ValueError:  # an ambiguous or unpinned name: no options, as for no spec
        return {}
    if path is None or path.suffix != ".json":
        return {}
    spec = _read_spec_capped(path)
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
    return out


def apply_spec_claude_options(options: dict[str, Any], agent: str | None) -> None:
    """Merge *agent*'s ``claudeCode`` block into a claude session's ``options``."""
    extra = spec_claude_options(agent)
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


__all__ = ["apply_spec_claude_options", "spec_claude_options"]
