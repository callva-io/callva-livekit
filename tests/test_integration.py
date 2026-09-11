"""End to end over real HTTP: a real client, a real server, a real signature."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import aiohttp
import pytest
from aiohttp import web

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

    async def upload(request: web.Request) -> web.Response:
        reader = await request.multipart()
        parts: dict[str, Any] = {}
        async for part in reader:
            parts[part.name or "?"] = (
                json.loads(await part.text()) if part.name == "event" else await part.read()
            )
        received.append({"multipart": parts})
        return web.json_response({"ok": True})

    async def config(request: web.Request) -> web.Response:
        received.append({"config_request": await request.json()})
        return web.json_response({"prompt": "hello {{ name }}", "variables": {"name": "Anna"}})

    app = web.Application()
    app.add_routes(
        [web.post("/hook", hook), web.post("/upload", upload), web.post("/config", config)]
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


async def test_a_file_arrives_next_to_its_event(server, tmp_path):
    base, received = server
    audio = tmp_path / "call.ogg"
    audio.write_bytes(b"OggS-not-really")

    sent = await transport.post_file(
        WebhookTarget(f"{base}/upload", SECRET),
        event="call.recording",
        payload={"event": "call.recording"},
        key="c1:call.recording:1",
        path=audio,
        filename="c1.ogg",
        content_type="audio/ogg",
    )

    assert sent
    assert received[0]["multipart"]["event"]["event"] == "call.recording"
    assert received[0]["multipart"]["file"] == b"OggS-not-really"


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
