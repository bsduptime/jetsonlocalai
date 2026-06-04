"""Shared pytest fixtures for the hermes-greeninvoice daemon tests.

The daemon package lives in `hermes_greeninvoice/` next to tests/. We
insert the parent on sys.path so tests can import it and the thin client.
All tests run in dry-run with a fake upstream — no GreenInvoice creds.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[1]  # hermes-agent/invoice-relay/
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "etc"
    state_dir = tmp_path / "var-lib"
    runtime_dir = tmp_path / "run"
    cfg_dir.mkdir(); state_dir.mkdir(); runtime_dir.mkdir()

    monkeypatch.setenv("HERMES_GREENINVOICE_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HERMES_GREENINVOICE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HERMES_GREENINVOICE_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("HERMES_GREENINVOICE_SOCKET", str(runtime_dir / "sock"))

    for k in list(os.environ):
        if k.startswith(("GI_", "CALLER_UID_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GI_DRY_RUN", "true")
    monkeypatch.setenv("GI_ENV", "sandbox")

    yield {
        "cfg_dir": cfg_dir,
        "state_dir": state_dir,
        "runtime_dir": runtime_dir,
        "socket_path": runtime_dir / "sock",
    }


@pytest.fixture
def load_cfg(_isolate_env):
    """Return a freshly-loaded Config snapshot from the isolated env."""
    from hermes_greeninvoice import config as _cfg

    def _load():
        cfg = _cfg.load_config()
        _cfg.ensure_state_dirs(cfg)
        from hermes_greeninvoice import ratelimit
        ratelimit.init_schema(cfg.ratelimit_db_path)
        return cfg

    return _load


@pytest.fixture
def daemon_process(_isolate_env, monkeypatch):
    """Spin up the daemon in a thread bound to the test socket. The test
    process connects from its own UID, mapped to caller 'elena'."""
    from hermes_greeninvoice import config as _cfg, daemon as _daemon, ratelimit

    monkeypatch.setenv("CALLER_UID_elena", str(os.getuid()))

    cfg = _cfg.load_config()
    _cfg.ensure_state_dirs(cfg)
    ratelimit.init_schema(cfg.ratelimit_db_path)

    clients = _daemon._ClientHolder(cfg)
    server = _daemon._bind_socket(cfg.socket_path)
    stop = threading.Event()
    t = threading.Thread(
        target=_daemon._accept_loop,
        args=(server, cfg, clients, stop),
        daemon=True,
    )
    t.start()
    time.sleep(0.05)
    yield str(cfg.socket_path)
    stop.set()
    try:
        server.close()
    except OSError:
        pass
    t.join(timeout=2.0)
