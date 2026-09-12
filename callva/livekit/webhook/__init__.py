"""Call lifecycle webhooks.

``call.started`` when someone is on the other end, ``call.ended`` when the call is over,
carrying the LiveKit session report verbatim — chat history, per-provider usage, session
options — inside a thin envelope that adds a stable call id, a direction, and a ``from``
and a ``to``.
"""

from .errors import collect as collect_errors
from .payload import ENDED, RECORDING, STARTED, build
from .service import attach, on_session_end, resolve_target
from .storage import Storage

__all__ = [
    "ENDED",
    "RECORDING",
    "STARTED",
    "Storage",
    "attach",
    "build",
    "collect_errors",
    "on_session_end",
    "resolve_target",
]
