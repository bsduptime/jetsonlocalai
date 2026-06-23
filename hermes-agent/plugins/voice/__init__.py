"""Hermes voice plugin — entry point.

Hermes' plugin loader calls `register(ctx)` exactly once at startup. Per the
plugin contract, if this raises the plugin is disabled but Hermes itself
continues. We re-raise on registration failure on purpose so a misconfigured
plugin is clearly absent rather than silently broken — the loader logs the
traceback. (Tool-time errors, in contrast, are caught inside the handler and
returned as JSON.)
"""

from __future__ import annotations

import logging

from .handler import speak_to_david
from .schemas import SPEAK_TO_DAVID

log = logging.getLogger("hermes.plugins.voice")


def register(ctx) -> None:
    try:
        ctx.register_tool(
            name="speak_to_david",
            toolset="voice",
            schema=SPEAK_TO_DAVID,
            handler=speak_to_david,
        )
        log.info("hermes-voice: speak_to_david tool registered")
    except Exception:
        log.exception("hermes-voice: failed to register voice tool")
        raise
