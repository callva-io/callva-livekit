from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..core import identity as _identity
from ..core import state as _state
from ..core.log import logger

ANSWER_TIMEOUT = 30.0
"""How long an outbound call may ring before it counts as unanswered."""

SIP_STATUS = "sip.callStatus"
ACTIVE = "active"
HANGUP = "hangup"

async def await_pickup(
    ctx: Any = None,
    *,
    timeout: float = ANSWER_TIMEOUT,
    direction: str | None = None,
) -> Any | None:
    """Wait until someone is actually on the call. ``None`` if nobody ever was.

    An inbound call is answered by the time a participant exists, and so is a browser
    session, so the participant joining is the whole of it. An outbound call is not: the
    participant appears while the phone is still ringing, and LiveKit says so in
    ``sip.callStatus``. Waiting for the participant alone reports a call as live while it
    is ringing, and reports one as live that was never picked up at all.

    Every listener is attached before the first await, and the participants already in the
    room are examined afterwards. Both halves are needed: a SIP trunk can answer and hang
    up inside a second, which slips past a plain wait, and a participant can already be
    there before this is ever called.

    Nothing is reported from here and nothing is torn down. The reason the call was not
    answered is left on the call's state, where the webhook module turns it into an
    outcome, and hanging up is the caller's decision to make — usually ``call.end()``.
    """
    st = _state.state(ctx)
    ctx = st.ctx
    room = getattr(ctx, "room", None)
    if room is None:
        return None

    outbound = _identity.resolve_direction(st.envelope, direction) == _identity.OUTBOUND

    done = asyncio.Event()
    answered: dict[str, Any] = {"participant": None, "reason": None}

    def succeed(participant: Any) -> None:
        if not done.is_set():
            answered["participant"] = participant
            done.set()

    def fail(reason: str) -> None:
        if not done.is_set():
            answered["reason"] = reason
            done.set()

    def examine(participant: Any) -> None:
        if not outbound:
            succeed(participant)
            return
        attributes = dict(getattr(participant, "attributes", None) or {})
        status = attributes.get(SIP_STATUS)
        if status == ACTIVE:
            succeed(participant)
        elif status == HANGUP:
            # The only terminal status LiveKit writes. Busy, declined and unavailable are
            # written as no attribute at all, which is why the disconnect below matters.
            fail(status)

    def on_connected(participant: Any) -> None:
        examine(participant)

    def on_attributes_changed(changed: dict, participant: Any) -> None:
        if not outbound or SIP_STATUS not in changed:
            return
        logger.debug("sip.callStatus -> %s (%s)", changed[SIP_STATUS], participant.identity)
        examine(participant)

    def on_disconnected(participant: Any) -> None:
        reason = _reason_name(getattr(participant, "disconnect_reason", None))
        st.extras["disconnect_reason"] = reason
        fail(reason or "disconnected")

    room.on("participant_connected", on_connected)
    room.on("participant_attributes_changed", on_attributes_changed)
    room.on("participant_disconnected", on_disconnected)

    try:
        for participant in list(getattr(room, "remote_participants", {}).values()):
            examine(participant)
            if done.is_set():
                break

        if not done.is_set():
            if outbound:
                logger.debug("outbound call placed, waiting for pickup")
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                answered["reason"] = "timeout"

        participant = answered["participant"]
        if participant is not None:
            logger.debug("the call was answered by %s", participant.identity)
            return participant

        reason = answered["reason"] or "timeout"
        st.unanswered_reason = reason
        logger.info("the call was not answered: %s", reason)
        return None
    finally:
        for event, handler in (
            ("participant_connected", on_connected),
            ("participant_attributes_changed", on_attributes_changed),
            ("participant_disconnected", on_disconnected),
        ):
            with contextlib.suppress(Exception):
                room.off(event, handler)


def _reason_name(reason: Any) -> str | None:
    if reason is None:
        return None
    return getattr(reason, "name", None) or str(reason)
