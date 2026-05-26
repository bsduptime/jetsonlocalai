"""JSON schemas exposed to the LLM via ctx.register_tool()."""

from __future__ import annotations

SEND_EMAIL = {
    "name": "send_email",
    "description": (
        "Send an email to a pre-approved contact. The recipient address must "
        "be on the operator-configured allowlist; otherwise this returns "
        "ok=false, error=not_allowed, reason=not_in_allowlist. Each recipient "
        "has a per-day cap that resets at local midnight; exceeding it "
        "returns ok=false, error=not_allowed, reason=rate_limit_exceeded. "
        "Attachments (optional) are absolute file paths to files under the "
        "configured staging directory (default /tmp). Allowed types: PDF, "
        "Markdown (.md), images (PNG/JPG/GIF/WEBP), audio (MP3/M4A/WAV/OGG/"
        "FLAC), CSV. Files are validated by magic bytes, not just extension. "
        "By default the tool is in dry-run mode and does NOT actually send — "
        "the rendered .eml is written to the state dir for inspection."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": (
                    "Recipient email address (bare address, no display name). "
                    "Must be on the allowlist."
                ),
            },
            "subject": {
                "type": "string",
                "description": "Subject line. No newlines. Max 200 chars.",
                "maxLength": 200,
            },
            "body": {
                "type": "string",
                "description": "Plain-text body of the email.",
            },
            "body_html": {
                "type": "string",
                "description": (
                    "Optional HTML body. The plain-text `body` is still "
                    "required and is included as the text/plain alternative."
                ),
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute file paths to attach. Each "
                    "path must resolve to a regular file under an allowed "
                    "prefix (default /tmp). Only PDF/Markdown/image/audio/CSV."
                ),
                "default": [],
            },
        },
        "required": ["to", "subject", "body"],
    },
}
