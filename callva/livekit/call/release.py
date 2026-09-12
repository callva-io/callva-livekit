from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..core import state as _state
from ..core.log import logger

QUIET_TIMEOUT = 10.0
"""Longest to wait for the agent to stop speaking before hanging up anyway."""

GRACE = 1.0
"""Seconds after it falls quiet, for audio already on its way to the caller."""

SPEAKING = "speaking"


async def end(
    ctx: Any = None,
    *,
    reason: str = "the agent ended the call",
    session: Any = None,
    wait: bool = True,
) -> None:
    """Hang up: let the caller go, then end the job.

    Shutting the job down only takes the agent out of the room. Whoever is on the other
    end stays connected to a room with nobody in it, listening to silence until the
    server's own ``empty_timeout`` expires — which on a telephone call means it simply has
    not ended. Deleting the room disconnects everyone, and it goes first so the caller is
    released before the shutdown sequence starts.

    The report and the recording are unaffected: they belong to that sequence, and a
    closed room does not interrupt it.

    Pass ``wait=False`` to hang up mid-sentence, for a call that is being abandoned rather
    than finished.
    """
    st = _state.state(ctx)
    ctx = st.ctx

    if st.ending:
        # Two things can decide to hang up on the same call — the agent's own tool and
        # whatever supervises the call — and they can decide it at the same moment.
        logger.debug("already ending this call, ignoring: %s", reason)
        return
    st.ending = True

    if wait:
        # Read at call time, not bound into the signature, so the module constant can be
        # changed by anyone who needs a different patience.
        if not await until_quiet(session or st.session, QUIET_TIMEOUT):
            logger.warning("still speaking after %ss, hanging up anyway", QUIET_TIMEOUT)
        await asyncio.sleep(GRACE)

    logger.info("ending the call: %s", reason)
    with contextlib.suppress(Exception):
        ctx.delete_room()
    ctx.shutdown(reason=reason)


async def until_quiet(session: Any, timeout: float = QUIET_TIMEOUT) -> bool:
    """Wait until the agent is no longer speaking. ``False`` if it never stopped.

    Hanging up mid-sentence is the failure this exists to prevent. A session that cannot
    say counts as quiet: refusing to hang up because nothing answered would be worse.
    """
    if session is None or getattr(session, "agent_state", None) != SPEAKING:
        return True

    quiet = asyncio.Event()

    def on_state_changed(event: Any) -> None:
        if getattr(event, "new_state", None) != SPEAKING:
            quiet.set()

    session.on("agent_state_changed", on_state_changed)
    try:
        await asyncio.wait_for(quiet.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        with contextlib.suppress(Exception):
            session.off("agent_state_changed", on_state_changed)


def leave_console_when_done(ctx: Any = None) -> None:
    """Stop the process when a console call is over. Does nothing on a real job.

    Console mode has no room, so nothing disconnects and nothing ends: the job finishes,
    the webhook goes out, the recording uploads, and the process sits there holding the
    microphone open.

    The exit is registered as a shutdown callback, which the framework runs last — after
    the session has closed, after ``on_session_end``, after the room is disconnected — so
    nothing is skipped by leaving from there. Opt-in, and named plainly, because a library
    that ends someone's process should be asked to.
    """
    st = _state.state(ctx)
    ctx = st.ctx

    if not getattr(ctx, "is_fake_job", lambda: False)():
        return

    async def leave(reason: str = "") -> None:
        import os
        import sys

        logger.info("console mode: the call is over, leaving (%s)", reason or "no reason")
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        os._exit(0)

    ctx.add_shutdown_callback(leave)
