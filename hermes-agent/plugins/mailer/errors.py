"""Error types + JSON shape helpers.

The handler MUST always return a JSON string. These exceptions are caught at
the handler boundary and turned into the documented response shapes.
"""

from __future__ import annotations

from typing import Any


class EmailToolError(Exception):
    """Base for everything thrown inside the email tool."""


class InvalidInput(EmailToolError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class NotAllowed(EmailToolError):
    def __init__(self, reason: str, payload: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.payload = payload or {}


class PreSendError(EmailToolError):
    """Transport rejected before bytes left the host (auth, validation, connect)."""


class PluginConfigError(EmailToolError):
    """Plugin can't operate — missing dep, malformed env."""


def ok_response(*, status: str, to: str, message_id: str | None,
                remaining_today: int, limit: int, resets_at: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "to": to,
        "message_id": message_id,
        "remaining_today": remaining_today,
        "limit": limit,
        "resets_at": resets_at,
    }


def not_allowed_response(*, reason: str, to: str, **extra: Any) -> dict[str, Any]:
    body = {"ok": False, "error": "not_allowed", "reason": reason, "to": to}
    body.update(extra)
    return body


def invalid_input_response(*, reason: str, detail: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "error": "invalid_input",
        "reason": reason,
        "detail": detail,
    }


def transport_failed_response(*, reason: str, to: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "transport_failed",
        "reason": reason,
        "to": to,
    }


def plugin_misconfigured_response(*, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "plugin_misconfigured",
        "reason": "plugin_dependency_missing",
        "detail": detail,
    }
