from __future__ import annotations

import json

from callva.livekit.core import envelope


def test_scoped_envelope_is_read():
    parsed = envelope.parse(json.dumps({"callva": {"call_id": "c1", "direction": "outbound"}}))

    assert parsed.call_id == "c1"
    assert parsed.direction == "outbound"
    assert not parsed.empty


def test_top_level_object_is_claimed_only_when_keys_are_ours():
    ours = envelope.parse(json.dumps({"direction": "outbound", "config": {"prompt": "hi"}}))
    assert ours.direction == "outbound"
    assert ours.config == {"prompt": "hi"}

    theirs = envelope.parse(json.dumps({"tenant": "acme", "campaign": 7}))
    assert theirs.empty
    assert theirs.direction is None


def test_host_metadata_is_left_alone_but_kept():
    raw = json.dumps({"their_key": "their value"})
    parsed = envelope.parse(raw)

    assert parsed.empty
    assert parsed.raw == raw


def test_non_json_metadata_never_raises():
    parsed = envelope.parse("not json at all")

    assert parsed.empty
    assert parsed.raw == "not json at all"


def test_empty_metadata():
    for value in (None, "", "   "):
        parsed = envelope.parse(value)
        assert parsed.empty
        assert parsed.raw is None


def test_blank_strings_are_treated_as_absent():
    parsed = envelope.parse(json.dumps({"callva": {"call_id": "  ", "direction": "outbound"}}))

    assert parsed.call_id is None
    assert parsed.direction == "outbound"


def test_unknown_keys_are_kept_as_extra():
    parsed = envelope.parse(json.dumps({"callva": {"call_id": "c1", "tenant": "acme"}}))

    assert parsed.extra == {"tenant": "acme"}


def test_core_state_stays_a_module():
    """Re-exporting ``state()`` from the package would shadow the submodule.

    Every internal ``from ..core import state`` would then bind a function, and the
    failure only shows up at call time, deep in a live call.
    """
    import inspect

    from callva.livekit.core import state

    assert inspect.ismodule(state)
