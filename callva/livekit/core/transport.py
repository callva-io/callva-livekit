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
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from . import env
from .log import MAX_QUOTED, delivery, logger

RETRY_DELAYS = (1.0, 4.0, 16.0)
"""Backoff between delivery attempts. Four attempts in total."""

CONFIG_RETRY_DELAYS = (0.5, 2.0)
"""Backoff while fetching configuration. Short: a live call is waiting on it."""

DEFAULT_TIMEOUT = 30.0
DEFAULT_CONFIG_TIMEOUT = 10.0


class FetchError(RuntimeError):
    """A request for configuration could not be completed.

    The message is this package's own account of the failure — ``HTTP 502``, the name of
    the client error that stopped the attempt, or how much unreadable body a good status
    came back with — and never a line of what the responder wrote. It is formatted into a
    log record at ERROR, which is the level
    :mod:`callva.livekit.webhook.errors` collects and delivers inside ``call.ended``, and
    the endpoint that answers a configuration request and the endpoint that receives a
    report belong to two different parties as often as to one.

    What the responder wrote is kept whole on :attr:`body`, for code still inside this
    process, and written to this machine's log by whoever read it.
    """

    def __init__(
        self,
        summary: str,
        *,
        status: int | None = None,
        body: str | None = None,
    ) -> None:
        self.status = status
        """The status the responder answered with, or ``None`` when no response arrived."""
        self.body = body
        """What the responder wrote, in full. In-process only: it is not in the message."""
        super().__init__(summary)


class ConfigError(RuntimeError):
    """Configuration for this call could not be resolved.

    Defined here rather than beside :func:`~callva.livekit.config.load` because the
    refusal below is one of these and is raised from this module. The public name is
    ``callva.livekit.config.ConfigError``, which is where a caller meets it.
    """


TERMINATE = "terminate"
"""The one action this package acts on. Anything else is left to whoever reads it."""


class ConfigRefused(ConfigError):
    """A configuration endpoint answered, and the answer was no.

    Not a failure to reach it. The request completed and the responder decided this call
    must not go ahead — the number is disabled, the balance is spent, too many calls are
    already up. Which of those it is belongs to the responder's own vocabulary: it travels
    here as an opaque ``reason_code`` and is never interpreted, exactly as ``environment``
    is passed through untouched.

    ``caller_message`` is what the responder composed for whoever is on the phone. It is
    the reason this is an object and not a log line: a message that is compiled,
    transmitted and then discarded was never worth sending.

    Every field here is text the other side wrote, kept whole on the object for the caller
    that has to act on it. The *message* is the part that travels: it is logged at ERROR
    by whoever handles the refusal and therefore delivered inside ``call.ended``, so the
    text it quotes is cut at :data:`~callva.livekit.core.log.MAX_QUOTED`. A responder that
    sends a megabyte does not get to put a megabyte in somebody else's records.

    A :class:`ConfigError`, because an agent that was refused has no configuration either
    and a caller that already handles that handles this. What it is *not* is a
    :class:`FetchError`: a responder that could not be asked and a responder that was
    asked and said no are two situations, and only the second one has anything to say.
    Code that wants the difference catches this ahead of :class:`ConfigError`.
    """

    def __init__(
        self,
        *,
        status: int,
        action: str,
        reason_code: str | None = None,
        caller_message: str | None = None,
        error: str | None = None,
    ) -> None:
        self.status = status
        """The HTTP status the refusal arrived with. A refusal is an answer at any of them."""
        self.action = action
        """What the responder asked for. ``terminate`` is the one this package acts on."""
        self.reason_code = reason_code
        """The responder's machine code for why. Opaque here, and never matched on."""
        self.caller_message = caller_message
        """What to say to whoever is on the phone, in the responder's words, if anything."""
        self.error = error
        """The responder's own description of the refusal, for a log and for a human."""
        said = error or caller_message or reason_code or action
        super().__init__(f"HTTP {status}: {said[:MAX_QUOTED]}")

    @classmethod
    def parse(cls, body: Any, *, status: int) -> ConfigRefused | None:
        """Read a refusal out of a response body, or ``None`` if this is not one.

        The shape is flat and top-level — ``{error, action, reason_code, caller_message}``
        — with nothing wrapped around it. A wrapper key would be one vendor's envelope,
        and this package knows none.

        Only ``action`` decides. A body that does not ask for the call to end is not a
        refusal however it is spelled, and a configuration response has no top-level
        ``action`` at all.

        What this is, exactly: a shape test on data the other side wrote. It is satisfied
        by any JSON object with a top-level ``action`` that strips and lowercases to
        ``terminate``, at any status including a 2xx, and nothing about the responder is
        authenticated by getting here. So it selects a channel and it does not vouch for
        what comes down it — a block page that happens to be JSON with that key is a
        refusal to this parser, and a 200 saying ``terminate`` carries whatever text it
        likes. Which is why the text it carries is bounded where it is quoted rather than
        trusted for having arrived.
        """
        if not isinstance(body, dict):
            return None

        action = _text(body.get("action"))
        if action is None or action.lower() != TERMINATE:
            return None

        return cls(
            status=status,
            action=action.lower(),
            reason_code=_text(body.get("reason_code")),
            caller_message=_text(body.get("caller_message")),
            error=_text(body.get("error")),
        )


def _text(value: Any) -> str | None:
    """A non-empty string, or nothing at all."""
    return value.strip() or None if isinstance(value, str) else None


_UNNAMED = "the configuration endpoint"
"""What an endpoint is called when its URL cannot be reduced to a name safely."""


def endpoint_name(url: str) -> str:
    """The endpoint as a name: scheme, host, port and path, and nothing else.

    Which endpoint was asked is the part of a URL that makes a failure actionable, and
    those four pieces say it. Userinfo, query and fragment are dropped, because that is
    where a key rides — ``?token=…``, ``https://id:secret@host`` — and this string is
    written into records that leave the machine.

    The path is kept whole, because without it a deployment serving several endpoints from
    one host cannot tell which of them failed. HTTP puts credentials in userinfo, in the
    query and in a header, and those are the three places this drops; a deployment that
    instead puts a secret in the path — ``/config/<token>`` — is putting it into records
    that leave here, and should name its endpoints so that it does not.

    A URL this cannot read is named rather than echoed, for the same reason. It degrades
    on anything at all rather than on a chosen list: both callers work this out before the
    request they are about to describe, so raising here would take down the call instead
    of reporting why it could not be configured.
    """
    try:
        parts = urlsplit(url)
        host, port, scheme, path = parts.hostname, parts.port, parts.scheme, parts.path
        if not scheme or not host:
            return _UNNAMED
        # hostname strips the brackets an IPv6 literal arrives in, and without them back
        # the name reads as a different host on a different port and parses as neither.
        netloc = f"[{host}]" if ":" in host else host
        return urlunsplit((scheme, f"{netloc}:{port}" if port else netloc, path, "", ""))
    except Exception:
        return _UNNAMED


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
                        delivery.debug("delivered %s to %s", event, target.url)
                        return True

                    text = (await response.text())[:500]
                    if response.status < 500:
                        delivery.error(
                            "%s rejected with HTTP %s, not retrying: %s",
                            event,
                            response.status,
                            text,
                        )
                        return False

                    delivery.warning("%s failed with HTTP %s: %s", event, response.status, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delivery.warning("%s delivery attempt %s failed: %s", event, attempt + 1, exc)

            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])

    delivery.error("%s could not be delivered to %s after %s attempts", event, target.url, attempts)
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

    The body is read on every status, not only on a good one. A responder that refuses a
    call says why in it, and that reason is the one thing worth having: reducing it to
    ``HTTP 402: …`` in a log line throws away a message the responder composed for the
    person on the phone. So a body asking for the call to end raises
    :class:`ConfigRefused` and is never retried — whatever status it came with. A refusal
    spelled as a 5xx is still an answer, and sitting through the backoff only hammers an
    endpoint that is already struggling on a call that was never going to proceed.

    An error body that asks for nothing is read the other way round. It was composed for
    nobody: it is whatever the responder's framework prints when something breaks, and this
    machine's log is where it belongs. The failure raised from here carries the status and
    this package's own words — see :class:`FetchError`.

    A good status carrying something that is not JSON is read the same way, and is the
    commonest way this fails in practice: a session expired and a proxy answered 200 with
    a login page. It is logged here in full like any other body and reported as the status,
    the number of bytes and where the decoder stopped.
    """
    body = json.dumps(payload, ensure_ascii=False, default=str)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resolved = timeout or env.get_float("CONFIG_TIMEOUT", DEFAULT_CONFIG_TIMEOUT)
    client_timeout = aiohttp.ClientTimeout(total=resolved or DEFAULT_CONFIG_TIMEOUT)
    attempts = len(CONFIG_RETRY_DELAYS) + 1
    named = endpoint_name(url)
    last = "no attempt was made"
    last_status: int | None = None
    last_body: str | None = None

    async with _client() as session:
        for attempt in range(attempts):
            try:
                async with session.post(
                    url, data=body.encode("utf-8"), headers=headers, timeout=client_timeout
                ) as response:
                    text = await response.text()
                    try:
                        decoded: Any = json.loads(text) if text.strip() else None
                        malformed: ValueError | None = None
                    except ValueError as exc:
                        decoded, malformed = None, exc

                    refusal = ConfigRefused.parse(decoded, status=response.status)
                    if refusal is not None:
                        # Raised, not logged: it carries everything a log line would say,
                        # and whoever handles it is who decides what the call does next.
                        raise refusal

                    if response.status < 300 and malformed is None:
                        return decoded

                    # Whatever came back goes into this line and no other, whole. It runs
                    # on the package logger at WARNING, below the ERROR level the error
                    # collector reads, so it reaches this machine's log and stops there —
                    # and this machine's log is the one place a post-mortem can read what
                    # the endpoint actually said, so it is not the place to abbreviate it.
                    logger.warning(
                        "config request to %s answered HTTP %s: %s", named, response.status, text
                    )

                    if response.status < 300:
                        # A good status carrying something that is not JSON: an SSO login
                        # page, a proxy's interstitial, a CDN block. The count of bytes and
                        # where the decoder gave up separate those from a truncated answer,
                        # and neither is a line of what the page said.
                        raise FetchError(
                            f"HTTP {response.status} was not JSON: {len(text)} bytes, {malformed}",
                            status=response.status,
                            body=text,
                        ) from malformed

                    last, last_status, last_body = f"HTTP {response.status}", response.status, text
                    if response.status < 500:
                        raise FetchError(last, status=last_status, body=last_body)
            except (FetchError, ConfigRefused):
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The class, not the sentence: a timeout's own text is empty, and what a
                # client error says about the attempt varies by library and version while
                # the kind of failure — timed out, could not connect, disconnected — is
                # what a reader acts on. The sentence goes in the line below it.
                last, last_status, last_body = type(exc).__name__, None, None
                logger.warning(
                    "config request to %s, attempt %s, failed: %s: %s",
                    named,
                    attempt + 1,
                    last,
                    exc,
                )

            if attempt < len(CONFIG_RETRY_DELAYS):
                await asyncio.sleep(CONFIG_RETRY_DELAYS[attempt])

    raise FetchError(last, status=last_status, body=last_body)
