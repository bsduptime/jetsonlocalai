"""hermes-calendar relay — the daemon side of the calendar plugin.

Holds all policy and side effects: contact resolution, rate limiting,
audit logging, and the transport that either (dry-run) renders what it
WOULD do or (google) actually writes the event and sends invites. The
plugin talks to this over a Unix socket and never sees credentials.

Public surface used by the daemon and by the demo/tests:
  - config.load_config(...)   -> Config
  - server.handle_request(...) -> dict
  - server.run_daemon(cfg)     -> never returns (serves the UDS)
"""

from __future__ import annotations

__all__ = ["config", "event", "transport", "server"]
