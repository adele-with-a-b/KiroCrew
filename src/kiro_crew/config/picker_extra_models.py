"""Read ``dashboard.model_picker_extra_models``: model ids a backend runs but does not list.

claude-agent-acp advertises only the rows in the user's Claude settings, yet passes an
unlisted id to Claude Code on a live pick. The operator names such ids per backend id;
only the claude backend reads them (:data:`READING_BACKENDS`), where the model picker
offers them beside the advertised rows.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.validation_parts.input_bounds import (
    MODEL_PICKER_HIDDEN_MODEL_ID_RE,
    MODEL_PICKER_HIDDEN_MODELS_MAX,
)

# Backends whose picker offers the added ids. Claude Code runs a model per chat
# process, so a pick reaches the adapter's own ``set_config_option``; a backend on
# the shared runtime checks a pick against its advertised list first and would
# refuse an added row.
READING_BACKENDS = frozenset({ACP_BACKEND_CLAUDE})


def coerce_picker_extra_models(raw: object) -> dict[str, list[str]]:
    """``{backend: [model id, ...]}`` from a stored value; anything malformed is dropped.

    Ids share the picker visibility list's grammar (``MODEL_PICKER_HIDDEN_MODEL_ID_RE``),
    so a provider's own spelling (``us.anthropic.…``, ``…[1m]``) is kept and whitespace or
    a control character is not. ``auto`` is the picker's own row and is never added.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    total = 0
    for backend, ids in raw.items():
        if not isinstance(backend, str) or not isinstance(ids, list):
            continue
        kept: list[str] = []
        for value in ids:
            model = value.strip() if isinstance(value, str) else ""
            if not model or model == "auto" or model in kept:
                continue
            if not MODEL_PICKER_HIDDEN_MODEL_ID_RE.fullmatch(model):
                continue
            if total >= MODEL_PICKER_HIDDEN_MODELS_MAX:
                break
            kept.append(model)
            total += 1
        if kept:
            out[backend] = kept
    return out


def extra_ids_for(cfg: Any, backend: str) -> list[str]:
    """The added ids *backend* offers under *cfg*; ``[]`` off :data:`READING_BACKENDS`."""
    if backend not in READING_BACKENDS:
        return []
    dashboard = getattr(cfg, "dashboard", None)
    extras = getattr(dashboard, "model_picker_extra_models", None) or {}
    return list(extras.get(backend) or ())
