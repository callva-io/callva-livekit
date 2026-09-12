from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeContext, envelope_metadata

from callva.livekit import config as callva_config
from callva.livekit.config import resolver
from callva.livekit.core import state as _state
from callva.livekit.core import transport

BODY = {
    "agent": {
        "prompt": "You are talking to {{ name }}.",
        "greeting": "Hello {{ name }}",
        "prompt_variables": {"name": "Anna", "attempt": 2},
    },
    "services": {"webhook": {"url": "https://tenant.test/hook", "secret": "k"}},
    "preset": {"name": "vertex"},
}


@pytest.fixture
def no_fetch(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the config fetch, recording what it was asked."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(url: str, *, payload: dict[str, Any], **_: Any) -> Any:
        calls.append((url, payload))
        return BODY

    monkeypatch.setattr(transport, "fetch_json", fetch)
    monkeypatch.setattr(resolver.transport, "fetch_json", fetch)
    return calls


async def test_inline_config_is_used_without_any_request(bind_context, no_fetch):
    bind_context(FakeContext(envelope_metadata(config=BODY)))

    config = await callva_config.load()

    assert config.source == "metadata"
    assert config.prompt == "You are talking to Anna."
    assert config.greeting == "Hello Anna"
    assert config.raw_prompt == "You are talking to {{ name }}."
    assert config.variables.get_int("attempt") == 2
    assert config.preset == {"name": "vertex"}
    assert no_fetch == [], "an inline body needs no round trip"


async def test_a_pointer_in_metadata_is_followed(bind_context, no_fetch):
    bind_context(FakeContext(envelope_metadata(config_url="https://platform.test/config")))

    config = await callva_config.load()

    assert config.source == "url"
    assert [url for url, _ in no_fetch] == ["https://platform.test/config"]


async def test_the_environment_url_is_used_when_metadata_is_empty(
    bind_context, no_fetch, monkeypatch
):
    monkeypatch.setenv("CONFIG_URL", "https://env.test/config")
    bind_context(FakeContext())

    config = await callva_config.load()

    assert config.source == "url"
    assert [url for url, _ in no_fetch] == ["https://env.test/config"]


async def test_a_pointer_in_metadata_beats_the_environment(bind_context, no_fetch, monkeypatch):
    monkeypatch.setenv("CONFIG_URL", "https://env.test/config")
    bind_context(FakeContext(envelope_metadata(config_url="https://pinned.test/config")))

    await callva_config.load()

    assert [url for url, _ in no_fetch] == ["https://pinned.test/config"]


async def test_no_source_at_all_is_not_an_error(bind_context, no_fetch):
    bind_context(FakeContext())

    config = await callva_config.load()

    assert config.source == "none"
    assert config.empty
    assert no_fetch == []


async def test_the_request_carries_the_call_context(bind_context, no_fetch, monkeypatch):
    monkeypatch.setenv("CONFIG_URL", "https://env.test/config")
    ctx = FakeContext()
    ctx.job.metadata = json.dumps({"tenant": "acme"})
    bind_context(ctx)

    await callva_config.load()

    _url, payload = no_fetch[0]
    assert payload["room"] == "call-1"
    assert payload["job_id"] == "AJ_test"
    assert payload["dispatch_id"] == "AD_test"
    assert payload["agent_name"] == "test-agent"
    assert payload["direction"] == "inbound"
    assert payload["from"]["number"] == "+37255512345"
    assert payload["to"]["number"] == "+3726001234"
    assert payload["sip"]["callID"] == "abc"
    assert payload["participant_identity"] == "sip_+37255512345"
    assert json.loads(payload["metadata"]) == {"tenant": "acme"}, "host metadata passes through"


async def test_a_webhook_in_the_response_reaches_the_shared_state(
    bind_context, no_fetch, monkeypatch
):
    monkeypatch.setenv("CONFIG_URL", "https://env.test/config")
    ctx = bind_context(FakeContext())

    await callva_config.load()

    assert _state.state(ctx).webhook == transport.WebhookTarget("https://tenant.test/hook", "k")


async def test_config_is_resolved_once_per_call(bind_context, no_fetch, monkeypatch):
    monkeypatch.setenv("CONFIG_URL", "https://env.test/config")
    bind_context(FakeContext())

    first = await callva_config.load()
    second = await callva_config.load()

    assert first is second
    assert len(no_fetch) == 1


async def test_a_failed_request_terminates_the_call(bind_context, monkeypatch):
    async def boom(*_: Any, **__: Any) -> Any:
        raise transport.FetchError("HTTP 500: upstream is down")

    monkeypatch.setattr(resolver.transport, "fetch_json", boom)
    monkeypatch.setenv("CONFIG_URL", "https://env.test/config")
    ctx = bind_context(FakeContext())

    with pytest.raises(callva_config.ConfigError, match="upstream is down"):
        await callva_config.load()

    assert ctx.shutdown_reason == "callva: configuration unavailable"


async def test_continuing_without_configuration_is_opt_in(bind_context, monkeypatch):
    async def boom(*_: Any, **__: Any) -> Any:
        raise transport.FetchError("nope")

    monkeypatch.setattr(resolver.transport, "fetch_json", boom)
    monkeypatch.setenv("CONFIG_URL", "https://env.test/config")
    ctx = bind_context(FakeContext())

    config = await callva_config.load(on_error="continue")

    assert config.empty
    assert ctx.shutdown_reason is None


async def test_an_empty_response_is_treated_as_a_failure(bind_context, monkeypatch):
    async def nothing(*_: Any, **__: Any) -> Any:
        return {}

    monkeypatch.setattr(resolver.transport, "fetch_json", nothing)
    monkeypatch.setenv("CONFIG_URL", "https://env.test/config")
    ctx = bind_context(FakeContext())

    with pytest.raises(callva_config.ConfigError, match="nothing usable"):
        await callva_config.load()

    assert ctx.shutdown_reason is not None


async def test_a_plain_path_is_read_as_a_file(bind_context, no_fetch, monkeypatch, tmp_path):
    document = tmp_path / "agent.json"
    document.write_text(json.dumps(BODY))
    monkeypatch.setenv("CONFIG_URL", str(document))
    bind_context(FakeContext())

    config = await callva_config.load()

    assert config.source == "file"
    assert config.prompt == "You are talking to Anna."
    assert no_fetch == [], "a file is read, not fetched"


async def test_a_file_url_is_read_as_a_file(bind_context, no_fetch, monkeypatch, tmp_path):
    document = tmp_path / "agent.json"
    document.write_text(json.dumps(BODY))
    monkeypatch.setenv("CONFIG_URL", document.as_uri())
    bind_context(FakeContext())

    assert (await callva_config.load()).source == "file"


async def test_a_file_answers_without_waiting_for_anyone(bind_context, monkeypatch, tmp_path):
    """The shortest development loop: no participant, no round trip."""
    document = tmp_path / "agent.json"
    document.write_text(json.dumps(BODY))
    monkeypatch.setenv("CONFIG_URL", str(document))

    ctx = FakeContext()

    async def never(**_):
        raise AssertionError("a file must not wait for a participant")

    ctx.wait_for_participant = never
    bind_context(ctx)

    assert (await callva_config.load()).prompt == "You are talking to Anna."


async def test_a_missing_file_terminates_the_call(bind_context, monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_URL", str(tmp_path / "absent.json"))
    ctx = bind_context(FakeContext())

    with pytest.raises(callva_config.ConfigError, match="could not read configuration"):
        await callva_config.load()

    assert ctx.shutdown_reason is not None


async def test_a_file_that_is_not_json_terminates_the_call(bind_context, monkeypatch, tmp_path):
    document = tmp_path / "agent.json"
    document.write_text("this is not json")
    monkeypatch.setenv("CONFIG_URL", str(document))
    bind_context(FakeContext())

    with pytest.raises(callva_config.ConfigError):
        await callva_config.load()


def test_telling_a_file_from_an_endpoint():
    from callva.livekit.config import as_path

    assert as_path("https://example.test/config") is None
    assert as_path("http://example.test/config") is None
    assert as_path("./agent.json") == Path("./agent.json")
    assert as_path("/etc/agent.json") == Path("/etc/agent.json")
    assert as_path("file:///etc/agent.json") == Path("/etc/agent.json")


# --- A call id the responder minted ------------------------------------------


@pytest.fixture
def naming_fetch(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A configuration endpoint that files the call under an id of its own."""

    async def fetch(url: str, **_: Any) -> Any:
        return {**BODY, "call": {"id": "019f0000-0000-7000-8000-00000000beef"}}

    monkeypatch.setattr(resolver.transport, "fetch_json", fetch)


async def test_the_responder_may_name_the_call(bind_context, naming_fetch, monkeypatch):
    """So both sides address one record by one id, and neither stores the other's."""
    monkeypatch.setenv("CONFIG_URL", "https://platform.test/config")
    ctx = bind_context(FakeContext())

    config = await callva_config.load()

    assert config.call_id == "019f0000-0000-7000-8000-00000000beef"
    assert _state.state(ctx).identity.id == "019f0000-0000-7000-8000-00000000beef"


async def test_the_dispatcher_outranks_the_responder(bind_context, naming_fetch, monkeypatch):
    """A call placed with an id was named before it began."""
    monkeypatch.setenv("CONFIG_URL", "https://platform.test/config")
    ctx = bind_context(FakeContext(envelope_metadata(call_id="placed-by-the-dispatcher")))

    await callva_config.load()

    assert _state.state(ctx).identity.id == "placed-by-the-dispatcher"


async def test_a_name_that_arrives_after_the_call_was_reported_is_refused(
    bind_context, naming_fetch, monkeypatch
):
    """Changing it then would split one call across two records."""
    monkeypatch.setenv("CONFIG_URL", "https://platform.test/config")
    ctx = bind_context(FakeContext())
    st = _state.state(ctx)
    _state.ensure_identity(st)
    original = st.identity.id
    st.started_sent = True

    await callva_config.load()

    assert st.identity.id == original


async def test_a_name_from_a_file_survives_until_the_identity_exists(
    bind_context, monkeypatch, tmp_path
):
    """A file answers before anyone has joined, so there is nothing to name yet."""
    document = tmp_path / "agent.json"
    document.write_text(json.dumps({**BODY, "call": {"id": "named-by-the-file"}}))
    monkeypatch.setenv("CONFIG_URL", str(document))
    ctx = bind_context(FakeContext())

    await callva_config.load()
    st = _state.state(ctx)
    assert st.identity is None, "a file needs no participant, so nothing resolved one"

    assert _state.ensure_identity(st).id == "named-by-the-file"


# --- the agent block -------------------------------------------------------


def parse(payload: Any) -> callva_config.CallConfig:
    return callva_config.CallConfig.parse(payload, source="url")


def test_an_agent_that_opens_with_a_written_line():
    config = parse(
        {"agent": {"greeting": "Hello", "greeting_type": "message"}},
    )

    assert config.agent.speaks_first is True
    assert config.agent.greeting_type == "message"
    assert config.greeting == "Hello"


def test_an_agent_that_opens_in_its_own_words():
    """The third state: it speaks first, but the greeting is an instruction, not a line."""
    config = parse(
        {"agent": {"greeting": "Greet them warmly", "greeting_type": "prompt"}},
    )

    assert config.agent.speaks_first is True
    assert config.agent.greeting_type == "prompt"
    assert config.greeting == "Greet them warmly"


def test_an_agent_that_waits_to_be_spoken_to():
    config = parse({"agent": {"greeting": "Hello", "agent_waits_for_user": True}})

    assert config.agent.speaks_first is False


def test_an_agent_opens_when_the_source_does_not_say():
    """Saying nothing must not produce a call where nobody ever speaks."""
    assert parse({"agent": {"prompt": "Be helpful."}}).agent.speaks_first is True


def test_the_limits_a_call_runs_under():
    config = parse(
        {
            "agent": {
                "max_duration_seconds": 600,
                "user_silence_timeout_seconds": 15,
                "call_silence_timeout_seconds": 30,
                "max_prompt_attempts": 2,
                "user_prompt_phrases": ["Are you still there?", 7],
                "farewell_type": "message",
                "farewell": "Goodbye",
            }
        }
    )

    assert config.agent.max_duration == 600.0
    assert config.agent.user_silence_timeout == 15.0
    assert config.agent.call_silence_timeout == 30.0
    assert config.agent.max_prompt_attempts == 2
    assert config.agent.prompt_phrases == ["Are you still there?"], "7 is not a phrase"
    assert config.agent.farewell_type == "message"
    assert config.agent.farewell == "Goodbye"


def test_zero_is_how_the_platform_spells_no_limit():
    config = parse(
        {"agent": {"max_duration_seconds": 0, "user_silence_timeout_seconds": 0}},
    )

    assert config.agent.max_duration is None
    assert config.agent.user_silence_timeout is None


def test_the_blocks_an_agent_may_not_understand_come_through_untouched():
    config = parse(
        {
            "preset": {"name": "gemini_vertex", "config": {"voice": "Aoede"}},
            "tools": {"endCall": {"type": "end_call", "enabled": True}},
            "call": {"id": "abc", "direction": "outbound"},
        }
    )

    assert config.preset["config"]["voice"] == "Aoede"
    assert config.tools["endCall"]["type"] == "end_call"
    assert config.call["direction"] == "outbound"
    assert config.call_id == "abc"


def test_a_body_in_the_old_flat_shape_carries_nothing():
    """The shape changed. A responder still sending the old one is not half-understood."""
    config = parse(
        {
            "prompt": "You are helpful.",
            "greeting": "Hello",
            "variables": {"name": "Anna"},
            "webhook": {"url": "https://tenant.test/hook"},
        }
    )

    assert config.empty
    assert config.prompt is None
    assert config.webhook is None


def test_nothing_is_read_from_two_places():
    """A root-level prompt is not a fallback for the agent's own."""
    config = parse({"prompt": "root", "agent": {"prompt": "agent"}})

    assert config.prompt == "agent"
