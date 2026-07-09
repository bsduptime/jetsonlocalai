"""Daemon-side configuration loader.

Reads `/etc/hermes-greeninvoice/.env` (overridable via
`HERMES_GREENINVOICE_CONFIG_DIR` for tests). Produces a typed Config
snapshot used by the request handler.

  $CONFIG_DIR/.env                 # GreenInvoice API key id/secret, env, dry-run
State (DB, audit log) lives under HERMES_GREENINVOICE_STATE_DIR
(default `/var/lib/hermes-greeninvoice`).

Rate limits are configured per *action class*, each with an hourly and a
daily cap:

  issue        — create a real, irreversible document (305/320/400). TIGHT.
  draft        — render a preview (no record, no number burned). LOOSE.
  client_write — create/update a client. LOOSE.

Reads (get/search/download-links) are never rate-limited.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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


# ---- environments --------------------------------------------------------

GREENINVOICE_BASE_URLS = {
    "production": "https://api.greeninvoice.co.il/api/v1",
    "sandbox": "https://sandbox.d.greeninvoice.co.il/api/v1",
}

# Expense invoice files are uploaded via a presigned-S3 flow whose "get an
# upload URL" endpoint lives on a DIFFERENT host from the JSON API. See
# apiclient.get_upload_url / upload_file_to_s3.
GREENINVOICE_FILE_UPLOAD_URLS = {
    "production": "https://apigw.greeninvoice.co.il",
    "sandbox": "https://api.sandbox.d.greeninvoice.co.il",
}

# Action classes and their DEFAULT caps (per_hour, per_day). Each is
# independently overridable via env, see load_config().
DEFAULT_LIMITS = {
    "issue": (3, 10),           # irreversible documents (issue invoice / report
                                # an expense to tax) — tight. close_expense
                                # deliberately shares this class so it can't be
                                # used to double the irreversible-action budget.
    "draft": (20, 60),          # previews — loose, just an anti-spam backstop
    "client_write": (20, 100),  # create/update client — loose
    "expense_write": (20, 100),  # create/delete expense, create supplier — loose
    "expense_upload": (15, 60),  # upload a file → OCR draft — moderate (OCR cost)
}

# Document types the broker will *issue*. 305 tax invoice, 320 invoice+
# receipt, 400 receipt (for the "a 305 got paid" flow). Nothing else is
# issuable through this tool — no credit notes, no deletes.
ISSUABLE_DOCUMENT_TYPES = {305, 320, 400}

# Expense document types (the vendor-side ledger): 10 invoice, 20 receipt,
# 30 invoice+receipt, 40 other. Expense VAT types: 0 before-VAT, 1 included,
# 2 exempt.
EXPENSE_DOCUMENT_TYPES = {10, 20, 30, 40}
EXPENSE_VAT_TYPES = {0, 1, 2}


@dataclass(frozen=True)
class Config:
    config_dir: Path
    state_dir: Path
    # A filesystem Path for the UDS listener, or a "tcp://127.0.0.1:<port>"
    # string for the portable TCP listener (Windows has no AF_UNIX in
    # CPython). Kept as str in TCP mode — Path() would mangle the "//".
    socket_path: Path | str

    audit_log_path: Path
    ratelimit_db_path: Path
    previews_dir: Path

    spool_previews: bool
    preview_retention_seconds: int
    preview_max_files: int

    dry_run: bool
    env: str            # "sandbox" | "production"
    base_url: str
    file_upload_base_url: str   # host for GET /file-upload/v1/url (expenses)

    api_key_id: str | None
    api_key_secret: str | None

    limit_tz: str
    limits: dict[str, tuple[int, int]]   # action_class -> (per_hour, per_day)
    max_request_bytes: int
    # Raw byte ceiling for an uploaded invoice file (framed body of an
    # upload_expense_file request). Separate from max_request_bytes, which
    # bounds the JSON header line only.
    max_upload_file_bytes: int
    reservation_ttl_seconds: int

    http_timeout_seconds: int
    min_request_interval_ms: int   # client-side throttle (3 req/s -> ~334ms)

    # Caller resolution: UID -> caller identity (from CALLER_UID_<name>=<uid>).
    caller_uid_map: dict[int, str] = field(default_factory=dict)
    # TCP caller resolution: shared secret -> caller identity (from
    # GI_CALLER_TOKEN_<name>=<secret>). TCP has no SO_PEERCRED, so identity
    # comes from a per-caller token the client sends in the envelope. The
    # daemon refuses ALL TCP requests when this map is empty (fail closed).
    caller_token_map: dict[str, str] = field(default_factory=dict)


_CALLER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


class ConfigError(Exception):
    pass


def _load_limits() -> dict[str, tuple[int, int]]:
    """Per-action caps. Each class N has GI_LIMIT_<N>_PER_HOUR /
    GI_LIMIT_<N>_PER_DAY overrides; otherwise DEFAULT_LIMITS applies."""
    limits: dict[str, tuple[int, int]] = {}
    for klass, (def_hr, def_day) in DEFAULT_LIMITS.items():
        hr = _int(os.environ.get(f"GI_LIMIT_{klass.upper()}_PER_HOUR"), def_hr)
        day = _int(os.environ.get(f"GI_LIMIT_{klass.upper()}_PER_DAY"), def_day)
        # Caps must be >= 0; a 0 cap means "blocked entirely" which is a
        # legitimate (paranoid) config choice.
        limits[klass] = (max(0, hr), max(0, day))
    return limits


def load_config() -> Config:
    config_dir = Path(
        os.environ.get("HERMES_GREENINVOICE_CONFIG_DIR") or "/etc/hermes-greeninvoice"
    )
    _load_env_file(config_dir / ".env")

    state_dir = Path(
        os.environ.get("HERMES_GREENINVOICE_STATE_DIR") or "/var/lib/hermes-greeninvoice"
    )
    runtime_dir = Path(
        os.environ.get("HERMES_GREENINVOICE_RUNTIME_DIR") or "/run/hermes-greeninvoice"
    )

    env = (os.environ.get("GI_ENV") or "sandbox").strip().lower()
    if env not in GREENINVOICE_BASE_URLS:
        raise ConfigError(
            f"GI_ENV must be one of {sorted(GREENINVOICE_BASE_URLS)}, got {env!r}"
        )
    base_url = (os.environ.get("GI_BASE_URL") or GREENINVOICE_BASE_URLS[env]).rstrip("/")
    file_upload_base_url = (
        os.environ.get("GI_FILE_UPLOAD_BASE_URL")
        or GREENINVOICE_FILE_UPLOAD_URLS[env]
    ).rstrip("/")

    # UID map: CALLER_UID_<name>=<int>.
    uid_map: dict[int, str] = {}
    for k, v in sorted(os.environ.items()):
        if not k.startswith("CALLER_UID_"):
            continue
        caller_name = k[len("CALLER_UID_"):].lower()
        if not _CALLER_NAME_RE.match(caller_name):
            raise ConfigError(
                f"invalid caller name in {k}={v!r}: must match {_CALLER_NAME_RE.pattern}"
            )
        try:
            uid = int(v.strip())
        except ValueError:
            raise ConfigError(f"non-integer UID in {k}={v!r}")
        if uid in uid_map and uid_map[uid] != caller_name:
            raise ConfigError(
                f"duplicate UID {uid}: maps to both {uid_map[uid]!r} and {caller_name!r}"
            )
        uid_map[uid] = caller_name

    # Token map: GI_CALLER_TOKEN_<name>=<secret> (TCP-mode caller identity).
    token_map: dict[str, str] = {}
    for k, v in sorted(os.environ.items()):
        if not k.startswith("GI_CALLER_TOKEN_"):
            continue
        caller_name = k[len("GI_CALLER_TOKEN_"):].lower()
        if not _CALLER_NAME_RE.match(caller_name):
            raise ConfigError(
                f"invalid caller name in {k}: must match {_CALLER_NAME_RE.pattern}"
            )
        token = v.strip()
        if len(token) < 16:
            raise ConfigError(
                f"{k}: token too short ({len(token)} chars, need >= 16) — "
                "generate one with `python -c \"import secrets; print(secrets.token_hex(24))\"`"
            )
        if token in token_map and token_map[token] != caller_name:
            raise ConfigError(
                f"duplicate caller token: {token_map[token]!r} and {caller_name!r} "
                "share the same secret"
            )
        token_map[token] = caller_name

    raw_socket = os.environ.get("HERMES_GREENINVOICE_SOCKET") or ""
    socket_path: Path | str
    if raw_socket.startswith("tcp:"):
        socket_path = raw_socket
    else:
        socket_path = Path(raw_socket) if raw_socket else (runtime_dir / "sock")

    return Config(
        config_dir=config_dir,
        state_dir=state_dir,
        socket_path=socket_path,
        audit_log_path=Path(
            os.environ.get("HERMES_GREENINVOICE_AUDIT_LOG_PATH")
            or (state_dir / "audit.log")
        ),
        ratelimit_db_path=Path(
            os.environ.get("HERMES_GREENINVOICE_RATELIMIT_DB_PATH")
            or (state_dir / "ratelimit.db")
        ),
        # Preview PDFs are spooled here (tmpfs under the RuntimeDirectory) so
        # the rendered PDF never crosses the socket into the agent's context.
        # Group-readable by hermes-greeninvoice-clients so the hermes user can
        # attach/send the file. Ephemeral by design.
        previews_dir=Path(
            os.environ.get("HERMES_GREENINVOICE_PREVIEWS_DIR")
            or (runtime_dir / "previews")
        ),
        spool_previews=_bool(os.environ.get("GI_SPOOL_PREVIEWS"), default=True),
        preview_retention_seconds=_int(
            os.environ.get("GI_PREVIEW_RETENTION_SECONDS"), 3600
        ),
        preview_max_files=_int(os.environ.get("GI_PREVIEW_MAX_FILES"), 50),
        dry_run=_bool(os.environ.get("GI_DRY_RUN"), default=True),
        env=env,
        base_url=base_url,
        file_upload_base_url=file_upload_base_url,
        api_key_id=(os.environ.get("GI_API_KEY_ID") or "").strip() or None,
        api_key_secret=(os.environ.get("GI_API_KEY_SECRET") or "").strip() or None,
        limit_tz=(os.environ.get("GI_LIMIT_TZ") or "local").strip(),
        limits=_load_limits(),
        max_request_bytes=_int(
            os.environ.get("HERMES_GREENINVOICE_MAX_REQUEST_BYTES"), 1 * 1024 * 1024
        ),
        max_upload_file_bytes=_int(
            os.environ.get("HERMES_GREENINVOICE_MAX_UPLOAD_FILE_BYTES"),
            10 * 1024 * 1024,
        ),
        reservation_ttl_seconds=_int(
            os.environ.get("GI_RESERVATION_TTL_SECONDS"), 120
        ),
        http_timeout_seconds=_int(os.environ.get("GI_HTTP_TIMEOUT_SECONDS"), 30),
        min_request_interval_ms=_int(os.environ.get("GI_MIN_REQUEST_INTERVAL_MS"), 350),
        caller_uid_map=uid_map,
        caller_token_map=token_map,
    )


def ensure_state_dirs(cfg: Config) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
