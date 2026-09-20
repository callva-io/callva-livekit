from __future__ import annotations

import logging
import os.path
import threading
from types import TracebackType
from typing import Any

from ..core.log import MAX_QUOTED, delivery

MAX_ERRORS = 100
"""Past this many, a call is broken in a way one more line will not explain."""

MAX_FRAMES = 20
"""Frames kept per exception, the middle dropped past it.

The outermost frames say which of this call's paths began the failure and the innermost
say what finally raised. A stack deep enough to need cutting is deep enough that what sits
between those two is a library's own plumbing, which nobody reading a report acts on.
"""

MAX_CAUSES = 3
"""Links of the caused-by chain kept. Deeper than that is a retry loop re-raising itself."""

DENY_PREFIXES = (delivery.name,)
"""The one path whose errors are not collected: the path that delivers the report.

A failing delivery logs its failure, that failure is collected, and the next report carries
it to the same endpoint that could not be reached — so the delivery path reports itself
forever. Nothing else in this library has that shape. Everything else it logs is about the
call, and a call that broke is the thing a report exists to say, so an error from a stack
that could not open, a configuration that could not be resolved or a plugin that could not
be registered travels in ``errors`` like any other.

Taken from the logger object rather than spelled again as a string: the deny and the logger
it denies are one fact, and the copy that is typed out is the copy that drifts. What no
name can enforce is that the reporting path actually logs on it, which is why
``tests/test_errors.py`` drives a failing delivery and a failing upload through the real
code and asserts the collector stayed empty.
"""


class _Collector(logging.Handler):
    """Keeps the errors of one call, for whoever reports it.

    Records arrive from whatever thread raised them — an SDK worker, an HTTP client's
    callback — so the buffer is guarded by a lock rather than held in a contextvar. A
    LiveKit worker runs one job per process, which is what makes a process-wide buffer
    the same thing as a per-call one.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(DENY_PREFIXES):
            return
        with self._lock:
            if len(self._records) >= MAX_ERRORS:
                self._dropped += 1
                return
            self._records.append(_describe(record))

    def drain(self) -> list[dict[str, Any]] | None:
        with self._lock:
            records, dropped = self._records, self._dropped
            self._records, self._dropped = [], 0
        if not records:
            return None
        if dropped:
            records.append({"logger": __name__, "message": f"and {dropped} more"})
        return records


def _describe(record: logging.LogRecord) -> dict[str, Any]:
    """One ERROR record as the report carries it.

    This handler is on the root logger, so the record may have been written by anything in
    the process — an SDK, a plugin, an HTTP client — and the report it goes into is read by
    whoever the deployment points ``WEBHOOK_URL`` at. Two things follow.

    The line itself is kept, and bounded. A log line is a sentence somebody wrote for an
    operator about what went wrong, which is the whole reason this collector exists: take
    it away and a call that fell apart reports that it fell apart and nothing more. But it
    is that author's sentence and not this package's, so it travels up to
    :data:`~callva.livekit.core.log.MAX_QUOTED` characters and no further. What a library
    attaches to a record structurally — ``extra=``, ``args`` past formatting, anything else
    in ``record.__dict__`` — is not read here at all and never was.

    The exception is rewritten rather than copied; :func:`_exception` says what it keeps.
    """
    described: dict[str, Any] = {
        "at": record.created,
        "logger": record.name,
        "level": record.levelname,
        "message": record.getMessage()[:MAX_QUOTED],
    }
    exc = record.exc_info[1] if record.exc_info else None
    if isinstance(exc, BaseException):
        described["exception"] = _exception(exc)
    return described


def _exception(exc: BaseException) -> str:
    """What failed and roughly where, in this package's words rather than the raiser's.

    Three things are deliberately absent, and each of them is somebody's to keep and not
    this report's.

    The exception's own message. A vendor's client writes it for a vendor's operator, and
    it is where an organisation id, a project, a quota, a prompt or a key echoed back ends
    up; ``logger.exception`` sites in this package and the one beside it all say what they
    were doing in the log line above, so the sentence a reader needs is the one
    :func:`_describe` already kept.

    The source line. ``traceback.format_exception`` renders one per frame by reading this
    deployment's files off its disk at the moment of the failure, which puts first-party
    source into a payload that leaves the machine.

    The filename. It is an absolute path inside this container, and it describes the
    deployment rather than the call.

    What is left is what a reader acts on: the class that was raised, what it was raised
    from, and the module, line and function of each frame it passed through. A module name
    says which package failed and where inside it — the same answer a filename gives,
    without also giving the layout of the machine.
    """
    lines = [_class(exc), *_frames(exc.__traceback__)]

    cause, depth = _caused_by(exc), 0
    while cause is not None and depth < MAX_CAUSES:
        lines.append(f"caused by: {_class(cause)}")
        cause, depth = _caused_by(cause), depth + 1

    return "\n".join(lines)


def _class(exc: BaseException) -> str:
    """The exception by its dotted class, which names the library as well as the kind."""
    module = type(exc).__module__
    name = type(exc).__qualname__
    return name if module in ("builtins", "__main__") else f"{module}.{name}"


def _caused_by(exc: BaseException) -> BaseException | None:
    """The exception this one was raised from, explicitly or by being raised while handling it."""
    if exc.__cause__ is not None:
        return exc.__cause__
    return None if exc.__suppress_context__ else exc.__context__


def _frames(tb: TracebackType | None) -> list[str]:
    """Each frame as module, line and function — read off the frame, never off the disk."""
    frames: list[str] = []
    while tb is not None:
        code = tb.tb_frame.f_code
        module = tb.tb_frame.f_globals.get("__name__")
        if not isinstance(module, str):
            # A frame with no module name — exec'd code, a REPL. Its file's base name is
            # the nearest thing to one, and is a name rather than a path.
            module = os.path.basename(code.co_filename)
        frames.append(f"  at {module}:{tb.tb_lineno} in {code.co_name}")
        tb = tb.tb_next

    if len(frames) > MAX_FRAMES:
        outer = MAX_FRAMES // 4
        head, tail = frames[:outer], frames[outer - MAX_FRAMES :]
        frames = [*head, f"  ... {len(frames) - MAX_FRAMES} frames", *tail]
    return frames


_collector: _Collector | None = None
_install_lock = threading.Lock()


def collect() -> None:
    """Start collecting errors, so a failed call can say what failed.

    Call it once, wherever the worker starts up. It attaches a handler to the root logger
    at ERROR level, which is a process-wide thing to do and therefore asked for rather
    than assumed: an application that routes its own errors may not want a second reader.

    Root level and not this package's: what breaks a call is usually not this package. It
    is the model's client, the SDK, a plugin. So what is collected is every ERROR line the
    process writes, which means the report carries other people's sentences about this
    call — bounded, and stripped of the machinery around them, but theirs. A deployment
    that points ``WEBHOOK_URL`` at a party it does not control is forwarding them, and
    that is the trade this call makes.

    What it collects reaches the consumer inside ``call.ended``. There is no separate
    event for a call that fell apart — that call still ends, and still reports; what was
    missing was ever saying why.
    """
    global _collector
    with _install_lock:
        if _collector is not None:
            return
        _collector = _Collector()
        logging.getLogger().addHandler(_collector)


def drain() -> list[dict[str, Any]] | None:
    """Take what has been collected so far, leaving the buffer empty."""
    return _collector.drain() if _collector is not None else None


def stop() -> None:
    """Detach the handler. For a test, or a worker shutting down."""
    global _collector
    with _install_lock:
        if _collector is None:
            return
        logging.getLogger().removeHandler(_collector)
        _collector = None
