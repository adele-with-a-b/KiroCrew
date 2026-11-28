"""A live session's ``process`` value in a status row, and the words a reader gets for it.

A ``session_status`` row and a worker tab's frame carry one of three
(``dashboard.session_control_parts.session_status.process_state`` decides which):

* :data:`LIVE`: this gateway holds the session's process;
* :data:`RELEASED`: no process runs (the idle sweep stopped it, or it never started);
  the next message starts one and keeps the conversation;
* :data:`KEPT`: a chat keeper kept the process running across a gateway restart and
  this gateway has not attached to it yet (``dashboard.chat_keeper_adopt``). It needs
  no message: one sent now starts the chat ahead of the adopt pass and is prompted
  into the kept session, where the turn it was running may still be going.

Leaf module: no Crew imports, so the dashboard and the MCP side both read it.
"""

from __future__ import annotations

LIVE = "live"
RELEASED = "released"
KEPT = "kept"

#: Appended to a resting row's status in the ``session_status`` tool's answer.
RELEASED_WORDS = ", released (no process running; the next message starts or resumes it)"
#: A kept row's whole status in that answer. It stands in for ``idle``, which reads
#: as "needs a decision".
KEPT_WORDS = (
    "kept running through the restart, waiting for the app to reconnect "
    "(wait and read again; do not message it)"
)
