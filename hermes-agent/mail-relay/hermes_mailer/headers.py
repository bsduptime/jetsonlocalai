"""Central RFC validation for header-bearing fields.

Same logic as the plugin's headers.py (verbatim, except it now lives in
the daemon). Used for `to`, `from`, `reply_to`, `subject` regardless of
transport. The Resend SDK does NOT go through email.message.EmailMessage
so we re-implement the strict bits ourselves and apply them uniformly.

Rejects:
  - CR / LF in any field (SMTP header-injection).
  - NUL bytes.
  - Addresses without `@`.
  - Display-name forms in the `to` argument (the agent must pass a bare addr).
  - Subjects > 200 chars.
"""

from __future__ import annotations

import re
from email.utils import parseaddr

from .errors import InvalidInput

_LOCAL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL = rf"{_LOCAL_ATOM}(\.{_LOCAL_ATOM})*"
_DOMAIN_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DOMAIN = rf"{_DOMAIN_LABEL}(\.{_DOMAIN_LABEL})+"
_EMAIL_RE = re.compile(rf"^{_LOCAL}@{_DOMAIN}$")


def _has_control(s: str) -> bool:
    return any(ch in s for ch in ("\r", "\n", "\x00"))


def validate_address_field(field_name: str, raw, *, allow_display_name: bool) -> str:
    if raw is None or not str(raw).strip():
        raise InvalidInput("missing_field", field_name)
    if not isinstance(raw, str):
        raise InvalidInput("invalid_field_type", field_name)
    if _has_control(raw):
        raise InvalidInput("header_injection", field_name)
    if len(raw) > 320:
        raise InvalidInput("field_too_long", field_name)
    name, addr = parseaddr(raw)
    if not addr or "@" not in addr:
        raise InvalidInput("invalid_email", field_name)
    if not _EMAIL_RE.fullmatch(addr):
        raise InvalidInput("invalid_email", field_name)
    if name and not allow_display_name:
        raise InvalidInput("display_name_not_allowed", field_name)
    if allow_display_name and _has_control(name):
        raise InvalidInput("header_injection", field_name)
    return raw


def address_only(validated_field: str) -> str:
    _, addr = parseaddr(validated_field)
    return addr.lower()


def validate_subject(raw) -> str:
    if raw is None:
        raise InvalidInput("missing_field", "subject")
    if not isinstance(raw, str):
        raise InvalidInput("invalid_field_type", "subject")
    if _has_control(raw):
        raise InvalidInput("header_injection", "subject")
    if len(raw) > 200:
        raise InvalidInput("subject_too_long", "subject")
    return raw


def validate_body(raw) -> str:
    if raw is None or raw == "":
        raise InvalidInput("missing_field", "body")
    if not isinstance(raw, str):
        raise InvalidInput("invalid_field_type", "body")
    if "\x00" in raw:
        raise InvalidInput("null_byte_in_body", "body")
    return raw
