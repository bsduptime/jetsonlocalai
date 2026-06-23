"""Shared pytest fixtures for the voice plugin.

The plugin directory would be imported as `voice`, but to keep the tests
independent of how Hermes' loader names it we bootstrap the package under a
fixed synthetic name (`hermes_voice_pkg`). The plugin's relative imports
(`from . import _client`) resolve under that name.

These tests cover the handler's input validation and the response-shape
contract. The two network hops (synthesize, deliver) are monkeypatched —
the tests never touch the real TTS server or the Mac listener.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ALIAS = "hermes_voice_pkg"


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
def _isolate_env(monkeypatch):
    # Point both hops at addresses that will never be hit, so a test that
    # forgets to monkeypatch fails loudly rather than talking to the real
    # TTS server or the Mac.
    monkeypatch.setenv("HERMES_VOICE_TTS_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_VOICE_LISTENER_URL", "http://127.0.0.1:9")
    yield
