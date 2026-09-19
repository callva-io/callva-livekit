"""Per-call configuration resolution.

Configuration reaches the agent through agent dispatch metadata — ``ctx.job.metadata`` —
either as a body or as a pointer to an endpoint, or from an endpoint named in the
environment. Room metadata is deliberately not used: it is broadcast to every participant
in the room, so a prompt placed there is readable by any connected client.

An endpoint may also answer no. That answer is an object — :class:`ConfigRefused`, a
:class:`ConfigError` that carries the refusal whole — and not a log line, because it can
carry a message the responder wrote for the person on the phone, along with a machine code
this package passes through without reading.
"""

from ..core.transport import ConfigError, ConfigRefused
from .models import AgentConfig, CallConfig, Variables
from .resolver import as_path, load, request_payload
from .template import render

__all__ = [
    "AgentConfig",
    "CallConfig",
    "ConfigError",
    "ConfigRefused",
    "Variables",
    "as_path",
    "load",
    "render",
    "request_payload",
]
