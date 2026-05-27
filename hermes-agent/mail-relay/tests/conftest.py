"""Shared pytest fixtures for the mail-relay daemon tests.

The daemon-side modules live in `hermes_mailer/` next to the tests/. We
insert the parent on sys.path so tests can `from hermes_mailer import …`.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[1]  # hermes-agent/mail-relay/
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Every test gets its own /etc + /var/lib + /run equivalents under
    tmp_path, plus a clean env."""
    cfg_dir = tmp_path / "etc"
    state_dir = tmp_path / "var-lib"
    runtime_dir = tmp_path / "run"
    cfg_dir.mkdir(); state_dir.mkdir(); runtime_dir.mkdir()
    (state_dir / "dryrun").mkdir()

    monkeypatch.setenv("HERMES_MAILER_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HERMES_MAILER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HERMES_MAILER_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("HERMES_MAILER_SOCKET", str(runtime_dir / "sock"))

    # Reset all EMAIL_* / SMTP_ / RESEND_ keys.
    for k in list(os.environ):
        if k.startswith(("EMAIL_", "SMTP_", "RESEND_", "CALLER_UID_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_DRY_RUN", "true")
    monkeypatch.setenv("EMAIL_TRANSPORT", "dry_run")
    monkeypatch.setenv("EMAIL_FROM", "Daemon Test <test@example.com>")

    # Reset the in-memory allowlist cache between tests.
    from hermes_mailer import allowlist as _a
    _a._reset_cache_for_tests()

    yield {
        "cfg_dir": cfg_dir,
        "state_dir": state_dir,
        "runtime_dir": runtime_dir,
        "socket_path": runtime_dir / "sock",
    }


@pytest.fixture
def fixtures_dir() -> Path:
    """Reuse the plugin's fixture binaries (tiny.pdf etc.). They live
    alongside the plugin code at hermes-agent/plugins/mailer/fixtures/."""
    return Path(__file__).resolve().parents[2] / "plugins" / "mailer" / "fixtures"


@pytest.fixture
def write_allowlist(_isolate_env):
    """Returns a callable: write_allowlist(yaml_text) installs the file."""
    cfg_dir = _isolate_env["cfg_dir"]

    def _write(text: str):
        (cfg_dir / "allowlist.yaml").write_text(text, encoding="utf-8")

    return _write


@pytest.fixture
def fake_resend(monkeypatch):
    """Install a fake `resend` module so tests can exercise the Resend
    path without touching the network."""
    import sys, types

    calls = []

    fake = types.ModuleType("resend")

    class _Emails:
        @staticmethod
        def send(params):
            calls.append(params)
            return {"id": "re_fake_123"}

    fake.Emails = _Emails
    fake.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake)
    return calls


@pytest.fixture
def daemon_process(_isolate_env, monkeypatch):
    """Spin up the daemon in a thread (not subprocess) bound to our
    test socket. Yields the socket path; tears down on exit."""
    from hermes_mailer import config as _cfg, daemon as _daemon

    # The daemon does SO_PEERCRED to resolve UID -> caller. In tests we
    # connect from the SAME process — UID is the test user. We map it
    # via CALLER_UID_<name>= env so the daemon treats us as "elena".
    monkeypatch.setenv(f"CALLER_UID_elena", str(os.getuid()))

    cfg = _cfg.load_config()
    _cfg.ensure_state_dirs(cfg)
    from hermes_mailer import ratelimit
    ratelimit.init_schema(cfg.ratelimit_db_path)

    server = _daemon._bind_socket(cfg.socket_path)
    stop = threading.Event()
    t = threading.Thread(
        target=_daemon._accept_loop,
        args=(server, cfg, stop),
        daemon=True,
    )
    t.start()
    # Wait briefly for the loop to start polling.
    time.sleep(0.05)
    yield str(cfg.socket_path)
    stop.set()
    try:
        server.close()
    except OSError:
        pass
    t.join(timeout=2.0)
