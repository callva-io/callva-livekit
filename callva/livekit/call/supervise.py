from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ..core import state as _state
from ..core.log import logger
from .release import DURATION, SILENCE, SPEAKING, USER, end

AWAY = "away"

INITIALIZING = "initializing"
"""The agent state a session holds before it has started, and holds again once it has closed."""

TOOL_STARTED = "tool_call_started"
TOOL_ENDED = "tool_call_ended"
"""The two ends of a tool call, as ``tool_execution_updated`` names them."""

JOB_SHUTDOWN = "job_shutdown"
"""The one close reason that means the job is already on its way down."""

PARTICIPANT_DISCONNECTED = "participant_disconnected"
"""The one close reason that names who ended the call. The rest do not say."""

PROMPT_GRACE = 3.0
"""Seconds between the quiet being long enough and the reminder being uttered.

A caller who stops mid-thought sounds exactly like a caller who has gone, right up until they
carry on — so the reminder is delayed by this much and dropped if anything happens inside it.
Three seconds is the production worker's, which is the behaviour being ported.
"""

QUIET_TICK = 0.5
"""How often the quiet is looked at again while somebody is speaking or a tool is running."""

UTTERANCE_TIMEOUT = 5.0
"""Longest to wait for a reminder to be handed over before giving up on that one.

The callable that utters a phrase is somebody else's, which is the whole arrangement: this
package never speaks, and what it is given is a coroutine it knows nothing about. A wait with
no bound on a stranger's coroutine is a watch that can be parked for the life of the process by
one that never returns — and a parked watch measures no quiet, ends no call and lets nothing
go.

Five seconds because handing the ask over is not the model answering it. A realtime model is
given five seconds to start speaking by ``livekit.plugins.google`` and ten by the SDK's own
duplex adapter; merely dispatching the ask cannot honestly need more than the shorter of those,
and on the stacks this is written for it needs none of it — they hand the ask to the provider's
session and return. This is a ceiling on the pathological case, not a deadline anything normal
runs against.
"""

_DURATION_TASK = "call.duration_task"
_AWAY_TASK = "call.away_task"
_CLOSED_TASK = "call.closed_task"
_ALONE_TASK = "call.alone_task"
_ALONE_HANDLER = "call.alone_handler"
_QUIET_TASK = "call.quiet_task"
_QUIET_DETACH = "call.quiet_detach"


def supervise(
    session: Any = None,
    *,
    ctx: Any = None,
    max_duration: float | None = None,
    end_when_away: bool = False,
    end_when_closed: bool = True,
    end_when_alone: bool = True,
    silence_timeout: float | None = None,
    prompt_phrases: Sequence[str] = (),
    max_prompt_attempts: int | None = None,
    call_silence_timeout: float | None = None,
    utter: Callable[[str], Awaitable[None]] | None = None,
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

    **Silence is watched two ways and they do not mix.** ``end_when_away`` is the short one:
    it leans on the session's own ``user_away_timeout``, hangs up the moment the framework
    calls the caller away, and knows nothing about reminding them first. ``silence_timeout``
    and the three arguments under it are the long one: the quiet is measured here, the caller
    is reminded up to ``max_prompt_attempts`` times, and only then is the call ended. A
    deployment that wants the second passes ``user_away_timeout=None`` to its session and
    leaves ``end_when_away`` alone — see :func:`_end_when_quiet` for why the two clocks must
    not both run.
    """
    st = _state.state(ctx)
    session = session or st.session

    if max_duration is not None:
        _cap_duration(st, max_duration)

    if end_when_alone:
        _end_when_alone(st)

    if session is None:
        if end_when_away or end_when_closed or silence_timeout is not None:
            logger.warning("no session to watch, so nobody will notice the call ending")
        return

    if end_when_away and silence_timeout is not None:
        # At warning and not at error: nothing configured is lost, and both clocks do work. What
        # happens is that the shorter of two answers to one question wins, which is the framework's
        # own fixed timeout beating whatever an operator set. It is the deployment's mistake rather
        # than a fact about this call, so it belongs in the deployment's log and not in the
        # tenant's report.
        logger.warning(
            "two clocks are watching this call for quiet: the framework's own away edge and the "
            "timeout configured for it. Whichever is shorter ends the call, and the away edge is "
            "not the one anybody configured"
        )

    if end_when_away:
        _end_when_away(st, session)

    if end_when_closed:
        _end_when_closed(st, session)

    if silence_timeout is not None:
        _end_when_quiet(
            st,
            session,
            silence_timeout=silence_timeout,
            phrases=[p.strip() for p in prompt_phrases if isinstance(p, str) and p.strip()],
            attempts=max_prompt_attempts or 0,
            call_silence_timeout=call_silence_timeout,
            utter=utter,
        )


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
            await end(
                st.ctx,
                reason=f"the call reached its limit of {max_duration}s",
                wait=False,
                ended_by=DURATION,
            )

    st.extras[_DURATION_TASK] = asyncio.ensure_future(expire())


def _end_when_away(st: _state.CallState, session: Any) -> None:
    """Hang up on the framework's own away edge.

    **Inert on a session whose own timer is off.** ``user_away_timeout=None`` is what disables
    it, and a session with it disabled never calls anyone away, so this watches an event that
    cannot arrive and the call is never ended for quiet at all. Nothing at runtime can tell:
    a session does not say what it was built with until it is asked, and an edge that never
    comes looks exactly like a caller who never went quiet. So it is written here, where
    somebody reaching for the argument reads it. The other wrong combination — this beside a
    configured ``silence_timeout``, which is two clocks on one silence — is visible from the
    arguments alone, and :func:`supervise` says so out loud when it sees both.

    It is also the framework's clock and not this one: the timeout is whatever the session was
    built with, the edge fires once per episode of quiet, and nothing re-arms it afterwards. An
    operator's own timeout, a reminder before the hangup, or a second chance after one belong
    to :func:`_end_when_quiet`, and the two must not both be armed on one call.
    """

    def on_user_state_changed(event: Any) -> None:
        if getattr(event, "new_state", None) != AWAY or st.ending:
            return
        logger.info("the caller has gone quiet, ending the call")
        # Held on the call's state: a task nobody holds can be collected before it runs,
        # and this one is the hangup.
        st.extras[_AWAY_TASK] = asyncio.ensure_future(
            end(st.ctx, reason="the caller went quiet", ended_by=SILENCE)
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
            end(st.ctx, reason="the caller hung up", wait=False, ended_by=USER)
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
            end(
                st.ctx,
                reason=f"the session closed: {reason}",
                wait=False,
                # Only one close reason says who ended the call. The others report
                # that it closed, not why, and a guess would be worse than silence.
                ended_by=USER if reason == PARTICIPANT_DISCONNECTED else None,
            )
        )

    session.on("close", on_close)


def _drop(st: _state.CallState, name: str) -> None:
    """Stop a supervising task — unless it is the one hanging the call up.

    :func:`end` sets ``st.ending`` before it does any of the work: it waits for the agent to
    stop speaking, then for a grace, and only then deletes the room and shuts the job down. A
    task cancelled inside that window abandons the hangup halfway, and the abandonment is
    silent and total — the flag is set, so every other watcher declines to hang up because it
    believes somebody already is, and nobody ever deletes the room. Whoever is on the line
    holds an open call to an empty room until the server's own timeout.

    Nothing is lost by declining, and that rests on every wait a supervising task makes being
    bounded rather than on hoping so. In the quiet watch: :func:`rest` is a sleep that re-reads
    the flag the moment it wakes, and the utterance — the one await that is a stranger's
    coroutine — is capped at :data:`UTTERANCE_TIMEOUT`. In :func:`end`: ``until_quiet`` is
    capped at ``QUIET_TIMEOUT`` and the grace is a sleep. In the duration cap: a sleep, then
    :func:`end`.

    So a task that is ending the call returns when the hangup does, and a task that is waiting
    returns after one of those. The long one is the duration cap, which sleeps out the whole
    limit before reading the flag and returning — a sleeping task on a job that is already
    shutting down, which the loop cancels on its way out in any case. It is not an unbounded
    wait, and it is what never abandoning a hangup costs.

    The only task that would otherwise run on is one on a call that is not ending, which is
    exactly the one this cancels.

    A task left alone is left held on the call's state as well as uncancelled: a task nobody
    holds can be collected before it finishes, and this one is finishing a hangup.
    """
    task = st.extras.get(name)
    if task is None or st.ending:
        return
    st.extras.pop(name, None)
    if not task.done():
        task.cancel()


def _on_the_call(session: Any) -> bool:
    """Whether there is a call whose quiet means anything, from what the session says in public.

    The framework's own timer refuses to arm before the caller's audio has been subscribed,
    reading a future on its room I/O that nobody outside it holds. Two public properties stand
    in for it. ``agent_state`` is ``initializing`` until ``start`` finishes and is set back to
    it when the session closes, so a session that is not running is not a call. And
    ``input.audio_enabled`` is false while the caller is deliberately unheard — which is what
    an agent does to stay deaf to ringback on an outbound call that has not been picked up.

    Neither is read once and remembered: an outbound call is deafened and then heard, and a
    clock that decided at its first look would measure the ringing.
    """
    if getattr(session, "agent_state", None) == INITIALIZING:
        return False
    return bool(getattr(getattr(session, "input", None), "audio_enabled", True))


def _end_when_quiet(
    st: _state.CallState,
    session: Any,
    *,
    silence_timeout: float,
    phrases: list[str],
    attempts: int,
    call_silence_timeout: float | None,
    utter: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Measure the quiet by our own clock: remind the caller, then end the call.

    The framework's timer is not this, and cannot be made into it. It fires once per episode
    of quiet — the edge into ``away`` — and nothing re-arms it: a reminder that goes unanswered
    leaves the caller away already, so no second edge ever comes. Counting reminders needs a
    clock that keeps running, which is this one, and a session running both would be two
    clocks disagreeing about the same silence.

    **Two things are being measured and they answer to different evidence.** The clock asks
    "is anybody making a sound", and the count asks "did they answer". Conflating them gets one
    of the two wrong whichever way it is done.

    *The clock restarts on any sign of life.* The voice detector saying the caller started, the
    voice detector saying they stopped, any transcribed event at all, the agent speaking, a
    tool landing. Noise counts, because what the clock prevents is talking over somebody — and
    a caller mid-sentence whose final transcript has not landed yet is the worst possible person
    to talk over.

    *The count goes back to zero only on words.* A transcribed event carrying text, interim or
    final. The line is noise against words and not interim against final: somebody mid-sentence
    is speaking, and an interim transcript with text is exactly that. A cough must not buy a
    fresh set of reminders, or the reminders are worth little.

    The one divergence from the production worker is on that last point: it clears the count on
    every transcribed event, an empty one included, and an empty transcript is the recogniser
    saying it heard a sound and found no words in it — which is the cough.

    The agent speaking and a tool landing move the clock and never the count: they are this
    side of the call, and a caller who has heard three answers and said nothing to any of them
    has still said nothing.

    **What is not quiet at all.** A tool in flight, which will speak when it lands; a session
    that is not running or cannot hear the caller; and the framework calling the caller away,
    which is the framework saying they have *not* stirred and must reset nothing.

    **Nothing here is invented.** No timeout, no phrase, no number of attempts. An operator who
    configured none of it is never reached at all, because ``silence_timeout=None`` leaves this
    unarmed; one who asked for reminders and wrote no phrase gets no reminder and a line saying
    so, rather than a phrase this package chose for their agent to say.
    """
    voice = utter if (phrases and utter is not None) else None
    if attempts and not phrases:
        # Error, which is the floor the collector behind ``call.ended`` reads and so the only
        # level that reaches the report. That is where this belongs: the phrases are the
        # operator's own configuration, they are the only one who can write some, and a caller
        # who hears nothing is the consequence of their agent as they set it up.
        logger.error(
            "%s reminders are configured for a caller who goes quiet and no phrase was written "
            "for them, so nothing is said; the attempts are still counted and the call still ends",
            attempts,
        )
    elif attempts and voice is None:
        # Warning, and the outcome being the same as above does not make the level the same.
        # The test is whether the operator can act on it, and this is the one line about a
        # quiet caller where they cannot: nobody passed a callable, which is how the worker
        # around this package was assembled and nothing an agent's configuration reaches. The
        # neighbouring lines about a stack that cannot speak *are* at error, because a preset
        # is something an operator chose and can change — so the inconsistency here is the
        # rule being applied rather than a lapse in it.
        logger.warning(
            "%s reminders are configured for a caller who goes quiet and nothing was supplied "
            "to utter them, so nothing is said; the attempts are still counted and the call "
            "still ends",
            attempts,
        )

    logger.debug(
        "watching this call for quiet: %ss, %s reminder(s), then %s",
        silence_timeout,
        attempts,
        f"ending it {call_silence_timeout}s later"
        if call_silence_timeout is not None
        else "letting the conversation go on",
    )

    if not attempts and call_silence_timeout is None:
        # Nothing to remind with and nothing to end on: there is no moment at which this could
        # act, so it does not wait for one.
        return

    last = time.time()
    used = 0
    user_speaking = False
    agent_speaking = False
    running_tools = 0

    def stirred() -> None:
        """Something happened on this call. The clock restarts; the count stands."""
        nonlocal last
        last = time.time()

    def answered() -> None:
        """Words from the caller. The clock restarts and the reminders are theirs again."""
        nonlocal last, used
        last = time.time()
        used = 0

    def on_user_state_changed(event: Any) -> None:
        nonlocal user_speaking
        state = getattr(event, "new_state", None)
        user_speaking = state == SPEAKING
        if state != AWAY:
            # Both edges are a sign of life and neither is an answer: the voice detector hears
            # sounds and does not know words from them. `away` is not an edge at all — it is
            # the framework's own verdict that the caller has *not* stirred, and reading it as
            # activity would add the framework's timeout to ours on any session that still has
            # one, pushing every reminder late by exactly that much.
            stirred()

    def on_agent_state_changed(event: Any) -> None:
        nonlocal agent_speaking
        agent_speaking = getattr(event, "new_state", None) == SPEAKING
        stirred()

    def on_user_input_transcribed(event: Any) -> None:
        # Every one of them moves the clock: voice detection can miss speech the recogniser
        # heard, and the recogniser emitting at all is somebody making a sound. Only the ones
        # carrying words return a spent reminder, and interim counts — a caller mid-sentence is
        # answering, whether or not the final transcript has landed.
        if (getattr(event, "transcript", "") or "").strip():
            answered()
        else:
            stirred()

    def on_tool_execution_updated(event: Any) -> None:
        nonlocal running_tools
        kind = getattr(getattr(event, "update", None), "type", None)
        if kind == TOOL_STARTED:
            running_tools += 1
        elif kind == TOOL_ENDED:
            running_tools = max(0, running_tools - 1)
            stirred()

    def on_close(_event: Any) -> None:
        """The call is over. Stop watching it, whatever reason the session gives for closing.

        This exists for one case: a session that closed without anything setting ``st.ending``
        — a caller who turned ``end_when_closed`` off, a job torn down under us. A watch left
        running on one of those measures a room nobody is in, forever, because a closed session
        reports its agent state as ``initializing`` and never leaves it.

        A call that is already ending needs nothing from here and must not be touched: see
        :func:`_drop`, which is where that is decided for every canceller.
        """
        _drop(st, _QUIET_TASK)

    watched = (
        ("user_state_changed", on_user_state_changed),
        ("agent_state_changed", on_agent_state_changed),
        ("user_input_transcribed", on_user_input_transcribed),
        ("tool_execution_updated", on_tool_execution_updated),
        ("close", on_close),
    )
    for event_name, handler in watched:
        session.on(event_name, handler)

    def detach() -> None:
        for event_name, handler in watched:
            with contextlib.suppress(Exception):
                session.off(event_name, handler)

    st.extras[_QUIET_DETACH] = detach

    def quiet_right_now() -> bool:
        """Whether there is any quiet to measure at this instant.

        Asked in two places — before the clock is read, and again after the grace period, which
        is a wait long enough for every one of these to change inside it. One predicate and not
        two lists, because two lists that are meant to be the same list diverge: the first time
        they did, a tool that started inside the grace window was spoken over by the reminder
        that had been decided before it.
        """
        return (
            _on_the_call(session)
            and not user_speaking
            and not agent_speaking
            and not running_tools
        )

    async def rest(seconds: float) -> bool:
        """Wait, and say afterwards whether there is still a call here to watch.

        Every wait below goes through this, and that is where "the watch does not outlive the
        call" is stated. ``st.ending`` is set synchronously by :func:`end` before anything else
        happens — whoever called it, and whether it was the duration cap, the caller hanging up,
        the session closing or the agent's own tool — so a watch that re-reads it after every
        sleep cannot act on a call that is already over. The reminder is why this has to be
        after *every* sleep rather than only at the top of the loop: the grace period is a wait
        with an utterance on the far side of it, and a caller who hangs up during one would
        otherwise be spoken to through a room that has already been deleted.

        Waking to find the call gone is the ordinary end of a supervised call, so it is not an
        error and is not logged.
        """
        await asyncio.sleep(max(0.0, seconds))
        return not st.ending

    async def remind(quiet: float) -> None:
        nonlocal last, used
        if st.ending:
            return
        used += 1
        if voice is None:
            # Counted and not spoken, and the line says which. A reminder nobody hears buys no
            # more of the caller's attention than no reminder at all, so the count is the same;
            # a log line claiming the caller was reminded would be the one thing this may not do.
            logger.info(
                "nobody has said anything for %.0fs, and reminder %s of %s goes unsaid",
                quiet,
                used,
                attempts,
            )
        else:
            logger.info(
                "nobody has said anything for %.0fs, reminding the caller (%s of %s)",
                quiet,
                used,
                attempts,
            )
            try:
                # Bounded because it is foreign: see :data:`UTTERANCE_TIMEOUT`. Neither ending
                # below is worth the call — a caller who hears nothing is no more present for
                # it, and the attempt is spent either way, because it was made.
                await asyncio.wait_for(voice(random.choice(phrases)), UTTERANCE_TIMEOUT)
            except asyncio.TimeoutError:
                # Caught before the clause below, which would otherwise take it: on 3.11 and
                # after, ``asyncio.TimeoutError`` is the builtin one and an ordinary exception.
                logger.error(
                    "the reminder for a quiet caller was not handed over within %ss, so it was "
                    "given up on",
                    UTTERANCE_TIMEOUT,
                )
            except Exception:
                # Error, unlike the two configuration lines above, and for a different reason
                # than either: this is not how anything was set up, it is something that
                # happened on this call. The reminder was configured, was due, was asked for
                # and did not reach the caller — a fact about this call, which is what the
                # report's errors are for. The timeout above is the same fact by a slower
                # route and is reported at the same level for the same reason.
                logger.exception("the reminder for a quiet caller could not be uttered")
        last = time.time()
        if used >= attempts and call_silence_timeout is None:
            logger.debug(
                "the caller has had the %s reminders this call is armed for; the conversation "
                "goes on, and speaking again gives them back",
                attempts,
            )

    async def watch() -> None:
        try:
            while not st.ending:
                if not _on_the_call(session):
                    # The quiet is measured from the call and not from before it. A clock that
                    # kept counting while the caller could not be heard would spend the whole
                    # of its patience in the instant they arrived.
                    stirred()
                    if not await rest(QUIET_TICK):
                        return
                    continue

                if not quiet_right_now():
                    # A tool in flight is not quiet: whatever it answers will be spoken when it
                    # lands, and its landing restarts the clock.
                    if not await rest(QUIET_TICK):
                        return
                    continue

                quiet = time.time() - last
                if quiet < silence_timeout:
                    if not await rest(silence_timeout - quiet):
                        return
                    continue

                if used < attempts:
                    stirred_at = last
                    if not await rest(PROMPT_GRACE):
                        return
                    if not quiet_right_now() or last != stirred_at:
                        logger.debug("the call stirred inside the grace period, so no reminder")
                        continue
                    await remind(quiet + PROMPT_GRACE)
                    continue

                if call_silence_timeout is None:
                    # This episode of quiet has had its reminders and there is nothing
                    # configured to end the call over. Back to waiting, because the reminders
                    # belong to an episode and not to the call: they are what happens after
                    # somebody has been talking and stops, and words from the caller give them
                    # back — which is the same thing ``answered`` says by clearing the count.
                    if not await rest(QUIET_TICK):
                        return
                    continue

                if quiet < call_silence_timeout:
                    # Looked at again on the ordinary tick rather than slept out in one go: the
                    # caller can still come back inside this last stretch, and a clock asleep
                    # until the hangup would not notice until it had already hung up.
                    if not await rest(min(QUIET_TICK, call_silence_timeout - quiet)):
                        return
                    continue

                logger.info(
                    "nobody has said anything for %.0fs after %s reminders, ending the call",
                    quiet,
                    used,
                )
                await end(
                    st.ctx,
                    reason=f"nobody said anything for {quiet:.0f}s",
                    session=session,
                    ended_by=SILENCE,
                )
                return
        except asyncio.CancelledError:
            pass
        except Exception:
            # Nothing about watching a call is worth taking the call down for, and a watch
            # that stopped without saying so is a call that quietly lost its silence timeout.
            #
            # At error, and the rule about what an operator can act on does not reach this.
            # That rule governs configuration states — things reported because somebody set
            # them up that way, where the choice is between telling the person who can change
            # it and telling nobody. An unexpected exception is not one of those. The collector
            # reads at error and nowhere below, so the alternative here is not a quieter
            # audience but no audience at all: an operator who sees this asks us, which is the
            # outcome worth having.
            logger.exception("the watch on a quiet caller stopped")
        finally:
            # The listeners and the watch end together, on every way out of it: the call
            # ending, the session closing, and the watch finding nothing left it can do. A
            # session holding handlers for a watch that is over holds the closures they were
            # written in, and nothing else was ever going to take them off — :func:`stop` is
            # a courtesy a caller may not use.
            st.extras.pop(_QUIET_TASK, None)
            st.extras.pop(_QUIET_DETACH, None)
            detach()

    st.extras[_QUIET_TASK] = asyncio.ensure_future(watch())


def stop(ctx: Any = None) -> None:
    """Stop watching, for a caller ending a call on its own terms without going through
    :func:`end`.

    Not what keeps a watch from outliving its call. That is stated where it has to hold whether
    anybody remembers to call anything — inside the watch, which re-reads ``st.ending`` after
    every wait and stops itself when the session closes. This is the courtesy on top: a caller
    that is tearing a call down its own way can say so and have the listeners come off now
    rather than when the session does.
    """
    st = _state.state(ctx)
    for name in (_DURATION_TASK, _QUIET_TASK):
        _drop(st, name)

    detach = st.extras.pop(_QUIET_DETACH, None)
    if detach is not None:
        detach()

    handler = st.extras.pop(_ALONE_HANDLER, None)
    room = getattr(st.ctx, "room", None)
    if handler is not None and room is not None:
        with contextlib.suppress(Exception):
            room.off("participant_disconnected", handler)
