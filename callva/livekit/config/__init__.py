"""Per-call configuration resolution.

Configuration reaches the agent through agent dispatch metadata — ``ctx.job.metadata`` —
either as a body or as a pointer to an endpoint, or from an endpoint named in the
environment. Room metadata is deliberately not used: it is broadcast to every participant
in the room, so a prompt placed there is readable by any connected client.
"""

from .models import CallConfig, Variables
from .resolver import ConfigError, load, request_payload
from .template import render

__all__ = [
    "CallConfig",
    "ConfigError",
    "Variables",
    "load",
    "render",
    "request_payload",
]
