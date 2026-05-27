"""Shared pytest fixtures.

We use the same `importlib.util.spec_from_file_location` trick the mailer
plugin uses: load the plugin under a synthetic package name so its relative
imports resolve regardless of where it lives on disk. This keeps tests
runnable both from the source tree and from the installed symlink path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ALIAS = "hermes_vault_pkg"


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


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Build a fresh tmp vault and point HERMES_VAULT_ROOT at it."""
    root = tmp_path / "vault"
    (root / "agents" / "hermes" / "observations").mkdir(parents=True)
    (root / "agents" / "hermes" / "memory").mkdir(parents=True)
    (root / "agents" / "hermes" / "drafts").mkdir(parents=True)
    (root / "areas").mkdir()
    (root / "daily").mkdir()
    (root / "INDEX.md").write_text(
        "---\nlast_compiled: 2026-05-27\n---\n# Index\n", encoding="utf-8"
    )
    (root / "areas" / "schedule.md").write_text(
        "---\nlast_compiled: 2026-05-27\n---\n# schedule\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def handler():
    """Return the plugin's handler module."""
    from hermes_vault_pkg import handler as h  # type: ignore
    return h
