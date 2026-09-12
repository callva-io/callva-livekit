"""The shape of a call: waiting for it to be answered, and ending it.

Neither is something the SDK models. ``await_pickup`` tells an outbound call that is
ringing from one that was answered; ``end`` releases the caller before the job shuts
down, which is the difference between a call that has ended and one that merely has no
agent in it.
"""

from .pickup import ANSWER_TIMEOUT, PICKUP_FAILURE, await_pickup
from .release import GRACE, QUIET_TIMEOUT, end, leave_console_when_done, until_quiet

__all__ = [
    "ANSWER_TIMEOUT",
    "GRACE",
    "PICKUP_FAILURE",
    "QUIET_TIMEOUT",
    "await_pickup",
    "end",
    "leave_console_when_done",
    "until_quiet",
]
