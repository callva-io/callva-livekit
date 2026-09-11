from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from callva.livekit.core import transport
from callva.livekit.core.transport import WebhookTarget


class FakeResponse:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class Boom:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self) -> Any:
        raise self._error

    async def __aexit__(self, *_: object) -> bool:
        return False


class FakeSession:
    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(200)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(transport.asyncio, "sleep", instant)


def use(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> FakeSession:
    monkeypatch.setattr(transport, "_session", lambda: (session, False))
    return session


TARGET = WebhookTarget(url="https://example.test/hook", secret="s3cret")


async def test_successful_delivery_is_one_request(monkeypatch):
    session = use(monkeypatch, FakeSession(FakeResponse(200)))

    assert await transport.post_json(TARGET, event="call.started", payload={"a": 1}, key="k")
    assert len(session.calls) == 1


async def test_server_errors_are_retried_then_give_up(monkeypatch, no_sleep):
    session = use(
        monkeypatch,
        FakeSession(*(FakeResponse(503, "nope") for _ in range(4))),
    )

    assert not await transport.post_json(TARGET, event="call.ended", payload={}, key="k")
    assert len(session.calls) == 4, "one attempt plus three retries"


async def test_a_retry_that_succeeds_stops_there(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeSession(FakeResponse(500), FakeResponse(200)))

    assert await transport.post_json(TARGET, event="call.ended", payload={}, key="k")
    assert len(session.calls) == 2


async def test_client_errors_fail_fast(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeSession(FakeResponse(422, "bad shape")))

    assert not await transport.post_json(TARGET, event="call.ended", payload={}, key="k")
    assert len(session.calls) == 1, "a 4xx is a refusal, not a hiccup"


async def test_network_errors_are_retried(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeSession(Boom(OSError("connection reset")), FakeResponse(200)))

    assert await transport.post_json(TARGET, event="call.started", payload={}, key="k")
    assert len(session.calls) == 2


async def test_signature_is_over_timestamp_and_body(monkeypatch):
    session = use(monkeypatch, FakeSession(FakeResponse(200)))

    payload = {"event": "call.started", "id": "k"}
    await transport.post_json(TARGET, event="call.started", payload=payload, key="k")

    _url, kwargs = session.calls[0]
    headers = kwargs["headers"]
    body = kwargs["data"].decode("utf-8")

    expected = hmac.new(
        b"s3cret",
        f"{headers['X-Callva-Timestamp']}.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()

    assert headers["X-Callva-Signature"] == f"sha256={expected}"
    assert json.loads(body) == payload


async def test_an_unsigned_target_sends_no_signature(monkeypatch):
    session = use(monkeypatch, FakeSession(FakeResponse(200)))

    await transport.post_json(
        WebhookTarget(url="https://example.test/hook"), event="e", payload={}, key="k"
    )

    headers = session.calls[0][1]["headers"]
    assert "X-Callva-Signature" not in headers
    assert headers["X-Callva-Idempotency-Key"] == "k"


async def test_config_fetch_returns_the_decoded_body(monkeypatch):
    use(monkeypatch, FakeSession(FakeResponse(200, '{"prompt": "hello"}')))

    assert await transport.fetch_json("https://example.test/config", payload={}) == {
        "prompt": "hello"
    }


async def test_config_fetch_gives_up_loudly_on_a_client_error(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeSession(FakeResponse(404, "no such agent")))

    with pytest.raises(transport.FetchError, match="404"):
        await transport.fetch_json("https://example.test/config", payload={})

    assert len(session.calls) == 1


async def test_config_fetch_retries_server_errors(monkeypatch, no_sleep):
    session = use(monkeypatch, FakeSession(*(FakeResponse(500) for _ in range(3))))

    with pytest.raises(transport.FetchError):
        await transport.fetch_json("https://example.test/config", payload={})

    assert len(session.calls) == 3, "short: a call is ringing while this runs"


async def test_config_fetch_rejects_a_body_that_is_not_json(monkeypatch, no_sleep):
    use(monkeypatch, FakeSession(FakeResponse(200, "<html>oops</html>")))

    with pytest.raises(transport.FetchError, match="not JSON"):
        await transport.fetch_json("https://example.test/config", payload={})


async def test_api_key_is_sent_as_a_bearer_token(monkeypatch):
    session = use(monkeypatch, FakeSession(FakeResponse(200, "{}")))

    await transport.fetch_json("https://example.test/config", payload={}, api_key="abc")

    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer abc"


def test_target_from_environment(monkeypatch):
    assert WebhookTarget.from_env() is None

    monkeypatch.setenv("CALLVA_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setenv("CALLVA_WEBHOOK_SECRET", "s")

    assert WebhookTarget.from_env() == WebhookTarget("https://example.test/hook", "s")


def test_target_from_a_mapping():
    assert WebhookTarget.from_dict({"url": "https://x.test", "secret": " k "}) == WebhookTarget(
        "https://x.test", "k"
    )
    assert WebhookTarget.from_dict({"url": "   "}) is None
    assert WebhookTarget.from_dict(None) is None
