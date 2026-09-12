from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from ..core import state as _state
from ..core.log import logger
from .release import end

AWAY = "away"

_DURATION_TASK = "call.duration_task"
_AWAY_TASK = "call.away_task"


def supervise(
    session: Any = None,
    *,
    ctx: Any = None,
    max_duration: float | None = None,
    end_when_away: bool = False,
) -> None:
    """Watch a call in progress and end it when it should not go on.

    Two things end a call that nobody is ending on purpose. It can run too long — a wrong
    number that never hangs up costs money for as long as it is open. Or the other end can
    simply be gone, which on a telephone line is indistinguishable from silence until
    enough of it has passed.

    Both hang up through the same path as anything else, so the caller is released and the
    report and recording still go out. Neither fires on a call already ending.

    ``end_when_away`` leans on the session's own ``user_away_timeout`` rather than timing
    silence here: the framework already measures it, and measuring it twice would only
    disagree.
    """
    st = _state.state(ctx)
    session = session or st.session

    if max_duration is not None:
        _cap_duration(st, max_duration)

    if end_when_away:
        if session is None:
            logger.warning("no session to watch, so nobody will notice the caller leaving")
        else:
            _end_when_away(st, session)


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


def stop(ctx: Any = None) -> None:
    """Stop watching. Called for a call that ended on its own terms."""
    st = _state.state(ctx)
    task = st.extras.pop(_DURATION_TASK, None)
    if task is not None and not task.done():
        task.cancel()
