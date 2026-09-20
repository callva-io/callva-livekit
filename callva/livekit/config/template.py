from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..core.log import logger

PLACEHOLDER = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}"
)

_MISSING = object()


def _lookup(variables: Mapping[str, Any], path: str) -> Any:
    """Resolve one placeholder's name, by the exact key first and by the path second.

    Both spellings are real and neither can be dropped. A variable sent for this call may be a
    structure - ``{{ customer.name }}`` reaching into one the platform composed - and that is
    walked. But a name may also *contain* a dot without being a path: ``agent.farewell`` is one
    key, written that way because that is what an operator types, and it sits flat in the pool
    beside every other. The exact key is tried first because it is the more specific match: a
    name somebody wrote whole is not an accident of punctuation.
    """
    if path in variables:
        return variables[path]

    cursor: Any = variables
    for part in path.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            return _MISSING
        cursor = cursor[part]
    return cursor


def _stringify(value: Any) -> str:
    """Render a value the way its producer wrote it.

    Booleans come back as ``true``/``false`` rather than Python's capitalised form, and a
    null renders as nothing at all, because both end up inside a prompt a model reads.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render(text: str | None, variables: Mapping[str, Any] | None) -> str | None:
    """Substitute ``{{ name }}`` placeholders from ``variables``.

    A placeholder with no matching variable is left exactly as it was and logged at
    warning level. Rendering never raises and never evaluates anything: one missing key
    must not take down a call that is already ringing.
    """
    if not text or not variables:
        return text

    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        path = match.group(1)
        value = _lookup(variables, path)
        if value is _MISSING:
            missing.append(path)
            return match.group(0)
        return _stringify(value)

    rendered = PLACEHOLDER.sub(substitute, text)

    if missing:
        logger.warning(
            "left %s unresolved placeholder(s) in place: %s",
            len(missing),
            ", ".join(sorted(set(missing))),
        )

    return rendered
