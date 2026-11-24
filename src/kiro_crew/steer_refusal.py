"""Why a steer could not go into the running turn, as a code and in plain words.

A steer that cannot be injected falls back to the queue and waits for the turn
to end (``chat_delivery.steer_into_running_turn``). The sender and the person
reading the queued card are told why, so a note that may wait a long time is
never a silent fall-back. One code per cause: the steer path in
``chat_delivery`` names the causes it sees, and a client names the ones only it
can see (:attr:`AcpClient.steer_refusal`). The code rides the queue entry's meta
under :data:`QUEUE_META_KEY`, the send result and each broadcast row; the
dashboard draws its own words for it from the i18n catalog
(``components.queueStack.steer_refusal.<code>``), and the session tools read
the sentence :func:`describe` gives.

Leaf module: no Crew imports, so the client, the dashboard and the MCP side can
all read it.
"""

from __future__ import annotations

#: The turn ended, or another turn took its place, while the steer waited to go in.
TURN_ENDED = "turn_ended"
#: The session's agent takes no message mid-turn.
NO_STEER = "no_steer"
#: The agent can drop an accepted steer with its turn, and steers from other
#: sessions into such an agent are off (``agent.peer_steer_loss_recovery``).
PEER_STEER_OFF = "peer_steer_off"
#: The slot runs a turn but no prompt is in flight: it is being built or wound up.
BETWEEN_STEPS = "between_steps"
#: The turn is a manual ``/compact``.
COMPACTING = "compacting"
#: The same text is already on its way into this turn.
SAME_PENDING = "same_pending"
#: The turn already holds as many steers as it takes.
TOO_MANY = "too_many"
#: The agent started this turn on its own (a background task it ran finished).
AGENT_TURN = "agent_turn"
#: The turn runs a command, not a prompt.
COMMAND_TURN = "command_turn"
#: The turn waits on a tool approval.
PERMISSION_WAIT = "permission_wait"
#: The text is longer than a steer may be.
TOO_LONG = "too_long"
#: The agent answered that it did not take it, or the write did not go out.
REFUSED = "refused"

#: Every code, in the order the table below gives them.
CODES = (
    TURN_ENDED,
    NO_STEER,
    PEER_STEER_OFF,
    BETWEEN_STEPS,
    COMPACTING,
    SAME_PENDING,
    TOO_MANY,
    AGENT_TURN,
    COMMAND_TURN,
    PERMISSION_WAIT,
    TOO_LONG,
    REFUSED,
)

#: The queue-entry meta key that carries the code of the steer this entry fell back from.
QUEUE_META_KEY = "steerRefusal"

_WHY = {
    TURN_ENDED: "its turn ended while the steer waited to go in",
    NO_STEER: "its agent takes no message in the middle of a turn",
    PEER_STEER_OFF: (
        "its agent can lose a steer, so steers from other sessions are off "
        "(setting agent.peer_steer_loss_recovery)"
    ),
    BETWEEN_STEPS: "no prompt was running at that moment: the turn was starting or finishing",
    COMPACTING: "it is compacting its context",
    SAME_PENDING: "the same message is already on its way into this turn",
    TOO_MANY: "this turn already holds as many steers as it takes",
    AGENT_TURN: (
        "the agent started this turn on its own, when a background task it ran "
        "finished, and such a turn takes no steers"
    ),
    COMMAND_TURN: "the turn is running a command, which takes no steers",
    PERMISSION_WAIT: "the turn is waiting on a tool approval",
    TOO_LONG: "the message is longer than a steer may be",
    REFUSED: "its agent did not take it",
}

_WAITS = {
    AGENT_TURN: (
        "It runs when that turn ends, which can be a long while: the agent keeps "
        "it going for as long as it works on its own"
    ),
    PERMISSION_WAIT: "It runs when this turn ends, after the approval is answered",
}


def known(code: object) -> str:
    """*code* when it is one of :data:`CODES`, else ``""``."""
    return code if isinstance(code, str) and code in _WHY else ""


def describe(code: object) -> str:
    """The sentence a sender reads for a steer that fell back to the queue with *code*.

    An unknown or empty code gets the plain fall-back sentence, so a reply from
    a gateway that names no cause still says what happened.
    """
    code = known(code)
    if not code:
        return "Your steer could not go into the running turn, so it was queued instead."
    waits = _WAITS.get(code, "It runs when the current turn ends")
    return f"Your steer could not go into the running turn: {_WHY[code]}. {waits}."


def describe_short(code: object) -> str:
    """The same cause in a clause, for one broadcast row: ``steer fell back: <why>``."""
    code = known(code)
    if not code:
        return "steer fell back to the queue"
    if code == AGENT_TURN:
        return f"steer fell back: {_WHY[code]}; it waits for that turn to end, which can be a long while"
    return f"steer fell back: {_WHY[code]}"
