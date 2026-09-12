from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from fakes import DEFAULT_SIP_ATTRIBUTES as DEFAULT_SIP
from fakes import FakeContext, FakeParticipant, FakeReport, envelope_metadata

from callva.livekit import webhook as callva_webhook
from callva.livekit.core import state as _state
from callva.livekit.core.transport import WebhookTarget
from callva.livekit.webhook import service


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture deliveries instead of making them."""
    captured: list[dict[str, Any]] = []

    async def post_json(target: Any, *, event: str, payload: dict, key: str, **_: Any) -> bool:
        captured.append({"kind": "json", "target": target, "event": event, "payload": payload})
        return True

    async def post_file(target: Any, *, event: str, payload: dict, **kwargs: Any) -> bool:
        captured.append(
            {"kind": "file", "target": target, "event": event, "payload": payload, **kwargs}
        )
        return True

    monkeypatch.setattr(service.transport, "post_json", post_json)
    monkeypatch.setattr(service.transport, "post_file", post_file)
    return captured


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> WebhookTarget:
    monkeypatch.setenv("WEBHOOK_URL", "https://example.test/hook")
    return WebhookTarget("https://example.test/hook")


async def live_call(ctx: FakeContext) -> None:
    """Run the participant entrypoints the way the SDK would."""
    for entrypoint in ctx.participant_entrypoints:
        await entrypoint(ctx, ctx._participant)


def test_attach_registers_both_hooks(bind_context):
    ctx = bind_context(FakeContext())

    callva_webhook.attach()

    assert len(ctx.participant_entrypoints) == 1
    assert len(ctx.shutdown_callbacks) == 1


def test_attaching_twice_does_nothing_the_second_time(bind_context):
    ctx = bind_context(FakeContext())

    callva_webhook.attach()
    callva_webhook.attach()

    assert len(ctx.participant_entrypoints) == 1


async def test_a_live_call_reports_that_it_started(bind_context, sent, target):
    ctx = bind_context(FakeContext())
    callva_webhook.attach()

    await live_call(ctx)

    assert len(sent) == 1
    body = sent[0]["payload"]
    assert body["event"] == "call.started"
    assert body["call"]["status"] == "in_progress"
    assert body["call"]["direction"] == "inbound"
    assert body["call"]["from"]["number"] == "+37255512345"
    assert body["call"]["to"]["number"] == "+3726001234"
    assert body["id"] == sent[0]["payload"]["id"]
    assert "session_report" not in body["livekit"]


async def test_livekit_data_is_nested_verbatim(bind_context, sent, target):
    ctx = bind_context(FakeContext())
    callva_webhook.attach()

    await live_call(ctx)

    livekit = sent[0]["payload"]["livekit"]
    assert livekit["room"]["name"] == "call-1"
    assert livekit["sip"]["callID"] == "abc"
    assert livekit["sip"]["callStatus"] == "active"
    assert livekit["participant"]["identity"] == "sip_+37255512345"


async def test_a_second_participant_does_not_start_the_call_again(bind_context, sent, target):
    ctx = bind_context(FakeContext())
    callva_webhook.attach()

    await live_call(ctx)
    await live_call(ctx)

    assert len(sent) == 1


async def test_the_finished_call_carries_the_session_report(bind_context, sent, target):
    ctx = bind_context(FakeContext())
    ctx.report = FakeReport()
    callva_webhook.attach()

    await live_call(ctx)
    await callva_webhook.on_session_end(ctx)

    ended = sent[1]["payload"]
    assert ended["event"] == "call.ended"
    assert ended["call"]["status"] == "completed"
    assert ended["livekit"]["session_report"]["sdk_version"] == "1.5.7"
    assert ended["call"]["duration"] is not None
    assert ended["call"]["id"] == sent[0]["payload"]["call"]["id"], "one id across both events"


async def test_the_shutdown_fallback_does_not_send_a_second_time(bind_context, sent, target):
    ctx = bind_context(FakeContext())
    ctx.report = FakeReport()
    callva_webhook.attach()

    await live_call(ctx)
    await callva_webhook.on_session_end(ctx)
    for callback in ctx.shutdown_callbacks:
        await callback("done")

    assert [item["payload"]["event"] for item in sent] == ["call.started", "call.ended"]


async def test_the_fallback_path_reports_it_is_on_a_short_budget(
    bind_context, sent, target, caplog
):
    ctx = bind_context(FakeContext())
    ctx.report = FakeReport()
    callva_webhook.attach()
    await live_call(ctx)

    with caplog.at_level(logging.WARNING, logger="callva.livekit"):
        for callback in ctx.shutdown_callbacks:
            await callback("done")

    assert "shutdown_process_timeout" in caplog.text
    assert [item["payload"]["event"] for item in sent] == ["call.started", "call.ended"]


async def test_a_call_with_no_target_configured_is_not_an_error(bind_context, sent):
    ctx = bind_context(FakeContext())
    ctx.report = FakeReport()
    callva_webhook.attach()

    await live_call(ctx)
    await callva_webhook.on_session_end(ctx)

    assert sent == []


async def test_a_session_that_never_started_still_reports_the_call(bind_context, sent, target):
    ctx = bind_context(FakeContext())
    callva_webhook.attach()
    await live_call(ctx)

    await callva_webhook.on_session_end(ctx)

    ended = sent[1]["payload"]
    assert ended["event"] == "call.ended"
    assert ended["livekit"]["session_report"] is None


def test_target_precedence(bind_context, monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://from-env.test/hook")
    ctx = bind_context(
        FakeContext(envelope_metadata(webhook={"url": "https://from-dispatch.test/hook"}))
    )
    st = _state.state(ctx)

    assert callva_webhook.resolve_target(st).url == "https://from-dispatch.test/hook"

    st.webhook = WebhookTarget("https://from-config.test/hook")
    assert callva_webhook.resolve_target(st).url == "https://from-config.test/hook"


def test_the_environment_is_the_last_resort(bind_context, monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://from-env.test/hook")
    ctx = bind_context(FakeContext())

    assert callva_webhook.resolve_target(_state.state(ctx)).url == "https://from-env.test/hook"


async def test_an_explicit_target_overrides_everything(bind_context, sent, monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://from-env.test/hook")
    ctx = bind_context(FakeContext())

    callva_webhook.attach(target=WebhookTarget("https://explicit.test/hook"))
    await live_call(ctx)

    assert sent[0]["target"].url == "https://explicit.test/hook"


async def test_the_recording_url_is_known_before_the_bytes_move(
    bind_context, sent, target, monkeypatch, tmp_path
):
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(b"ogg")

    uploads: list[tuple[str, Any]] = []

    class FakeStorage:
        bucket = "calls"

        @classmethod
        def from_env(cls) -> FakeStorage:
            return cls()

        def key(self, name: str) -> str:
            return f"recordings/{name}"

        def public_url(self, key: str) -> str:
            return f"https://cdn.test/{key}"

        async def put_file(self, key: str, path: Path, _type: str) -> bool:
            uploads.append(("file", key))
            return True

        async def put_json(self, key: str, document: Any) -> bool:
            uploads.append(("json", key))
            return True

    monkeypatch.setattr(service, "Storage", FakeStorage)

    ctx = bind_context(FakeContext())
    ctx.report = FakeReport(audio_recording_path=audio)
    callva_webhook.attach()

    await live_call(ctx)
    await callva_webhook.on_session_end(ctx)

    call_id = sent[0]["payload"]["call"]["id"]
    ended = sent[1]["payload"]

    assert ended["recording"]["url"] == f"https://cdn.test/recordings/{call_id}.ogg"
    assert uploads == [
        ("file", f"recordings/{call_id}.ogg"),
        ("json", f"recordings/{call_id}.session.json"),
    ], "both are filed under the call id, and the report says which of them it is"
    assert ended["recording"]["session_report_key"] == f"recordings/{call_id}.session.json"


async def test_without_storage_the_recording_follows_the_webhook(
    bind_context, sent, target, tmp_path
):
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(b"ogg")

    ctx = bind_context(FakeContext())
    ctx.report = FakeReport(audio_recording_path=audio)
    callva_webhook.attach()

    await live_call(ctx)
    await callva_webhook.on_session_end(ctx)

    assert sent[1]["payload"]["recording"]["delivery"] == "multipart"
    assert sent[2]["kind"] == "file"
    assert sent[2]["event"] == "call.recording"
    assert sent[2]["path"] == audio


async def test_no_recording_at_all(bind_context, sent, target):
    ctx = bind_context(FakeContext())
    ctx.report = FakeReport()
    callva_webhook.attach()

    await live_call(ctx)
    await callva_webhook.on_session_end(ctx)

    assert sent[1]["payload"]["recording"] is None
    assert len(sent) == 2


def test_the_shutdown_callback_can_take_the_reason(bind_context):
    """The SDK reads ``__code__`` to decide whether to pass the shutdown reason.

    A closure works; a functools.partial would have no ``__code__`` and would crash the
    job at registration time.
    """
    ctx = bind_context(FakeContext())
    callva_webhook.attach()

    callback = ctx.shutdown_callbacks[0]
    assert callback.__code__.co_argcount >= 1


async def test_the_fallback_does_not_need_the_ambient_job(bind_context, sent, target, monkeypatch):
    """A shutdown callback runs in its own task; the contextvar may be gone by then."""
    ctx = bind_context(FakeContext())
    ctx.report = FakeReport()
    callva_webhook.attach()
    await live_call(ctx)

    def no_ambient_job():
        raise AssertionError("the fallback must not reach for the ambient job context")

    monkeypatch.setattr(_state, "context", no_ambient_job)

    for callback in ctx.shutdown_callbacks:
        await callback("room disconnected")

    assert [item["payload"]["event"] for item in sent] == ["call.started", "call.ended"]


async def test_a_simulated_job_reports_its_start_without_a_participant(
    bind_context, sent, target
):
    """Console mode runs a mock room nobody joins, so waiting for a participant is waiting
    forever. The call still starts and still has to say so."""
    ctx = FakeContext()
    ctx.fake_job = True
    ctx.report = FakeReport()
    bind_context(ctx)

    callva_webhook.attach()
    await asyncio.sleep(0)

    assert [item["payload"]["event"] for item in sent] == ["call.started"]
    started = sent[0]["payload"]
    assert started["call"]["from"]["number"] is None, "a console call has no parties"
    assert started["call"]["id"]


async def test_a_simulated_call_never_ends_before_it_begins(bind_context, sent, target):
    """A short console session can finish while the start is still in flight."""
    ctx = FakeContext()
    ctx.fake_job = True
    ctx.report = FakeReport()
    bind_context(ctx)

    callva_webhook.attach()
    await callva_webhook.on_session_end(ctx)

    assert [item["payload"]["event"] for item in sent] == ["call.started", "call.ended"]
    assert sent[0]["payload"]["call"]["id"] == sent[1]["payload"]["call"]["id"]


async def test_a_real_job_still_waits_for_someone_to_join(bind_context, sent, target):
    bind_context(FakeContext())

    callva_webhook.attach()
    await asyncio.sleep(0)

    assert sent == [], "nothing to report until the call is actually live"


async def test_a_caller_already_in_the_room_is_not_missed(bind_context, sent, target):
    """An inbound call's participant joins before the job starts.

    The SDK replays already-present participants to participant entrypoints inside
    ctx.connect(), once — before attach() could have registered anything. Waiting on the
    entrypoint would be waiting for a join that already happened, and the call would only
    ever report its end.
    """
    ctx = FakeContext()
    ctx.room.remote_participants = {"sip_+3725258198": ctx._participant}
    ctx.report = FakeReport()
    bind_context(ctx)

    callva_webhook.attach()
    await asyncio.sleep(0)

    assert [item["payload"]["event"] for item in sent] == ["call.started"]
    started = sent[0]["payload"]
    assert started["call"]["from"]["number"] == "+37255512345"
    assert started["call"]["to"]["number"] == "+3726001234"
    assert started["livekit"]["sip"]["callID"] == "abc"


async def test_the_recording_is_keyed_on_the_call_not_on_unknown(bind_context, sent, target):
    """A call that never reported a start still has to key its recording on its own id."""
    ctx = FakeContext()
    ctx.report = FakeReport(audio_recording_path=Path(__file__))
    bind_context(ctx)

    callva_webhook.attach()
    st = _state.state(ctx)
    st.extras.pop("webhook.started_task", None)
    st.started_sent = True  # as if the start was missed entirely

    await callva_webhook.on_session_end(ctx)

    ended = sent[-1]["payload"]
    call_id = ended["call"]["id"]
    assert call_id and call_id != "unknown"
    assert ended["recording"]["filename"] == f"{call_id}.ogg"


async def test_the_live_rooms_async_sid_is_never_read(bind_context, sent, target):
    """rtc.Room.sid is a coroutine; reading it in a sync builder can only leak one."""
    ctx = FakeContext()
    ctx.report = FakeReport()
    bind_context(ctx)

    callva_webhook.attach()
    await callva_webhook.on_session_end(ctx)

    room = sent[-1]["payload"]["livekit"]["room"]
    assert room["name"] == "call-1"
    assert room["sid"] == "RM_test", "taken from the job, where it is a plain string"


async def test_a_ringing_call_is_dialing_not_started(bind_context, sent, target):
    """An outbound participant exists from the first ring.

    Reporting that as the call starting would tell the consumer somebody answered while
    the phone is still ringing. It is still worth saying the dial went out.
    """
    ctx = FakeContext(participant=FakeParticipant(**{**DEFAULT_SIP, "sip.callStatus": "ringing"}))
    ctx.room.remote_participants = {"sip_x": ctx._participant}
    bind_context(ctx)

    callva_webhook.attach()
    await asyncio.sleep(0)

    assert [item["payload"]["event"] for item in sent] == ["call.dialing"]
    assert sent[0]["payload"]["call"]["status"] == "dialing"
    assert sent[0]["payload"]["livekit"]["sip"]["callStatus"] == "ringing"


async def test_the_start_is_reported_the_moment_they_pick_up(bind_context, sent, target):
    caller = FakeParticipant(**{**DEFAULT_SIP, "sip.callStatus": "ringing"})
    ctx = FakeContext(participant=caller)
    ctx.room.remote_participants = {"sip_x": caller}
    bind_context(ctx)

    callva_webhook.attach()
    await asyncio.sleep(0)
    assert [item["payload"]["event"] for item in sent] == ["call.dialing"]

    caller.attributes["sip.callStatus"] = "active"
    ctx.room.emit_attributes_changed({"sip.callStatus": "active"}, caller)
    await asyncio.sleep(0)

    assert [item["payload"]["event"] for item in sent] == ["call.dialing", "call.started"]
    assert sent[-1]["payload"]["call"]["from"]["number"] == "+37255512345"
    assert sent[0]["payload"]["call"]["id"] == sent[1]["payload"]["call"]["id"]


async def test_an_answered_inbound_call_is_not_held(bind_context, sent, target):
    """Inbound is already answered when the participant appears, so the same check lets it
    straight through without anyone having to declare a direction."""
    ctx = FakeContext()
    ctx.room.remote_participants = {"sip_x": ctx._participant}
    bind_context(ctx)

    callva_webhook.attach()
    await asyncio.sleep(0)

    assert [item["payload"]["event"] for item in sent] == ["call.started"]


async def test_dialing_is_said_once_however_many_rings_follow(bind_context, sent, target):
    caller = FakeParticipant(**{**DEFAULT_SIP, "sip.callStatus": "dialing"})
    ctx = FakeContext(participant=caller)
    ctx.room.remote_participants = {"sip_x": caller}
    bind_context(ctx)

    callva_webhook.attach()
    await asyncio.sleep(0)

    caller.attributes["sip.callStatus"] = "ringing"
    ctx.room.emit_attributes_changed({"sip.callStatus": "ringing"}, caller)
    await asyncio.sleep(0)

    assert [item["payload"]["event"] for item in sent] == ["call.dialing"]


async def test_an_answered_call_completed(bind_context, sent, target):
    ctx = FakeContext()
    ctx.room.remote_participants = {"sip_x": ctx._participant}
    ctx.report = FakeReport()
    bind_context(ctx)

    callva_webhook.attach()
    await asyncio.sleep(0)
    await callva_webhook.on_session_end(ctx)

    assert sent[-1]["payload"]["call"]["status"] == "completed"


async def test_how_an_unanswered_call_ended_is_said_in_our_words(bind_context, sent, target):
    """LiveKit writes no callStatus for a refused call — it freezes at ringing and the
    participant vanishes. The outcome has to come from the disconnect reason."""
    cases = {
        "USER_UNAVAILABLE": "no_answer",
        "CONNECTION_TIMEOUT": "no_answer",
        "USER_REJECTED": "rejected",
        "CLIENT_INITIATED": "canceled",
        "SIP_TRUNK_FAILURE": "failed",
        "MEDIA_FAILURE": "failed",
        None: "no_answer",
    }

    for reason, expected in cases.items():
        sent.clear()
        caller = FakeParticipant(**{**DEFAULT_SIP, "sip.callStatus": "ringing"})
        ctx = FakeContext(participant=caller)
        ctx.room.remote_participants = {"sip_x": caller}
        ctx.report = FakeReport()
        bind_context(ctx)

        callva_webhook.attach()
        await asyncio.sleep(0)
        ctx.room.emit_participant_disconnected(caller, reason)
        await callva_webhook.on_session_end(ctx)

        ended = sent[-1]["payload"]
        assert ended["event"] == "call.ended"
        assert ended["call"]["status"] == expected, f"{reason} should read as {expected}"
        assert ended["livekit"].get("disconnect_reason") == reason


async def test_an_unanswered_call_never_claims_it_started(bind_context, sent, target):
    caller = FakeParticipant(**{**DEFAULT_SIP, "sip.callStatus": "ringing"})
    ctx = FakeContext(participant=caller)
    ctx.room.remote_participants = {"sip_x": caller}
    ctx.report = FakeReport()
    bind_context(ctx)

    callva_webhook.attach()
    await asyncio.sleep(0)
    ctx.room.emit_participant_disconnected(caller, "USER_UNAVAILABLE")
    await callva_webhook.on_session_end(ctx)

    assert [item["payload"]["event"] for item in sent] == ["call.dialing", "call.ended"]


async def test_what_went_wrong_reaches_the_consumer(bind_context, monkeypatch):
    """A call that failed still ends and still reports; what was missing was the reason."""
    import logging

    from callva.livekit.webhook import errors as _errors

    sent: list[dict] = []

    async def capture(target, *, event, payload, key, **_):
        sent.append(payload)

    monkeypatch.setattr(service.transport, "post_json", capture)
    monkeypatch.setenv("WEBHOOK_URL", "https://tenant.test/hook")

    ctx = bind_context(FakeContext())
    _errors.collect()
    try:
        logging.getLogger("some.plugin").error("the model refused the session")
        await service.on_session_end(ctx)
    finally:
        _errors.stop()

    assert [e["message"] for e in sent[-1]["errors"]] == ["the model refused the session"]


async def test_a_call_that_went_fine_reports_no_errors(bind_context, monkeypatch):
    sent: list[dict] = []

    async def capture(target, *, event, payload, key, **_):
        sent.append(payload)

    monkeypatch.setattr(service.transport, "post_json", capture)
    monkeypatch.setenv("WEBHOOK_URL", "https://tenant.test/hook")

    await service.on_session_end(bind_context(FakeContext()))

    assert sent[-1]["errors"] is None
