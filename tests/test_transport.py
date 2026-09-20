from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fakes import Boom, FakeHttpSession, FakeResponse
from fakes import use_http as use

from callva.livekit.core import transport
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


def test_a_url_that_cannot_be_read_is_named_rather_than_echoed():
    """It is unreadable to us and not to whoever reads the report, so it is not quoted."""
    named = "the configuration endpoint"

    assert transport.endpoint_name("https://platform.test:not-a-port/config") == named
    assert transport.endpoint_name("not a url at all") == named
    assert transport.endpoint_name("") == named
