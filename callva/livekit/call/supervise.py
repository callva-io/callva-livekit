from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from ..core import state as _state
from ..core.log import logger
from .release import end

AWAY = "away"

JOB_SHUTDOWN = "job_shutdown"
"""The one close reason that means the job is already on its way down."""

_DURATION_TASK = "call.duration_task"
_AWAY_TASK = "call.away_task"
_CLOSED_TASK = "call.closed_task"
_ALONE_TASK = "call.alone_task"
_ALONE_HANDLER = "call.alone_handler"


def supervise(
    session: Any = None,
    *,
    ctx: Any = None,
    max_duration: float | None = None,
    end_when_away: bool = False,
    end_when_closed: bool = True,
    end_when_alone: bool = True,
) -> None:
    """Watch a call in progress and end it when it should not go on.

    Two things end a call that nobody is ending on purpose. It can run too long — a wrong
    number that never hangs up costs money for as long as it is open. Or the other end can
    simply be gone, which on a telephone line is indistinguishable from silence until
    enough of it has passed.

    And the commonest ending of all: the caller hangs up. The framework closes the session
    then, but not the job — the agent stays in the room, the report never goes out, and
    the call is simply lost.

    That one is watched twice, on purpose. The room says a participant left, which is true
    whether or not a session exists yet — a caller can drop while the configuration is
    still being fetched, and there is nothing to close then. The session says it closed,
    which also covers it ending for reasons that are not a disconnect at all.

    All of them hang up through the same path as anything else, so the caller is released
    and the report and recording still go out. None fires on a call already ending.

    ``end_when_away`` leans on the session's own ``user_away_timeout`` rather than timing
    silence here: the framework already measures it, and measuring it twice would only
    disagree.
    """
    st = _state.state(ctx)
    session = session or st.session

    if max_duration is not None:
        _cap_duration(st, max_duration)

    if end_when_alone:
        _end_when_alone(st)

    if session is None:
        if end_when_away or end_when_closed:
            logger.warning("no session to watch, so nobody will notice the call ending")
        return

    if end_when_away:
        _end_when_away(st, session)

    if end_when_closed:
        _end_when_closed(st, session)


def _cap_duration(st: _state.CallState, max_duration: float) -> None:
    async def expire() -> None:
        started = st.started_at or time.time()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(max(0.0, started + max_duration - time.time()))
            if st.ending:
                return
            logger.info("the call reached its limit of %ss", max_duration)
            # Mid-sentence on purpose: the limit is the point, and waiting for the agent
            # to finish would put the cap wherever the agent happened to be.
            await end(st.ctx, reason=f"the call reached its limit of {max_duration}s", wait=False)

    st.extras[_DURATION_TASK] = asyncio.ensure_future(expire())


def _end_when_away(st: _state.CallState, session: Any) -> None:
    def on_user_state_changed(event: Any) -> None:
        if getattr(event, "new_state", None) != AWAY or st.ending:
            return
        logger.info("the caller has gone quiet, ending the call")
        # Held on the call's state: a task nobody holds can be collected before it runs,
        # and this one is the hangup.
        st.extras[_AWAY_TASK] = asyncio.ensure_future(
            end(st.ctx, reason="the caller went quiet")
        )

    session.on("user_state_changed", on_user_state_changed)


def _end_when_alone(st: _state.CallState) -> None:
    room = getattr(st.ctx, "room", None)
    if room is None:
        return

    def on_participant_disconnected(participant: Any) -> None:
        if st.ending:
            return
        logger.info("%s left, ending the call", getattr(participant, "identity", "someone"))
        st.extras[_ALONE_TASK] = asyncio.ensure_future(
            end(st.ctx, reason="the caller hung up", wait=False)
        )

    st.extras[_ALONE_HANDLER] = on_participant_disconnected
    room.on("participant_disconnected", on_participant_disconnected)


def _end_when_closed(st: _state.CallState, session: Any) -> None:
    def on_close(event: Any) -> None:
        reason = getattr(getattr(event, "reason", None), "value", None) or "closed"
        if reason == JOB_SHUTDOWN or st.ending:
            # The job going down is what closes the session in the first place; ending it
            # again from here would be answering our own hangup.
            return
        if reason == "error":
            # Answered is not the same as completed when the call died of something. The
            # outcome is the webhook's to decide; this is the fact it lacked.
            st.failure = reason
        logger.info("the session closed (%s), ending the call", reason)
        st.extras[_CLOSED_TASK] = asyncio.ensure_future(
            end(st.ctx, reason=f"the session closed: {reason}", wait=False)
        )

    session.on("close", on_close)


def stop(ctx: Any = None) -> None:
    """Stop watching. Called for a call that ended on its own terms."""
    st = _state.state(ctx)
    task = st.extras.pop(_DURATION_TASK, None)
    if task is not None and not task.done():
        task.cancel()

    handler = st.extras.pop(_ALONE_HANDLER, None)
    room = getattr(st.ctx, "room", None)
    if handler is not None and room is not None:
        with contextlib.suppress(Exception):
            room.off("participant_disconnected", handler)
