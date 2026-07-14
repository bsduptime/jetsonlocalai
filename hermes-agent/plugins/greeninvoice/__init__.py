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

from . import hooks, visiongate
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

    # visiongate: a local vision model classifies inbound images before the LLM turn
    # (advisory) and gates the expense upload (enforcing, escalates to a human).
    # Registration failure here must NOT take the tools down — but it DOES leave the
    # upload ungated, so say so loudly. GI_VISIONGATE=0 disables it deliberately.
    if not visiongate.ENABLED:
        log.warning("visiongate: DISABLED by GI_VISIONGATE=0 — expense uploads are ungated")
        return
    try:
        ctx.register_hook("pre_gateway_dispatch", hooks.pre_gateway_dispatch)
        ctx.register_hook("pre_tool_call", hooks.pre_tool_call)
        # Deliberately loud: this box emits nothing at INFO, and "did the security gate
        # actually come up?" must be answerable from the journal.
        visiongate.audit("hooks registered model=%s observe=%s",
                         visiongate.MODEL, visiongate.OBSERVE)
        # Load the model now, in the background, so the first receipt after a restart
        # doesn't pay a cold load. Never blocks startup.
        visiongate.warm_model()
    except Exception:
        log.exception(
            "visiongate: FAILED to register hooks — expense uploads will be REFUSED "
            "(the upload handler requires a clearance that only the gate can issue). "
            "Set GI_VISIONGATE=0 to run without the gate.")
