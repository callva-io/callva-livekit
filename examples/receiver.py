"""A webhook receiver for trying the package out locally.

    python examples/receiver.py

Listens on ``http://localhost:878/hook``, verifies the signature when
``CALLVA_WEBHOOK_SECRET`` is set, prints a summary of every event and writes the full
body — and any recording that arrives — into ``./received``.

It also answers ``/config`` with a small configuration, so pointing
``CALLVA_CONFIG_URL`` at it exercises that path too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from aiohttp import web

PORT = int(os.environ.get("PORT", "878"))
SECRET = os.environ.get("CALLVA_WEBHOOK_SECRET")
OUTPUT = Path("received")

CONFIG = {
    "prompt": "You are speaking with {{ name }}. Be brief and warm.",
    "greeting": "Hi {{ name }}, thanks for calling. How can I help?",
    "variables": {"name": "Anna"},
}


def _number(party: dict | None) -> str | None:
    return (party or {}).get("number")


def verify(body: bytes, headers) -> str:
    if not SECRET:
        return "unsigned"

    signature = headers.get("X-Callva-Signature", "")
    timestamp = headers.get("X-Callva-Timestamp", "")
    expected = hmac.new(
        SECRET.encode(), f"{timestamp}.{body.decode()}".encode(), hashlib.sha256
    ).hexdigest()

    if hmac.compare_digest(signature, f"sha256={expected}"):
        return "signature ok"
    return "BAD SIGNATURE"


def summarize(event: dict) -> str:
    call = event.get("call") or {}
    lines = [
        f"  call    {call.get('id')}  {call.get('direction')}  {call.get('status')}",
        f"  parties {_number(call.get('from'))} -> {_number(call.get('to'))}",
    ]

    report = (event.get("livekit") or {}).get("session_report")
    if report:
        history = (report.get("chat_history") or {}).get("items") or []
        lines.append(f"  report  {len(history)} chat items, usage: {bool(report.get('usage'))}")
    if call.get("duration") is not None:
        lines.append(f"  lasted  {call['duration']}s")
    if event.get("recording"):
        lines.append(f"  audio   {event['recording']}")

    return "\n".join(lines)


async def hook(request: web.Request) -> web.Response:
    OUTPUT.mkdir(exist_ok=True)

    if request.content_type.startswith("multipart/"):
        return await recording(request)

    body = await request.read()
    event = json.loads(body)
    name = event.get("event", "unknown")

    print(f"\n{name}  [{verify(body, request.headers)}]")
    print(summarize(event))

    (OUTPUT / f"{event.get('id', name)}.json").write_text(
        json.dumps(event, indent=2, ensure_ascii=False)
    )
    return web.json_response({"ok": True})


async def recording(request: web.Request) -> web.Response:
    reader = await request.multipart()
    saved = []

    async for part in reader:
        if part.name == "event":
            print(f"\n{json.loads(await part.text()).get('event')}  [multipart]")
        elif part.name == "file":
            path = OUTPUT / (part.filename or "recording.ogg")
            with path.open("wb") as handle:
                while chunk := await part.read_chunk():
                    handle.write(chunk)
            saved.append(f"{path} ({path.stat().st_size} bytes)")

    for line in saved:
        print(f"  saved   {line}")

    return web.json_response({"ok": True})


async def config(request: web.Request) -> web.Response:
    body = await request.json()
    print(f"\nconfig request for {body.get('direction')} call")
    print(f"  parties {_number(body.get('from'))} -> {_number(body.get('to'))}")
    return web.json_response(CONFIG)


def main() -> None:
    app = web.Application(client_max_size=512 * 1024 * 1024)
    app.add_routes([web.post("/hook", hook), web.post("/config", config)])

    print(f"listening on http://localhost:{PORT}")
    print(f"  webhooks -> http://localhost:{PORT}/hook")
    print(f"  config   -> http://localhost:{PORT}/config")
    print(f"  signature verification {'on' if SECRET else 'off'}")

    web.run_app(app, port=PORT, print=None)


if __name__ == "__main__":
    main()
