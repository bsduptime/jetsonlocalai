"""Hermes calendar plugin — entry point.

Hermes' plugin loader calls `register(ctx)` exactly once at startup. Same
contract as the mailer plugin: if this raises, the plugin is disabled but
Hermes keeps running. We re-raise on registration failure on purpose so a
misconfigured plugin is clearly absent rather than silently broken.
Tool-time errors, in contrast, are caught inside the handler and returned
as JSON.
"""

from __future__ import annotations

import logging

from .handler import create_event, list_contacts
from .schemas import CREATE_EVENT, LIST_CONTACTS

log = logging.getLogger("hermes.plugins.calendar")


def register(ctx) -> None:
    try:
        ctx.register_tool(
            name="create_event",
            toolset="familycal",
            schema=CREATE_EVENT,
            handler=create_event,
        )
        ctx.register_tool(
            name="list_contacts",
            toolset="familycal",
            schema=LIST_CONTACTS,
            handler=list_contacts,
        )
        log.info("hermes-calendar: create_event + list_contacts tools registered")
    except Exception:
        log.exception("hermes-calendar: failed to register calendar tools")
        raise
