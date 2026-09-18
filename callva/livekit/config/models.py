from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.transport import WebhookTarget
from .template import render


class Variables(dict):
    """The call's variables, with their JSON types intact.

    A number stays a number and a boolean stays a boolean; only substitution into a
    prompt takes the string form. The typed accessors are for reading a variable in code
    without re-checking what the producer sent.
    """

    def get_str(self, name: str, default: str | None = None) -> str | None:
        value = self.get(name)
        return default if value is None else str(value)

    def get_int(self, name: str, default: int | None = None) -> int | None:
        value = self.get(name)
        if isinstance(value, bool) or value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_float(self, name: str, default: float | None = None) -> float | None:
        value = self.get(name)
        if isinstance(value, bool) or value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def get_bool(self, name: str, default: bool | None = None) -> bool | None:
        value = self.get(name)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
        return default


@dataclass
class AgentConfig:
    """The agent, and how this call is to be conducted.

    Everything here describes the conversation rather than the speech stack: who the agent
    is, whether it opens the call, and the limits the call runs under. An agent that builds
    its own pipeline still wants all of it.
    """

    id: str | None = None
    name: str | None = None
    greeting_type: str | None = None
    """``message`` when the greeting is a line to speak, ``prompt`` when it is an
    instruction to compose one from. ``None`` when the source did not say."""
    speaks_first: bool = True
    """Whether the agent opens the call at all. The wire spells this the other way round,
    as ``agent_waits_for_user``; it is inverted here so the name matches the question a
    caller actually asks."""
    max_duration: float | None = None
    user_silence_timeout: float | None = None
    """How long the caller may stay silent before the agent prompts them."""
    call_silence_timeout: float | None = None
    """How long the call may stay silent before it is ended."""
    max_prompt_attempts: int | None = None
    prompt_phrases: list[str] = field(default_factory=list)
    """What to say to a caller who has gone quiet."""
    farewell_type: str | None = None
    farewell: str | None = None

    @classmethod
    def parse(cls, payload: Any) -> AgentConfig:
        if not isinstance(payload, dict):
            return cls()

        phrases = payload.get("user_prompt_phrases")
        return cls(
            id=_text(payload.get("id")),
            name=_text(payload.get("name")),
            greeting_type=_text(payload.get("greeting_type")),
            # Absent means the agent opens: an agent that never speaks first is the
            # unusual case, and saying nothing should not produce a silent call.
            speaks_first=not _flag(payload.get("agent_waits_for_user"), default=False),
            max_duration=_seconds(payload.get("max_duration_seconds")),
            user_silence_timeout=_seconds(payload.get("user_silence_timeout_seconds")),
            call_silence_timeout=_seconds(payload.get("call_silence_timeout_seconds")),
            max_prompt_attempts=_count(payload.get("max_prompt_attempts")),
            prompt_phrases=[p for p in phrases if isinstance(p, str)]
            if isinstance(phrases, list)
            else [],
            farewell_type=_text(payload.get("farewell_type")),
            farewell=_text(payload.get("farewell")),
        )


@dataclass
class CallConfig:
    """The configuration resolved for one call.

    ``prompt`` and ``greeting`` come back rendered. The unrendered forms are kept beside
    them for callers that need the original.

    The blocks below ``agent`` are passed through as they arrived. ``preset`` is the one
    that varies with the speech stack, and an agent that builds its own pipeline ignores
    it; the rest of the configuration means the same thing whatever the stack.
    """

    prompt: str | None = None
    greeting: str | None = None
    raw_prompt: str | None = None
    raw_greeting: str | None = None
    call_id: str | None = None
    """An id the responder minted for this call. Adopted unless the dispatcher named one."""
    variables: Variables = field(default_factory=Variables)
    webhook: WebhookTarget | None = None
    agent: AgentConfig = field(default_factory=AgentConfig)
    raw_agent: dict[str, Any] = field(default_factory=dict)
    """The agent block as it arrived. Typing it loses whatever this schema does not name,
    and the report echoes the block back so that whoever sent it reads their own values."""
    preset: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    call: dict[str, Any] = field(default_factory=dict)
    environment: Any = None
    """Which deployment of the sender this call belongs to, in their own words. Passed
    through untouched; this package never decides it."""
    extra: dict[str, Any] = field(default_factory=dict)
    source: str = "none"
    """Where this came from: ``metadata``, ``file``, ``url``, or ``none``."""

    @property
    def empty(self) -> bool:
        return not any(
            (self.prompt, self.greeting, self.variables, self.preset, self.tools, self.extra)
        )

    @classmethod
    def parse(cls, payload: Any, *, source: str) -> CallConfig:
        """Build a config from a decoded response body. Never raises.

        Every value has exactly one home. The prompt, the greeting and the variables belong
        to the agent; the call id belongs to the call; the webhook belongs to the services.
        Nothing is read from two places, so nothing can disagree with itself.
        """
        if not isinstance(payload, dict):
            return cls(source=source)

        agent_block = payload.get("agent")
        agent_block = agent_block if isinstance(agent_block, dict) else {}
        call_block = payload.get("call")
        call_block = call_block if isinstance(call_block, dict) else {}
        services = payload.get("services")
        services = services if isinstance(services, dict) else {}

        variables = agent_block.get("prompt_variables")
        variables = Variables(variables) if isinstance(variables, dict) else Variables()

        raw_prompt = _text(agent_block.get("prompt"))
        raw_greeting = _text(agent_block.get("greeting"))

        return cls(
            prompt=render(raw_prompt, variables),
            greeting=render(raw_greeting, variables),
            raw_prompt=raw_prompt,
            raw_greeting=raw_greeting,
            call_id=_text(call_block.get("id")),
            variables=variables,
            webhook=WebhookTarget.from_dict(services.get("webhook")),
            agent=AgentConfig.parse(agent_block),
            raw_agent=agent_block,
            preset=_block(payload.get("preset")),
            tools=_block(payload.get("tools")),
            call=call_block,
            environment=payload.get("environment"),
            extra=_block(payload.get("extra")),
            source=source,
        )


def _text(value: Any) -> str | None:
    """A non-empty string, or nothing at all."""
    return value.strip() or None if isinstance(value, str) else None


def _block(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _flag(value: Any, *, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _seconds(value: Any) -> float | None:
    """A positive duration. Zero is how the platform spells "no limit", so it is nothing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None
