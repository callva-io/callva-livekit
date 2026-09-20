from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from typing import Any

import pytest
from fakes import FakeContext, FakeParticipant, FakeSession, envelope_metadata

from callva.livekit import call
from callva.livekit.core import state as _state

RINGING = {"sip.phoneNumber": "+37255512345", "sip.callStatus": "ringing"}
ANSWERED = {"sip.phoneNumber": "+37255512345", "sip.callStatus": "active"}


def outbound(**attributes: str) -> FakeContext:
    ctx = FakeContext(envelope_metadata(direction="outbound"))
    ctx.room.remote_participants.clear()
    if attributes:
        ctx.room.remote_participants["sip_x"] = FakeParticipant("sip_x", **attributes)
    return ctx


# --- Being answered ----------------------------------------------------------


async def test_an_inbound_call_is_answered_the_moment_someone_is_there(bind_context):
    """Nobody rings an inbound call: the participant exists because it was picked up."""
    ctx = bind_context(FakeContext())
    ctx.room.remote_participants["caller"] = FakeParticipant("caller")

    assert (await call.await_pickup(timeout=0.2)).identity == "caller"


async def test_a_participant_that_joins_later_is_still_caught(bind_context):
    ctx = bind_context(FakeContext())
    ctx.room.remote_participants.clear()

    async def join() -> None:
        await asyncio.sleep(0.01)
        ctx.room.emit_participant_connected(FakeParticipant("late"))

    picked, _ = await asyncio.gather(call.await_pickup(timeout=1.0), join())
    assert picked.identity == "late"


async def test_an_outbound_call_is_not_answered_while_it_rings(bind_context):
    """The participant appears as the phone starts ringing; the call has not begun."""
    ctx = bind_context(outbound(**RINGING))

    assert await call.await_pickup(timeout=0.05) is None
    assert _state.state(ctx).unanswered_reason == "timeout"


async def test_an_outbound_call_is_answered_when_the_status_says_so(bind_context):
    ctx = bind_context(outbound(**RINGING))
    participant = ctx.room.remote_participants["sip_x"]

    async def pick_up() -> None:
        await asyncio.sleep(0.01)
        participant.attributes["sip.callStatus"] = "active"
        ctx.room.emit_attributes_changed({"sip.callStatus": "active"}, participant)

    picked, _ = await asyncio.gather(call.await_pickup(timeout=1.0), pick_up())
    assert picked is participant


async def test_a_call_already_answered_before_we_looked(bind_context):
    """Attaching listeners is not enough — a trunk can answer before this is called."""
    bind_context(outbound(**ANSWERED))

    assert (await call.await_pickup(timeout=0.2)).identity == "sip_x"


# --- Not being answered ------------------------------------------------------


async def test_the_reason_survives_for_whoever_reports_the_call(bind_context):
    """Busy and declined are written as no status at all; the disconnect carries them."""
    ctx = bind_context(outbound(**RINGING))
    participant = ctx.room.remote_participants["sip_x"]

    async def refuse() -> None:
        await asyncio.sleep(0.01)
        ctx.room.emit_participant_disconnected(
            participant, type("Reason", (), {"name": "USER_REJECTED"})()
        )

    picked, _ = await asyncio.gather(call.await_pickup(timeout=1.0), refuse())

    assert picked is None
    st = _state.state(ctx)
    assert st.unanswered_reason == "USER_REJECTED"
    assert st.extras["disconnect_reason"] == "USER_REJECTED"


async def test_a_trunk_that_answers_and_hangs_up_does_not_slip_through(bind_context):
    """Under a second, and a plain wait for a participant would call it answered."""
    ctx = bind_context(outbound(**RINGING))
    participant = ctx.room.remote_participants["sip_x"]

    async def flicker() -> None:
        await asyncio.sleep(0.01)
        participant.attributes["sip.callStatus"] = "hangup"
        ctx.room.emit_attributes_changed({"sip.callStatus": "hangup"}, participant)

    picked, _ = await asyncio.gather(call.await_pickup(timeout=1.0), flicker())

    assert picked is None
    assert _state.state(ctx).unanswered_reason == "hangup"


async def test_listeners_are_removed_either_way(bind_context):
    ctx = bind_context(outbound(**ANSWERED))

    await call.await_pickup(timeout=0.2)

    assert ctx.room.handlers == {}


# --- Ending ------------------------------------------------------------------


@pytest.fixture
def no_grace(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(call.release, "GRACE", 0.0)


async def test_the_caller_is_released_before_the_job_ends(bind_context, no_grace):
    """FakeContext refuses a delete that arrives after the shutdown."""
    ctx = bind_context(FakeContext())

    await call.end(reason="done")

    assert ctx.deleted_room is True
    assert ctx.shutdown_reason == "done"


async def test_it_will_not_hang_up_mid_sentence(bind_context, no_grace):
    ctx = bind_context(FakeContext())
    session = FakeSession(agent_state="speaking")

    async def finish() -> None:
        await asyncio.sleep(0.01)
        session.stop_speaking()

    await asyncio.gather(call.end(session=session), finish())

    assert ctx.deleted_room is True


async def test_a_session_that_never_stops_is_hung_up_on(bind_context, no_grace, monkeypatch):
    monkeypatch.setattr(call.release, "QUIET_TIMEOUT", 0.02)
    ctx = bind_context(FakeContext())

    await call.end(session=FakeSession(agent_state="speaking"))

    assert ctx.shutdown_reason is not None


async def test_an_abandoned_call_is_not_waited_on(bind_context):
    """`wait=False` is for a call being dropped, not finished."""
    ctx = bind_context(FakeContext())

    await call.end(session=FakeSession(agent_state="speaking"), wait=False, reason="gone")

    assert ctx.shutdown_reason == "gone"


async def test_a_session_that_cannot_say_counts_as_quiet(bind_context, no_grace):
    """Refusing to hang up because nothing answered would be worse than hanging up."""
    ctx = bind_context(FakeContext())

    await call.end(session=None)

    assert ctx.deleted_room is True


# --- Console -----------------------------------------------------------------


def test_the_console_exit_is_registered_only_for_a_console(bind_context):
    ctx = bind_context(FakeContext())
    ctx.fake_job = True

    call.leave_console_when_done()

    assert len(ctx.shutdown_callbacks) == 1


def test_a_real_job_is_left_alone(bind_context):
    ctx = bind_context(FakeContext())

    call.leave_console_when_done()

    assert ctx.shutdown_callbacks == []


# --- Watching a call in progress ---------------------------------------------


async def test_a_call_that_runs_too_long_is_ended(bind_context, no_grace):
    ctx = bind_context(FakeContext())

    call.supervise(max_duration=0.02)
    await asyncio.sleep(0.1)

    assert ctx.deleted_room is True
    assert "limit" in (ctx.shutdown_reason or "")


async def test_the_limit_does_not_wait_for_the_agent_to_finish(bind_context, no_grace):
    """Waiting would put the cap wherever the agent happened to be."""
    ctx = bind_context(FakeContext())
    st = _state.state(ctx)
    st.session = FakeSession(agent_state="speaking")

    call.supervise(max_duration=0.02)
    await asyncio.sleep(0.1)

    assert ctx.shutdown_reason is not None


async def test_the_limit_is_dropped_when_the_call_ends_first(bind_context, no_grace):
    ctx = bind_context(FakeContext())

    call.supervise(max_duration=0.02)
    call.stop()
    await asyncio.sleep(0.1)

    assert ctx.shutdown_reason is None


async def test_a_caller_who_has_gone_is_hung_up_on(bind_context, no_grace):
    ctx = bind_context(FakeContext())
    session = FakeSession()

    call.supervise(session, end_when_away=True)
    session.go_away()
    await asyncio.sleep(0.05)

    assert ctx.deleted_room is True
    assert ctx.shutdown_reason == "the caller went quiet"


async def test_a_caller_who_merely_stopped_talking_is_not(bind_context, no_grace):
    ctx = bind_context(FakeContext())
    session = FakeSession()

    call.supervise(session, end_when_away=True)
    session.go_away("listening")
    await asyncio.sleep(0.05)

    assert ctx.shutdown_reason is None


async def test_only_the_first_hangup_counts(bind_context, no_grace):
    """The agent's own tool and the supervisor can decide at the same moment."""
    ctx = bind_context(FakeContext())

    await call.end(reason="first")
    await call.end(reason="second")

    assert ctx.shutdown_reason == "first"


# --- A call nobody was ever on -----------------------------------------------


async def test_an_unanswered_call_is_still_reported_as_one(bind_context):
    """The whole point of recording the reason rather than acting on it."""
    from callva.livekit.webhook import payload as _payload

    ctx = bind_context(outbound(**RINGING))
    await call.await_pickup(timeout=0.05)

    st = _state.state(ctx)
    body = _payload.build(st, event=_payload.ENDED, key="k", status="no_answer")

    assert body["call"]["reason"] == "timeout"
    assert body["call"]["status"] == "no_answer"


async def test_a_caller_who_hangs_up_ends_the_job(bind_context, no_grace):
    """The commonest ending there is. The framework closes the session, not the job."""
    ctx = bind_context(FakeContext())
    session = FakeSession()

    call.supervise(session)
    session.close("participant_disconnected")
    await asyncio.sleep(0.05)

    assert ctx.shutdown_reason == "the session closed: participant_disconnected"


async def test_our_own_shutdown_is_not_answered_with_another(bind_context, no_grace):
    """The job going down is what closes the session; ending again would be a loop."""
    ctx = bind_context(FakeContext())
    session = FakeSession()

    call.supervise(session)
    session.close("job_shutdown")
    await asyncio.sleep(0.05)

    assert ctx.shutdown_reason is None


async def test_the_room_saying_so_is_enough(bind_context, no_grace):
    """A caller can drop while the configuration is still being fetched. No session yet."""
    ctx = bind_context(FakeContext())
    participant = FakeParticipant("sip_x")
    ctx.room.remote_participants["sip_x"] = participant

    call.supervise()
    ctx.room.emit_participant_disconnected(participant)
    await asyncio.sleep(0.05)

    assert ctx.deleted_room is True
    assert ctx.shutdown_reason == "the caller hung up"


async def test_watching_twice_still_hangs_up_once(bind_context, no_grace):
    """The room and the session both report it; the second must do nothing."""
    ctx = bind_context(FakeContext())
    session = FakeSession()
    participant = FakeParticipant("sip_x")

    call.supervise(session)
    ctx.room.emit_participant_disconnected(participant)
    session.close("participant_disconnected")
    await asyncio.sleep(0.05)

    assert ctx.shutdown_reason == "the caller hung up"


async def test_a_call_that_died_of_an_error_did_not_complete(bind_context, no_grace):
    """Answered is not the same as completed when the session broke underneath it."""
    from callva.livekit.webhook import service as _service

    ctx = bind_context(FakeContext())
    session = FakeSession()
    st = _state.state(ctx)
    st.started_sent = True

    call.supervise(session)
    session.close("error")
    await asyncio.sleep(0.05)

    assert st.failure == "error"
    assert _service._outcome(st) == "failed"


async def test_a_call_that_merely_ended_still_completed(bind_context, no_grace):
    from callva.livekit.webhook import service as _service

    ctx = bind_context(FakeContext())
    session = FakeSession()
    st = _state.state(ctx)
    st.started_sent = True

    call.supervise(session)
    session.close("participant_disconnected")
    await asyncio.sleep(0.05)

    assert _service._outcome(st) == "completed"


# --- Who ended the call ------------------------------------------------------


def ended_by(ctx: FakeContext) -> str | None:
    return _state.state(ctx).ended_by


async def test_the_agent_hanging_up_is_the_agent(bind_context, no_grace):
    ctx = bind_context(FakeContext())

    await call.end(reason="the agent said goodbye")

    assert ended_by(ctx) == call.AGENT


async def test_the_other_end_leaving_owns_the_ending(bind_context, no_grace):
    ctx = bind_context(FakeContext())
    participant = FakeParticipant("sip_x")
    ctx.room.remote_participants["sip_x"] = participant

    call.supervise()
    ctx.room.emit_participant_disconnected(participant)
    await asyncio.sleep(0.05)

    assert ended_by(ctx) == call.USER


async def test_a_duration_limit_owns_the_ending(bind_context, no_grace):
    ctx = bind_context(FakeContext())

    call.supervise(max_duration=0.02)
    await asyncio.sleep(0.1)

    assert ended_by(ctx) == call.DURATION


async def test_silence_owns_the_ending(bind_context, no_grace):
    ctx = bind_context(FakeContext())
    session = FakeSession()

    call.supervise(session, end_when_away=True)
    session.go_away()
    await asyncio.sleep(0.05)

    assert ended_by(ctx) == call.SILENCE


async def test_a_call_nobody_answered_was_ended_by_nobody(bind_context):
    """This path knows it before anything hangs up, which is why it claims it."""
    ctx = bind_context(outbound(**RINGING))

    await call.await_pickup(timeout=0.05)

    assert ended_by(ctx) == call.NO_ANSWER


async def test_the_first_claim_is_the_one_that_stands(bind_context, no_grace):
    """An ending has one cause, and the first observer is the closest to it."""
    ctx = bind_context(outbound(**RINGING))

    await call.await_pickup(timeout=0.05)
    await call.end(reason="nobody picked up", wait=False)

    assert ended_by(ctx) == call.NO_ANSWER, "hanging up afterwards is not the ending"


async def test_a_claim_of_its_own_is_the_caller_s_to_make(bind_context, no_grace):
    """A transfer, a tool, a supervisor pulling the call: not ours to name."""
    ctx = bind_context(FakeContext())

    assert call.ended_by("transfer") is True
    assert call.ended_by("agent") is False, "first writer wins"

    await call.end()

    assert _state.state(ctx).ended_by == "transfer"


async def test_a_session_closing_for_a_reason_that_names_nobody_claims_nothing(
    bind_context, no_grace
):
    """A guess about who ended it would be worse than saying nothing."""
    ctx = bind_context(FakeContext())
    session = FakeSession()

    call.supervise(session, end_when_alone=False)
    session.close("error")
    await asyncio.sleep(0.05)

    assert ctx.shutdown_reason is not None
    assert ended_by(ctx) is None


async def test_the_ending_reaches_the_report(bind_context, no_grace):
    from callva.livekit.webhook import payload as _payload

    ctx = bind_context(FakeContext())
    await call.end(reason="done")

    body = _payload.build(_state.state(ctx), event=_payload.ENDED, key="k", status="completed")

    assert body["call"]["ended_by"] == call.AGENT


async def test_a_report_for_a_call_nobody_ended_says_so(bind_context):
    from callva.livekit.webhook import payload as _payload

    ctx = bind_context(FakeContext())

    body = _payload.build(_state.state(ctx), event=_payload.ENDED, key="k", status="completed")

    assert body["call"]["ended_by"] is None, "present always, so there is one shape to handle"


# --- A caller who has gone quiet ---------------------------------------------
#
# The framework has a clock of its own and it answers a different question: one edge, at a
# fixed timeout, fired once per episode of quiet and never re-armed. What the platform lets an
# operator configure is a sequence — wait this long, remind them, wait again, remind them
# again, and only then hang up — and a single edge cannot carry it. So the quiet is measured
# here, from the events the session publishes, and every number comes from the configuration.


supervising = importlib.import_module("callva.livekit.call.supervise")
"""The module, not the function of the same name the package exports over it."""


class Voice:
    """Whatever the caller of ``supervise`` supplies to utter a phrase. Here, a recorder.

    A plain async callable, which is the whole contract: a phrase in, nothing out. The package
    never speaks, and what is under test is that it asks.

    ``None`` is recorded rather than dropped, because it is an ask like any other: it is the
    package saying that the operator wrote no phrase for this reminder, and whatever is on this
    side of the callable is expected to find words of its own. A fake that swallowed it would
    pass for a package that never asked at all.
    """

    def __init__(self, raises: Exception | None = None) -> None:
        self.said: list[str | None] = []
        self._raises = raises

    async def __call__(self, phrase: str | None) -> None:
        self.said.append(phrase)
        if self._raises is not None:
            raise self._raises


@pytest.fixture
def brisk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same behaviour on a clock a test can wait out."""
    monkeypatch.setattr(supervising, "PROMPT_GRACE", 0.01)
    monkeypatch.setattr(supervising, "QUIET_TICK", 0.01)


@pytest.fixture
def patient(monkeypatch: pytest.MonkeyPatch) -> None:
    """A grace period long enough for a call to end inside one, which is the point of it.

    Three seconds on a real call, and three seconds is a long time on a telephone line: long
    enough for the caller to hang up, for a duration cap to expire, or for the agent's own tool
    to end the call. Scaled, not shortened, so the window is still a window.
    """
    monkeypatch.setattr(supervising, "PROMPT_GRACE", 0.4)
    monkeypatch.setattr(supervising, "QUIET_TICK", 0.01)


def watch(ctx: Any, session: Any, **configured: Any) -> Voice:
    """Supervise a call the way an operator configured it, and hand back what was said."""
    voice = Voice()
    call.supervise(session, ctx=ctx, utter=voice, **configured)
    return voice


async def test_an_operator_who_configured_nothing_is_watched_for_nothing(
    bind_context, no_grace, brisk
):
    """The platform's zero means no limit, and the package turns it into no clock at all.

    Not a clock with a generous number in it. An agent nobody configured a silence timeout for
    is one the production worker never started a silence monitor for, and inventing a default
    here would hang up on callers whose operator asked for nothing of the sort.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(ctx, session)
    await asyncio.sleep(0.2)

    assert voice.said == []
    assert ctx.shutdown_reason is None
    # Nothing is even listening: the events the clock runs on are not subscribed to.
    assert "user_state_changed" not in session.handlers
    assert "user_input_transcribed" not in session.handlers


async def test_a_quiet_caller_is_reminded_up_to_the_cap_and_then_hung_up_on(
    bind_context, no_grace, brisk
):
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you still there?"],
        max_prompt_attempts=2,
        call_silence_timeout=0.05,
    )
    await asyncio.sleep(0.6)

    assert voice.said == ["are you still there?", "are you still there?"]
    assert ctx.deleted_room is True
    assert "nobody said anything" in (ctx.shutdown_reason or "")
    # The ending is the one this package already owns a word for.
    assert _state.state(ctx).ended_by == call.SILENCE


async def test_the_phrase_comes_from_the_operator_and_is_one_of_theirs(
    bind_context, no_grace, brisk
):
    ctx = bind_context(FakeContext())

    voice = watch(
        ctx,
        FakeSession(),
        silence_timeout=0.02,
        prompt_phrases=["hello?", "can you hear me?"],
        max_prompt_attempts=3,
        call_silence_timeout=5.0,
    )
    await asyncio.sleep(0.3)

    assert voice.said and set(voice.said) <= {"hello?", "can you hear me?"}


async def test_reminders_configured_with_no_phrase_are_uttered_with_nothing(
    bind_context, no_grace, brisk, caplog
):
    """The reminder still happens, and this package still chooses no words for it.

    An operator who asked for two reminders and wrote none of the words is not an operator who
    asked for silence — they asked for the caller to be checked on. What this package may not
    do is answer that by inventing a sentence: it would be words of its own, in a language of
    its own choosing, in their agent's mouth. So the ask goes out carrying ``None``, which says
    that nothing was written, and whoever is doing the speaking answers for what is said.

    Nothing about this is an error. It is a configuration that works.
    """
    ctx = bind_context(FakeContext())

    with caplog.at_level(logging.INFO, logger="callva.livekit"):
        voice = watch(
            ctx,
            FakeSession(),
            silence_timeout=0.05,
            max_prompt_attempts=2,
            call_silence_timeout=0.05,
        )
        await asyncio.sleep(0.6)

    assert voice.said == [None, None], "the reminders were not asked for, or words were invented"
    assert ctx.deleted_room is True
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_second_episode_of_quiet_is_asked_for_again_with_nothing_written(
    bind_context, no_grace, brisk
):
    """The reminders belong to an episode, and nothing about that turns on having phrases.

    A caller who answers and goes quiet again gets the whole set back. With no phrase written
    that is the same set of asks, each one carrying ``None`` — so whatever composes the words
    is asked again rather than once per call.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        max_prompt_attempts=1,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.15)
    assert voice.said == [None], "the first reminder never happened"

    session.transcribe("still here", is_final=True)
    await asyncio.sleep(0.2)

    assert voice.said == [None, None], "the caller answering did not give the reminder back"


async def test_reminders_with_nothing_to_utter_them_are_still_counted_and_still_end(
    bind_context, no_grace, brisk, caplog
):
    """Timing, counting and ending are this package's; the voice is not, and may be absent."""
    ctx = bind_context(FakeContext())

    with caplog.at_level(logging.INFO, logger="callva.livekit"):
        call.supervise(
            FakeSession(),
            ctx=ctx,
            silence_timeout=0.05,
            prompt_phrases=["are you there?"],
            max_prompt_attempts=2,
            call_silence_timeout=0.05,
        )
        await asyncio.sleep(0.6)

    counted = [r.getMessage() for r in caplog.records if "goes unsaid" in r.getMessage()]
    assert len(counted) == 2, "the reminders were not counted"
    assert ctx.deleted_room is True
    # Warning, though the outcome matches the test above: nobody passed a callable, which is
    # how this deployment assembled its worker and not anything the operator wrote. Error is
    # the floor the collector reads and a record at it is delivered to the operator inside
    # `call.ended`, so it is reserved for what they can act on.
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1 and "nothing was supplied" in said[0]
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_a_caller_who_speaks_inside_the_grace_period_is_not_reminded(
    bind_context, no_grace, monkeypatch
):
    """A pause mid-thought sounds exactly like an absence until it ends.

    The reminder is decided and then held for a moment, and anything at all in that moment
    drops it. Without the hold, a caller drawing breath is talked over by their own agent.
    """
    monkeypatch.setattr(supervising, "PROMPT_GRACE", 0.15)
    monkeypatch.setattr(supervising, "QUIET_TICK", 0.01)
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.1)
    session.start_speaking()
    await asyncio.sleep(0.2)

    assert voice.said == [], "the caller was talked over"
    assert ctx.shutdown_reason is None


def cough_by_voice(session: FakeSession) -> None:
    """A noise: the detector hears it start and stop, and no words come of it."""
    session.start_speaking()
    session.stop_talking()


def cough_by_transcript(session: FakeSession) -> None:
    """The recogniser saying it heard a sound and found nothing in it."""
    session.transcribe("", is_final=True)


@pytest.mark.parametrize(
    ("cough", "what"),
    [
        (cough_by_voice, "voice detection heard a noise"),
        (cough_by_transcript, "the recogniser heard a sound and no words in it"),
    ],
)
async def test_a_cough_moves_the_clock_and_buys_back_no_reminder(
    bind_context, no_grace, brisk, cough, what
):
    """Two measurements, two kinds of evidence, and conflating them gets one of them wrong.

    The clock asks whether anybody is making a sound, because what it prevents is talking over
    somebody — so a noise moves it. The count asks whether they answered, and a noise is not an
    answer: a cough that bought back a reminder would hand a caller who is not there an endless
    supply of them, and the reminders would be worth little.

    The production worker draws the line in the same place, between the voice detector and the
    recogniser. It is drawn one step further here: an empty transcript is the recogniser
    reporting a sound it found no words in, which is the cough again.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.1,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=1,
        call_silence_timeout=10.0,
    )

    await asyncio.sleep(0.06)
    cough(session)
    await asyncio.sleep(0.07)
    # The reminder was due at 0.1 and the cough pushed it out to 0.16.
    assert voice.said == [], f"{what}, and the caller was talked over anyway"

    await asyncio.sleep(0.15)
    assert len(voice.said) == 1, "the clock never resumed"

    cough(session)
    await asyncio.sleep(0.4)
    assert len(voice.said) == 1, f"{what}, and it returned a reminder that was spent"


@pytest.mark.parametrize(
    ("is_final", "what"),
    [(False, "mid-sentence, before the final transcript lands"), (True, "a finished turn")],
)
async def test_words_from_the_caller_return_the_reminders(
    bind_context, no_grace, brisk, is_final, what
):
    """The line is noise against words, not interim against final.

    Somebody mid-sentence is speaking, and an interim transcript carrying text is exactly that.
    Waiting for the final one would hold a caller who is plainly answering to a count spent
    while they were quiet.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=1,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.15)
    assert len(voice.said) == 1, "the first reminder never happened"

    session.transcribe("I am here", is_final=is_final)
    await asyncio.sleep(0.3)

    assert len(voice.said) == 2, f"{what} did not return the spent reminder"


async def test_the_agent_speaking_moves_the_clock_and_returns_nothing(
    bind_context, no_grace, brisk
):
    """This side of the call is a sign of life and never an answer.

    A caller listening to a long answer is not a caller who has gone, so the clock waits for
    them. A caller who has heard three answers and said nothing to any of them has still said
    nothing, so the count does not move.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.1,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=1,
        call_silence_timeout=10.0,
    )

    await asyncio.sleep(0.06)
    session.agent_speaks()
    session.stop_speaking()
    await asyncio.sleep(0.07)

    assert voice.said == [], "the agent was talked over by its own supervisor"

    await asyncio.sleep(0.15)
    assert len(voice.said) == 1

    session.agent_speaks()
    session.stop_speaking()
    await asyncio.sleep(0.4)

    assert len(voice.said) == 1, "the agent speaking returned a reminder the caller never earned"


async def test_a_tool_in_flight_is_not_quiet(bind_context, no_grace, brisk):
    """The agent is working, and whatever it finds will be spoken when it lands.

    The framework refuses to arm its own timer while a tool is running and restarts the window
    when the last one lands. Both halves are read here off the event it publishes, because the
    register of running tools it keeps is its own.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=0.05,
    )
    session.run_tool("tool_call_started")
    await asyncio.sleep(0.3)

    assert voice.said == [], "the caller was reminded while the agent was working"
    assert ctx.shutdown_reason is None

    session.run_tool("tool_call_ended")
    await asyncio.sleep(0.3)

    assert voice.said, "the clock never restarted after the tool landed"


@pytest.mark.parametrize(
    ("unheard", "why"),
    [
        ({"agent_state": "initializing"}, "the session has not started"),
        ({"audio_enabled": False}, "the caller's audio is switched off"),
    ],
)
async def test_the_clock_does_not_run_before_the_caller_is_on_the_call(
    bind_context, no_grace, brisk, unheard, why
):
    """An outbound call rings before anybody answers, and ringing is not silence.

    Nor is it patience spent: when the call does begin, the caller gets the whole timeout and
    not what is left of it. A clock that had been counting through the ringing would remind a
    caller who had said nothing yet because they had only just picked up.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession(**unheard)

    voice = watch(
        ctx,
        session,
        silence_timeout=0.1,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.3)

    assert voice.said == [], why
    assert ctx.shutdown_reason is None

    session.agent_state = "listening"
    session.input.audio_enabled = True
    await asyncio.sleep(0.06)

    assert voice.said == [], "the quiet before the call was counted against the caller"

    await asyncio.sleep(0.15)
    assert voice.said, "the clock never started"


async def test_a_late_transcript_counts_as_the_caller_having_spoken(bind_context, no_grace, brisk):
    """Voice detection can miss speech the recogniser heard, and then the transcript is all.

    The framework refreshes its own timer on exactly this, for exactly this reason. A clock
    that watched only the detector would remind a caller who was talking the whole time.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.1,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.06)
    session.transcribe("I am here")
    await asyncio.sleep(0.07)

    assert voice.said == [], "a caller the recogniser heard was reminded anyway"

    await asyncio.sleep(0.2)
    assert voice.said, "the clock never resumed"


@pytest.mark.parametrize(
    ("transcript", "is_final", "why"),
    [
        ("I am", False, "the caller is mid-sentence and the final has not landed yet"),
        ("", True, "the recogniser heard something and made nothing of it"),
    ],
)
async def test_anything_at_all_from_the_recogniser_is_the_caller_being_there(
    bind_context, no_grace, brisk, transcript, is_final, why
):
    """Interim guesses included, and empty ones. The bar is a sound, not a sentence.

    An interim transcript is the recogniser saying somebody is talking right now, which is the
    worst possible moment to talk over them — and it is the moment a rule that waited for a
    final transcript would pick. The production worker resets on every transcribed event and
    the framework refreshes its own timer on a final one with or without words; being stricter
    than either is how a caller mid-sentence gets asked whether they are still there.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.1,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.06)
    session.transcribe(transcript, is_final=is_final)
    await asyncio.sleep(0.07)

    assert voice.said == [], why

    await asyncio.sleep(0.2)
    assert voice.said, "the clock never resumed"


async def test_the_recogniser_forgives_the_reminders_already_spent(bind_context, no_grace, brisk):
    """Words the voice detector never reported are still words.

    Detection can miss speech the recogniser heard, and a caller whose every turn arrived that
    way would otherwise be hung up on with a count that never went back to zero.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=1,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.15)
    assert len(voice.said) == 1, "the first reminder never happened"

    session.transcribe("mm", is_final=False)
    await asyncio.sleep(0.2)

    assert len(voice.said) == 2, "one attempt was configured and the transcript did not return it"


async def test_the_framework_calling_the_caller_away_is_not_the_caller_stirring(
    bind_context, no_grace, brisk
):
    """``away`` is the framework's verdict that the caller has *not* stirred.

    On a session that still has its own timer this arrives mid-quiet, and reading it as
    activity would add the framework's timeout to the operator's — a first reminder at
    forty-five seconds where thirty was configured, with nothing to say why.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.1,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.05)
    session.go_away()
    await asyncio.sleep(0.08)

    assert voice.said, "the away edge pushed the reminder back by a timeout nobody configured"


# --- The watch and the call it watches end together --------------------------------------


async def test_a_caller_who_hangs_up_is_not_spoken_to_afterwards(bind_context, no_grace, patient):
    """The grace period is a wait with an utterance on the far side of it.

    A reminder decided at the last quiet moment of a call is uttered three seconds later, and
    in those three seconds the caller can hang up, the duration cap can expire, or the agent's
    own tool can end the call. The room is then deleted and the job is shutting down, and a
    stack asked to speak is asked to speak into it.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=0.05,
    )
    # Quiet long enough that the reminder is decided, then gone before the grace period is out.
    await asyncio.sleep(0.15)
    ctx.room.emit_participant_disconnected(FakeParticipant("caller"))
    await asyncio.sleep(0.8)

    st = _state.state(ctx)
    assert st.ending is True and st.ended_by == call.USER
    assert voice.said == [], "the caller was spoken to after they had hung up"


async def test_a_call_that_ended_some_other_way_is_not_reminded_either(
    bind_context, no_grace, patient
):
    """The same window, reached through the duration cap rather than through the caller.

    Anything that sets the ending inside the grace period closes it, which is why the check is
    on the wait and not on the hangup: the watch does not have to know what ended the call.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=10.0,
        max_duration=0.2,
    )
    await asyncio.sleep(0.8)

    assert _state.state(ctx).ended_by == call.DURATION
    assert voice.said == [], "the limit ended the call and the caller was reminded anyway"


async def test_a_closed_session_stops_being_watched(bind_context, no_grace, brisk):
    """A finished call must not leave a task spinning for as long as the worker lives.

    A closed session reports its agent state as ``initializing`` and never leaves it, so a
    watch that only asked "is there a call on" would wake, decide there is not, restart the
    clock and sleep again, forever. ``end_when_closed`` is off here on purpose: that is the one
    close this package does not turn into an ending of its own, so it is the one that proves
    the watch stops on the close itself rather than on the hangup that usually follows it.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=0.05,
        end_when_closed=False,
    )
    await asyncio.sleep(0.02)
    session.close()
    await asyncio.sleep(0.3)

    assert voice.said == []
    assert ctx.shutdown_reason is None
    assert supervising._QUIET_TASK not in _state.state(ctx).extras, "the watch is still running"


# --- Two clocks, and a patience that ends nothing ----------------------------------------


async def test_arming_both_clocks_on_one_call_is_said_out_loud(bind_context, caplog):
    """``end_when_away`` beside a configured timeout is two answers to one question.

    Both work, which is the problem: the framework's fixed fifteen seconds beats whatever the
    operator set, so the call is ended by the clock nobody configured. Nothing warns about it
    anywhere else, and the two combined are silently wrong rather than loudly wrong.
    """
    ctx = bind_context(FakeContext())

    with caplog.at_level(logging.INFO, logger="callva.livekit"):
        call.supervise(FakeSession(), ctx=ctx, end_when_away=True, silence_timeout=30.0)
    call.stop(ctx)

    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warned and "two clocks" in warned[0]


async def test_a_call_configured_to_remind_and_carry_on_does_exactly_that(
    bind_context, no_grace, brisk, caplog
):
    """Remind the caller, then let the conversation continue. An ordinary thing to configure.

    The production worker falls back to thirty seconds where no final patience is set, and that
    fallback is a default held in a worker, which this package may not invent. Nothing is left
    dangling by leaving it out: the call still ends, at the duration limit every agent carries.
    So the zero is honoured and nothing is said about it — a call configured this way is not a
    call configured wrongly, and a line calling it one would be this package second-guessing
    the operator in their own report.
    """
    ctx = bind_context(FakeContext())

    with caplog.at_level(logging.DEBUG, logger="callva.livekit"):
        voice = watch(
            ctx,
            FakeSession(),
            silence_timeout=0.05,
            prompt_phrases=["are you there?"],
            max_prompt_attempts=1,
        )
        await asyncio.sleep(0.4)

    assert voice.said == ["are you there?"]
    assert ctx.shutdown_reason is None, "a timeout nobody configured ended the call"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_stack_that_raises_when_asked_does_not_take_the_call_with_it(
    bind_context, no_grace, brisk, caplog
):
    ctx = bind_context(FakeContext())
    voice = Voice(raises=RuntimeError("the provider refused"))

    with caplog.at_level(logging.INFO, logger="callva.livekit"):
        call.supervise(
            FakeSession(),
            ctx=ctx,
            silence_timeout=0.05,
            prompt_phrases=["are you there?"],
            max_prompt_attempts=1,
            call_silence_timeout=0.05,
            utter=voice,
        )
        await asyncio.sleep(0.5)

    # It was asked, it raised, the attempt was spent anyway and the call still ended.
    assert voice.said == ["are you there?"]
    assert ctx.deleted_room is True
    assert any(r.exc_info for r in caplog.records if r.levelno >= logging.ERROR)


async def test_the_quiet_is_no_longer_watched_once_the_call_is_over(bind_context, no_grace, brisk):
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=0.05,
    )
    call.stop(ctx)
    await asyncio.sleep(0.3)

    assert voice.said == []
    assert ctx.shutdown_reason is None
    # And the session is not left holding listeners that outlive the watch.
    assert session.handlers.get("user_state_changed") is None


async def test_what_end_when_away_leans_on_is_a_session_setting_that_can_be_off():
    """Read off a constructed session, not off a comment about one.

    ``user_away_timeout`` is what produces the ``away`` edge. Left alone it is fifteen seconds,
    which is the timeout a caller thinking is hung up on by; set to ``None`` the session holds
    nothing, emits no edge, and ``end_when_away`` on it can never fire. Both halves are facts
    about the framework, and they are why a deployment that measures its own quiet turns the
    setting off and leaves ``end_when_away`` alone.
    """
    from livekit.agents import AgentSession

    assert inspect.signature(AgentSession.__init__).parameters["user_away_timeout"].default == 15.0
    assert AgentSession(user_away_timeout=None).options.user_away_timeout is None


# --- The watch, the hangup, and the listeners all end together ---------------------------


async def test_a_session_closing_mid_hangup_does_not_abandon_the_hangup(
    bind_context, brisk, monkeypatch
):
    """The window is real and it is the ordinary one: the caller hangs up as we hang up.

    ``end`` sets the ending flag first and does the work afterwards — it waits for the agent to
    stop speaking, then for a grace period, and only then deletes the room and shuts the job
    down. A close arriving inside that stretch used to cancel the watch mid-hangup, and every
    other watcher then declined to hang up because the flag told it somebody already was. The
    room was never deleted and the caller held an open line to an empty room.

    The grace is scaled here rather than removed, because the window *is* the grace: a test
    that took it away would be testing a hangup with no middle.
    """
    monkeypatch.setattr(call.release, "GRACE", 0.2)
    ctx = bind_context(FakeContext())
    session = FakeSession()
    st = _state.state(ctx)

    watch(ctx, session, silence_timeout=0.05, call_silence_timeout=0.05)

    for _ in range(200):
        await asyncio.sleep(0.005)
        if st.ending and not ctx.deleted_room:
            break
    assert st.ending and not ctx.deleted_room, "the window under test never opened"

    session.close()
    await asyncio.sleep(0.6)

    assert ctx.deleted_room is True, "the caller was left in a room nobody deleted"
    assert "nobody said anything" in (ctx.shutdown_reason or "")
    assert st.ended_by == call.SILENCE


async def test_the_listeners_come_off_when_the_watch_finishes(bind_context, no_grace, brisk):
    """A session holding handlers for a watch that is over holds the closures they were written
    in, and nothing outside the watch was ever going to take them off."""
    ctx = bind_context(FakeContext())
    session = FakeSession()

    watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=1,
        call_silence_timeout=0.05,
    )
    await asyncio.sleep(0.6)

    assert ctx.deleted_room is True
    # Only the close watch that ends a call is left, which is not this one's and stays.
    assert set(session.handlers) == {"close"}
    assert len(session.handlers["close"]) == 1
    assert supervising._QUIET_DETACH not in _state.state(ctx).extras


# --- Quiet is one question, asked the same way wherever it is asked -----------------------


async def test_a_tool_that_starts_inside_the_grace_period_is_not_spoken_over(
    bind_context, no_grace, patient
):
    """The reminder is decided before the grace and uttered after it, and a tool can start in
    between: the model reaches for something at the last quiet moment and the answer lands a
    second later, under "are you still there?".

    The gate before the clock is read and the re-check after the grace are the same question at
    two moments, so they are the same predicate. Two lists that were meant to be one list are
    how a tool ended up counting at the top and not at the bottom.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.15)
    session.run_tool("tool_call_started")
    await asyncio.sleep(0.5)

    assert voice.said == [], "the caller was spoken over by a reminder decided before the tool"

    session.run_tool("tool_call_ended")
    await asyncio.sleep(0.7)

    assert voice.said, "the clock never restarted after the tool landed"


async def test_a_caller_who_goes_off_the_call_inside_the_grace_period_is_not_spoken_to(
    bind_context, no_grace, patient
):
    """The other half of the same predicate, at the same moment. A caller who can no longer be
    heard is not a caller who is refusing to answer, and the wait is long enough for one to
    become the other."""
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=10.0,
    )
    await asyncio.sleep(0.15)
    session.input.audio_enabled = False
    await asyncio.sleep(0.5)

    assert voice.said == []

    session.input.audio_enabled = True
    await asyncio.sleep(0.7)

    assert voice.said, "the clock never restarted once the caller was back"


# --- Reminders belong to an episode of quiet -------------------------------------------


async def test_the_reminders_are_a_caller_going_quiet_and_not_a_budget_for_the_call(
    bind_context, no_grace, brisk
):
    """Somebody who stops talking is reminded; somebody who comes back and stops again is
    reminded again.

    A thirty-second timeout, quiet at one minute, two reminders, the caller back at two and
    talking for five — and then quiet again at seven with nothing left, on a count spent six
    minutes earlier. Clearing the count when they speak already says the reminders are theirs
    afresh; a watch that gave up after the first episode said the opposite.
    """
    ctx = bind_context(FakeContext())
    session = FakeSession()

    voice = watch(
        ctx,
        session,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
    )
    await asyncio.sleep(0.35)
    assert len(voice.said) == 2, "the first episode was not reminded as configured"

    session.transcribe("sorry, I am still here")
    await asyncio.sleep(0.35)

    assert len(voice.said) == 4, "the second episode of quiet got no reminders of its own"
    assert ctx.shutdown_reason is None, "a timeout nobody configured ended the call"


async def test_a_call_with_nothing_to_do_about_the_quiet_is_not_watched_for_it(
    bind_context, no_grace, brisk
):
    """A timeout with no reminders behind it and no ending in front of it can never act."""
    ctx = bind_context(FakeContext())
    session = FakeSession()

    watch(ctx, session, silence_timeout=0.05)
    await asyncio.sleep(0.2)

    assert ctx.shutdown_reason is None
    assert "user_state_changed" not in session.handlers


# --- The utterance is somebody else's coroutine ------------------------------------------


class Hangs:
    """An ``utter`` that takes the phrase and never comes back.

    Not a contrived shape: the callable is the caller's, this package knows nothing about what
    is on the other side of it, and a network that stops answering looks exactly like this.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, phrase: str) -> None:
        self.asked.append(phrase)
        await asyncio.sleep(3600)


async def test_an_utterance_that_never_returns_does_not_park_the_watch(
    bind_context, no_grace, brisk, monkeypatch
):
    """The one await in the watch that is a stranger's coroutine, and so the one with a bound.

    An unbounded wait here is a watch parked for the life of the process: it measures no quiet,
    ends no call, never reaches its own clean-up and holds five listeners on a session that is
    long gone. A worker takes many calls.

    Giving up on the ask is not a failure of the call. The attempt is spent, because it was
    made, and the call goes on to its configured ending.
    """
    monkeypatch.setattr(supervising, "UTTERANCE_TIMEOUT", 0.05)
    ctx = bind_context(FakeContext())
    session = FakeSession()
    voice = Hangs()

    call.supervise(
        session,
        ctx=ctx,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=0.05,
        utter=voice,
    )
    await asyncio.sleep(0.9)

    assert len(voice.asked) == 2, "the attempts were not spent"
    assert ctx.deleted_room is True, "the call never reached its ending"
    assert supervising._QUIET_TASK not in _state.state(ctx).extras, "the watch is still parked"
    assert set(session.handlers) == {"close"}, "the listeners were never taken off"


async def test_a_call_that_ends_while_the_utterance_hangs_still_lets_the_watch_go(
    bind_context, no_grace, brisk, monkeypatch
):
    """The caller hangs up while the watch is inside the ask, which is where the bound matters.

    Nothing else can free it. The hangup sets the ending flag, so every canceller declines —
    deliberately, because a task cancelled mid-hangup abandons one — and the watch is not at a
    wait that re-reads the flag. It is at somebody else's await. Bounding that one is what
    turns "declining to cancel is safe" from a hope into a fact.
    """
    monkeypatch.setattr(supervising, "UTTERANCE_TIMEOUT", 0.3)
    ctx = bind_context(FakeContext())
    session = FakeSession()
    voice = Hangs()
    st = _state.state(ctx)

    call.supervise(
        session,
        ctx=ctx,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=10.0,
        utter=voice,
    )
    for _ in range(200):
        await asyncio.sleep(0.005)
        if voice.asked:
            break
    assert voice.asked, "the ask never went out, so the window under test never opened"

    ctx.room.emit_participant_disconnected(FakeParticipant("caller"))
    await asyncio.sleep(0.8)

    assert st.ending is True and ctx.deleted_room is True
    assert supervising._QUIET_TASK not in st.extras, "the watch is still parked in the ask"
    assert set(session.handlers) == {"close"}, "the listeners were never taken off"


async def test_giving_up_on_a_reminder_is_reported_and_does_not_end_the_call(
    bind_context, no_grace, brisk, monkeypatch, caplog
):
    """A caller who heard nothing is the same fact whether the ask raised or simply hung."""
    monkeypatch.setattr(supervising, "UTTERANCE_TIMEOUT", 0.05)
    ctx = bind_context(FakeContext())
    voice = Hangs()

    with caplog.at_level(logging.INFO, logger="callva.livekit"):
        call.supervise(
            FakeSession(),
            ctx=ctx,
            silence_timeout=0.05,
            prompt_phrases=["are you there?"],
            max_prompt_attempts=1,
            utter=voice,
        )
        await asyncio.sleep(0.4)

    assert len(voice.asked) == 1
    assert ctx.shutdown_reason is None, "the call was ended over a reminder"
    failed = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(failed) == 1 and "not handed over" in failed[0]
