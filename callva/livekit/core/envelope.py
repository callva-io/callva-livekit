from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .log import logger

ENVELOPE_KEY = "callva"

_OWN_KEYS = frozenset({"call_id", "direction", "config", "config_url", "webhook", "from", "to"})


@dataclass
class DispatchEnvelope:
    """What the dispatcher said about this call, read from ``ctx.job.metadata``.

    Job metadata is a free-form string that the host application may already be using for
    its own purposes, so the envelope is looked for under a ``callva`` key first. A
    top-level object is only claimed when it carries keys that are unambiguously ours.
    """

    call_id: str | None = None
    direction: str | None = None
    config: dict[str, Any] | None = None
    config_url: str | None = None
    webhook: dict[str, Any] | None = None
    from_number: str | None = None
    to_number: str | None = None
    """Who the dispatcher said this call is between.

    A placed call knows the number it is calling before anyone picks up, and a call nobody
    picks up never produces a participant to read it from. Without this such a call is
    reported with no number at all, which is most of what makes it worth reporting.
    """
    raw: str | None = None
    """The original metadata string, forwarded to a config endpoint unchanged."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Everything else found alongside our keys."""

    @property
    def empty(self) -> bool:
        return not any((self.call_id, self.direction, self.config, self.config_url, self.webhook))


def parse(metadata: str | None) -> DispatchEnvelope:
    """Parse job metadata into an envelope. Never raises."""
    if not metadata or not metadata.strip():
        return DispatchEnvelope()

    try:
        decoded = json.loads(metadata)
    except (ValueError, TypeError):
        logger.debug("job metadata is not JSON, no dispatch envelope taken from it")
        return DispatchEnvelope(raw=metadata)

    if not isinstance(decoded, dict):
        return DispatchEnvelope(raw=metadata)

    scoped = decoded.get(ENVELOPE_KEY)
    if isinstance(scoped, dict):
        body = scoped
    elif _OWN_KEYS & decoded.keys():
        body = decoded
    else:
        return DispatchEnvelope(raw=metadata)

    def _dict(key: str) -> dict[str, Any] | None:
        value = body.get(key)
        return value if isinstance(value, dict) else None

    def _str(key: str) -> str | None:
        value = body.get(key)
        return value.strip() or None if isinstance(value, str) else None

    def _number(key: str) -> str | None:
        """A party is a bare number, or the same shape the webhook reports it in."""
        value = body.get(key)
        if isinstance(value, dict):
            value = value.get("number")
        return value.strip() or None if isinstance(value, str) else None

    return DispatchEnvelope(
        call_id=_str("call_id"),
        direction=_str("direction"),
        config=_dict("config"),
        config_url=_str("config_url"),
        webhook=_dict("webhook"),
        from_number=_number("from"),
        to_number=_number("to"),
        raw=metadata,
        extra={k: v for k, v in body.items() if k not in _OWN_KEYS},
    )
