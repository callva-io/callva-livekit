from __future__ import annotations

import logging
import threading
import traceback
from typing import Any

MAX_ERRORS = 100
"""Past this many, a call is broken in a way one more line will not explain."""

DENY_PREFIXES = ("callva.livekit.webhook",)
"""The one path whose errors are not collected: the path that delivers the report.

A failing delivery logs its failure, that failure is collected, and the next report carries
it to the same endpoint that could not be reached — so the delivery path reports itself
forever. Nothing else in this library has that shape. Everything else it logs is about the
call, and a call that broke is the thing a report exists to say, so an error from a stack
that could not open, a configuration that could not be resolved or a plugin that could not
be registered travels in ``errors`` like any other.

The prefix is a logger name and not a module path: the reporting path logs on
:data:`callva.livekit.core.log.delivery`, which is where that name comes from.
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
    described: dict[str, Any] = {
        "at": record.created,
        "logger": record.name,
        "level": record.levelname,
        "message": record.getMessage(),
    }
    if record.exc_info:
        described["exception"] = "".join(traceback.format_exception(*record.exc_info)).strip()
    return described


_collector: _Collector | None = None
_install_lock = threading.Lock()


def collect() -> None:
    """Start collecting errors, so a failed call can say what failed.

    Call it once, wherever the worker starts up. It attaches a handler to the root logger
    at ERROR level, which is a process-wide thing to do and therefore asked for rather
    than assumed: an application that routes its own errors may not want a second reader.

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
