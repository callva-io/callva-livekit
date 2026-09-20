from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fakes import Boom, FakeHttpSession, FakeResponse
from fakes import use_http as use

from callva.livekit.core import transport
from callva.livekit.core.log import MAX_QUOTED
from callva.livekit.core.transport import WebhookTarget

TARGET = WebhookTarget(url="https://example.test/hook", secret="s3cret")


async def test_successful_delivery_is_one_request(monkeypatch):
    session = use(monkeypatch, FakeHttpSession(FakeResponse(200)))

    assert await transport.post_json(TARGET, event="call.started", payload={"a": 1}, key="k")
    assert len(session.calls) == 1


async def test_server_errors_are_retried_then_give_up(monkeypatch, no_sleep):
    session = use(
        monkeypatch,
        FakeHttpSession(*(FakeResponse(503, "nope") for _ in range(4))),
    )

    assert not await transport.post_json(TARGET, event="call.ended", payload={}, key="k")
    assert len(session.calls) == 4, "one attempt plus three retries"


async def test_a_retry_that_succeeds_stops_there(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeHttpSession(FakeResponse(500), FakeResponse(200)))

    assert await transport.post_json(TARGET, event="call.ended", payload={}, key="k")
    assert len(session.calls) == 2


async def test_client_errors_fail_fast(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeHttpSession(FakeResponse(422, "bad shape")))

    assert not await transport.post_json(TARGET, event="call.ended", payload={}, key="k")
    assert len(session.calls) == 1, "a 4xx is a refusal, not a hiccup"


async def test_network_errors_are_retried(monkeypatch, no_sleep):
    reset = Boom(OSError("connection reset"))
    session = use(monkeypatch, FakeHttpSession(reset, FakeResponse(200)))

    assert await transport.post_json(TARGET, event="call.started", payload={}, key="k")
    assert len(session.calls) == 2


async def test_signature_is_over_timestamp_and_body(monkeypatch):
    session = use(monkeypatch, FakeHttpSession(FakeResponse(200)))

    payload = {"event": "call.started", "id": "k"}
    await transport.post_json(TARGET, event="call.started", payload=payload, key="k")

    _url, kwargs = session.calls[0]
    headers = kwargs["headers"]
    body = kwargs["data"].decode("utf-8")

    expected = hmac.new(
        b"s3cret",
        f"{headers['X-Webhook-Timestamp']}.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()

    assert headers["X-Webhook-Signature"] == f"sha256={expected}"
    assert json.loads(body) == payload


async def test_an_unsigned_target_sends_no_signature(monkeypatch):
    session = use(monkeypatch, FakeHttpSession(FakeResponse(200)))

    await transport.post_json(
        WebhookTarget(url="https://example.test/hook"), event="e", payload={}, key="k"
    )

    headers = session.calls[0][1]["headers"]
    assert "X-Webhook-Signature" not in headers
    assert headers["X-Webhook-Idempotency-Key"] == "k"


async def test_config_fetch_returns_the_decoded_body(monkeypatch):
    use(monkeypatch, FakeHttpSession(FakeResponse(200, '{"prompt": "hello"}')))

    assert await transport.fetch_json("https://example.test/config", payload={}) == {
        "prompt": "hello"
    }


async def test_config_fetch_gives_up_loudly_on_a_client_error(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeHttpSession(FakeResponse(404, "no such agent")))

    with pytest.raises(transport.FetchError, match="404"):
        await transport.fetch_json("https://example.test/config", payload={})

    assert len(session.calls) == 1


async def test_a_client_error_keeps_its_body_for_code_still_in_this_process(monkeypatch, no_sleep):
    """The one place the page is kept whole, alongside this machine's log. Nothing is lost
    by narrowing what is reported; it is moved to where holding it is somebody's right."""
    page = "<html><body>no such agent: tenant 4417 has no number +3726001234</body></html>"
    use(monkeypatch, FakeHttpSession(FakeResponse(404, page)))

    with pytest.raises(transport.FetchError) as raised:
        await transport.fetch_json("https://example.test/config", payload={})

    assert raised.value.status == 404
    assert raised.value.body == page
    assert str(raised.value) == "HTTP 404"


async def test_config_fetch_retries_server_errors(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeHttpSession(*(FakeResponse(500) for _ in range(3))))

    with pytest.raises(transport.FetchError):
        await transport.fetch_json("https://example.test/config", payload={})

    assert len(session.calls) == 3, "short: a call is ringing while this runs"


async def test_config_fetch_rejects_a_body_that_is_not_json(monkeypatch, no_sleep):
    use(monkeypatch, FakeHttpSession(FakeResponse(200, "<html>oops</html>")))

    with pytest.raises(transport.FetchError, match="not JSON"):
        await transport.fetch_json("https://example.test/config", payload={})


async def test_api_key_is_sent_as_a_bearer_token(monkeypatch):
    session = use(monkeypatch, FakeHttpSession(FakeResponse(200, "{}")))

    await transport.fetch_json("https://example.test/config", payload={}, api_key="abc")

    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer abc"


def test_target_from_environment(monkeypatch):
    assert WebhookTarget.from_env() is None

    monkeypatch.setenv("WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setenv("WEBHOOK_SECRET", "s")

    assert WebhookTarget.from_env() == WebhookTarget("https://example.test/hook", "s")


def test_target_from_a_mapping():
    assert WebhookTarget.from_dict({"url": "https://x.test", "secret": " k "}) == WebhookTarget(
        "https://x.test", "k"
    )
    assert WebhookTarget.from_dict({"url": "   "}) is None
    assert WebhookTarget.from_dict(None) is None


# --- A configuration endpoint that says no -----------------------------------

REFUSED = json.dumps(
    {
        "error": "This number is disabled.",
        "action": "terminate",
        "reason_code": "number_disabled",
        "caller_message": "Sorry, this number is not in service.",
    }
)


async def test_a_refusal_is_one_request_and_carries_what_it_said(monkeypatch, no_sleep):
    """402 and not a cent left. The reason is the whole point of asking."""
    session = use(monkeypatch, FakeHttpSession(FakeResponse(402, REFUSED)))

    with pytest.raises(transport.ConfigRefused) as raised:
        await transport.fetch_json("https://example.test/config", payload={})

    refusal = raised.value
    assert refusal.status == 402
    assert refusal.action == "terminate"
    assert refusal.reason_code == "number_disabled"
    assert refusal.caller_message == "Sorry, this number is not in service."
    assert refusal.error == "This number is disabled."
    assert len(session.calls) == 1, "an answer is not a hiccup"


async def test_a_refusal_spelled_as_a_server_error_is_still_not_retried(monkeypatch, no_sleep):
    """Retrying it would hammer an endpoint already struggling, for a call it refused."""
    session = use(monkeypatch, FakeHttpSession(FakeResponse(503, REFUSED)))

    with pytest.raises(transport.ConfigRefused):
        await transport.fetch_json("https://example.test/config", payload={})

    assert len(session.calls) == 1


async def test_a_refusal_is_not_a_failure_to_fetch(monkeypatch, no_sleep):
    """The two are different answers, and only one of them has anything to say."""
    use(monkeypatch, FakeHttpSession(FakeResponse(402, REFUSED)))

    with pytest.raises(transport.ConfigRefused):
        try:
            await transport.fetch_json("https://example.test/config", payload={})
        except transport.FetchError as exc:  # pragma: no cover - the assertion is the point
            raise AssertionError("a refusal must not arrive as a FetchError") from exc


def test_a_refusal_is_a_configuration_error():
    """So an agent that handles configuration failing needs no new handler for this."""
    refusal = transport.ConfigRefused(status=402, action="terminate")

    assert isinstance(refusal, transport.ConfigError)
    assert not isinstance(refusal, transport.FetchError)


async def test_a_client_error_that_asks_for_nothing_is_still_a_fetch_error(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeHttpSession(FakeResponse(422, '{"error": "bad shape"}')))

    with pytest.raises(transport.FetchError, match="422"):
        await transport.fetch_json("https://example.test/config", payload={})

    assert len(session.calls) == 1


async def test_a_server_error_that_asks_for_nothing_is_still_retried(monkeypatch, no_sleep):
    session = use(
        monkeypatch,
        FakeHttpSession(*(FakeResponse(500, '{"error": "down"}') for _ in range(3))),
    )

    with pytest.raises(transport.FetchError):
        await transport.fetch_json("https://example.test/config", payload={})

    assert len(session.calls) == 3


async def test_an_error_body_that_is_not_json_is_read_no_further(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeHttpSession(FakeResponse(404, "<html>no such agent</html>")))

    with pytest.raises(transport.FetchError, match="404"):
        await transport.fetch_json("https://example.test/config", payload={})

    assert len(session.calls) == 1


async def test_a_refusal_answered_with_a_good_status_is_still_a_refusal(monkeypatch, no_sleep):
    """The action decides, not the status: a responder that says no at 200 means it."""
    use(monkeypatch, FakeHttpSession(FakeResponse(200, REFUSED)))

    with pytest.raises(transport.ConfigRefused):
        await transport.fetch_json("https://example.test/config", payload={})


async def test_a_configuration_is_not_mistaken_for_a_refusal(monkeypatch):
    body = {"agent": {"prompt": "hello"}, "preset": {"name": "vertex"}}
    use(monkeypatch, FakeHttpSession(FakeResponse(200, json.dumps(body))))

    assert await transport.fetch_json("https://example.test/config", payload={}) == body


async def test_what_a_good_status_that_is_not_json_says_and_keeps(monkeypatch, no_sleep):
    """An expired session and a proxy's login page, which is what this branch is really for."""
    page = "<!doctype html><html><body>Sign in</body></html>"
    session = use(monkeypatch, FakeHttpSession(FakeResponse(200, page)))

    with pytest.raises(transport.FetchError) as raised:
        await transport.fetch_json("https://example.test/config", payload={})

    assert str(raised.value) == (
        f"HTTP 200 was not JSON: {len(page)} bytes, Expecting value: line 1 column 1 (char 0)"
    )
    assert raised.value.status == 200
    assert raised.value.body == page, "kept whole for code still in this process"
    assert len(session.calls) == 1, "a page is an answer, not a hiccup"


async def test_a_server_error_that_is_not_json_is_still_retried(monkeypatch, no_sleep):
    """The not-JSON branch belongs to a good status only: a 500 HTML page is still a 500."""
    pages = (FakeResponse(500, "<html>ow</html>") for _ in range(3))
    session = use(monkeypatch, FakeHttpSession(*pages))

    with pytest.raises(transport.FetchError, match=r"^HTTP 500$"):
        await transport.fetch_json("https://example.test/config", payload={})

    assert len(session.calls) == 3


def test_a_refusal_quotes_what_it_was_told_up_to_a_limit():
    """The message is what travels, and the responder wrote all of it."""
    refusal = transport.ConfigRefused(status=402, action="terminate", error="X" * 100_000)

    assert refusal.error == "X" * 100_000, "kept whole on the object for the caller"
    assert str(refusal) == f"HTTP 402: {'X' * MAX_QUOTED}"


def test_what_makes_a_body_a_refusal_is_a_shape_and_not_a_credential():
    """Said plainly because the opposite was written down: that an error page "cannot
    arrive by construction". It can. Any JSON object with a top-level terminate does."""
    block_page = {
        "action": "terminate",
        "error": "Request blocked. Support ID: 18446744073709551616",
        "reason_code": "waf_rule_942100",
    }

    refusal = transport.ConfigRefused.parse(block_page, status=403)

    assert refusal is not None
    assert refusal.reason_code == "waf_rule_942100"


def test_only_the_action_makes_a_body_a_refusal():
    """No wrapper key, and nothing else read: a wrapper would be one vendor's envelope."""
    assert transport.ConfigRefused.parse({"error": "nope"}, status=402) is None
    assert transport.ConfigRefused.parse({"action": "continue"}, status=402) is None
    assert transport.ConfigRefused.parse({"data": {"action": "terminate"}}, status=402) is None
    assert transport.ConfigRefused.parse("terminate", status=402) is None
    assert transport.ConfigRefused.parse(None, status=402) is None

    refusal = transport.ConfigRefused.parse({"action": " Terminate "}, status=429)
    assert refusal is not None
    assert refusal.action == "terminate"
    assert refusal.reason_code is None
    assert refusal.caller_message is None


# --- Naming an endpoint in something that will be written down ----------------


def test_an_endpoint_is_named_by_the_parts_that_identify_it():
    """Scheme, host, port and path say which endpoint. The rest is where a key rides."""
    assert (
        transport.endpoint_name("https://svc:s3cret@platform.test/v1/config?token=abc#frag")
        == "https://platform.test/v1/config"
    )
    assert transport.endpoint_name("http://10.0.0.4:8080/config") == "http://10.0.0.4:8080/config"
    assert transport.endpoint_name("https://platform.test") == "https://platform.test"


def test_an_ipv6_host_is_named_in_the_brackets_it_arrived_in():
    """``hostname`` strips them, and a name without them is a different host on a
    different port — ``2001:db8::1:8443`` — which parses as neither and reads as a lie."""
    assert (
        transport.endpoint_name("https://[2001:db8::1]:8443/v1/config")
        == "https://[2001:db8::1]:8443/v1/config"
    )
    assert transport.endpoint_name("https://[2001:db8::1]/v1/config") == (
        "https://[2001:db8::1]/v1/config"
    )
    assert transport.endpoint_name("http://[::1]:8080/config") == "http://[::1]:8080/config"


def test_a_url_that_cannot_be_read_is_named_rather_than_echoed():
    """It is unreadable to us and not to whoever reads the report, so it is not quoted."""
    named = "the configuration endpoint"

    assert transport.endpoint_name("https://platform.test:not-a-port/config") == named
    assert transport.endpoint_name("not a url at all") == named
    assert transport.endpoint_name("") == named


def test_a_url_that_is_not_a_string_is_named_rather_than_raised_on():
    """Both callers work this name out before the request it is about to describe, and
    outside any try. A raise here takes the call down instead of reporting it."""
    named = "the configuration endpoint"

    assert transport.endpoint_name(None) == named
    assert transport.endpoint_name(b"https://platform.test/v1/config") == named
    assert transport.endpoint_name(42) == named
    assert transport.endpoint_name(object()) == named
