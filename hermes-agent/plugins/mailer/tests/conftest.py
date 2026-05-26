"""Shared pytest fixtures.

The plugin directory is named `email/` because Hermes uses the dir name as
the plugin name — but that collides with Python's stdlib `email` package.
We sidestep the collision by registering the plugin under a non-colliding
synthetic name (`hermes_email_pkg`) using importlib. The plugin's own
relative imports (`from .errors import …`) resolve under that synthetic
name. In production, Hermes' own loader names the package whatever it
likes — the relative-import structure is robust to both.
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
    # Loading __init__.py also imports handler/schemas eagerly, which is fine.
    spec.loader.exec_module(mod)


_bootstrap_plugin_package()


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    pdir = tmp_path / "email-plugin"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "state").mkdir(parents=True, exist_ok=True)
    (pdir / "state" / "dryrun").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMAIL_PLUGIN_DIR", str(pdir))
    for k in list(os.environ):
        if k.startswith("EMAIL_") and k != "EMAIL_PLUGIN_DIR":
            monkeypatch.delenv(k, raising=False)
        if k.startswith("SMTP_") or k.startswith("RESEND_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_DRY_RUN", "true")
    monkeypatch.setenv("EMAIL_TRANSPORT", "dry_run")
    monkeypatch.setenv("EMAIL_FROM", "Hermes Test <test@example.com>")
    # Allow attachments from the test tmp_path too, not just /tmp/.
    monkeypatch.setenv("EMAIL_ATTACHMENT_ALLOWED_PREFIXES",
                       f"/tmp/:{tmp_path}/")
    yield pdir


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
