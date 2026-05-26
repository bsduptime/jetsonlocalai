from __future__ import annotations

import os
import shutil

import pytest

from hermes_email_pkg import attachments
from hermes_email_pkg.errors import InvalidInput


@pytest.mark.parametrize("fname", [
    "tiny.pdf", "tiny.png", "tiny.jpg", "tiny.gif", "tiny.webp",
    "tiny.mp3", "tiny.m4a", "tiny.wav", "tiny.ogg", "tiny.flac",
    "tiny.csv", "tiny.md",
])
def test_each_fixture_loads(stage_fixture, fname, tmp_path):
    p = stage_fixture(fname)
    a = attachments.load_attachment(
        str(p),
        max_bytes=1_000_000,
        allowed_prefixes=[str(tmp_path) + "/"],
    )
    assert a.name == fname
    assert a.mime == attachments.MIME_FOR_EXT[p.suffix.lower()]
    assert len(a.content) == p.stat().st_size


def test_rejects_relative_path(tmp_path):
    with pytest.raises(InvalidInput) as ei:
        attachments.load_attachment(
            "relative.pdf",
            max_bytes=10_000,
            allowed_prefixes=[str(tmp_path) + "/"],
        )
    assert ei.value.reason == "attachment_path_not_absolute"


def test_rejects_path_outside_prefix(stage_fixture, tmp_path):
    p = stage_fixture("tiny.pdf")
    with pytest.raises(InvalidInput) as ei:
        attachments.load_attachment(
            str(p),
            max_bytes=10_000,
            allowed_prefixes=["/nonexistent/prefix/"],
        )
    assert ei.value.reason == "attachment_outside_allowed_prefixes"


def test_rejects_wrong_extension(stage_fixture, tmp_path):
    p = stage_fixture("tiny.pdf", rename="evil.exe")
    with pytest.raises(InvalidInput) as ei:
        attachments.load_attachment(
            str(p),
            max_bytes=10_000,
            allowed_prefixes=[str(tmp_path) + "/"],
        )
    assert ei.value.reason == "attachment_extension_not_allowed"


def test_rejects_magic_mismatch(tmp_path):
    """File renamed to .pdf but content is not a PDF — magic mismatch."""
    p = tmp_path / "fake.pdf"
    p.write_bytes(b"not a real pdf\n")
    with pytest.raises(InvalidInput) as ei:
        attachments.load_attachment(
            str(p),
            max_bytes=10_000,
            allowed_prefixes=[str(tmp_path) + "/"],
        )
    assert ei.value.reason == "attachment_magic_mismatch"


def test_rejects_empty(tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(InvalidInput) as ei:
        attachments.load_attachment(
            str(p),
            max_bytes=10_000,
            allowed_prefixes=[str(tmp_path) + "/"],
        )
    assert ei.value.reason == "attachment_empty"


def test_rejects_too_large(stage_fixture, tmp_path):
    p = stage_fixture("tiny.pdf")
    with pytest.raises(InvalidInput) as ei:
        attachments.load_attachment(
            str(p),
            max_bytes=10,
            allowed_prefixes=[str(tmp_path) + "/"],
        )
    assert ei.value.reason == "attachment_too_large"


def test_rejects_fifo(tmp_path):
    """A FIFO masquerading as a .pdf must be rejected before any read."""
    fifo = tmp_path / "weird.pdf"
    os.mkfifo(str(fifo))
    try:
        with pytest.raises(InvalidInput) as ei:
            attachments.load_attachment(
                str(fifo),
                max_bytes=10_000,
                allowed_prefixes=[str(tmp_path) + "/"],
            )
        assert ei.value.reason == "attachment_not_regular_file"
    finally:
        fifo.unlink()


def test_symlink_to_outside_prefix_rejected(stage_fixture, tmp_path):
    """A symlink under /tmp/ pointing to /etc/passwd must be rejected by the
    prefix check (resolve() collapses the symlink before we compare)."""
    target = tmp_path / "secret"
    target.write_text("secret", encoding="utf-8")
    # Move it OUTSIDE the allowed prefix
    outside_dir = tmp_path.parent / "outside-prefix"
    outside_dir.mkdir(exist_ok=True)
    outside_target = outside_dir / "secret"
    shutil.move(str(target), str(outside_target))
    link = tmp_path / "link.pdf"
    link.symlink_to(outside_target)
    try:
        with pytest.raises(InvalidInput) as ei:
            attachments.load_attachment(
                str(link),
                max_bytes=10_000,
                allowed_prefixes=[str(tmp_path) + "/"],
            )
        assert ei.value.reason == "attachment_outside_allowed_prefixes"
    finally:
        outside_target.unlink()
        outside_dir.rmdir()


def test_load_all_enforces_total(stage_fixture, tmp_path):
    p1 = stage_fixture("tiny.pdf", rename="a.pdf")
    p2 = stage_fixture("tiny.pdf", rename="b.pdf")
    each_size = p1.stat().st_size
    with pytest.raises(InvalidInput) as ei:
        attachments.load_all(
            [str(p1), str(p2)],
            max_attachment_bytes=10_000,
            max_total_bytes=each_size + 1,    # smaller than 2 * size
            allowed_prefixes=[str(tmp_path) + "/"],
        )
    assert ei.value.reason == "attachments_total_too_large"


def test_load_all_empty_returns_empty():
    assert attachments.load_all(
        [],
        max_attachment_bytes=10_000,
        max_total_bytes=100_000,
        allowed_prefixes=["/tmp/"],
    ) == []
