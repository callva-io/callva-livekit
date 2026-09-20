"""Send this worker's spans and logs somewhere they can be read.

The framework already produces the hard part. Every job, every turn, every transcription and
generation and synthesis is already a span with real timings on it, and every log line is
already structured. What is missing is a way out: the framework exports all of it to LiveKit
Cloud and nowhere else, so a deployment with its own collector sees none of it.

This is that way out, and it is the whole of what this module does. It installs OpenTelemetry
providers pointed at a collector before the framework looks for any, and the framework then
adds its own processors on top of them rather than building its own - so what leaves this
worker is the framework's own telemetry, not a second one written here.

**Why this matters more than it sounds.** A voice agent's faults are timing faults. Whether the
caller's words arrived before the turn closed, how long a model took to begin speaking, which
of four components was slow on a call that felt slow - none of it is visible in a log line and
all of it is in the spans. Every question of that kind that has been answered about this
platform was answered by querying them.

**Configuration is OpenTelemetry's own, not this package's.** ``OTEL_EXPORTER_OTLP_ENDPOINT``
turns it on and names the collector; ``OTEL_EXPORTER_OTLP_HEADERS`` carries whatever that
collector authenticates with; ``OTEL_SERVICE_NAME`` and ``OTEL_RESOURCE_ATTRIBUTES`` say who is
reporting. Not one of them is read here - the OpenTelemetry SDK reads its own specified
variables, appends the per-signal paths, and builds the resource. Nothing is parsed, nothing is
re-spelled, and no vendor is named: a collector is a URL and a header, and which product is
behind it is the deployment's business.

    OTEL_EXPORTER_OTLP_ENDPOINT=https://ingest.eu2.example.com:443
    OTEL_EXPORTER_OTLP_HEADERS=some-ingestion-key=...
    OTEL_SERVICE_NAME=callva-worker

Unset the endpoint and this module does nothing at all - no providers, no processors, no
threads - which is what a deployment that has not asked for telemetry should get.

**Metrics are not installed**, deliberately. Spans and logs answer questions somebody is
actually asking; a metrics pipeline nobody reads is cost and moving parts for a dashboard that
does not exist. It belongs here the day someone decides to read one.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from opentelemetry.sdk._logs import LogRecordProcessor
from opentelemetry.sdk.trace import SpanProcessor

from ..core.log import logger

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
"""What turns this on. OpenTelemetry's own variable, read by its own SDK and not by this."""

DISABLED = "OTEL_SDK_DISABLED"
"""OpenTelemetry's own kill switch, honoured here because a deployment that sets it means it."""

_installed = False
_lock = threading.Lock()
_marks: dict[str, str] = {}
"""What every span and log leaving this process is stamped with. See :func:`identify`.

A plain dict rather than a context variable, because batch processors export from threads of
their own that inherited no context - and because a job process serves one call at a time, so
a value that is process-wide is also call-wide.
"""


def identify(**marks: Any) -> None:
    """Say which call the telemetry leaving this process belongs to.

    Without this, spans and logs arrive with timings and no way to tell whose call they
    describe - which makes them almost useless, because every question worth asking is about
    one call or about calls that share something.

    Replaces rather than merges: a process serves one call at a time, and a mark left over from
    the previous one would attach somebody else's identifier to this one's spans. Call it with
    nothing to clear.

    Values are stringified and empty ones dropped, so a caller can pass whatever it has without
    filtering first.
    """
    with _lock:
        _marks.clear()
        _marks.update({k: str(v) for k, v in marks.items() if v not in (None, "")})


def started() -> bool:
    """Whether telemetry is installed in this process."""
    return _installed


def start() -> bool:
    """Install the providers, if a collector was named. Once per process; never raises.

    **Call this from inside the job entrypoint, not at module import.** The supervisor process
    replays worker log records through its own queue, so a log handler attached before the fork
    exports every line a second time, without the call it belonged to. The reference worker
    carries that lesson in a comment; this carries it in the only instruction that prevents it.

    It must also run before the session starts, because the framework looks for pre-installed
    providers at that moment and merges its processors onto whatever it finds. Installed after,
    it finds none, builds its own, and everything but the outermost span goes to the collector
    the framework chose instead of the one this deployment did.

    Returns whether telemetry is now running, so a caller that wants to say so can.
    """
    global _installed
    with _lock:
        if _installed:
            return True
        if os.environ.get(DISABLED, "").strip().lower() in ("1", "true", "yes", "on"):
            return False
        if not os.environ.get(ENDPOINT, "").strip():
            return False

        try:
            _install()
        except Exception as exc:
            # Telemetry is how a call is understood afterwards; it is not how a call is
            # conducted. A collector that cannot be reached, a package that will not import,
            # a malformed variable - none of that is a reason for somebody's call to fail.
            logger.warning(
                "telemetry could not be installed, so this call goes unrecorded: %s", exc
            )
            return False

        _installed = True
    logger.info("telemetry: spans and logs are going to %s", os.environ[ENDPOINT].strip())
    return True


def _install() -> None:
    """Build the two pipelines and hand them to the framework.

    Every argument the exporters and the resource would take is left for the OpenTelemetry SDK
    to read from its own environment variables, which is what it is specified to do: the
    per-signal path, the headers, the service name and the resource attributes all arrive that
    way. Passing them here would be a second spelling of a settled contract.
    """
    from opentelemetry import _logs as logs_api
    from opentelemetry import trace as trace_api
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create()

    traces = TracerProvider(resource=resource)
    # Ahead of the exporter, so a span is stamped when it starts rather than when it is sent.
    # A batch leaves this process long after the call it describes has ended, by which time the
    # marks belong to whoever is on the line now.
    traces.add_span_processor(_MarkSpans())
    traces.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace_api.set_tracer_provider(traces)
    _tell_the_framework(traces)

    records = LoggerProvider(resource=resource)
    records.add_log_record_processor(_MarkRecords())
    records.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    logs_api.set_logger_provider(records)
    _bridge_python_logging(records)


def _tell_the_framework(provider: Any) -> None:
    """Point the framework's own tracer at this provider.

    It keeps a reference of its own, separate from OpenTelemetry's global, and uses that one
    for everything inside a call. Left alone it builds its own, and a collector then receives
    the single span that wraps the job and none of the per-turn spans underneath it - which are
    the ones worth having.
    """
    try:
        from livekit.agents.telemetry import set_tracer_provider
    except ImportError:
        logger.warning(
            "this framework exposes no way to point its tracer at a collector, so only the "
            "outermost span of each call will arrive"
        )
        return
    set_tracer_provider(provider)


def _bridge_python_logging(provider: Any) -> None:
    """Forward ordinary log records into the collector, unless the framework already does.

    A logger provider is not a sink on its own: something has to put ``logging`` records into
    it, and that something is a handler on the root logger. The framework attaches one of its
    own - but only where it is talking to LiveKit Cloud, which is a property of the URL rather
    than of anything configured here. Where it will, attaching a second handler bound to the
    same provider exports every line twice; where it will not, attaching none means the
    collector receives no logs at all.

    So the question is asked of the framework rather than answered here, and the fallback when
    it cannot be asked is to attach: a deployment that wanted logs and got them twice has a
    nuisance, and one that wanted logs and got none has nothing.
    """
    if _framework_attaches_its_own():
        logger.debug("the framework attaches its own log handler on this deployment")
        return

    from opentelemetry.sdk._logs import LoggingHandler

    logging.getLogger().addHandler(LoggingHandler(level=logging.NOTSET, logger_provider=provider))


def _framework_attaches_its_own() -> bool:
    """Whether the framework will put its own OTLP handler on the root logger.

    It does so for a LiveKit Cloud deployment and not otherwise, and the test for that is the
    framework's own rather than a second copy of it here - a hostname rule this package
    repeated would be a hostname rule this package had to keep true.
    """
    try:
        from livekit.agents.utils.misc import is_cloud
    except ImportError:
        return False

    url = os.environ.get("LIVEKIT_OBSERVABILITY_URL", "").strip()
    if url:
        return True
    return is_cloud(os.environ.get("LIVEKIT_URL", "").strip())


class _MarkSpans(SpanProcessor):
    """Stamps every span, as it starts, with whoever :func:`identify` last named.

    Subclassed from the SDK's own processor rather than merely shaped like one. The base
    grows hooks - this version calls an ``_on_ending`` that the last one did not - and a
    processor that only answers the methods it knew about raises inside the SDK the moment a
    span ends. Inheriting means an unknown hook gets the base's answer instead of an error on
    a live call.
    """

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        with _lock:
            marks = dict(_marks)
        if marks:
            span.set_attributes(marks)


class _MarkRecords(LogRecordProcessor):
    """The same, for log records, which travel a pipeline of their own."""

    def on_emit(self, log_record: Any) -> None:
        with _lock:
            marks = dict(_marks)
        record = getattr(log_record, "log_record", log_record)
        if not marks or record is None:
            return
        carried = getattr(record, "attributes", None)
        if carried is None:
            record.attributes = marks
            return
        # Updated in place rather than replaced: the SDK hands over a bounded mapping of its
        # own that the exporter reads back, and a plain dictionary put in its place loses the
        # limits it was carrying.
        for name, value in marks.items():
            carried[name] = value

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


__all__ = ["ENDPOINT", "identify", "start", "started"]
