from __future__ import annotations

from typing import Any

import pytest
from fakes import FakeContext


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(__import__("os").environ):
        if name.startswith("CALLVA_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ctx() -> FakeContext:
    return FakeContext()


@pytest.fixture
def bind_context(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make ``core.state.context()`` resolve to a supplied fake context."""
    from callva.livekit.core import state as state_module

    def bind(context: Any) -> Any:
        monkeypatch.setattr(state_module, "context", lambda: context)
        return context

    return bind

