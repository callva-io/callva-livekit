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

MAX_QUOTED = 500
"""The most of another party's text this package copies into a record that leaves here.

ERROR is that boundary: a record at that level is delivered inside ``call.ended`` to
whatever endpoint the deployment names, and the party that wrote the text, the party
running this worker and the party receiving the report are three parties as often as one.
So text this package did not compose — a responder's refusal, the message of an exception
raised by a library, a line another logger wrote — is quoted up to here and no further.

Text this package composed itself is not foreign and is not cut: an endpoint named by
:func:`callva.livekit.core.transport.endpoint_name`, a status, a file it was told to read.
Below the boundary nothing is cut at all. This machine's log is where the whole of what
another party said belongs, and a post-mortem reads it there.
"""
