from __future__ import annotations

import time
from typing import Any

from ..core import state as _state
from ..core.log import logger

DIALING = "call.dialing"
STARTED = "call.started"
ENDED = "call.ended"
RECORDING = "call.recording"


def _job_dict(job: Any) -> dict[str, Any] | None:
    try:
        from google.protobuf.json_format import MessageToDict

        return MessageToDict(job, preserving_proto_field_name=True)
    except Exception:
        logger.debug("could not serialize the job", exc_info=True)
        return None


def _room_dict(room: Any, job: Any) -> dict[str, Any] | None:
    """The live room, plus the sid from the job.

    ``rtc.Room.sid`` is an async property: reading it produces a coroutine that a
    synchronous builder can only leave un-awaited. The job's copy of the room carries the
    same value as a plain string.
    """
    if room is None:
        return None
    return {
        "name": getattr(room, "name", None),
        "sid": getattr(getattr(job, "room", None), "sid", None) or None,
        "metadata": getattr(room, "metadata", None) or None,
    }


def _participant_dict(participant: Any) -> dict[str, Any] | None:
    if participant is None:
        return None
    attributes = getattr(participant, "attributes", None)
    return {
        "identity": getattr(participant, "identity", None),
        "name": getattr(participant, "name", None) or None,
        "kind": getattr(participant, "kind", None),
        "metadata": getattr(participant, "metadata", None) or None,
        "attributes": dict(attributes) if attributes else None,
    }


def _tags_dict(ctx: Any) -> dict[str, Any] | None:
    tagger = getattr(ctx, "tagger", None)
    if tagger is None:
        return None
    try:
        tags = sorted(tagger.tags)
        outcome = tagger.outcome
        reason = tagger.outcome_reason
    except Exception:
        logger.debug("could not read session tags", exc_info=True)
        return None

    if not tags and not outcome:
        return None
    return {"tags": tags, "outcome": outcome, "reason": reason}


def build(
    st: _state.CallState,
    *,
    event: str,
    key: str,
    status: str,
    participant: Any = None,
    session_report: dict[str, Any] | None = None,
    recording: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble one event.

    The envelope is thin on purpose. Everything LiveKit produces is nested verbatim under
    ``livekit``, so a field the SDK adds tomorrow reaches the consumer without a release
    here. The only reshaping is the ``call`` block, which carries the three things LiveKit
    does not model: a stable call id, a direction, and a ``from`` and a ``to``.
    """
    identity = _state.ensure_identity(st, participant=participant)
    ctx = st.ctx

    call: dict[str, Any] = {
        **identity.to_dict(),
        "started_at": st.started_at or identity.started_at or None,
        "ended_at": st.ended_at or None,
        "duration": round(st.ended_at - st.started_at, 3)
        if st.ended_at and st.started_at
        else None,
        "status": status,
        # Only a call nobody was ever on has one: why it never began.
        "reason": st.unanswered_reason,
    }

    job = getattr(ctx, "job", None)
    livekit: dict[str, Any] = {
        "room": _room_dict(getattr(ctx, "room", None), job),
        "job": _job_dict(job),
        "participant": _participant_dict(participant),
        "sip": identity.sip,
    }
    if (reason := st.extras.get("disconnect_reason")) is not None:
        livekit["disconnect_reason"] = reason

    if event == ENDED:
        # Always present on a finished call, null when the report could not be built, so
        # that a consumer never has to handle two shapes of the same event.
        livekit["session_report"] = session_report

    return {
        "event": event,
        "id": key,
        "timestamp": time.time(),
        "call": call,
        "livekit": livekit,
        "recording": recording,
        # Everything that went wrong during the call, in the words of whoever logged it.
        # Null when nothing did, and when nobody asked for them to be collected.
        "errors": errors,
        "tags": _tags_dict(ctx),
    }
