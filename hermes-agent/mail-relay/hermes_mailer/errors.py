"""Error types used by daemon and handler.

These exceptions are caught at the daemon's connection boundary and
mapped to JSON responses. They never reach the client as Python
exceptions — the client sees only the documented response envelope.
"""

from __future__ import annotations


class MailerError(Exception):
    """Base for all daemon-side errors."""


class ProtocolError(MailerError):
    """Wire-protocol violation (bad version, malformed JSON, oversized request,
    unknown op, etc.). Maps to error=protocol in the response."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class InvalidInput(MailerError):
    """Field-level validation failure. Maps to error=invalid_input."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class NotAllowed(MailerError):
    """Policy rejection (allowlist or rate-limit). Maps to error=not_allowed."""

    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


class PreSendError(MailerError):
    """Transport rejected before bytes left the host (auth, validation 4xx,
    connect refused). Maps to error=transport_failed, reason=pre_send."""


class PluginConfigError(MailerError):
    """Daemon misconfigured — missing dep, malformed env. Maps to
    error=invalid_input, reason=plugin_misconfigured."""
