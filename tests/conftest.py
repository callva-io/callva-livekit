from __future__ import annotations

from typing import Any

import pytest
from fakes import FakeContext

# Unprefixed names are the package's public contract, so a developer's own shell can
# collide with them. Every test starts from none of them being set.
MANAGED_ENV = (
    "WEBHOOK_URL",
    "WEBHOOK_SECRET",
    "WEBHOOK_TIMEOUT",
    "CONFIG_URL",
    "CONFIG_API_KEY",
    "CONFIG_TIMEOUT",
    "CALL_DIRECTION",
    "RECORDING_TIMEOUT",
    "RECORDING_S3_BUCKET",
    "RECORDING_S3_ENDPOINT_URL",
    "RECORDING_S3_REGION",
    "RECORDING_S3_ACCESS_KEY_ID",
    "RECORDING_S3_SECRET_ACCESS_KEY",
    "RECORDING_S3_PUBLIC_BASE_URL",
    "RECORDING_S3_PREFIX",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MANAGED_ENV:
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

