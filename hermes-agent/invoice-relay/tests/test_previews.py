"""Preview PDFs are spooled to a file; the base64 blob never rides back."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from hermes_greeninvoice import handler

PDF_BYTES = b"%PDF-1.7\n%fake-invoice-pdf\n"


class PreviewClient:
    """Mimics GreenInvoice /documents/preview returning a base64 PDF."""

    def post(self, path, body, *, idempotent=True):
        assert path == "/documents/preview"
        return {"file": base64.b64encode(PDF_BYTES).decode("ascii"),
                "type": body.get("type")}


def _draft_args():
    return {
        "type": 305,
        "client": {"id": "cli_1"},
        "income": [{"description": "Work", "quantity": 1, "price": 100,
                    "currency": "ILS"}],
        "currency": "ILS",
    }


def _req(op, args):
    return {"v": 1, "op": op, "request_id": "smoke123", "args": args}


def test_live_draft_spools_pdf_and_strips_blob(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    cfg = load_cfg()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("draft_invoice", _draft_args()),
                          get_client=lambda: PreviewClient())
    assert resp["ok"] is True
    result = resp["result"]
    # The base64 blob must be GONE; a path must be present.
    assert "file" not in result
    assert result["preview_pdf_bytes"] == len(PDF_BYTES)
    p = Path(result["preview_pdf_path"])
    assert p.is_file()
    assert p.read_bytes() == PDF_BYTES
    # File landed under the configured previews dir.
    assert str(p).startswith(str(cfg.previews_dir))


def test_spool_disabled_keeps_inline(load_cfg, monkeypatch):
    monkeypatch.setenv("GI_DRY_RUN", "false")
    monkeypatch.setenv("GI_SPOOL_PREVIEWS", "false")
    cfg = load_cfg()
    resp = handler.handle(cfg=cfg, caller="elena",
                          request=_req("draft_invoice", _draft_args()),
                          get_client=lambda: PreviewClient())
    assert "file" in resp["result"]
    assert "preview_pdf_path" not in resp["result"]


def test_prune_caps_file_count(load_cfg, monkeypatch):
    from hermes_greeninvoice import previews
    monkeypatch.setenv("GI_PREVIEW_MAX_FILES", "3")
    cfg = load_cfg()
    previews.ensure_dir(cfg)
    # Create 5 stale-ordered files.
    import os, time
    for i in range(5):
        f = cfg.previews_dir / f"p{i}.pdf"
        f.write_bytes(b"x")
        os.utime(f, (time.time() - (10 - i), time.time() - (10 - i)))
    previews.prune(cfg)
    remaining = sorted(cfg.previews_dir.glob("*.pdf"))
    assert len(remaining) == 3  # newest 3 kept
