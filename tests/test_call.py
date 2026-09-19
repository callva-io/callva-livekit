from __future__ import annotations

import asyncio
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
