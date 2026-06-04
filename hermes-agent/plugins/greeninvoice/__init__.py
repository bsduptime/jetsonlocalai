"""Hermes greeninvoice plugin — entry point.

Hermes' plugin loader calls `register(ctx)` once at startup. We register
all GreenInvoice tools under the `greeninvoice` toolset. Each tool is a
thin shim over the privilege-separated hermes-greeninvoice daemon; the
plugin never holds the API key. On registration failure we re-raise so a
misconfigured plugin is clearly absent rather than silently broken (tool-
time errors, by contrast, are caught and returned as JSON).
"""

from __future__ import annotations

import logging

from .handler import HANDLERS
from .schemas import ALL_SCHEMAS

log = logging.getLogger("hermes.plugins.greeninvoice")


def register(ctx) -> None:
    try:
        by_name = {s["name"]: s for s in ALL_SCHEMAS}
        for name, handler in HANDLERS.items():
            ctx.register_tool(
                name=name,
                toolset="greeninvoice",
                schema=by_name[name],
                handler=handler,
            )
        log.info("hermes-greeninvoice: registered %d tools", len(HANDLERS))
    except Exception:
        log.exception("hermes-greeninvoice: failed to register tools")
        raise
