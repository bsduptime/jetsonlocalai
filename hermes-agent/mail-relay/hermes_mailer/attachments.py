"""Daemon-side attachment validation.

The DAEMON validates attachments from BYTES (not paths). The client is
responsible for opening the file on disk, checking it's under the allowed
prefix + a regular file, and shipping the bytes. The daemon then:

  - Re-checks the extension (against the filename the client sent).
  - Re-checks the size against per-attachment + per-message caps.
  - Re-checks the magic bytes against the extension.

A compromised client can lie about the filename but cannot smuggle e.g.
an `.exe` past the daemon, because the magic-byte check happens on the
actual bytes regardless of what filename the client claimed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import InvalidInput

ALLOWED_EXTENSIONS = {
    ".pdf", ".md",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".mp3", ".m4a", ".wav", ".ogg", ".flac",
    ".csv",
}

MIME_FOR_EXT = {
    ".pdf":  "application/pdf",
    ".md":   "text/markdown",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".wav":  "audio/wav",
    ".ogg":  "audio/ogg",
    ".flac": "audio/flac",
    ".csv":  "text/csv",
}

_M4A_BRANDS = {b"M4A ", b"M4B ", b"M4V ", b"mp42", b"isom", b"qt  ", b"M4P "}

_BAD_BASENAME_CHARS = {"/", "\x00"}


@dataclass(frozen=True)
class Attachment:
    name: str
    mime: str
    content: bytes


def _check_text_like(content: bytes) -> bool:
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    for ch in decoded:
        o = ord(ch)
        if o < 0x20 and ch not in ("\t", "\r", "\n"):
            return False
    return True


def magic_bytes_ok(ext: str, content: bytes) -> bool:
    if ext == ".pdf":
        return content.startswith(b"%PDF-")
    if ext == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return content.startswith(b"\xff\xd8\xff")
    if ext == ".gif":
        return content[:6] in (b"GIF87a", b"GIF89a")
    if ext == ".webp":
        return content[:4] == b"RIFF" and len(content) >= 12 and content[8:12] == b"WEBP"
    if ext == ".mp3":
        if content.startswith(b"ID3"):
            return True
        if len(content) >= 2 and content[0] == 0xFF and (content[1] & 0xE0) == 0xE0:
            return True
        return False
    if ext == ".m4a":
        if len(content) < 12:
            return False
        if content[4:8] != b"ftyp":
            return False
        return content[8:12] in _M4A_BRANDS
    if ext == ".wav":
        return content[:4] == b"RIFF" and len(content) >= 12 and content[8:12] == b"WAVE"
    if ext == ".ogg":
        return content.startswith(b"OggS")
    if ext == ".flac":
        return content.startswith(b"fLaC")
    if ext in (".csv", ".md"):
        return _check_text_like(content)
    return False


def validate_attachment(*, filename: str, content: bytes,
                         max_bytes: int) -> Attachment:
    if not isinstance(filename, str) or not filename:
        raise InvalidInput("attachment_invalid_filename", repr(filename))
    if any(c in filename for c in _BAD_BASENAME_CHARS):
        raise InvalidInput("attachment_bad_basename", filename)
    # Strip any path components a lying client might have included.
    basename = os.path.basename(filename)
    if not basename or basename in (".", ".."):
        raise InvalidInput("attachment_bad_basename", filename)
    if not isinstance(content, (bytes, bytearray)):
        raise InvalidInput("attachment_invalid_content", "not_bytes")
    if len(content) == 0:
        raise InvalidInput("attachment_empty", basename)
    if len(content) > max_bytes:
        raise InvalidInput("attachment_too_large", basename)
    ext = os.path.splitext(basename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise InvalidInput("attachment_extension_not_allowed", basename)
    if not magic_bytes_ok(ext, bytes(content)):
        raise InvalidInput("attachment_magic_mismatch", basename)
    return Attachment(name=basename, mime=MIME_FOR_EXT[ext], content=bytes(content))


def validate_all(items, *, max_attachment_bytes: int,
                 max_total_bytes: int) -> list[Attachment]:
    attachments: list[Attachment] = []
    total = 0
    for item in items or []:
        if not isinstance(item, dict):
            raise InvalidInput("attachment_invalid_item", type(item).__name__)
        # content_b64 is base64 string on the wire; decoding is done in
        # the daemon's request handler BEFORE calling this function, which
        # receives raw bytes. This function is also unit-testable with bytes.
        filename = item.get("filename")
        content = item.get("content")
        a = validate_attachment(
            filename=filename, content=content,
            max_bytes=max_attachment_bytes,
        )
        total += len(a.content)
        if total > max_total_bytes:
            raise InvalidInput("attachments_total_too_large", str(total))
        attachments.append(a)
    return attachments
