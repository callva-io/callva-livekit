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
    assert _state.state(ctx).extras[call.PICKUP_FAILURE] == "timeout"


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
    assert st.extras[call.PICKUP_FAILURE] == "USER_REJECTED"
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
    assert _state.state(ctx).extras[call.PICKUP_FAILURE] == "hangup"


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
