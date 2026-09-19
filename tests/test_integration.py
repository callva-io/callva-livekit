"""End to end over real HTTP: a real client, a real server, a real signature."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from fakes import FakeContext

from callva.livekit.core import transport
from callva.livekit.core.transport import WebhookTarget

SECRET = "dev-secret"


@pytest.fixture
async def server():
    """A receiver that records what it was sent and checks the signature itself."""
    received: list[dict[str, Any]] = []

    async def hook(request: web.Request) -> web.Response:
        body = await request.read()
        expected = hmac.new(
            SECRET.encode(),
            f"{request.headers.get('X-Webhook-Timestamp')}.{body.decode()}".encode(),
            hashlib.sha256,
        ).hexdigest()

        received.append(
            {
                "payload": json.loads(body),
                "event": request.headers.get("X-Webhook-Event"),
                "key": request.headers.get("X-Webhook-Idempotency-Key"),
                "signature_valid": hmac.compare_digest(
                    request.headers.get("X-Webhook-Signature", ""), f"sha256={expected}"
                ),
            }
        )
        return web.json_response({"ok": True})

    async def config(request: web.Request) -> web.Response:
        received.append({"config_request": await request.json()})
        return web.json_response({"prompt": "hello {{ name }}", "variables": {"name": "Anna"}})

    async def refuse(request: web.Request) -> web.Response:
        received.append({"refusal_request": await request.json()})
        return web.json_response(
            {
                "error": "Balance exhausted.",
                "action": "terminate",
                "reason_code": "insufficient_balance",
                "caller_message": "Sorry, this service is unavailable right now.",
            },
            status=402,
        )

    app = web.Application()
    app.add_routes(
        [web.post("/hook", hook), web.post("/config", config), web.post("/refuse", refuse)]
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = runner.addresses[0][1]
    yield f"http://127.0.0.1:{port}", received

    await runner.cleanup()


async def test_a_signed_event_arrives_intact(server):
    base, received = server

    sent = await transport.post_json(
        WebhookTarget(f"{base}/hook", SECRET),
        event="call.started",
        payload={"event": "call.started", "call": {"id": "c1"}},
        key="c1:call.started:1",
    )

    assert sent
    assert received[0]["event"] == "call.started"
    assert received[0]["key"] == "c1:call.started:1"
    assert received[0]["signature_valid"], "the receiver could verify what we signed"
    assert received[0]["payload"]["call"]["id"] == "c1"


async def test_a_config_request_round_trips(server):
    base, received = server

    body = await transport.fetch_json(f"{base}/config", payload={"direction": "inbound"})

    assert body["prompt"] == "hello {{ name }}"
    assert received[0]["config_request"] == {"direction": "inbound"}


async def test_a_session_we_own_is_closed_again(monkeypatch, server):
    """Outside a job there is no shared session, so the one we make must not leak."""
    base, _ = server
    session = aiohttp.ClientSession()
    monkeypatch.setattr(transport, "_session", lambda: (session, True))

    await transport.post_json(
        WebhookTarget(f"{base}/hook"), event="e", payload={}, key="k"
    )

    assert session.closed


async def test_a_shared_session_is_left_open(monkeypatch, server):
    """Inside a job the SDK owns the session; closing it would break the rest of the call."""
    base, _ = server
    session = aiohttp.ClientSession()
    monkeypatch.setattr(transport, "_session", lambda: (session, False))

    try:
        await transport.post_json(
            WebhookTarget(f"{base}/hook"), event="e", payload={}, key="k"
        )
        assert not session.closed
    finally:
        await session.close()


async def test_a_refused_call_asks_once_and_keeps_what_it_was_told(
    server, bind_context, monkeypatch
):
    """The whole point: one request, and the message the responder wrote survives it."""
    from callva.livekit import config as callva_config

    base, received = server
    monkeypatch.setenv("CONFIG_URL", f"{base}/refuse")
    ctx = bind_context(FakeContext())

    with pytest.raises(callva_config.ConfigRefused) as raised:
        await callva_config.load()

    assert raised.value.status == 402
    assert raised.value.reason_code == "insufficient_balance"
    assert raised.value.caller_message == "Sorry, this service is unavailable right now."
    assert len([r for r in received if "refusal_request" in r]) == 1
    assert ctx.shutdown_reason is None, "what to say and when to hang up stay the caller's"


async def test_an_agent_written_before_refusals_existed_still_handles_one(
    server, bind_context, monkeypatch
):
    """Over real HTTP: the one handler an agent already had catches the new answer."""
    from callva.livekit import config as callva_config

    base, _ = server
    monkeypatch.setenv("CONFIG_URL", f"{base}/refuse")
    bind_context(FakeContext())

    caught: Exception | None = None
    try:
        await callva_config.load()
    except callva_config.ConfigError as exc:
        caught = exc

    assert isinstance(caught, callva_config.ConfigRefused)
    assert caught.caller_message == "Sorry, this service is unavailable right now."


async def test_continuing_past_a_refusal_still_continues(server, bind_context, monkeypatch):
    """`on_error="continue"` means what it meant: an empty config back, nothing raised."""
    from callva.livekit import config as callva_config

    base, _ = server
    monkeypatch.setenv("CONFIG_URL", f"{base}/refuse")
    ctx = bind_context(FakeContext())

    config = await callva_config.load(on_error="continue")

    assert config.empty
    assert config.source == "none"
    assert ctx.shutdown_reason is None
