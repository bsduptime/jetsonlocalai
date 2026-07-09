"""Shared pytest fixtures — same spec_from_file_location bootstrap as the
vault/mailer plugin tests, so the package's relative imports resolve
regardless of where the plugin lives on disk."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ALIAS = "hermes_household_pkg"


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
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "household"
    monkeypatch.setenv("HOUSEHOLD_STATE_DIR", str(d))
    return d


@pytest.fixture
def store(state_dir):
    import hermes_household_pkg._store as _store
    return _store


@pytest.fixture
def handler(state_dir):
    import hermes_household_pkg.handler as handler
    return handler
