from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..core import env, transport
from ..core import state as _state
from ..core.log import logger
from .models import CallConfig

OnError = Literal["terminate", "continue"]


class ConfigError(RuntimeError):
    """Configuration for this call could not be resolved."""


def as_path(endpoint: str) -> Path | None:
    """A local file, or ``None`` if this names a remote endpoint.

    ``file:///etc/agent.json`` and a bare ``./agent.json`` both mean the same thing. A
    file answers instantly and needs no participant, which makes it the shortest possible
    development loop; it cannot answer per caller, which is why it is not the production
    channel.
    """
    if endpoint.startswith("file://"):
        return Path(url2pathname(urlparse(endpoint).path))
    if "://" in endpoint:
        return None
    return Path(endpoint).expanduser()


async def _read_file(path: Path) -> Any:
    text = await asyncio.get_running_loop().run_in_executor(None, path.read_text)
    return json.loads(text)


async def load(
    *,
    url: str | None = None,
    api_key: str | None = None,
    direction: str | None = None,
    on_error: OnError = "terminate",
    timeout: float | None = None,
) -> CallConfig:
    """Resolve the configuration for this call.

    Resolution order:

    1. a body inside ``ctx.job.metadata`` — returns immediately, before anyone has joined
    2. a pointer inside ``ctx.job.metadata`` — followed
    3. ``CALLVA_CONFIG_URL`` (or the ``url`` argument) — followed

    A pointer is either an endpoint, asked with the call's own context as the request body
    so the responder can answer "who called which number", or a local file — ``file://…``
    or a plain path — read as it is.

    Only the endpoint path needs the SIP envelope to build its request, so only it waits
    for the participant to join. A body in metadata and a file both answer immediately.

    When configuration cannot be resolved the call is terminated and the reason logged: an
    agent without its prompt is a broken call either way, and failing quietly hides it.
    Pass ``on_error="continue"`` to receive an empty config instead.
    """
    st = _state.state()

    if st.config is not None:
        return st.config

    envelope = st.envelope

    if envelope.config is not None:
        return _store(st, CallConfig.parse(envelope.config, source="metadata"))

    endpoint = envelope.config_url or url or env.get("CONFIG_URL")
    if not endpoint:
        logger.debug("no configuration source: job metadata carries none and no URL is set")
        return _store(st, CallConfig(source="none"))

    path = as_path(endpoint)
    if path is not None:
        try:
            body = await _read_file(path)
        except (OSError, ValueError) as exc:
            return _fail(st, on_error, f"could not read configuration from {path}: {exc}")

        config = CallConfig.parse(body, source="file")
        if config.empty:
            return _fail(st, on_error, f"configuration file {path} carried nothing usable")

        logger.debug("resolved configuration from %s", path)
        return _store(st, config)

    try:
        participant = await st.ctx.wait_for_participant()
    except Exception as exc:
        return _fail(st, on_error, f"no participant joined, cannot request configuration: {exc}")

    identity = _state.ensure_identity(st, participant=participant, direction=direction)

    try:
        body = await transport.fetch_json(
            endpoint,
            payload=request_payload(st, participant),
            api_key=api_key or env.get("CONFIG_API_KEY"),
            timeout=timeout,
        )
    except transport.FetchError as exc:
        return _fail(st, on_error, f"configuration request to {endpoint} failed: {exc}")

    config = CallConfig.parse(body, source="url")
    if config.empty:
        return _fail(st, on_error, f"configuration response from {endpoint} carried nothing usable")

    logger.debug("resolved configuration for call %s from %s", identity.id, endpoint)
    return _store(st, config)


def request_payload(st: _state.CallState, participant: Any) -> dict[str, Any]:
    """The body of a configuration request.

    The request is the question: it carries who is calling, which number they reached and
    how the call arrived, so the responder can decide what to send back.
    """
    identity = _state.ensure_identity(st, participant=participant)
    job = st.ctx.job

    return {
        "room": getattr(job.room, "name", None),
        "job_id": job.id,
        "dispatch_id": getattr(job, "dispatch_id", None) or None,
        "agent_name": getattr(job, "agent_name", None) or None,
        "direction": identity.direction,
        "from": identity.from_party.to_dict(),
        "to": identity.to_party.to_dict(),
        "sip": identity.sip,
        "participant_identity": getattr(participant, "identity", None),
        "metadata": st.envelope.raw,
    }


def _store(st: _state.CallState, config: CallConfig) -> CallConfig:
    st.config = config
    if config.webhook is not None:
        st.webhook = config.webhook
    return config


def _fail(st: _state.CallState, on_error: OnError, reason: str) -> CallConfig:
    if on_error == "continue":
        logger.error("%s; continuing without configuration", reason)
        return _store(st, CallConfig(source="none"))

    logger.error("%s; terminating the call", reason)
    try:
        st.ctx.shutdown(reason="callva: configuration unavailable")
    except Exception:
        logger.debug("could not request shutdown", exc_info=True)
    raise ConfigError(reason)
