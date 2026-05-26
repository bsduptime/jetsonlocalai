"""Configuration loader.

Reads the plugin-private .env file (loaded into os.environ) and produces a
typed Config object. Centralizes defaults so handler / transport code never
sprinkles os.environ reads around.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .dotenv import load_dotenv
from .hermes_home import plugin_data_dir


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


def _prefixes(env_value: str | None, default: list[str]) -> list[str]:
    if env_value is None or env_value.strip() == "":
        return default
    return [p for p in (s.strip() for s in env_value.split(":")) if p]


@dataclass(frozen=True)
class Config:
    plugin_dir: Path
    state_dir: Path
    allowlist_path: Path
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
    allowed_prefixes: list[str]
    reservation_ttl_seconds: int

    resend_api_key: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_starttls: bool


def load_config() -> Config:
    """Load .env into os.environ (idempotent) and return Config snapshot."""
    pdir = plugin_data_dir()
    env_path = pdir / ".env"
    load_dotenv(env_path)

    state_dir = pdir / "state"

    return Config(
        plugin_dir=pdir,
        state_dir=state_dir,
        allowlist_path=Path(
            os.environ.get("EMAIL_ALLOWLIST_PATH") or (pdir / "allowlist.yaml")
        ),
        audit_log_path=Path(
            os.environ.get("EMAIL_AUDIT_LOG_PATH") or (state_dir / "sent.log")
        ),
        ratelimit_db_path=Path(
            os.environ.get("EMAIL_RATELIMIT_DB_PATH") or (state_dir / "ratelimit.db")
        ),
        dryrun_dir=Path(
            os.environ.get("EMAIL_DRYRUN_DIR") or (state_dir / "dryrun")
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
        allowed_prefixes=_prefixes(
            os.environ.get("EMAIL_ATTACHMENT_ALLOWED_PREFIXES"), ["/tmp/"]
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
    )


def ensure_state_dirs(cfg: Config) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg.dryrun_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
