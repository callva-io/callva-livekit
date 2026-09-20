from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..core import env, transport
from ..core import state as _state
from ..core.log import MAX_QUOTED, logger
from ..core.transport import ConfigError, ConfigRefused
from .models import CallConfig

OnError = Literal["terminate", "continue"]


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
    3. ``CONFIG_URL`` (or the ``url`` argument) — followed

    A pointer is either an endpoint, asked with the call's own context as the request body
    so the responder can answer "who called which number", or a local file — ``file://…``
    or a plain path — read as it is.

    Only the endpoint path needs the SIP envelope to build its request, so only it waits
    for the participant to join. A body in metadata and a file both answer immediately.

    When configuration cannot be resolved the call is terminated and the reason logged: an
    agent without its prompt is a broken call either way, and failing quietly hides it.
    Pass ``on_error="continue"`` to receive an empty config instead.

    A responder may also answer no. That raises :class:`ConfigRefused`, which carries the
    status, an opaque code and whatever the responder wrote for the person on the phone:
    a refusal is an answer rather than a failure to get one, and flattening it into the
    unavailable-configuration path would throw the message away. It is a
    :class:`ConfigError` all the same — an agent that was refused is as short of a prompt
    as one that could not ask — so a caller handling configuration failing handles this
    too, and one that wants the specific case catches :class:`ConfigRefused` first.

    ``on_error="continue"`` covers it like anything else: a caller who asked to carry on
    without configuration carries on, and the refusal is logged.

    Nothing is torn down for a refusal. It may carry a message composed for the person on
    the phone, and hanging up here would discard the one thing it was sent to deliver. Say
    whatever it asks and then end the call — ``call.end()`` — which keeps every ending in
    this package the caller's decision.
    """
    st = _state.state()

    if st.config is not None:
        return st.config

    # A prompt may speak about where this call is being held. The room is the job's, not the
    # configuration's, so it is read here - the one place that holds both - and handed to
    # every parse below rather than looked up again inside any of them.
    room_name = getattr(getattr(st.ctx, "room", None), "name", None)

    envelope = st.envelope

    if envelope.config is not None:
        return _store(st, CallConfig.parse(envelope.config, source="metadata", room_name=room_name))

    endpoint = envelope.config_url or url or env.get("CONFIG_URL")
    if not endpoint:
        logger.debug("no configuration source: job metadata carries none and no URL is set")
        return _store(st, CallConfig(source="none"))

    path = as_path(endpoint)
    if path is not None:
        try:
            body = await _read_file(path)
        except (OSError, ValueError) as exc:
            reason = f"could not read configuration from {path}: {str(exc)[:MAX_QUOTED]}"
            return _fail(st, on_error, reason)

        config = CallConfig.parse(body, source="file", room_name=room_name)
        if config.empty:
            return _fail(st, on_error, f"configuration file {path} carried nothing usable")

        logger.debug("resolved configuration from %s", path)
        return _store(st, config)

    try:
        participant = await st.ctx.wait_for_participant()
    except Exception as exc:
        reason = f"no participant joined, cannot request configuration: {str(exc)[:MAX_QUOTED]}"
        return _fail(st, on_error, reason)

    identity = _state.ensure_identity(st, participant=participant, direction=direction)
    named = transport.endpoint_name(endpoint)

    try:
        body = await transport.fetch_json(
            endpoint,
            payload=request_payload(st, participant),
            api_key=api_key or env.get("CONFIG_API_KEY"),
            timeout=timeout,
        )
    except ConfigRefused as refusal:
        if on_error == "continue":
            # A caller who asked to carry on without configuration is as short of one
            # here as anywhere else, and said what to do about that.
            return _fail(st, on_error, f"{named} refused this call: {refusal}")
        # An answered "no" travels whole. Routing it through the unavailable-configuration
        # path would flatten it into a log line and lose the code and the message with it.
        logger.info("%s refused this call: %s", named, refusal)
        raise
    except transport.FetchError as exc:
        return _fail(st, on_error, f"configuration request to {named} failed: {exc}")

    config = CallConfig.parse(body, source="url", room_name=room_name)
    if config.empty:
        return _fail(st, on_error, f"configuration response from {named} carried nothing usable")

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
    if config.call_id and _state.adopt_call_id(st, config.call_id):
        logger.debug("this call is filed as %s by the configuration source", config.call_id)
    return config


def _fail(st: _state.CallState, on_error: OnError, reason: str) -> CallConfig:
    """Say why configuration is not there, and do what the caller asked about it.

    Both branches log at ERROR, and an ERROR record is what
    :mod:`callva.livekit.webhook.errors` collects and delivers inside ``call.ended``. So a
    reason handed here leaves the machine: it carries this package's own account of the
    failure — which endpoint by name, what status came back, which file could not be read
    — and never a line the configuration endpoint wrote for itself. The endpoint that
    serves a configuration and the endpoint that receives a report can be two parties, and
    one party's stack trace is not the other party's to keep. The whole body is in this
    container's log, which is where a post-mortem reads it.

    A refusal is the exception, and is one on purpose: a responder that answers
    ``action: terminate`` in the flat shape :class:`ConfigRefused` parses is opting into a
    channel for text it means to have passed on. What that gate is, though, is a shape
    test on data the other side wrote — a top-level ``action`` that lowercases to
    ``terminate``, at any status — and a WAF's JSON block page or a 200 with arbitrary
    prose in ``error`` satisfies it as readily as a platform does. So the refusal's text
    is bounded where it is quoted rather than trusted for having come that way; anything
    exceeding :data:`~callva.livekit.core.log.MAX_QUOTED` is cut when the message is built.

    Any other text this did not compose is cut there too, at the point it is copied in —
    a client error's sentence, an ``OSError``'s. What this package wrote itself is not,
    because there is nobody else's words in it to bound.
    """
    if on_error == "continue":
        logger.error("%s; continuing without configuration", reason)
        return _store(st, CallConfig(source="none"))

    logger.error("%s; terminating the call", reason)
    try:
        st.ctx.shutdown(reason="callva: configuration unavailable")
    except Exception:
        logger.debug("could not request shutdown", exc_info=True)
    raise ConfigError(reason)
