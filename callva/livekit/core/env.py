"""Environment variables.

Names describe the job, not the vendor. This is an extension to the LiveKit Agents SDK;
that a URL happens to point at CallVA is configuration, not identity.
"""

from __future__ import annotations

import os


def get(name: str, default: str | None = None) -> str | None:
    """Read an environment variable, treating a blank value as unset."""
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def get_bool(name: str, default: bool = False) -> bool:
    value = get(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def get_float(name: str, default: float | None = None) -> float | None:
    value = get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default
