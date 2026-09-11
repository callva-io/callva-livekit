from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from ..core import state as _state
from ..core import transport
from ..core.log import logger
from ..core.transport import WebhookTarget
from . import payload as _payload
from .storage import Storage

_ATTACHED = "webhook.attached"
_PARTICIPANT = "webhook.participant"
_TARGET_OVERRIDE = "webhook.target_override"
_STARTED_TASK = "webhook.started_task"
_PICKUP_ARMED = "webhook.pickup_armed"
_DIALING_SENT = "webhook.dialing_sent"

SIP_STATUS = "sip.callStatus"
SIP_ACTIVE = "active"

# Our own vocabulary, mapped from what LiveKit reports. It is deliberately not the
# carrier's and not a copy of anyone's wire format: a consumer of this package should not
# have to change when a trunk moves from one operator to another.
#
# The resolution is honest rather than aspirational. LiveKit's `sip.callStatus` carries no
# terminal outcome at all — busy, declined and unavailable are written as no attribute,
# leaving it frozen at `ringing` — so the outcome comes from the participant's disconnect
# reason, which cannot tell busy from declined. Claiming that distinction would be
# inventing it.
_OUTCOMES = {
    "USER_UNAVAILABLE": "no_answer",
    "CONNECTION_TIMEOUT": "no_answer",
    "USER_REJECTED": "rejected",
    "CLIENT_INITIATED": "canceled",
    "SIP_TRUNK_FAILURE": "failed",
    "MEDIA_FAILURE": "failed",
    "AGENT_ERROR": "failed",
}
COMPLETED = "completed"
UNANSWERED_DEFAULT = "no_answer"

FALLBACK_WARNING = (
    "sending the call.ended webhook from a shutdown callback, which the worker bounds by "
    "shutdown_process_timeout (10s by default) before killing the process. Pass "
    "on_session_end=callva_webhook.on_session_end to AgentServer.rtc_session for a 300s "
    "budget, or raise shutdown_process_timeout."
)


def attach(
    session: Any = None,
    *,
    direction: str | None = None,
    target: WebhookTarget | None = None,
) -> None:
    """Arm call webhooks for this job.

    Takes no :class:`JobContext`: it is read from the SDK's own contextvar. Nothing is
    patched and nothing about the session is changed — the session is only held so that
    its report can be built when the call ends.

    Safe to call before or after ``ctx.connect()``. Calling it twice does nothing the
    second time.
    """
    st = _state.state()

    if st.extras.get(_ATTACHED):
        logger.debug("webhooks are already attached to this job")
        return
    st.extras[_ATTACHED] = True

    st.session = session
    if target is not None:
        st.extras[_TARGET_OVERRIDE] = target
    if direction is not None:
        st.extras["direction"] = direction

    ctx = st.ctx
    ctx.add_participant_entrypoint(_on_participant)
    _watch_disconnect(ctx, st)

    # The context is captured here rather than read from the SDK's contextvar at shutdown:
    # a callback runs in its own task, and nothing guarantees the ambient job is still set
    # by then. A plain closure, never a partial — the SDK inspects `__code__` to decide
    # whether to pass it the shutdown reason.
    async def _shutdown(reason: str = "") -> None:
        await _on_shutdown(ctx, reason)

    ctx.add_shutdown_callback(_shutdown)

    if _nobody_will_join(ctx):
        # A simulated job — console mode — has a mock room that no one ever joins, so the
        # participant entrypoint would never fire and the call would only ever report its
        # end. The session starting is the closest thing to the call going live.
        st.extras[_STARTED_TASK] = asyncio.create_task(_on_participant(ctx, None))
    elif (present := _already_present(ctx)) is not None:
        # An inbound call's participant is in the room before the job even starts, and the
        # SDK replays them to participant entrypoints inside ctx.connect() — once, before
        # this function could have registered anything. Waiting on the entrypoint would be
        # waiting for a join that already happened.
        st.extras[_STARTED_TASK] = asyncio.create_task(_on_participant(ctx, present))

    logger.debug("call webhooks attached")


def _nobody_will_join(ctx: Any) -> bool:
    try:
        return bool(ctx.is_fake_job())
    except Exception:
        return False


async def _report_dialing(ctx: Any, st: _state.CallState, participant: Any) -> None:
    """Say that the dial went out, once, however many rings follow.

    LiveKit moves through `dialing` and then `ringing`, and on some carriers only one of
    them ever appears. Two near-identical events seconds apart are noise to a consumer
    registering a call, so this is one event and the exact status travels in
    `livekit.sip.callStatus`.
    """
    if st.extras.get(_DIALING_SENT):
        return
    st.extras[_DIALING_SENT] = True

    _state.ensure_identity(st, participant=participant, direction=st.extras.get("direction"))

    target = resolve_target(st)
    if target is None:
        return

    key = transport.idempotency_key(st.identity.id, _payload.DIALING)
    body = _payload.build(
        st,
        event=_payload.DIALING,
        key=key,
        status="dialing",
        participant=participant,
    )
    await transport.post_json(target, event=_payload.DIALING, payload=body, key=key)


def _outcome(st: _state.CallState) -> str:
    """How the call ended, in our words.

    A call that was answered completed, whatever happened afterwards. One that never was
    is described by why the other end went away.
    """
    if st.started_sent:
        return COMPLETED
    reason = st.extras.get("disconnect_reason")
    return _OUTCOMES.get(reason, UNANSWERED_DEFAULT)


def _watch_disconnect(ctx: Any, st: _state.CallState) -> None:
    """Remember why the other end went away; it is the only outcome signal we get."""
    room = getattr(ctx, "room", None)
    if room is None or not hasattr(room, "on"):
        return

    def on_disconnected(participant: Any) -> None:
        st.extras["disconnect_reason"] = _reason_name(
            getattr(participant, "disconnect_reason", None)
        )

    room.on("participant_disconnected", on_disconnected)


def _reason_name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        from livekit.protocol import models

        return str(models.DisconnectReason.Name(value))
    except Exception:
        return None


def _arm_pickup(ctx: Any, st: _state.CallState) -> None:
    """Wait for the ringing to be answered, once per job."""
    if st.extras.get(_PICKUP_ARMED):
        return
    st.extras[_PICKUP_ARMED] = True

    room = getattr(ctx, "room", None)
    if room is None or not hasattr(room, "on"):
        return

    def on_attributes_changed(changed: dict, participant: Any) -> None:
        if changed.get(SIP_STATUS) != SIP_ACTIVE or st.started_sent:
            return
        st.extras[_STARTED_TASK] = asyncio.create_task(_on_participant(ctx, participant))

    room.on("participant_attributes_changed", on_attributes_changed)


def _already_present(ctx: Any) -> Any | None:
    """The remote party, if they joined before the webhooks were armed."""
    try:
        from livekit.agents.job import DEFAULT_PARTICIPANT_KINDS as kinds
    except Exception:
        kinds = None

    participants = getattr(getattr(ctx, "room", None), "remote_participants", None)
    if not participants:
        return None

    for participant in participants.values():
        if kinds is None or getattr(participant, "kind", None) in kinds:
            return participant
    return None


def resolve_target(st: _state.CallState) -> WebhookTarget | None:
    """Where this call's events go.

    An explicit argument wins, then the configuration response — that is what makes one
    worker serve many tenants — then what the dispatcher declared, then the environment,
    which is the deployment default.
    """
    override = st.extras.get(_TARGET_OVERRIDE)
    if isinstance(override, WebhookTarget):
        return override

    if st.webhook is not None:
        return st.webhook

    from_envelope = WebhookTarget.from_dict(st.envelope.webhook)
    if from_envelope is not None:
        st.webhook = from_envelope
        return from_envelope

    from_env = WebhookTarget.from_env()
    if from_env is not None:
        st.webhook = from_env
    return from_env


def _ringing(participant: Any) -> bool:
    """True while a SIP participant exists but has not picked up.

    An outbound call's participant materialises as soon as the phone starts ringing, and
    reporting that as the call starting would tell the consumer somebody answered when
    nobody has. Inbound is answered by the time the participant appears, so the same check
    passes it straight through without having to know the direction.
    """
    attributes = getattr(participant, "attributes", None) or {}
    status = attributes.get(SIP_STATUS)
    return bool(status) and status != SIP_ACTIVE


async def _on_participant(ctx: Any, participant: Any = None) -> None:
    """The call is live.

    Normally because a participant joined. On a simulated job there is no one to join, so
    it is the session starting instead, and the call then has no parties.
    """
    st = _state.state(ctx)

    if st.started_sent:
        return

    if participant is not None and _ringing(participant):
        logger.debug(
            "%s is still ringing, holding the start",
            getattr(participant, "identity", "?"),
        )
        _arm_pickup(ctx, st)
        await _report_dialing(ctx, st, participant)
        return

    st.started_sent = True
    st.started_at = time.time()
    st.extras[_PARTICIPANT] = participant

    _state.ensure_identity(st, participant=participant, direction=st.extras.get("direction"))

    target = resolve_target(st)
    if target is None:
        logger.debug("no webhook target configured, not sending %s", _payload.STARTED)
        return

    key = transport.idempotency_key(st.identity.id, _payload.STARTED)
    body = _payload.build(
        st,
        event=_payload.STARTED,
        key=key,
        status="in_progress",
        participant=participant,
    )
    await transport.post_json(target, event=_payload.STARTED, payload=body, key=key)


async def on_session_end(ctx: Any = None) -> None:
    """Report the finished call and store its recording.

    Pass this to ``AgentServer.rtc_session(on_session_end=...)``. It runs after the
    session has closed and its recording has been finalized, but before the room is
    disconnected, with its own ``session_end_timeout`` budget of 300 seconds.
    """
    st = _state.state(ctx)

    if st.ended_sent:
        return
    st.ended_sent = True
    st.ended_at = time.time()

    # A short simulated session can finish while the start is still in flight; a consumer
    # should never see a call end before it began.
    started = st.extras.get(_STARTED_TASK)
    if started is not None and not started.done():
        with contextlib.suppress(Exception):
            await started

    participant = st.extras.get(_PARTICIPANT)
    # Before anything reads it: the recording is keyed on the call id, and a call that
    # never reported its start has no identity yet.
    _state.ensure_identity(st, participant=participant, direction=st.extras.get("direction"))

    report, report_dict = _session_report(st)

    storage = Storage.from_env()
    recording_path = _recording_path(report)
    target = resolve_target(st)

    recording, upload = _plan_recording(st, storage, recording_path, target)

    body = _payload.build(
        st,
        event=_payload.ENDED,
        key=transport.idempotency_key(st.identity.id, _payload.ENDED)
        if st.identity
        else _payload.ENDED,
        status=_outcome(st),
        participant=participant,
        session_report=report_dict,
        recording=recording,
    )

    # The webhook goes first: the call is closed out with a terminal status even if the
    # process does not survive the upload that follows.
    if target is not None:
        await transport.post_json(
            target, event=_payload.ENDED, payload=body, key=body["id"]
        )
    else:
        logger.debug("no webhook target configured, not sending %s", _payload.ENDED)

    if upload is not None:
        await upload(body, report_dict)


async def _on_shutdown(ctx: Any, _reason: str = "") -> None:
    """Fallback for agents that cannot reach ``on_session_end``."""
    st = _state.state(ctx)
    if st.ended_sent:
        return

    logger.warning(FALLBACK_WARNING)
    await on_session_end(ctx)


def _session_report(st: _state.CallState) -> tuple[Any, dict[str, Any] | None]:
    try:
        report = st.ctx.make_session_report(st.session)
    except Exception as exc:
        logger.warning("could not build the session report: %s", exc)
        return None, None

    try:
        return report, report.to_dict()
    except Exception:
        logger.warning("could not serialize the session report", exc_info=True)
        return report, None


def _recording_path(report: Any) -> Path | None:
    path = getattr(report, "audio_recording_path", None) if report else None
    if path is None:
        return None
    path = Path(path)
    return path if path.exists() else None


def _plan_recording(
    st: _state.CallState,
    storage: Storage | None,
    path: Path | None,
    target: WebhookTarget | None,
) -> tuple[dict[str, Any] | None, Any]:
    """Decide how the recording travels, and describe it for the webhook body.

    Object storage is the default because a long call does not belong in one request. The
    key is deterministic, so the URL is known before the bytes move and the webhook can
    carry it.
    """
    call_id = st.identity.id if st.identity else "unknown"

    if storage is not None:
        audio_key = storage.key(f"{call_id}.ogg")
        transcript_key = storage.key(f"{call_id}.json")
        described = {
            "url": storage.public_url(audio_key),
            "bucket": storage.bucket,
            "audio_key": audio_key if path else None,
            "transcript_key": transcript_key,
        }

        async def upload(_body: dict[str, Any], report_dict: dict[str, Any] | None) -> None:
            if path is not None:
                await storage.put_file(audio_key, path, "audio/ogg")
            if report_dict is not None:
                await storage.put_json(transcript_key, report_dict)

        return described, upload

    if path is not None and target is not None:

        async def upload(body: dict[str, Any], _report: dict[str, Any] | None) -> None:
            await transport.post_file(
                target,
                event=_payload.RECORDING,
                payload={**body, "event": _payload.RECORDING},
                key=transport.idempotency_key(call_id, _payload.RECORDING),
                path=path,
                filename=f"{call_id}.ogg",
                content_type="audio/ogg",
            )

        return {"delivery": "multipart", "filename": f"{call_id}.ogg"}, upload

    return None, None
