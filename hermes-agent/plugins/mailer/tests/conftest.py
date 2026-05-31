"""Shared pytest fixtures for the mailer plugin (the thin UDS client).

The plugin directory would be imported as `mailer`, but to keep the tests
independent of how Hermes' loader names it we bootstrap the package under a
fixed synthetic name (`hermes_email_pkg`). The plugin's relative imports
(`from . import _client`) resolve under that name.

These tests cover only what the CLIENT does: field-type pre-validation,
client-side attachment *path* validation, envelope framing, response
`v`-stripping, and the `daemon_unreachable` path. The policy enforcement
(allowlist, rate-limit, magic bytes, header injection, transports, and the
contacts directory) lives in the daemon and is tested under
`../../mail-relay/tests/`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ALIAS = "hermes_email_pkg"


def _bootstrap_plugin_package() -> None:
    if PACKAGE_ALIAS in sys.modules:
        return
    init_path = PLUGIN_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        PACKAGE_ALIAS,
        init_path,
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
    # Attachments may be staged anywhere under tmp_path (an allowed prefix)
    # in addition to /tmp/.
    monkeypatch.setenv("EMAIL_ATTACHMENT_ALLOWED_PREFIXES",
                       f"/tmp/:{tmp_path}/")
    # Point the client at a socket that does NOT exist, so a test that
    # reaches the connect() step gets `daemon_unreachable` instead of
    # accidentally talking to the real daemon running on this host.
    monkeypatch.setenv("HERMES_MAILER_SOCKET", str(tmp_path / "no-such.sock"))
    for k in list(os.environ):
        if k.startswith("EMAIL_MAX_"):
            monkeypatch.delenv(k, raising=False)
    yield tmp_path


@pytest.fixture
def fixtures_dir() -> Path:
    return PLUGIN_ROOT / "fixtures"


@pytest.fixture
def stage_fixture(fixtures_dir, tmp_path):
    """Copy a fixture into tmp_path (an allowed prefix) and return its path."""
    import shutil

    def _stage(name: str, *, rename: str | None = None) -> Path:
        src = fixtures_dir / name
        dst = tmp_path / (rename or name)
        shutil.copy2(src, dst)
        return dst

    return _stage
