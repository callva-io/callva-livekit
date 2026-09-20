"""The shape of a call: waiting for it to be answered, and ending it.

Neither is something the SDK models. ``await_pickup`` tells an outbound call that is
ringing from one that was answered; ``end`` releases the caller before the job shuts
down, which is the difference between a call that has ended and one that merely has no
agent in it.

Every ending this module can see for itself is claimed as it happens — a call nobody
answered, a duration limit, the other end leaving, silence — and ``ended_by`` lets
whoever ends a call some other way claim it too. The first claim is the one that stands.

Silence is the one of those the framework half-models and this module finishes. ``supervise``
measures the quiet on its own clock, hands a reminder to an ``Utterance`` its caller supplies,
and ends the call when the reminders run out — none of which the framework's single
``user_away_timeout`` edge can express.
"""

from .pickup import ANSWER_TIMEOUT, await_pickup
from .release import (
    AGENT,
    DURATION,
    GRACE,
    NO_ANSWER,
    QUIET_TIMEOUT,
    SILENCE,
    USER,
    end,
    ended_by,
    leave_console_when_done,
    until_quiet,
)
from .supervise import PROMPT_GRACE, Utterance, stop, supervise

__all__ = [
    "AGENT",
    "ANSWER_TIMEOUT",
    "DURATION",
    "GRACE",
    "NO_ANSWER",
    "PROMPT_GRACE",
    "QUIET_TIMEOUT",
    "SILENCE",
    "USER",
    "Utterance",
    "await_pickup",
    "end",
    "ended_by",
    "leave_console_when_done",
    "stop",
    "supervise",
    "until_quiet",
]
