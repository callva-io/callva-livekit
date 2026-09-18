"""Shared call identity, per-job state, transport and logging.

Modules in this package never import one another. They read and write the
:class:`CallState` that lives here, keyed off the ambient LiveKit job, which is how
configuration resolved by one module reaches another.
"""

from .envelope import ENVELOPE_KEY, DispatchEnvelope, parse
from .identity import (
    INBOUND,
    OUTBOUND,
    CallIdentity,
    Party,
    disconnect_reason_name,
    resolve,
    resolve_direction,
)
from .log import logger
from .state import CallState, NoJobContext, context, ensure_identity
from .state import state as call_state
from .transport import WebhookTarget, post_json
from .version import __version__

# `state` deliberately stays bound to the submodule: re-exporting the function under that
# name would shadow it, and every internal `from ..core import state` would silently get a
# function instead of the module.

__all__ = [
    "ENVELOPE_KEY",
    "INBOUND",
    "OUTBOUND",
    "CallIdentity",
    "CallState",
    "DispatchEnvelope",
    "NoJobContext",
    "Party",
    "WebhookTarget",
    "__version__",
    "call_state",
    "context",
    "disconnect_reason_name",
    "ensure_identity",
    "logger",
    "parse",
    "post_json",
    "resolve",
    "resolve_direction",
]
