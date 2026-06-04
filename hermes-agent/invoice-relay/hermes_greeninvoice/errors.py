"""Error types used by the daemon and handler.

These are caught at the daemon's connection boundary and mapped to JSON
responses. They never reach the client as Python exceptions — the client
sees only the documented response envelope (see PROTOCOL.md).
"""

from __future__ import annotations


class GreenInvoiceError(Exception):
    """Base for all daemon-side errors."""


class ProtocolError(GreenInvoiceError):
    """Wire-protocol violation (bad version, malformed JSON, oversized
    request, unknown op, unknown caller). Maps to error=protocol."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class InvalidInput(GreenInvoiceError):
    """Field-level validation failure. Maps to error=invalid_input."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class NotAllowed(GreenInvoiceError):
    """Policy rejection (rate-limit, missing confirmation). Maps to
    error=not_allowed."""

    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


class UpstreamError(GreenInvoiceError):
    """GreenInvoice API returned an error, or the call could not be made
    (network, auth). Maps to error=upstream_failed."""

    def __init__(self, reason: str, detail: str = "", status: int | None = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.status = status


class ConfigError(GreenInvoiceError):
    """Daemon misconfigured — missing creds, malformed env. Maps to
    error=invalid_input, reason=daemon_misconfigured."""
