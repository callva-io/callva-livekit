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


class FakeRoom:
    def __init__(self, name: str = "call-1") -> None:
        self.name = name
        self.sid = "RM_test"
        self.metadata = ""


class FakeJob:
    def __init__(self, metadata: str | None = None) -> None:
        self.id = "AJ_test"
        self.dispatch_id = "AD_test"
        self.agent_name = "test-agent"
        self.metadata = metadata
        self.room = FakeRoom()


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

    def add_participant_entrypoint(self, fnc: Any, **_: Any) -> None:
        self.participant_entrypoints.append(fnc)

    def add_shutdown_callback(self, cb: Any) -> None:
        self.shutdown_callbacks.append(cb)

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_reason = reason

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
