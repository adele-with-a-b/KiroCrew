"""Read ``dashboard.model_picker_extra_models``: model ids a backend runs but does not list.

A backend's advertised list can leave out a model it runs (claude-agent-acp, for one,
passes an unlisted id to Claude Code on a live pick). The operator names such ids per
backend id, and the model picker offers them beside what the backend advertised.
"""

from __future__ import annotations

from kiro_crew.validation import MODEL_PICKER_HIDDEN_MODEL_ID_RE, MODEL_PICKER_HIDDEN_MODELS_MAX


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
