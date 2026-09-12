"""Per-call configuration resolution.

Configuration reaches the agent through agent dispatch metadata — ``ctx.job.metadata`` —
either as a body or as a pointer to an endpoint, or from an endpoint named in the
environment. Room metadata is deliberately not used: it is broadcast to every participant
in the room, so a prompt placed there is readable by any connected client.
"""

from .models import AgentConfig, CallConfig, Variables
from .resolver import ConfigError, as_path, load, request_payload
from .template import render

__all__ = [
    "AgentConfig",
    "CallConfig",
    "ConfigError",
    "Variables",
    "as_path",
    "load",
    "render",
    "request_payload",
]
