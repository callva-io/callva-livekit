from __future__ import annotations

import json
from typing import Any

import pytest
from fakes import FakeContext, envelope_metadata

from callva.livekit import config as callva_config
from callva.livekit.config import resolver
from callva.livekit.core import state as _state
from callva.livekit.core import transport

BODY = {
    "prompt": "You are talking to {{ name }}.",
    "greeting": "Hello {{ name }}",
    "variables": {"name": "Anna", "attempt": 2},
    "webhook": {"url": "https://tenant.test/hook", "secret": "k"},
    "extra": {"preset": "vertex"},
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
    assert config.extra == {"preset": "vertex"}
    assert no_fetch == [], "an inline body needs no round trip"


async def test_a_pointer_in_metadata_is_followed(bind_context, no_fetch):
    bind_context(FakeContext(envelope_metadata(config_url="https://platform.test/config")))

    config = await callva_config.load()

    assert config.source == "url"
    assert [url for url, _ in no_fetch] == ["https://platform.test/config"]


async def test_the_environment_url_is_used_when_metadata_is_empty(
    bind_context, no_fetch, monkeypatch
):
    monkeypatch.setenv("CALLVA_CONFIG_URL", "https://env.test/config")
    bind_context(FakeContext())

    config = await callva_config.load()

    assert config.source == "url"
    assert [url for url, _ in no_fetch] == ["https://env.test/config"]


async def test_a_pointer_in_metadata_beats_the_environment(bind_context, no_fetch, monkeypatch):
    monkeypatch.setenv("CALLVA_CONFIG_URL", "https://env.test/config")
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
    monkeypatch.setenv("CALLVA_CONFIG_URL", "https://env.test/config")
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
    monkeypatch.setenv("CALLVA_CONFIG_URL", "https://env.test/config")
    ctx = bind_context(FakeContext())

    await callva_config.load()

    assert _state.state(ctx).webhook == transport.WebhookTarget("https://tenant.test/hook", "k")


async def test_config_is_resolved_once_per_call(bind_context, no_fetch, monkeypatch):
    monkeypatch.setenv("CALLVA_CONFIG_URL", "https://env.test/config")
    bind_context(FakeContext())

    first = await callva_config.load()
    second = await callva_config.load()

    assert first is second
    assert len(no_fetch) == 1


async def test_a_failed_request_terminates_the_call(bind_context, monkeypatch):
    async def boom(*_: Any, **__: Any) -> Any:
        raise transport.FetchError("HTTP 500: upstream is down")

    monkeypatch.setattr(resolver.transport, "fetch_json", boom)
    monkeypatch.setenv("CALLVA_CONFIG_URL", "https://env.test/config")
    ctx = bind_context(FakeContext())

    with pytest.raises(callva_config.ConfigError, match="upstream is down"):
        await callva_config.load()

    assert ctx.shutdown_reason == "callva: configuration unavailable"


async def test_continuing_without_configuration_is_opt_in(bind_context, monkeypatch):
    async def boom(*_: Any, **__: Any) -> Any:
        raise transport.FetchError("nope")

    monkeypatch.setattr(resolver.transport, "fetch_json", boom)
    monkeypatch.setenv("CALLVA_CONFIG_URL", "https://env.test/config")
    ctx = bind_context(FakeContext())

    config = await callva_config.load(on_error="continue")

    assert config.empty
    assert ctx.shutdown_reason is None


async def test_an_empty_response_is_treated_as_a_failure(bind_context, monkeypatch):
    async def nothing(*_: Any, **__: Any) -> Any:
        return {}

    monkeypatch.setattr(resolver.transport, "fetch_json", nothing)
    monkeypatch.setenv("CALLVA_CONFIG_URL", "https://env.test/config")
    ctx = bind_context(FakeContext())

    with pytest.raises(callva_config.ConfigError, match="nothing usable"):
        await callva_config.load()

    assert ctx.shutdown_reason is not None
