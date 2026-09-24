from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import Awaitable, Callable
from typing import Any

from ..core import state as _state
from ..core.log import logger

QUIET_TIMEOUT = 10.0
"""Longest to wait for the agent to stop speaking before hanging up anyway."""

GRACE = 1.0
"""Seconds after it falls quiet, for audio already on its way to the caller."""

SPEAKING = "speaking"

AGENT = "agent"
"""The agent decided: a tool call, a farewell, anything on this side of the call."""
USER = "user"
"""The other end decided: they hung up, or their client left the room."""
SILENCE = "silence"
"""Nobody said anything for long enough that the call was ended over it."""
DURATION = "duration"
"""The call reached the limit it was placed under."""
NO_ANSWER = "no_answer"
"""Nobody ever answered, so nobody ended it: it never began."""


def ended_by(who: str, ctx: Any = None) -> bool:
    """Claim who ended this call. ``True`` if the claim was taken.

    The package claims what it can see for itself — a call nobody answered, a duration
    limit reached, the other end leaving, silence, and its own :func:`end` — and anything
    else is yours to name: a tool the agent ran, a transfer, a supervisor pulling the
    call. Claim it before ending the call, and it reaches the report.

    First writer wins, so claiming late over something already claimed does nothing.
    """
    return _state.claim_ending(_state.state(ctx), who)


async def end(
    ctx: Any = None,
    *,
    reason: str = "the agent ended the call",
    session: Any = None,
    wait: bool = True,
    ended_by: str | None = AGENT,
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

    ``ended_by`` is who this hangup is on behalf of, and it reaches the report. The
    default is the agent, because calling this is the agent deciding; whatever supervises
    the call passes its own. Pass ``None`` to hang up without claiming the ending, for an
    ending whose cause is somebody else's to name.
    """
    st = _state.state(ctx)
    ctx = st.ctx

    if st.ending:
        # Two things can decide to hang up on the same call — the agent's own tool and
        # whatever supervises the call — and they can decide it at the same moment.
        logger.debug("already ending this call, ignoring: %s", reason)
        return
    st.ending = True

    if ended_by is not None:
        _state.claim_ending(st, ended_by)

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


def hangs_up_on_failure(
    entrypoint: Callable[[Any], Awaitable[None]],
) -> Callable[[Any], Awaitable[None]]:
    """Make an entrypoint that cannot conduct its call hang it up, never leave it ringing.

    An exception out of an entrypoint ends the job and nothing else. The room outlives it,
    so a caller on an inbound line that was never answered goes on hearing it ring, and
    one on a line that was answered hears nothing, until the server gives up on the room -
    a call that failed and says so to nobody on the phone. Whatever the entrypoint did not
    handle itself is therefore logged, which reaches ``call.ended`` with the rest of the
    call's errors, and the call is hung up at once: the room deleted, the job shut down.

    Every such failure is hung up on in silence. Some are worth a sentence to the caller -
    a balance that has run out is theirs to act on, and hearing so is better than hearing
    the line drop - but most are ours, a server or a provider failing, and none of those
    is anything to say to whoever answered. A failure that should be spoken is a refusal
    the platform words for the caller, and that path exists already: the configuration
    request answers no with a caller message, and the worker speaks it. Choosing which
    other failures join it is left to that channel rather than decided here.

    Put it under the session decorator, so that what the server registers is the guarded
    function::

        @server.rtc_session(agent_name=...)
        @call.hangs_up_on_failure
        async def entrypoint(ctx: JobContext) -> None: ...

    ``functools.wraps`` keeps the entrypoint's name and module, which is how the job
    process finds it again: the function is handed to that process by reference.
    """

    @functools.wraps(entrypoint)
    async def guarded(ctx: Any) -> None:
        try:
            await entrypoint(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("this call could not be conducted; hanging up")
            await end(ctx, reason="the call could not be conducted", wait=False, ended_by=None)

    return guarded


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
