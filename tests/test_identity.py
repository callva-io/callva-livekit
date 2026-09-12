from __future__ import annotations

from fakes import FakeParticipant, envelope_metadata

from callva.livekit.core import identity
from callva.livekit.core.envelope import DispatchEnvelope, parse

SIP = {
    "sip.phoneNumber": "+37255512345",
    "sip.trunkPhoneNumber": "+3726001234",
    "sip.callID": "abc",
    "sip.twilio.callSid": "CA123",
    "other.attribute": "ignored",
}


def test_sip_attributes_are_nested_not_renamed():
    tree = identity.sip_attributes(SIP)

    assert tree == {
        "phoneNumber": "+37255512345",
        "trunkPhoneNumber": "+3726001234",
        "callID": "abc",
        "twilio": {"callSid": "CA123"},
    }


def test_no_sip_attributes_gives_nothing():
    assert identity.sip_attributes({"other": "x"}) is None
    assert identity.sip_attributes(None) is None


def test_direction_defaults_to_inbound():
    assert identity.resolve_direction(DispatchEnvelope()) == identity.INBOUND


def test_dispatcher_declaration_wins_over_everything(monkeypatch):
    monkeypatch.setenv("CALL_DIRECTION", "inbound")
    envelope = parse(envelope_metadata(direction="outbound"))

    assert identity.resolve_direction(envelope, "inbound") == identity.OUTBOUND


def test_explicit_override_beats_the_environment(monkeypatch):
    monkeypatch.setenv("CALL_DIRECTION", "inbound")

    assert identity.resolve_direction(DispatchEnvelope(), "outbound") == identity.OUTBOUND


def test_environment_is_the_last_word_before_the_default(monkeypatch):
    monkeypatch.setenv("CALL_DIRECTION", "outbound")

    assert identity.resolve_direction(DispatchEnvelope()) == identity.OUTBOUND


def test_nonsense_direction_is_ignored_with_a_warning(caplog):
    assert identity.resolve_direction(DispatchEnvelope(direction="sideways")) == identity.INBOUND


def test_inbound_reads_from_the_caller():
    resolved = identity.resolve(
        envelope=DispatchEnvelope(), participant=FakeParticipant(**SIP)
    )

    assert resolved.direction == identity.INBOUND
    assert resolved.from_party.number == "+37255512345"
    assert resolved.to_party.number == "+3726001234"
    assert resolved.from_party.identity == "sip_+37255512345"


def test_outbound_swaps_the_parties():
    resolved = identity.resolve(
        envelope=DispatchEnvelope(direction="outbound"), participant=FakeParticipant(**SIP)
    )

    assert resolved.direction == identity.OUTBOUND
    assert resolved.from_party.number == "+3726001234"
    assert resolved.to_party.number == "+37255512345"


def test_call_id_is_generated_when_the_dispatcher_did_not_pin_one():
    first = identity.resolve(envelope=DispatchEnvelope())
    second = identity.resolve(envelope=DispatchEnvelope())

    assert first.id and second.id and first.id != second.id


def test_dispatcher_can_pin_the_call_id():
    resolved = identity.resolve(envelope=DispatchEnvelope(call_id="platform-42"))

    assert resolved.id == "platform-42"


def test_identity_survives_without_a_participant():
    resolved = identity.resolve(envelope=DispatchEnvelope())

    assert resolved.sip is None
    assert resolved.from_party.number is None
    assert resolved.to_dict()["from"]["number"] is None


def test_the_dispatcher_names_the_numbers_when_nobody_answered():
    """A call nobody picks up produces no participant, and so no SIP attributes at all."""
    placed = parse(
        envelope_metadata(
            direction="outbound", **{"from": "+3726361029", "to": "+3725258198"}
        )
    )

    call = identity.resolve(envelope=placed, participant=None)

    assert call.direction == "outbound"
    assert call.from_party.number == "+3726361029"
    assert call.to_party.number == "+3725258198"


def test_what_actually_happened_beats_what_was_declared():
    """The SIP envelope is the call; the dispatcher's numbers only fill gaps."""
    placed = parse(envelope_metadata(direction="outbound", to="+37200000000"))

    call = identity.resolve(envelope=placed, participant=FakeParticipant(**SIP))

    assert call.to_party.number == "+37255512345"


def test_a_party_may_be_given_in_the_shape_it_is_reported_in():
    placed = parse(envelope_metadata(to={"number": "+3725258198"}))

    assert placed.to_number == "+3725258198"
