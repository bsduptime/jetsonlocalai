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

from . import confirmgate, hooks
from .handler import HANDLERS
from .schemas import ALL_SCHEMAS

log = logging.getLogger("hermes.plugins.greeninvoice")


def register(ctx) -> None:
    # confirmgate: require David to confirm the numbers in Telegram before
    # gi_create_expense writes an expense to his real Morning books. No vision model.
    #
    # Register the gate FIRST, and make its failure FATAL to the whole plugin. If the hook
    # can't be installed we must NOT go on to register gi_create_expense — an ungated
    # money-write tool is worse than an absent one. A re-raise here means the plugin is
    # simply absent (consistent with the tool-registration policy below), which fails
    # loudly at startup instead of silently exposing an ungated write.
    #
    # GI_EXPENSE_CONFIRM=0 skips the gate deliberately (operator-only, env, not
    # model-reachable) — the only supported way to run creates ungated.
    if confirmgate.ENABLED:
        try:
            ctx.register_hook("pre_tool_call", hooks.pre_tool_call)
            log.info("greeninvoice: expense-confirm gate registered")
        except Exception:
            log.exception(
                "greeninvoice: FAILED to register the expense-confirm gate — refusing to "
                "register the expense tools ungated. Set GI_EXPENSE_CONFIRM=0 to run "
                "without the gate deliberately.")
            raise
    else:
        log.warning("GI_EXPENSE_CONFIRM=0 — expense creates are NOT confirmed with David")

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
