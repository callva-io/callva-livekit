from __future__ import annotations

import logging

logger = logging.getLogger("callva.livekit")
"""The package logger.

The package never sets a level, never attaches a handler and never touches the root
logger. Whatever the host application has configured is what applies.
"""
