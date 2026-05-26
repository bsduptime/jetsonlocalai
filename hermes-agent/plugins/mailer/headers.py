"""Central RFC validation for header-bearing fields.

Used for `to`, `from`, `reply_to`, `subject` on every send, regardless of
transport. The Resend SDK does NOT go through email.message.EmailMessage so
we cannot rely on EmailMessage's own validation; we re-implement the strict
bits ourselves and apply them uniformly.

Reject:
  - CR / LF in any field (the SMTP header-injection classic).
  - NUL bytes.
  - Addresses without `@`.
  - Display-name forms in the `to` argument (the agent must pass a bare addr).
  - Subjects > 200 chars.
"""

from __future__ import annotations

import re
from email.utils import parseaddr

from .errors import InvalidInput

# Permissive RFC-5321-ish address regex. Not full RFC 5322 — we want to
# allow normal addresses (incl. plus-tags, internal dots) and reject obvious
# garbage. Local part is a dot-separated sequence of atoms: no leading/
# trailing dot, no consecutive dots.
_LOCAL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL = rf"{_LOCAL_ATOM}(\.{_LOCAL_ATOM})*"
_DOMAIN_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DOMAIN = rf"{_DOMAIN_LABEL}(\.{_DOMAIN_LABEL})+"
_EMAIL_RE = re.compile(rf"^{_LOCAL}@{_DOMAIN}$")


def _has_control(s: str) -> bool:
    return any(ch in s for ch in ("\r", "\n", "\x00"))


def validate_address_field(field_name: str, raw: str | None, *, allow_display_name: bool) -> str:
    """Validate `from`, `reply_to`, or `to`.

    `allow_display_name=False` is used for `to` — the agent passes a bare
    address. `allow_display_name=True` is used for `from`/`reply_to`, which
    come from operator-controlled .env and may carry a Name <addr> form.

    Returns the validated string suitable for use as a header / API field.
    """
    if raw is None or not str(raw).strip():
        raise InvalidInput("missing_field", field_name)
    raw = str(raw)
    if _has_control(raw):
        raise InvalidInput("header_injection", field_name)
    if len(raw) > 320:  # RFC 5321 max forward-path length, generous
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
    """Extract the bare address from a validated `Name <addr>` form."""
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
