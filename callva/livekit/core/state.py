from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import JobContext, get_job_context

from .envelope import DispatchEnvelope, parse
from .identity import CallIdentity
from .log import logger


class NoJobContext(RuntimeError):
    """Raised when the package is used outside a running LiveKit job."""


def context() -> JobContext:
    """The ambient :class:`JobContext`.

    Resolved through the SDK's own contextvar, so nothing has to be threaded through the
    caller's code.
    """
    ctx = get_job_context(required=False)
    if ctx is None:
        raise NoJobContext(
            "no LiveKit job context is active; call this from inside an agent entrypoint "
            "or a session hook"
        )
    return ctx


@dataclass
class CallState:
    """Everything the modules of this package share about one call.

    Owned here so that modules never import each other: the config module fills in what it
    resolved, the webhook module reads it, and anything added later does the same.
    """

    ctx: JobContext
    envelope: DispatchEnvelope
    identity: CallIdentity | None = None
    config: Any = None
    session: Any = None
    webhook: Any = None
    started_at: float = 0.0
    ended_at: float = 0.0
    started_sent: bool = False
    ended_sent: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


_states: weakref.WeakKeyDictionary[JobContext, CallState] = weakref.WeakKeyDictionary()


def state(ctx: JobContext | None = None) -> CallState:
    """The :class:`CallState` for this job, created on first use."""
    ctx = ctx or context()
    existing = _states.get(ctx)
    if existing is not None:
        return existing

    created = CallState(ctx=ctx, envelope=parse(_job_metadata(ctx)))
    _states[ctx] = created
    return created


def _job_metadata(ctx: JobContext) -> str | None:
    try:
        return ctx.job.metadata
    except Exception:  # a fake or partially built job in tests
        return None


def adopt_call_id(st: CallState, call_id: str) -> bool:
    """Take an id the other side minted for this call, and say whether it was taken.

    A configuration endpoint that creates the call's record before answering can name it
    here. Both sides then address one record by one id, and neither has to carry a field
    holding the other's. It is the only thing about a call's identity that configuration
    may decide, and it decides nothing about the call — only what to file it under.

    The dispatcher outranks it: a call placed with an id was named before it began. And
    once any event has gone out the id is what the consumer matched on, so a late change
    would split one call across two records — exactly what naming it was meant to avoid.
    """
    call_id = call_id.strip()
    if not call_id:
        return False

    if st.envelope.call_id:
        return False

    if st.started_sent or st.extras.get("webhook.dialing_sent"):
        logger.warning(
            "configuration named call id %r after this call was already reported as %r; "
            "keeping the reported one",
            call_id,
            st.identity.id if st.identity else None,
        )
        return False

    if st.identity is not None:
        st.identity.id = call_id
    else:
        # Identity has not been built yet — a config that answered from job metadata or a
        # file gets here first. Held until it is.
        st.extras["call_id"] = call_id

    return True


def ensure_identity(
    st: CallState,
    *,
    participant: Any | None = None,
    direction: str | None = None,
) -> CallIdentity:
    """Resolve the call identity once, refining it when the participant turns up.

    Configuration may be resolved before anyone has joined, so the identity can be built
    without a participant and completed later. The call id and start time survive that
    refinement: they are what both webhooks and the stored recording are keyed on.
    """
    from .identity import resolve as _resolve

    if st.identity is None:
        st.identity = _resolve(
            envelope=st.envelope, participant=participant, direction=direction
        )
        if adopted := st.extras.pop("call_id", None):
            st.identity.id = adopted
        return st.identity

    if participant is not None and st.identity.sip is None:
        refined = _resolve(envelope=st.envelope, participant=participant, direction=direction)
        refined.id = st.identity.id
        refined.started_at = st.identity.started_at
        st.identity = refined

    return st.identity
