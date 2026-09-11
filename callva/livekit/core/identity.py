from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import env
from .envelope import DispatchEnvelope
from .log import logger

INBOUND = "inbound"
OUTBOUND = "outbound"

SIP_PREFIX = "sip."

_REMOTE_NUMBER_KEYS = ("phoneNumber", "phone_number")
_LOCAL_NUMBER_KEYS = ("trunkPhoneNumber", "trunk_phone_number")


@dataclass(frozen=True)
class Party:
    """One end of the call."""

    number: str | None = None
    identity: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "identity": self.identity, "name": self.name}


@dataclass
class CallIdentity:
    """Who is on this call, which way it goes, and what to call it.

    Derived by the package. Nothing here is taken from the call's configuration: an
    inbound call is described by the SIP envelope it arrived in, and a dispatched call by
    what the dispatcher declared.
    """

    id: str
    direction: str
    from_party: Party
    to_party: Party
    sip: dict[str, Any] | None = None
    started_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "direction": self.direction,
            "from": self.from_party.to_dict(),
            "to": self.to_party.to_dict(),
        }


def sip_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Collect the ``sip.*`` participant attributes into a nested dict, unchanged.

    ``sip.twilio.callSid`` becomes ``{"twilio": {"callSid": ...}}``. Keys are never
    renamed and nothing is dropped, so attributes LiveKit adds later flow through without
    a release here.
    """
    if not attributes:
        return None

    tree: dict[str, Any] = {}
    for key, value in attributes.items():
        if not key.startswith(SIP_PREFIX):
            continue
        parts = key[len(SIP_PREFIX) :].split(".")
        cursor = tree
        for part in parts[:-1]:
            existing = cursor.get(part)
            if not isinstance(existing, dict):
                existing = {}
                cursor[part] = existing
            cursor = existing
        cursor[parts[-1]] = value

    return tree or None


def _first(source: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_direction(envelope: DispatchEnvelope, override: str | None = None) -> str:
    """Resolve the call direction. Never inferred from participant state.

    The dispatcher's declaration wins, then an explicit override or ``CALLVA_DIRECTION``,
    then inbound. The default is sound rather than a guess: an outbound call is always
    placed by someone, so it always arrives with dispatch metadata.
    """
    for candidate in (envelope.direction, override, env.get("DIRECTION")):
        if not candidate:
            continue
        value = candidate.strip().lower()
        if value in (INBOUND, OUTBOUND):
            return value
        logger.warning(
            "ignoring unknown call direction %r, expected inbound or outbound", candidate
        )

    return INBOUND


def resolve(
    *,
    envelope: DispatchEnvelope,
    participant: Any | None = None,
    direction: str | None = None,
    started_at: float | None = None,
) -> CallIdentity:
    """Build the call identity from the dispatch envelope and the SIP envelope."""
    resolved_direction = resolve_direction(envelope, direction)

    attributes = getattr(participant, "attributes", None) or {}
    sip = sip_attributes(attributes)

    remote = Party(
        number=_first(sip, _REMOTE_NUMBER_KEYS) if sip else None,
        identity=getattr(participant, "identity", None) or None,
        name=getattr(participant, "name", None) or None,
    )
    local = Party(number=_first(sip, _LOCAL_NUMBER_KEYS) if sip else None)

    if resolved_direction == OUTBOUND:
        from_party, to_party = local, remote
    else:
        from_party, to_party = remote, local

    return CallIdentity(
        id=envelope.call_id or uuid.uuid4().hex,
        direction=resolved_direction,
        from_party=from_party,
        to_party=to_party,
        sip=sip,
        started_at=started_at if started_at is not None else time.time(),
    )
