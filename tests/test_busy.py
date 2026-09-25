"""Work the framework never sees, said out loud so the quiet watch can count it.

A model the agent asks itself - a backend behind a voice that has already said "one moment" -
leaves the line silent for seconds and publishes nothing the watch can read. ``busy`` is how
that work is declared, and what is under test is that it is read the way a tool in flight is:
no reminder while it is held, the clock restarted when it is released, the count untouched.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest
from fakes import FakeContext, FakeSession

from callva.livekit import call
from callva.livekit.core import state as _state

supervising = importlib.import_module("callva.livekit.call.supervise")


@pytest.fixture
def no_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(call.release, "GRACE", 0.0)


@pytest.fixture
def brisk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervising, "PROMPT_GRACE", 0.01)
    monkeypatch.setattr(supervising, "QUIET_TICK", 0.01)


def watch(ctx: Any, session: Any) -> list[str | None]:
    said: list[str | None] = []

    async def utter(phrase: str | None) -> None:
        said.append(phrase)

    call.supervise(
        session,
        ctx=ctx,
        utter=utter,
        silence_timeout=0.05,
        prompt_phrases=["are you there?"],
        max_prompt_attempts=2,
        call_silence_timeout=0.05,
    )
    return said


async def test_work_held_busy_is_not_quiet(bind_context, no_grace, brisk):
    ctx = bind_context(FakeContext())
    said = watch(ctx, FakeSession())

    with call.busy():
        await asyncio.sleep(0.3)
        assert said == [], "the caller was reminded while the agent was working"
        assert ctx.shutdown_reason is None

    await asyncio.sleep(0.3)
    assert said, "the clock never restarted once the work landed"


async def test_releasing_it_restarts_the_clock(bind_context, no_grace, brisk):
    ctx = bind_context(FakeContext())
    said: list[str | None] = []

    async def utter(phrase: str | None) -> None:
        said.append(phrase)

    call.supervise(
        FakeSession(),
        ctx=ctx,
        utter=utter,
        silence_timeout=0.3,
        max_prompt_attempts=1,
    )

    await asyncio.sleep(0.2)
    with call.busy():
        await asyncio.sleep(0.2)
    # Without the restart, 0.4 s of quiet is already past the timeout here.
    await asyncio.sleep(0.15)
    assert said == [], "the clock kept counting through the work"

    await asyncio.sleep(0.3)
    assert said == [None]


async def test_it_nests_and_holds_while_anybody_holds_it(bind_context):
    ctx = bind_context(FakeContext())
    st = _state.state(ctx)

    with call.busy():
        with call.busy():
            assert supervising._busy(st)
        assert supervising._busy(st), "the outer holder was released by the inner one"
    assert not supervising._busy(st)


async def test_outside_a_job_it_holds_nothing_and_raises_nothing():
    ran = False
    with call.busy():
        ran = True
    assert ran


async def test_a_caller_who_never_declares_work_is_watched_exactly_as_before(
    bind_context, no_grace, brisk
):
    """Nothing about busy() is armed until somebody holds it: the watch reads no count."""
    ctx = bind_context(FakeContext())
    st = _state.state(ctx)
    said = watch(ctx, FakeSession())

    assert supervising._BUSY not in st.extras
    await asyncio.sleep(0.6)

    assert said == ["are you there?", "are you there?"]
    assert st.ended_by == call.SILENCE
    assert supervising._BUSY not in st.extras
