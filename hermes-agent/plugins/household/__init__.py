"""Hermes household plugin — entry point.

Hermes' plugin loader calls `register(ctx)` exactly once at startup. Same
contract as the familycal/mailer plugins: if this raises, the plugin is
disabled but Hermes keeps running; tool-time errors are caught inside the
handlers and returned as JSON strings.
"""

from __future__ import annotations

import logging

from .handler import shopping_add, shopping_clear, shopping_list, shopping_remove
from .schemas import SHOPPING_ADD, SHOPPING_CLEAR, SHOPPING_LIST, SHOPPING_REMOVE

log = logging.getLogger("hermes.plugins.household")


def register(ctx) -> None:
    try:
        ctx.register_tool(
            name="shopping_add",
            toolset="household",
            schema=SHOPPING_ADD,
            handler=shopping_add,
        )
        ctx.register_tool(
            name="shopping_remove",
            toolset="household",
            schema=SHOPPING_REMOVE,
            handler=shopping_remove,
        )
        ctx.register_tool(
            name="shopping_list",
            toolset="household",
            schema=SHOPPING_LIST,
            handler=shopping_list,
        )
        ctx.register_tool(
            name="shopping_clear",
            toolset="household",
            schema=SHOPPING_CLEAR,
            handler=shopping_clear,
        )
        log.info("household: shopping_add/remove/list/clear tools registered")
    except Exception:
        log.exception("household: failed to register tools")
        raise
