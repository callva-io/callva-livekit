from __future__ import annotations

import os

PREFIX = "CALLVA_"


def get(name: str, default: str | None = None) -> str | None:
    """Read ``CALLVA_<name>`` from the environment, treating blanks as unset."""
    value = os.environ.get(PREFIX + name)
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
