"""Daemon-side configuration loader.

Reads `/etc/hermes-mailer/.env` (overridable via `HERMES_MAILER_CONFIG_DIR`
for tests). Produces a typed Config snapshot used by the request handler.

Caller-related directories live under the same prefix:
  $HERMES_MAILER_CONFIG_DIR/.env              # secrets, transport choice
  $HERMES_MAILER_CONFIG_DIR/allowlist.yaml    # single-caller convenience
  $HERMES_MAILER_CONFIG_DIR/allowlists/<caller>.yaml  # multi-caller form

State (DB, audit log, dry-run dumps) lives under HERMES_MAILER_STATE_DIR
(default `/var/lib/hermes-mailer`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


# ---- tiny inline dotenv parser (no third-party dep at startup) -----------

_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _DOTENV_LINE.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            if not val or val[0] not in ("\"", "'"):
                # strip inline " #comment" if no quoting
                hash_idx = val.find(" #")
                if hash_idx >= 0:
                    val = val[:hash_idx].rstrip()
            if len(val) >= 2 and val[0] == val[-1] == '"':
                val = val[1:-1].encode("utf-8").decode("unicode_escape")
            elif len(val) >= 2 and val[0] == val[-1] == "'":
                val = val[1:-1]
            if key not in os.environ:
                os.environ[key] = val


# ---- helpers -------------------------------------------------------------

def _bool(env_value: str | None, default: bool) -> bool:
    if env_value is None:
        return default
    return env_value.strip().lower() in {"1", "true", "yes", "on"}


def _int(env_value: str | None, default: int) -> int:
    if env_value is None or env_value.strip() == "":
        return default
    try:
        return int(env_value.strip())
    except ValueError:
        return default


# ---- typed config --------------------------------------------------------

@dataclass(frozen=True)
class Config:
    config_dir: Path
    state_dir: Path
    socket_path: Path

    allowlist_single_path: Path           # single-caller convenience form
    allowlists_dir: Path                  # multi-caller form (preferred when present)
    audit_log_path: Path
    ratelimit_db_path: Path
    dryrun_dir: Path

    dry_run: bool
    transport: str

    email_from: str
    reply_to: str | None

    limit_tz: str
    max_attachment_bytes: int
    max_total_bytes: int
    max_request_bytes: int
    reservation_ttl_seconds: int

    resend_api_key: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_starttls: bool

    # Caller resolution: UID -> caller identity.
    # Populated from CALLER_UID_<caller>=<uid> env keys, e.g.
    #   CALLER_UID_elena=1001
    # If the connecting UID is root (0), it is mapped to caller "_root_admin"
    # which has no policy by default (sends are rejected as unknown_caller
    # unless an explicit allowlist for "_root_admin" exists).
    caller_uid_map: dict[int, str]


_CALLER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


class ConfigError(Exception):
    pass


def load_config() -> Config:
    config_dir = Path(os.environ.get("HERMES_MAILER_CONFIG_DIR") or "/etc/hermes-mailer")
    _load_env_file(config_dir / ".env")

    state_dir = Path(os.environ.get("HERMES_MAILER_STATE_DIR") or "/var/lib/hermes-mailer")
    runtime_dir = Path(os.environ.get("HERMES_MAILER_RUNTIME_DIR") or "/run/hermes-mailer")

    # UID map: env vars of the form CALLER_UID_<name>=<int>.
    # Constraints (validated at load time, not first request):
    #   - caller name lowercase + underscores only, max 30 chars
    #   - UID values are integers
    #   - no two callers share the same UID
    # The transformation env-key -> caller-name is identity (no
    # underscore->hyphen rewriting); allowlist files for "winnow_agent"
    # therefore live at allowlists/winnow_agent.yaml.
    uid_map: dict[int, str] = {}
    for k, v in sorted(os.environ.items()):
        if not k.startswith("CALLER_UID_"):
            continue
        caller_name = k[len("CALLER_UID_"):].lower()
        if not _CALLER_NAME_RE.match(caller_name):
            raise ConfigError(
                f"invalid caller name in {k}={v!r}: "
                f"must match {_CALLER_NAME_RE.pattern}"
            )
        try:
            uid = int(v.strip())
        except ValueError:
            raise ConfigError(f"non-integer UID in {k}={v!r}")
        if uid in uid_map and uid_map[uid] != caller_name:
            raise ConfigError(
                f"duplicate UID {uid}: maps to both "
                f"{uid_map[uid]!r} and {caller_name!r}"
            )
        uid_map[uid] = caller_name

    return Config(
        config_dir=config_dir,
        state_dir=state_dir,
        socket_path=Path(os.environ.get("HERMES_MAILER_SOCKET") or (runtime_dir / "sock")),
        allowlist_single_path=Path(
            os.environ.get("HERMES_MAILER_ALLOWLIST_PATH") or (config_dir / "allowlist.yaml")
        ),
        allowlists_dir=Path(
            os.environ.get("HERMES_MAILER_ALLOWLISTS_DIR") or (config_dir / "allowlists")
        ),
        audit_log_path=Path(
            os.environ.get("HERMES_MAILER_AUDIT_LOG_PATH") or (state_dir / "sent.log")
        ),
        ratelimit_db_path=Path(
            os.environ.get("HERMES_MAILER_RATELIMIT_DB_PATH") or (state_dir / "ratelimit.db")
        ),
        dryrun_dir=Path(
            os.environ.get("HERMES_MAILER_DRYRUN_DIR") or (state_dir / "dryrun")
        ),
        dry_run=_bool(os.environ.get("EMAIL_DRY_RUN"), default=True),
        transport=(os.environ.get("EMAIL_TRANSPORT") or "dry_run").strip().lower(),
        email_from=(os.environ.get("EMAIL_FROM") or "").strip(),
        reply_to=(os.environ.get("EMAIL_REPLY_TO") or "").strip() or None,
        limit_tz=(os.environ.get("EMAIL_LIMIT_TZ") or "local").strip(),
        max_attachment_bytes=_int(
            os.environ.get("EMAIL_MAX_ATTACHMENT_BYTES"), 10 * 1024 * 1024
        ),
        max_total_bytes=_int(
            os.environ.get("EMAIL_MAX_TOTAL_BYTES"), 25 * 1024 * 1024
        ),
        max_request_bytes=_int(
            os.environ.get("HERMES_MAILER_MAX_REQUEST_BYTES"), 36 * 1024 * 1024
        ),
        reservation_ttl_seconds=_int(
            os.environ.get("EMAIL_RESERVATION_TTL_SECONDS"), 600
        ),
        resend_api_key=(os.environ.get("RESEND_API_KEY") or "").strip() or None,
        smtp_host=(os.environ.get("SMTP_HOST") or "").strip() or None,
        smtp_port=_int(os.environ.get("SMTP_PORT"), 587),
        smtp_username=(os.environ.get("SMTP_USERNAME") or "").strip() or None,
        smtp_password=(os.environ.get("SMTP_PASSWORD") or "").strip() or None,
        smtp_starttls=_bool(os.environ.get("SMTP_STARTTLS"), default=True),
        caller_uid_map=uid_map,
    )


def ensure_state_dirs(cfg: Config) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg.dryrun_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
