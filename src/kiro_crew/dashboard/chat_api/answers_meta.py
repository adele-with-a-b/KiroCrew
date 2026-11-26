"""The answering mode's fold on a person's sent row: ``meta.answers``.

When the person sends the answers an app's card collected (the dashboard's
answering mode, ``website/src/linkPreview/answering/``), the dashboard folds the
row as "N answers sent" with the app's name, and the full message opens behind
"Show full message". The row's content and the turn's input stay the whole text.

The fold is Crew's words over the person's own send, so it is kept only on a
person's send (never an app token's or an agent's, which could otherwise fold a
row it wrote to look like the person's answers), and only in its one shape: a
whole ``count`` from 1 to :data:`MAX_ANSWERS` and a short ``app`` name. Anything
else is dropped, and the row renders as a plain message.
"""

from __future__ import annotations

from typing import Any

#: The row meta key the dashboard reads (``answersFold.ts``).
ANSWERS_META_KEY = "answers"
#: Most answers one send may count.
MAX_ANSWERS = 999
#: Longest app name the fold shows.
MAX_APP_CHARS = 64


def answers_meta(raw: Any, *, person_send: bool) -> dict[str, Any] | None:
    """The fold to keep on the row, or None to drop it."""
    if not person_send or not isinstance(raw, dict):
        return None
    count = raw.get("count")
    app = raw.get("app")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ANSWERS:
        return None
    if not isinstance(app, str):
        return None
    name = "".join(ch for ch in app if ch.isprintable()).strip()
    if not name or len(name) > MAX_APP_CHARS:
        return None
    return {"count": count, "app": name}


__all__ = ["ANSWERS_META_KEY", "MAX_ANSWERS", "MAX_APP_CHARS", "answers_meta"]
