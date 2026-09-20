from __future__ import annotations

import logging

logger = logging.getLogger("callva.livekit")
"""The package logger.

The package never sets a level, never attaches a handler and never touches the root
logger. Whatever the host application has configured is what applies.
"""

delivery = logger.getChild("webhook")
"""The logger of the path that reports a call: building the body, delivering it, and
storing what it announces.

Named apart from the rest of the package because it is the one path whose own failures
cannot travel in the report it is failing to deliver. Everything else this library logs
is about the call and belongs in that report — see
:data:`callva.livekit.webhook.errors.DENY_PREFIXES`, which is the only reader of this
distinction.
"""
