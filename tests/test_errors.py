from __future__ import annotations

import json
import logging
import socket

import pytest
from fakes import Boom, FakeContext, FakeHttpSession, FakeResponse, use_http

from callva.livekit import config as callva_config
from callva.livekit import webhook as callva_webhook
from callva.livekit.config import resolver
from callva.livekit.core import log, transport
from callva.livekit.core.transport import WebhookTarget
from callva.livekit.webhook import errors
from callva.livekit.webhook.storage import Storage


@pytest.fixture(autouse=True)
def collecting() -> None:
    errors.collect()
    yield
    errors.stop()


def test_nothing_collected_is_nothing_reported():
    assert errors.drain() is None


def test_an_error_anywhere_in_the_process_is_kept():
    logging.getLogger("some.plugin").error("the model refused the session")

    collected = errors.drain()

    assert len(collected) == 1
    assert collected[0]["message"] == "the model refused the session"
    assert collected[0]["logger"] == "some.plugin"


def test_a_traceback_comes_with_it():
    try:
        raise RuntimeError("no credit on the account")
    except RuntimeError:
        logging.getLogger("some.plugin").exception("session refused")

    collected = errors.drain()

    assert "no credit on the account" in collected[0]["exception"]


def test_warnings_are_not_errors():
    logging.getLogger("some.plugin").warning("slow")

    assert errors.drain() is None


def test_the_delivery_path_does_not_report_itself():
    """A failing delivery logs an error, which would be reported, which would fail...

    Through the logger object, never through its name: what the deny list has to hold is
    the path, and the name it currently goes by is not a second fact to pin.
    """
    log.delivery.error("could not deliver call.ended")

    assert errors.drain() is None


def test_the_delivery_path_is_the_only_one_of_ours_that_is_silenced():
    """Every module in this library reports the call except the one delivering the report.

    Asserted through the real loggers rather than against the prefix tuple: reading
    DENY_PREFIXES here would pass against any value it happened to hold, and the value it
    held for a year dropped everything below into a container log and nowhere else.
    """
    log.logger.error("the stack this call runs on could not be asked how it opens")
    log.delivery.error("could not deliver call.ended")

    collected = errors.drain()

    assert [e["message"] for e in collected] == [
        "the stack this call runs on could not be asked how it opens"
    ]


async def test_what_this_library_logs_about_the_call_reaches_the_report(
    bind_context, monkeypatch
):
    """The configuration that could not be resolved, driven through the resolver itself.

    Logged by a production path and not by a name typed into the test: what the report has
    to carry is the error this library really raises about a call, and the only evidence
    that it travels is that the collector holds it after the code that raises it ran.

    The name it arrives under is the package logger, which is the one every module of this
    library and of the internal distribution that shares the namespace logs on. A reader of
    ``errors`` is told the path, not the wheel: nothing is decided differently by knowing
    which distribution shipped the module, and giving the internal one a logger of its own
    would be a naming scheme with no reader.
    """
    bind_context(FakeContext())

    async def refuse(*_: object, **__: object) -> object:
        raise transport.FetchError("HTTP 500: upstream is down")

    monkeypatch.setattr(resolver.transport, "fetch_json", refuse)

    await callva_config.load(url="https://platform.test/config", on_error="continue")

    collected = errors.drain()

    assert [e["logger"] for e in collected] == ["callva.livekit"]
    assert "upstream is down" in collected[0]["message"]


def test_a_call_that_breaks_without_stopping_is_capped():
    for n in range(errors.MAX_ERRORS + 5):
        logging.getLogger("some.plugin").error("failure %s", n)

    collected = errors.drain()

    assert len(collected) == errors.MAX_ERRORS + 1
    assert collected[-1]["message"] == "and 5 more"


def test_draining_empties_the_buffer():
    logging.getLogger("some.plugin").error("once")

    assert errors.drain() is not None
    assert errors.drain() is None


def test_collecting_twice_attaches_one_handler():
    root = logging.getLogger()
    before = len(root.handlers)

    errors.collect()

    assert len(root.handlers) == before


def test_nothing_is_collected_until_asked():
    errors.stop()
    logging.getLogger("some.plugin").error("unheard")

    assert errors.drain() is None


# --- The reporting path cannot report itself ---------------------------------
#
# What keeps the loop shut is that the delivery path logs on the one logger ``DENY_PREFIXES``
# names. A test that reads ``DENY_PREFIXES``, or that logs on that name itself, asserts the
# deny list and never the connection: a delivery site that reached for the package logger
# would leave the deny list right, every such test green, and the loop wide open. So every
# test below installs the real collector, drives a real failure through the real delivery
# code, and asserts that the collector stayed empty.

TARGET = WebhookTarget(url="https://example.test/hook")


async def test_a_rejection_is_not_quoted_back_to_the_endpoint_that_rejected_it(
    monkeypatch, no_sleep
):
    """A 4xx: the receiver understood call.started and refused it, and says why."""
    use_http(monkeypatch, FakeHttpSession(FakeResponse(422, "unprocessable: unknown call id")))

    assert not await transport.post_json(TARGET, event="call.started", payload={}, key="k")
    assert errors.drain() is None


async def test_a_delivery_that_exhausts_its_retries_reports_nothing(monkeypatch, no_sleep):
    """A 5xx through all four attempts, which logs once more when it gives up."""
    use_http(monkeypatch, FakeHttpSession(*(FakeResponse(503, "gateway down") for _ in range(4))))

    assert not await transport.post_json(TARGET, event="call.started", payload={}, key="k")
    assert errors.drain() is None


async def test_a_refused_connection_reports_nothing(monkeypatch, no_sleep):
    """Nothing is listening: every attempt raises on the way out, and the last one is loud."""
    refused = ConnectionRefusedError(61, "Connection refused")
    use_http(monkeypatch, FakeHttpSession(*(Boom(refused) for _ in range(4))))

    assert not await transport.post_json(TARGET, event="call.started", payload={}, key="k")
    assert errors.drain() is None


async def test_a_host_that_does_not_resolve_reports_nothing(monkeypatch, no_sleep):
    """The endpoint's name is gone, which fails before a socket is ever opened."""
    unresolved = socket.gaierror(-2, "Name or service not known")
    use_http(monkeypatch, FakeHttpSession(*(Boom(unresolved) for _ in range(4))))

    assert not await transport.post_json(TARGET, event="call.started", payload={}, key="k")
    assert errors.drain() is None


async def test_a_delivery_that_really_goes_out_and_really_fails_reports_nothing(no_sleep):
    """The same, with nothing faked: a real client, a real socket, a real refusal.

    The port is bound and released, so the address is routable and nothing is behind it.
    This is the only one of these that also holds whatever the HTTP client itself logs on
    the way down, which no deny list of ours covers.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    target = WebhookTarget(url=f"http://127.0.0.1:{port}/hook")

    assert not await transport.post_json(
        target, event="call.started", payload={}, key="k", timeout=2.0
    )
    assert errors.drain() is None


async def test_an_upload_that_fails_reports_nothing(monkeypatch):
    """The recording and the session report travel by upload, and that path reports too."""

    class Refusing:
        def put_object(self, **_: object) -> None:
            raise RuntimeError("AccessDenied: not authorized to perform s3:PutObject")

    monkeypatch.setattr(Storage, "_client", lambda _self: Refusing())

    assert not await Storage(bucket="recordings").put_json("call-1.session.json", {"a": 1})
    assert errors.drain() is None


async def test_an_upload_with_no_client_installed_reports_nothing(monkeypatch):
    """The other way ``_run`` fails: the [s3] extra was never installed.

    Raised from the patched client rather than left to the real ``import boto3``, so the
    branch is driven whether or not this environment happens to have boto3.
    """

    def missing(_self: object) -> object:
        raise ImportError("No module named 'boto3'")

    monkeypatch.setattr(Storage, "_client", missing)

    assert not await Storage(bucket="recordings").put_json("call-1.session.json", {"a": 1})
    assert errors.drain() is None


async def test_a_rejected_start_does_not_ride_out_inside_the_call_that_ended(
    bind_context, monkeypatch, no_sleep
):
    """The whole loop, end to end, as it was found: one call, two deliveries, one endpoint.

    call.started is rejected with a 422 and the rejection is logged. call.ended is built
    minutes later out of whatever the collector holds and posted to the same endpoint. If
    the delivery path's own failure were collected, the body below would carry the text of
    its own rejection back to the receiver that wrote it.
    """
    monkeypatch.setenv("WEBHOOK_URL", "https://example.test/hook")
    rejected = (FakeResponse(422, "unprocessable: unknown call id") for _ in range(2))
    session = use_http(monkeypatch, FakeHttpSession(*rejected))

    ctx = bind_context(FakeContext())
    callva_webhook.attach()
    for entrypoint in ctx.participant_entrypoints:
        await entrypoint(ctx, ctx._participant)
    await callva_webhook.on_session_end(ctx)

    posted = [kwargs["data"].decode("utf-8") for _url, kwargs in session.calls]
    assert len(posted) == 2, "call.started and call.ended"
    assert '"errors": null' in posted[1]
    assert "unprocessable" not in posted[1]


# --- What the configuration endpoint said stays where it was said -------------
#
# A configuration failure is logged at ERROR and therefore delivered inside call.ended, to
# whatever WEBHOOK_URL the deployment names. That endpoint and the one that serves the
# configuration are two parties as often as one, so what the report carries about the
# failure is this package's own account of it. The body is on the exception for code still
# in this process and in this container's log for the post-mortem, and reaches neither the
# message nor any other field of the record.

TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/srv/config/app/resolver.py", line 88, in resolve\n'
    "    tenant = Tenant.objects.get(number=number)\n"
    "django.db.utils.OperationalError: FATAL: password authentication failed "
    'for user "config_ro" at 10.0.0.4'
)
"""What a configuration endpoint answers when its own database is down."""


async def test_a_configuration_failure_reports_the_status_and_not_the_page(
    bind_context, monkeypatch, no_sleep
):
    """A real 500 with a real stack trace in it, through the real fetch and the real collector.

    The whole record is searched, not only the message: a body kept anywhere in it — under
    another key, inside an exception — has left the machine just the same.
    """
    use_http(monkeypatch, FakeHttpSession(*(FakeResponse(500, TRACE) for _ in range(3))))
    bind_context(FakeContext())

    with pytest.raises(callva_config.ConfigError):
        await callva_config.load(url="https://platform.test/v1/config")

    collected = errors.drain()

    assert [e["message"] for e in collected] == [
        "configuration request to https://platform.test/v1/config failed: HTTP 500; "
        "terminating the call"
    ]
    assert "Traceback" not in json.dumps(collected)
    assert "password authentication failed" not in json.dumps(collected)


async def test_a_refused_request_reports_the_status_and_not_the_page(
    bind_context, monkeypatch, no_sleep
):
    """The other branch: a 4xx is not retried, and fails from inside the attempt."""
    use_http(monkeypatch, FakeHttpSession(FakeResponse(403, TRACE)))
    bind_context(FakeContext())

    await callva_config.load(url="https://platform.test/v1/config", on_error="continue")

    collected = errors.drain()

    assert [e["message"] for e in collected] == [
        "configuration request to https://platform.test/v1/config failed: HTTP 403; "
        "continuing without configuration"
    ]
    assert "django.db" not in json.dumps(collected)


async def test_a_failure_with_no_response_is_named_by_what_stopped_it(
    bind_context, monkeypatch, no_sleep
):
    """Nothing answered, so there is no status to report. A timeout's own text is empty."""
    use_http(monkeypatch, FakeHttpSession(*(Boom(TimeoutError()) for _ in range(3))))
    bind_context(FakeContext())

    await callva_config.load(url="https://platform.test/v1/config", on_error="continue")

    collected = errors.drain()

    assert [e["message"] for e in collected] == [
        "configuration request to https://platform.test/v1/config failed: TimeoutError; "
        "continuing without configuration"
    ]


async def test_the_endpoint_is_reported_by_name_and_asked_in_full(
    bind_context, monkeypatch, no_sleep
):
    """Which endpoint failed is worth reporting. The key it is asked with is not.

    The request still goes out with everything, which is the point: the parts that carry a
    secret are dropped from what is written down, not from what is sent.
    """
    session = use_http(monkeypatch, FakeHttpSession(FakeResponse(401, "invalid token")))
    bind_context(FakeContext())
    url = "https://svc:s3cret@platform.test/v1/config?token=abc123#frag"

    await callva_config.load(url=url, on_error="continue")

    collected = errors.drain()

    assert [e["message"] for e in collected] == [
        "configuration request to https://platform.test/v1/config failed: HTTP 401; "
        "continuing without configuration"
    ]
    assert "s3cret" not in json.dumps(collected)
    assert "abc123" not in json.dumps(collected)
    assert session.calls[0][0] == url


async def test_the_container_log_still_carries_what_the_endpoint_said(
    bind_context, monkeypatch, no_sleep, caplog
):
    """The same failure, read from this machine's log: the page is there, whole.

    Two levels, one failure. WARNING is where the body is written and ERROR is where the
    account of the failure is, and the collector takes only the second — so a post-mortem
    on this container reads everything and the report reads what it can act on.
    """
    use_http(monkeypatch, FakeHttpSession(*(FakeResponse(500, TRACE) for _ in range(3))))
    bind_context(FakeContext())

    with caplog.at_level(logging.WARNING, logger="callva.livekit"):
        await callva_config.load(url="https://platform.test/v1/config", on_error="continue")

    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    assert len(warned) == 3, "one per attempt"
    assert TRACE in warned[0]
    assert "HTTP 500" in warned[0]
    # A single line of it, because the record is searched as JSON and a newline is escaped
    # there: the whole trace would be "absent" from the dump however plainly it was in it.
    assert "django.db.utils.OperationalError" not in json.dumps(errors.drain())


async def test_the_body_is_on_the_failure_for_code_still_in_this_process(monkeypatch, no_sleep):
    """Nothing is thrown away. A caller that wants the page holds the exception."""
    use_http(monkeypatch, FakeHttpSession(*(FakeResponse(503, TRACE) for _ in range(3))))

    with pytest.raises(transport.FetchError) as raised:
        await transport.fetch_json("https://platform.test/v1/config", payload={})

    assert raised.value.status == 503
    assert raised.value.body == TRACE
    assert str(raised.value) == "HTTP 503"
