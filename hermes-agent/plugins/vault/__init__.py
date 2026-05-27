"""Hermes vault plugin — entry point.

Hermes' plugin loader calls `register(ctx)` exactly once at startup. We
re-raise on registration failure so a misconfigured plugin is clearly
absent rather than silently broken — the upstream loader logs the traceback.
"""

from __future__ import annotations

import logging

from .handler import (
    vault_conflict_scan,
    vault_read,
    vault_session_brief,
    vault_write_memory,
    vault_write_observation,
)
from .schemas import (
    VAULT_CONFLICT_SCAN,
    VAULT_READ,
    VAULT_SESSION_BRIEF,
    VAULT_WRITE_MEMORY,
    VAULT_WRITE_OBSERVATION,
)

log = logging.getLogger("hermes.plugins.vault")

_TOOLSET = "vault"

_TOOLS = (
    ("vault_session_brief", VAULT_SESSION_BRIEF, vault_session_brief),
    ("vault_read", VAULT_READ, vault_read),
    ("vault_write_observation", VAULT_WRITE_OBSERVATION, vault_write_observation),
    ("vault_write_memory", VAULT_WRITE_MEMORY, vault_write_memory),
    ("vault_conflict_scan", VAULT_CONFLICT_SCAN, vault_conflict_scan),
)


def register(ctx) -> None:
    try:
        for name, schema, handler in _TOOLS:
            ctx.register_tool(name=name, toolset=_TOOLSET, schema=schema, handler=handler)
            log.info("hermes-vault: %s registered", name)
    except Exception:
        log.exception("hermes-vault: failed to register tools")
        raise
