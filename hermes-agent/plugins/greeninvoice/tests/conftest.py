"""Fixtures for the greeninvoice plugin (thin UDS client + upload path guard).

The plugin directory would be imported as `greeninvoice`, but to stay
independent of Hermes' loader naming we bootstrap it under a fixed synthetic
package name so its relative imports (`from . import _client`) resolve.

These tests cover only the CLIENT side: the upload file-path allowlist and
framing handoff. Daemon policy (validation, rate-limit, the confirm gate, the
S3 SSRF guard) is tested under ../../../invoice-relay/tests/.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ALIAS = "hermes_gi_pkg"


def _bootstrap_plugin_package() -> None:
    if PACKAGE_ALIAS in sys.modules:
        return
    init_path = PLUGIN_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        PACKAGE_ALIAS, init_path,
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load plugin from {init_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_ALIAS] = mod
    spec.loader.exec_module(mod)


_bootstrap_plugin_package()


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    # Uploads may be read only from this allowed dir.
    monkeypatch.setenv("GI_UPLOAD_ALLOWED_DIRS", str(tmp_path / "media"))
    (tmp_path / "media").mkdir()
    # Point the client at a socket that does NOT exist so a test that reaches
    # connect() gets daemon_unreachable rather than the real daemon.
    monkeypatch.setenv("HERMES_GREENINVOICE_SOCKET", str(tmp_path / "no-such.sock"))
    for k in list(os.environ):
        if k.startswith("GI_UPLOAD_MAX"):
            monkeypatch.delenv(k, raising=False)
    yield tmp_path
