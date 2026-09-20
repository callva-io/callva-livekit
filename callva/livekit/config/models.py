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

    ROOM_NAME = "room_name"
    """The one pool entry this package supplies rather than receives."""

    @property
    def empty(self) -> bool:
        """Whether the responder gave us nothing to run a call on.

        The room's name is not counted. It is a fact about where the call is being held that
        this worker already had and put into the pool itself, so a configuration holding
        nothing else is still a configuration holding nothing - and the caller that refuses an
        empty one has to keep refusing it.
        """
        told = {k: v for k, v in self.variables.items() if k != self.ROOM_NAME}
        return not any((self.prompt, self.greeting, told, self.preset, self.tools, self.extra))

    @classmethod
    def parse(cls, payload: Any, *, source: str, room_name: str | None = None) -> CallConfig:
        """Build a config from a decoded response body. Never raises.

        Every value has exactly one home. The prompt, the greeting and the variables belong
        to the agent; the call id belongs to the call; the webhook belongs to the services.
        Nothing is read from two places, so nothing can disagree with itself.

        ``room_name`` is the one value that is not in the body, because it is a fact about
        where this call is being held rather than about the agent holding it. It is passed in
        so that a prompt may reach it, and passing nothing leaves it out of the pool.
        """
        if not isinstance(payload, dict):
            return cls(source=source)

        agent_block = payload.get("agent")
        agent_block = agent_block if isinstance(agent_block, dict) else {}
        call_block = payload.get("call")
        call_block = call_block if isinstance(call_block, dict) else {}
        services = payload.get("services")
        services = services if isinstance(services, dict) else {}

        variables = Variables(_pool(agent_block, call_block, room_name))

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


AGENT_VARIABLES = ("name", "greeting", "farewell")
"""The agent's own fields a prompt may reach, under ``agent.`` and their own name.

The farewell is the reason this exists. The platform compiles one and validates it, and no
worker has ever said it: it is not a line anybody is told to utter at the end of a call, it is
a value an operator writes into their prompt as ``{{agent.farewell}}`` and surrounds with
whatever they want done with it. The same is true of the greeting and the name, which is why
the three travel together and why this is a list rather than one field read on its own.
"""


def _pool(
    agent_block: dict[str, Any], call_block: dict[str, Any], room_name: str | None
) -> dict[str, Any]:
    """Everything a prompt's ``{{placeholders}}`` may resolve against, in precedence order.

    Four sources, and the later ones win, which is the order the production worker settled on
    and the order that makes sense read aloud: where the call is being held, then what the
    agent is, then who this call is with, then whatever was sent for this call in particular.
    A dispatch that overrode a variable meant to override it, and the call record it is
    speaking about is more specific than the agent it is speaking as.

    The call record is flattened one level and no further. It is a flat record, and a nested
    value rendered into a prompt would arrive as a stringified structure - text nobody wrote,
    in the middle of an instruction somebody did. A value that is absent is left out rather
    than resolved to nothing, so an unresolved placeholder still shows up as one instead of
    quietly becoming an empty space in the prompt.
    """
    pool: dict[str, Any] = {}

    if room_name:
        pool["room_name"] = room_name

    for key in AGENT_VARIABLES:
        value = agent_block.get(key)
        if value is not None:
            pool[f"agent.{key}"] = value

    for key, value in call_block.items():
        if value is None or isinstance(value, (dict, list)):
            continue
        pool[key] = value

    sent = agent_block.get("prompt_variables")
    if isinstance(sent, dict):
        pool.update(sent)

    return pool


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
