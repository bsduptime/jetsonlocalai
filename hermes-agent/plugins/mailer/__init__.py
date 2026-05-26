"""Hermes mailer plugin — entry point.

Hermes' plugin loader calls `register(ctx)` exactly once at startup. Per
the Hermes plugin contract, if this function raises the plugin is disabled
but Hermes itself continues. We re-raise on registration failure on
purpose so a misconfigured plugin is clearly absent rather than silently
broken — the upstream loader will log the traceback. (Tool-time errors,
in contrast, are caught inside the handler and returned as JSON.)
"""

from __future__ import annotations

import logging

from .handler import send_email
from .schemas import SEND_EMAIL

log = logging.getLogger("hermes.plugins.mailer")


def register(ctx) -> None:
    try:
        ctx.register_tool(
            name="send_email",
            toolset="mailer",
            schema=SEND_EMAIL,
            handler=send_email,
        )
        log.info("hermes-mailer: send_email tool registered")
    except Exception:
        log.exception("hermes-mailer: failed to register send_email")
        raise
