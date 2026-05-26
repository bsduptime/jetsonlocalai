"""Attachment validation and loading.

Open-once, validate-on-fd, read-from-fd. This pattern (Codex Finding 4)
defeats TOCTOU swap attacks where an attacker validates a benign-looking
file and then replaces it before the bytes are loaded.

For each attachment:
  1. Path must be absolute.
  2. Resolve symlinks (strict=True — raises if missing).
  3. Resolved path must start with one of the allowed prefixes.
  4. Extension must be allowed.
  5. Open with O_NOFOLLOW | O_CLOEXEC.
  6. fstat must show S_ISREG (no FIFOs, devices, etc. — Codex Finding 5).
  7. Size must be within per-attachment limit.
  8. Read content off the open fd.
  9. Magic bytes must match the extension.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class Attachment:
    name: str
    mime: str
    content: bytes


def _check_text_like(content: bytes) -> bool:
    """Decode the WHOLE file as UTF-8 and ensure no control bytes except
    tab/cr/lf. We've already capped file size at EMAIL_MAX_ATTACHMENT_BYTES
    so memory is bounded — full check defeats the "benign prefix then
    binary tail" trick."""
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
        # MPEG audio sync: 11 bits set (0xFFE0..0xFFFF).
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


def _check_prefix(resolved: Path, allowed_prefixes: list[str]) -> bool:
    s = str(resolved)
    for prefix in allowed_prefixes:
        # Normalize so /tmp matches /tmp/ ; require trailing slash semantically.
        norm = prefix if prefix.endswith("/") else prefix + "/"
        if s == norm.rstrip("/"):
            return True  # the dir itself; weird but be lenient
        if s.startswith(norm):
            return True
    return False


def load_attachment(raw_path: str, *, max_bytes: int, allowed_prefixes: list[str]) -> Attachment:
    if not isinstance(raw_path, str) or not raw_path:
        raise InvalidInput("attachment_invalid_path", repr(raw_path))
    p = Path(raw_path)
    if not p.is_absolute():
        raise InvalidInput("attachment_path_not_absolute", raw_path)
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        raise InvalidInput("attachment_not_found", raw_path) from None
    except OSError as e:
        raise InvalidInput("attachment_resolve_failed", str(e)) from None
    if not _check_prefix(resolved, allowed_prefixes):
        raise InvalidInput("attachment_outside_allowed_prefixes", str(resolved))
    ext = resolved.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise InvalidInput("attachment_extension_not_allowed", resolved.name)

    # Pre-flight lstat so a FIFO/device/etc. doesn't block in os.open.
    # We re-check with fstat after open to close the lstat→open TOCTOU
    # window. O_NONBLOCK is belt-and-suspenders in case the filesystem
    # somehow swaps a regular file for a FIFO between the two calls.
    try:
        pre_stat = os.lstat(str(resolved))
    except OSError as e:
        raise InvalidInput("attachment_stat_failed", str(e)) from None
    if not stat.S_ISREG(pre_stat.st_mode):
        raise InvalidInput("attachment_not_regular_file", resolved.name)

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(str(resolved), flags)
    except OSError as e:
        raise InvalidInput("attachment_open_failed", str(e)) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise InvalidInput("attachment_not_regular_file", resolved.name)
        if st.st_size == 0:
            raise InvalidInput("attachment_empty", resolved.name)
        if st.st_size > max_bytes:
            raise InvalidInput("attachment_too_large", resolved.name)
        # Read up to st.st_size first; defend against truncation/growth.
        chunks = []
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) != st.st_size:
            # File shrank between fstat and read — suspicious; reject.
            raise InvalidInput("attachment_short_read", resolved.name)
        # Final sanity: are there trailing bytes (file grew)? Try one more read;
        # if anything comes back, the file grew between fstat and now. Reject.
        extra = os.read(fd, 1)
        if extra:
            raise InvalidInput("attachment_changed_during_read", resolved.name)
    finally:
        os.close(fd)

    if not magic_bytes_ok(ext, content):
        raise InvalidInput("attachment_magic_mismatch", resolved.name)
    return Attachment(name=resolved.name, mime=MIME_FOR_EXT[ext], content=content)


def load_all(
    raw_paths: list[str],
    *,
    max_attachment_bytes: int,
    max_total_bytes: int,
    allowed_prefixes: list[str],
) -> list[Attachment]:
    attachments: list[Attachment] = []
    total = 0
    for rp in raw_paths or []:
        a = load_attachment(
            rp,
            max_bytes=max_attachment_bytes,
            allowed_prefixes=allowed_prefixes,
        )
        total += len(a.content)
        if total > max_total_bytes:
            raise InvalidInput("attachments_total_too_large", str(total))
        attachments.append(a)
    return attachments
