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
class CallConfig:
    """The configuration resolved for one call.

    ``prompt`` and ``greeting`` come back rendered. The unrendered forms are kept beside
    them for callers that need the original.
    """

    prompt: str | None = None
    greeting: str | None = None
    raw_prompt: str | None = None
    raw_greeting: str | None = None
    call_id: str | None = None
    """An id the responder minted for this call. Adopted unless the dispatcher named one."""
    variables: Variables = field(default_factory=Variables)
    webhook: WebhookTarget | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    source: str = "none"
    """Where this came from: ``metadata``, ``file``, ``url``, or ``none``."""

    @property
    def empty(self) -> bool:
        return not any((self.prompt, self.greeting, self.variables, self.extra))

    @classmethod
    def parse(cls, payload: Any, *, source: str) -> CallConfig:
        """Build a config from a decoded response body. Never raises."""
        if not isinstance(payload, dict):
            return cls(source=source)

        variables = payload.get("variables")
        variables = Variables(variables) if isinstance(variables, dict) else Variables()

        raw_prompt = payload.get("prompt")
        raw_prompt = raw_prompt if isinstance(raw_prompt, str) else None
        raw_greeting = payload.get("greeting")
        raw_greeting = raw_greeting if isinstance(raw_greeting, str) else None

        extra = payload.get("extra")
        call_id = payload.get("call_id")

        return cls(
            prompt=render(raw_prompt, variables),
            greeting=render(raw_greeting, variables),
            raw_prompt=raw_prompt,
            raw_greeting=raw_greeting,
            call_id=call_id.strip() or None if isinstance(call_id, str) else None,
            variables=variables,
            webhook=WebhookTarget.from_dict(payload.get("webhook")),
            extra=extra if isinstance(extra, dict) else {},
            source=source,
        )
