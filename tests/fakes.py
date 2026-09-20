from __future__ import annotations

import json
from typing import Any

DEFAULT_SIP_ATTRIBUTES = {
    "sip.phoneNumber": "+37255512345",
    "sip.trunkPhoneNumber": "+3726001234",
    "sip.callID": "abc",
    "sip.callStatus": "active",
}


class FakeParticipant:
    def __init__(self, identity: str = "sip_+37255512345", **attributes: str) -> None:
        self.identity = identity
        self.name = ""
        self.kind = 3
        self.metadata = ""
        self.attributes = dict(attributes) if attributes else dict(DEFAULT_SIP_ATTRIBUTES)


class FakeProtoRoom:
    """The room as it arrives inside the job: plain fields, sid included."""

    def __init__(self, name: str = "call-1") -> None:
        self.name = name
        self.sid = "RM_test"
        self.metadata = ""


class FakeRoom:
    """The live room. ``sid`` is an async property here exactly as it is in rtc.Room."""

    def __init__(self, name: str = "call-1") -> None:
        self.name = name
        self.metadata = ""
        self.remote_participants: dict[str, Any] = {}
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Any) -> None:
        listeners = self.handlers.get(event) or []
        if handler in listeners:
            listeners.remove(handler)
        if not listeners:
            self.handlers.pop(event, None)

    def emit(self, event: str, *args: Any) -> None:
        for handler in list(self.handlers.get(event) or []):
            handler(*args)

    def emit_participant_connected(self, participant: Any) -> None:
        self.remote_participants[participant.identity] = participant
        self.emit("participant_connected", participant)

    def emit_attributes_changed(self, changed: dict, participant: Any) -> None:
        self.emit("participant_attributes_changed", changed, participant)

    def emit_participant_disconnected(self, participant: Any, reason: Any = None) -> None:
        participant.disconnect_reason = reason
        self.remote_participants.pop(participant.identity, None)
        self.emit("participant_disconnected", participant)

    @property
    async def sid(self) -> str:
        raise AssertionError("rtc.Room.sid is a coroutine and must never be read directly")


class FakeJob:
    def __init__(self, metadata: str | None = None) -> None:
        self.id = "AJ_test"
        self.dispatch_id = "AD_test"
        self.agent_name = "test-agent"
        self.metadata = metadata
        self.room = FakeProtoRoom()


class FakeTagger:
    def __init__(self) -> None:
        self.tags: set[str] = set()
        self.outcome: str | None = None
        self.outcome_reason: str | None = None


class FakeContext:
    """Enough of a JobContext for the package to run against."""

    def __init__(self, metadata: str | None = None, participant: Any = None) -> None:
        self.job = FakeJob(metadata)
        self.room = FakeRoom()
        self.tagger = FakeTagger()
        self.shutdown_reason: str | None = None
        self.participant_entrypoints: list[Any] = []
        self.shutdown_callbacks: list[Any] = []
        self._participant = participant or FakeParticipant()
        self.report: Any = None
        self.fake_job = False
        self.deleted_room = False

    def add_participant_entrypoint(self, fnc: Any, **_: Any) -> None:
        self.participant_entrypoints.append(fnc)

    def add_shutdown_callback(self, cb: Any) -> None:
        self.shutdown_callbacks.append(cb)

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_reason = reason

    def delete_room(self) -> None:
        if self.shutdown_reason is not None:
            raise AssertionError("the caller must be released before the job shuts down")
        self.deleted_room = True

    def is_fake_job(self) -> bool:
        return self.fake_job

    async def wait_for_participant(self, **_: Any) -> Any:
        return self._participant

    def make_session_report(self, _session: Any = None) -> Any:
        if self.report is None:
            raise RuntimeError("no AgentSession in this test")
        return self.report


def envelope_metadata(**body: Any) -> str:
    return json.dumps({"callva": body})


class FakeReport:
    """Stands in for a LiveKit SessionReport."""

    def __init__(self, audio_recording_path: Any = None) -> None:
        self.audio_recording_path = audio_recording_path
        self.chat_history = {"items": []}

    def to_dict(self) -> dict:
        return {
            "job_id": "AJ_test",
            "chat_history": self.chat_history,
            "usage": [],
            "sdk_version": "1.5.7",
        }


class FakeInput:
    """The session's input side, which says whether the caller can be heard at all."""

    def __init__(self, audio_enabled: bool = True) -> None:
        self.audio_enabled = audio_enabled


class FakeSession:
    """Enough of an AgentSession to be waited on and watched.

    Several listeners per event, because that is what the real emitter does and what a call
    under supervision has: the away watch and the silence watch both want
    ``user_state_changed``, and a fake that kept one would let a test pass on a session where
    one of them had been quietly overwritten.
    """

    def __init__(self, agent_state: str = "listening", audio_enabled: bool = True) -> None:
        self.agent_state = agent_state
        self.input = FakeInput(audio_enabled)
        self.handlers: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Any) -> None:
        listeners = self.handlers.get(event) or []
        if handler in listeners:
            listeners.remove(handler)
        if not listeners:
            self.handlers.pop(event, None)

    def emit(self, event: str, payload: Any) -> None:
        for handler in list(self.handlers.get(event) or []):
            handler(payload)

    def close(self, reason: str = "participant_disconnected") -> None:
        self.emit("close", type("Event", (), {"reason": type("R", (), {"value": reason})()})())

    def go_away(self, state: str = "away") -> None:
        self.emit(
            "user_state_changed",
            type("Event", (), {"old_state": "listening", "new_state": state})(),
        )

    def start_speaking(self) -> None:
        """The caller opens their mouth, as voice detection reports it."""
        self.emit(
            "user_state_changed",
            type("Event", (), {"old_state": "listening", "new_state": "speaking"})(),
        )

    def agent_speaks(self) -> None:
        """The agent starts talking, which is this side of the call making a sound."""
        self.agent_state = "speaking"
        self.emit(
            "agent_state_changed",
            type("Event", (), {"old_state": "listening", "new_state": "speaking"})(),
        )

    def stop_speaking(self, state: str = "listening") -> None:
        self.agent_state = state
        self.emit(
            "agent_state_changed",
            type("Event", (), {"old_state": "speaking", "new_state": state})(),
        )

    def stop_talking(self) -> None:
        """The caller falls silent again, which is not the same as having gone."""
        self.emit(
            "user_state_changed",
            type("Event", (), {"old_state": "speaking", "new_state": "listening"})(),
        )

    def transcribe(self, transcript: str = "hello", is_final: bool = True) -> None:
        """A transcript arrives, which on a missed detection is the only sign of speech."""
        self.emit(
            "user_input_transcribed",
            type("Event", (), {"transcript": transcript, "is_final": is_final})(),
        )

    def run_tool(self, kind: str = "tool_call_started") -> None:
        """One end of a tool call, as ``tool_execution_updated`` reports it."""
        self.emit(
            "tool_execution_updated",
            type("Event", (), {"update": type("Update", (), {"type": kind})()})(),
        )


# --- The HTTP side: what a delivery or a configuration fetch talks to ---------


class FakeResponse:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class Boom:
    """A request that fails the way the network fails: on the way out."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self) -> Any:
        raise self._error

    async def __aexit__(self, *_: object) -> bool:
        return False


class FakeHttpSession:
    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(200)


def use_http(monkeypatch: Any, session: FakeHttpSession) -> FakeHttpSession:
    """Make the transport talk to ``session`` instead of the network."""
    from callva.livekit.core import transport

    monkeypatch.setattr(transport, "_session", lambda: (session, False))
    return session
