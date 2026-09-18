from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiohttp

from . import env
from .log import logger

RETRY_DELAYS = (1.0, 4.0, 16.0)
"""Backoff between delivery attempts. Four attempts in total."""

CONFIG_RETRY_DELAYS = (0.5, 2.0)
"""Backoff while fetching configuration. Short: a live call is waiting on it."""

DEFAULT_TIMEOUT = 30.0
DEFAULT_CONFIG_TIMEOUT = 10.0


class FetchError(RuntimeError):
    """A request for configuration could not be completed."""


@dataclass(frozen=True)
class WebhookTarget:
    """Where call events go, and what signs them."""

    url: str
    secret: str | None = None

    @classmethod
    def from_env(cls) -> WebhookTarget | None:
        url = env.get("WEBHOOK_URL")
        if not url:
            return None
        return cls(url=url, secret=env.get("WEBHOOK_SECRET"))

    @classmethod
    def from_dict(cls, source: Any) -> WebhookTarget | None:
        """Build a target from a ``{"url": ..., "secret": ...}`` mapping."""
        if not isinstance(source, dict):
            return None
        url = source.get("url")
        if not isinstance(url, str) or not url.strip():
            return None
        secret = source.get("secret")
        return cls(
            url=url.strip(),
            secret=secret.strip() if isinstance(secret, str) and secret.strip() else None,
        )


def _session() -> tuple[aiohttp.ClientSession, bool]:
    """The session to use, and whether we own it.

    Inside a job the SDK keeps a shared session, which must not be closed here. Outside
    one — a test, a script — we make our own, and closing it is then our responsibility.
    """
    try:
        from livekit.agents.utils import http_context

        return http_context.http_session(), False
    except Exception:
        return aiohttp.ClientSession(), True


@contextlib.asynccontextmanager
async def _client() -> AsyncIterator[aiohttp.ClientSession]:
    session, owned = _session()
    try:
        yield session
    finally:
        if owned:
            await session.close()


def _signature_headers(target: WebhookTarget, body: str) -> dict[str, str]:
    if not target.secret:
        return {}
    timestamp = str(int(time.time()))
    digest = hmac.new(
        target.secret.encode("utf-8"),
        f"{timestamp}.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {"X-Webhook-Signature": f"sha256={digest}", "X-Webhook-Timestamp": timestamp}


def idempotency_key(call_id: str, event: str) -> str:
    return f"{call_id}:{event}:{int(time.time() * 1000)}"


async def _deliver(
    target: WebhookTarget,
    *,
    event: str,
    headers: dict[str, str],
    build_body: Any,
    timeout: float,
) -> bool:
    """Attempt delivery, retrying on 5xx and network failures only.

    A 4xx means the receiver understood and refused; retrying it wastes the shutdown
    budget and delivers nothing, so it fails fast.
    """
    attempts = len(RETRY_DELAYS) + 1
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    async with _client() as session:
        for attempt in range(attempts):
            try:
                async with session.post(
                    target.url,
                    headers=headers,
                    timeout=client_timeout,
                    **build_body(),
                ) as response:
                    if response.status < 300:
                        logger.debug("delivered %s to %s", event, target.url)
                        return True

                    text = (await response.text())[:500]
                    if response.status < 500:
                        logger.error(
                            "%s rejected with HTTP %s, not retrying: %s",
                            event,
                            response.status,
                            text,
                        )
                        return False

                    logger.warning("%s failed with HTTP %s: %s", event, response.status, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s delivery attempt %s failed: %s", event, attempt + 1, exc)

            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])

    logger.error("%s could not be delivered to %s after %s attempts", event, target.url, attempts)
    return False


async def post_json(
    target: WebhookTarget,
    *,
    event: str,
    payload: dict[str, Any],
    key: str,
    timeout: float | None = None,
) -> bool:
    """Deliver one event as JSON."""
    body = json.dumps(payload, ensure_ascii=False, default=str)
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Event": event,
        "X-Webhook-Idempotency-Key": key,
        **_signature_headers(target, body),
    }

    return await _deliver(
        target,
        event=event,
        headers=headers,
        build_body=lambda: {"data": body.encode("utf-8")},
        timeout=timeout or env.get_float("WEBHOOK_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT,
    )


async def fetch_json(
    url: str,
    *,
    payload: dict[str, Any],
    api_key: str | None = None,
    timeout: float | None = None,
) -> Any:
    """POST ``payload`` and return the decoded JSON response.

    Raises :class:`FetchError` when every attempt fails or the response is not usable.
    Retries are deliberately short and few because a call is ringing while this runs.
    """
    body = json.dumps(payload, ensure_ascii=False, default=str)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resolved = timeout or env.get_float("CONFIG_TIMEOUT", DEFAULT_CONFIG_TIMEOUT)
    client_timeout = aiohttp.ClientTimeout(total=resolved or DEFAULT_CONFIG_TIMEOUT)
    attempts = len(CONFIG_RETRY_DELAYS) + 1
    last = "no attempt was made"

    async with _client() as session:
        for attempt in range(attempts):
            try:
                async with session.post(
                    url, data=body.encode("utf-8"), headers=headers, timeout=client_timeout
                ) as response:
                    text = await response.text()
                    if response.status < 300:
                        try:
                            return json.loads(text) if text.strip() else None
                        except ValueError as exc:
                            raise FetchError(f"response was not JSON: {exc}") from exc

                    last = f"HTTP {response.status}: {text[:500]}"
                    if response.status < 500:
                        raise FetchError(last)
                    logger.warning("config request failed, %s", last)
            except FetchError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = str(exc)
                logger.warning("config request attempt %s failed: %s", attempt + 1, exc)

            if attempt < len(CONFIG_RETRY_DELAYS):
                await asyncio.sleep(CONFIG_RETRY_DELAYS[attempt])

    raise FetchError(last)
