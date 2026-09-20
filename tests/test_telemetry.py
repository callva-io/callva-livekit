"""Getting a worker's spans and logs out to somewhere they can be read.

The proof that matters here is not that the code runs - it is that a collector receives
something. So the end-to-end test stands a real HTTP server up, points the exporters at it
through the ordinary OpenTelemetry variables, and reads what arrives. It runs in a subprocess
because installing a provider is a once-per-process act that OpenTelemetry will not let a test
undo, and a test that quietly failed to install would otherwise pass.

Everything above that is a decision this module makes rather than a pipeline it builds, and
those are tested in-process.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from callva.livekit import telemetry


@pytest.fixture(autouse=True)
def uninstalled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that has installed nothing, which is what every test starts from."""
    monkeypatch.setattr(telemetry, "_installed", False)
    monkeypatch.setattr(telemetry, "_marks", {})


# --------------------------------------------------------------------------------------
# When it installs and when it does not


def test_a_deployment_that_named_no_collector_installs_nothing():
    # No providers, no processors, no threads - which is what a deployment that has not asked
    # for telemetry should get, rather than a pipeline exporting into the void.
    assert telemetry.start() is False
    assert telemetry.started() is False


@pytest.mark.parametrize("said", ["1", "true", "TRUE", "yes", "on"])
def test_opentelemetrys_own_kill_switch_is_honoured(monkeypatch, said):
    monkeypatch.setenv(telemetry.ENDPOINT, "https://collector.example.com")
    monkeypatch.setenv(telemetry.DISABLED, said)

    assert telemetry.start() is False


def test_an_endpoint_of_whitespace_is_no_endpoint(monkeypatch):
    monkeypatch.setenv(telemetry.ENDPOINT, "   ")
    assert telemetry.start() is False


def test_installing_twice_installs_once(monkeypatch):
    monkeypatch.setenv(telemetry.ENDPOINT, "https://collector.example.com")
    installs: list[Any] = []
    monkeypatch.setattr(telemetry, "_install", lambda: installs.append(1))

    assert telemetry.start() is True
    assert telemetry.start() is True
    assert installs == [1]


def test_a_collector_that_cannot_be_installed_does_not_cost_the_call(monkeypatch, caplog):
    # Telemetry is how a call is understood afterwards; it is not how a call is conducted.
    monkeypatch.setenv(telemetry.ENDPOINT, "https://collector.example.com")

    def broken() -> None:
        raise RuntimeError("no exporter here")

    monkeypatch.setattr(telemetry, "_install", broken)

    with caplog.at_level(logging.WARNING, logger="callva.livekit"):
        assert telemetry.start() is False

    assert telemetry.started() is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


# --------------------------------------------------------------------------------------
# Whose call this telemetry belongs to


def test_marks_are_replaced_rather_than_merged():
    # A process serves one call at a time, and a mark left from the previous one would attach
    # somebody else's identifier to this one's spans.
    telemetry.identify(call_id="first", agent_id="a")
    telemetry.identify(call_id="second")

    assert telemetry._marks == {"call_id": "second"}


def test_identifying_with_nothing_clears():
    telemetry.identify(call_id="first")
    telemetry.identify()

    assert telemetry._marks == {}


def test_a_caller_may_pass_whatever_it_has():
    # Absent identifiers are ordinary - a console run has no call id - so the caller is not
    # asked to filter before calling.
    telemetry.identify(call_id="c", agent_id=None, environment="", direction="inbound", n=7)

    assert telemetry._marks == {"call_id": "c", "direction": "inbound", "n": "7"}


def test_a_span_is_stamped_when_it_starts_rather_than_when_it_is_sent():
    # A batch leaves this process long after the call it describes has ended, by which time the
    # marks belong to whoever is on the line now.
    telemetry.identify(call_id="while-it-was-running")
    stamped: dict[str, Any] = {}
    span = type("Span", (), {"set_attributes": stamped.update})()

    telemetry._MarkSpans().on_start(span)
    telemetry.identify(call_id="somebody-else")

    assert stamped == {"call_id": "while-it-was-running"}


def test_a_log_record_is_stamped_without_losing_what_it_carried():
    telemetry.identify(call_id="c")
    record = type("Record", (), {"attributes": {"code.lineno": 12}})()

    telemetry._MarkRecords().on_emit(type("Data", (), {"log_record": record})())

    assert record.attributes == {"code.lineno": 12, "call_id": "c"}


def test_nothing_is_stamped_before_a_call_is_identified():
    record = type("Record", (), {"attributes": {"code.lineno": 12}})()

    telemetry._MarkRecords().on_emit(type("Data", (), {"log_record": record})())

    assert record.attributes == {"code.lineno": 12}


def test_the_hooks_declared_here_are_the_ones_the_sdk_calls():
    # Both processors are the SDK's to call, and it has changed which methods it calls between
    # versions - an `_on_ending` on spans, `on_emit` rather than `emit` on records. A processor
    # that answers the wrong name raises inside the SDK while a call is running, so the names
    # are held to the base classes rather than remembered.
    from opentelemetry.sdk._logs import LogRecordProcessor
    from opentelemetry.sdk.trace import SpanProcessor

    assert issubclass(telemetry._MarkSpans, SpanProcessor)
    assert issubclass(telemetry._MarkRecords, LogRecordProcessor)
    assert not getattr(telemetry._MarkRecords, "__abstractmethods__", None)
    assert not getattr(telemetry._MarkSpans, "__abstractmethods__", None)


# --------------------------------------------------------------------------------------
# The log handler, which is the framework's on one kind of deployment and ours on the other


def test_the_framework_attaches_its_own_on_a_cloud_deployment(monkeypatch):
    # Attaching a second handler bound to the same provider exports every line twice.
    monkeypatch.setenv("LIVEKIT_URL", "wss://something-abcdef.livekit.cloud")
    assert telemetry._framework_attaches_its_own() is True


def test_it_does_not_on_a_self_hosted_one(monkeypatch):
    # Where the framework will not, attaching none means the collector receives no logs at all.
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit.internal:7880")
    assert telemetry._framework_attaches_its_own() is False


def test_an_observability_url_set_by_hand_is_the_frameworks_too(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit.internal:7880")
    monkeypatch.setenv("LIVEKIT_OBSERVABILITY_URL", "https://obs.example.com")
    assert telemetry._framework_attaches_its_own() is True


def test_the_test_for_cloud_is_the_frameworks_own_and_not_a_copy():
    # A hostname rule this package repeated would be a hostname rule this package had to keep
    # true. Held to the framework's, so a change there is a failure here rather than a silent
    # divergence about which deployment gets logs.
    from livekit.agents.utils.misc import is_cloud

    assert is_cloud("wss://x.livekit.cloud") is True
    assert is_cloud("wss://x.livekit.run") is True
    assert is_cloud("ws://livekit.internal:7880") is False


def test_a_framework_that_cannot_be_asked_gets_our_handler(monkeypatch):
    # A deployment that wanted logs and got them twice has a nuisance; one that wanted logs and
    # got none has nothing.
    monkeypatch.setattr(telemetry.os.environ, "get", lambda *a, **k: "")
    monkeypatch.setitem(sys.modules, "livekit.agents.utils.misc", None)

    assert telemetry._framework_attaches_its_own() is False


# --------------------------------------------------------------------------------------
# What actually arrives at a collector


COLLECTOR = textwrap.dedent(
    """
    import json, logging, os, sys, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    arrived = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            arrived.append((self.path, dict(self.headers), body))
            self.send_response(200); self.send_header("content-length", "0"); self.end_headers()
        def log_message(self, *a): pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:" + str(server.server_port)
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "x-ingestion-key=secret"
    os.environ["OTEL_SERVICE_NAME"] = "callva-under-test"
    os.environ["LIVEKIT_URL"] = "ws://livekit.internal:7880"
    os.environ["OTEL_BSP_SCHEDULE_DELAY"] = "50"

    sys.path.insert(0, sys.argv[1])
    from callva.livekit import telemetry

    assert telemetry.start() is True, "nothing was installed"
    telemetry.identify(call_id="the-call", agent_id="the-agent")

    from opentelemetry import trace
    with trace.get_tracer("test").start_as_current_span("a-turn"):
        logging.getLogger("callva.livekit").error("something a reader would want")

    from opentelemetry import _logs as logs_api
    trace.get_tracer_provider().force_flush()
    logs_api.get_logger_provider().force_flush()

    print(json.dumps({
        "paths": sorted({path for path, _, _ in arrived}),
        "keyed": all(h.get("x-ingestion-key") == "secret" for _, h, _ in arrived),
        "service": any(b"callva-under-test" in body for _, _, body in arrived),
        "call": any(b"the-call" in body for _, _, body in arrived),
        "agent": any(b"the-agent" in body for _, _, body in arrived),
        "span": any(b"a-turn" in body for _, _, body in arrived),
        "line": any(b"something a reader would want" in body for _, _, body in arrived),
    }))
    """
)


def test_a_collector_receives_the_spans_and_the_logs_with_the_call_on_them():
    """The whole point, proven against a server rather than against a mock.

    Installing a provider is once per process and OpenTelemetry will not let it be undone, so
    this runs somewhere it can happen for real - and a run where nothing installed fails on the
    assertion inside rather than passing quietly.
    """
    package = str(Path(__file__).resolve().parent.parent)
    run = subprocess.run(
        [sys.executable, "-c", COLLECTOR, package],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    got = json.loads(run.stdout.strip().splitlines()[-1])

    # Both signals, each at the path OpenTelemetry appends for it - neither spelled here.
    assert got["paths"] == ["/v1/logs", "/v1/traces"]
    # The header the collector authenticates with, read from OpenTelemetry's own variable.
    assert got["keyed"], "the ingestion header did not reach the collector"
    assert got["service"], "OTEL_SERVICE_NAME did not reach the resource"
    # A span, a log line, and the call both belong to.
    assert got["span"], "the span never arrived"
    assert got["line"], "the log line never arrived"
    assert got["call"] and got["agent"], "telemetry arrived without the call it describes"
