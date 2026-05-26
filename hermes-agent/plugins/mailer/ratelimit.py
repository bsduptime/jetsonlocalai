"""SQLite-backed rate limiter with reservation pattern.

Statuses (Codex Finding 1 — distinguish transport failure modes):
  - reserved          : INSERTed, transport not yet called. Counts (while fresh).
  - sent              : transport confirmed acceptance. Counts.
  - dry_run           : dry-run path. Does NOT count.
  - failed_pre_send   : transport rejected before bytes left host. Does NOT count.
  - unknown_post_send : transport call started, outcome unknown. Counts (conservative).

The reservation pattern (BEGIN IMMEDIATE inside a single transaction) gives
us atomic check-and-insert so even if Hermes ever runs tool calls in
parallel (it doesn't today, but future-proofing is cheap), we don't oversend.

Stale `reserved` rows older than RESERVATION_TTL_SECONDS are reclassified to
`unknown_post_send` at plugin load time (and on first call of the day).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_COUNTED_STATUSES = ("reserved", "sent", "unknown_post_send")

_init_lock = threading.Lock()


@dataclass
class Reservation:
    row_id: int
    recipient: str
    local_day: str
    limit: int


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level="DEFERRED")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_schema(db_path: Path) -> None:
    with _init_lock:
        conn = _connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sends (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient        TEXT    NOT NULL,
                    reserved_at_utc  TEXT    NOT NULL,
                    finalized_at_utc TEXT,
                    local_day        TEXT    NOT NULL,
                    status           TEXT    NOT NULL,
                    message_id       TEXT,
                    subject_trunc    TEXT,
                    byte_size        INTEGER,
                    attachment_count INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_sends_recipient_day_status
                    ON sends(recipient, local_day, status);
                CREATE INDEX IF NOT EXISTS idx_sends_status_reserved_at
                    ON sends(status, reserved_at_utc);
                """
            )
            conn.commit()
        finally:
            conn.close()


def _resolve_tz(tz_name: str) -> ZoneInfo | timezone:
    if tz_name == "local" or not tz_name:
        # Use the local zone via the LOCAL_TZ env or system. Python's
        # datetime.now().astimezone() uses the system local tz; we'll lean on that.
        return None  # type: ignore[return-value]  (sentinel — caller handles)
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        # Fall back to UTC if config is bad rather than crashing.
        return timezone.utc


def now_in_tz(tz_name: str) -> datetime:
    tz = _resolve_tz(tz_name)
    if tz is None:
        return datetime.now().astimezone()
    return datetime.now(tz)


def local_day_str(tz_name: str) -> str:
    return now_in_tz(tz_name).strftime("%Y-%m-%d")


def next_midnight_iso(tz_name: str) -> str:
    now_local = now_in_tz(tz_name)
    tomorrow = (now_local + timedelta(days=1)).date()
    midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=now_local.tzinfo)
    return midnight.isoformat(timespec="seconds")


def reap_stale_reservations(db_path: Path, ttl_seconds: int) -> int:
    """Reclassify any `reserved` rows older than ttl → `unknown_post_send`.

    Returns the number of rows reaped. Called at plugin load and (optionally)
    before each reserve to prevent stale rows from accumulating.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE sends SET status='unknown_post_send', "
                "finalized_at_utc=? "
                "WHERE status='reserved' AND reserved_at_utc < ?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), cutoff),
            )
            return cur.rowcount
    finally:
        conn.close()


def count_today(db_path: Path, recipient: str, local_day: str) -> int:
    """Count rows that consume the daily quota for (recipient, day)."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"SELECT count(*) FROM sends WHERE recipient=? AND local_day=? "
            f"AND status IN ({','.join('?' * len(_COUNTED_STATUSES))})",
            (recipient.lower(), local_day, *_COUNTED_STATUSES),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


@contextmanager
def reserve(
    db_path: Path,
    *,
    recipient: str,
    limit: int,
    local_day: str,
    subject_trunc: str,
    byte_size: int,
    attachment_count: int,
    ttl_seconds: int,
):
    """Context manager that atomically reserves a slot.

    Yields (Reservation, count_after_insert) on success.
    Raises RateLimitExceeded if the recipient is at or above their limit.

    The caller MUST call finalize_*() on the yielded reservation.
    If the caller's block raises and they did not finalize, the reservation
    is left as `reserved` and will be reaped at the next startup or
    before the next reserve. (We can't reliably finalize on exception here
    without knowing whether the send actually happened.)
    """
    # Opportunistic reap so we don't compete with our own stale rows.
    reap_stale_reservations(db_path, ttl_seconds)

    recipient_lc = recipient.lower()
    conn = _connect(db_path)
    try:
        conn.isolation_level = None  # manual txn control
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = conn.execute(
                f"SELECT count(*) FROM sends WHERE recipient=? AND local_day=? "
                f"AND status IN ({','.join('?' * len(_COUNTED_STATUSES))})",
                (recipient_lc, local_day, *_COUNTED_STATUSES),
            ).fetchone()[0]
            if count >= limit:
                conn.execute("ROLLBACK")
                raise RateLimitExceeded(
                    recipient=recipient_lc, limit=limit, sent_today=count
                )
            now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            cur = conn.execute(
                "INSERT INTO sends "
                "(recipient, reserved_at_utc, local_day, status, "
                " subject_trunc, byte_size, attachment_count) "
                "VALUES (?, ?, ?, 'reserved', ?, ?, ?)",
                (recipient_lc, now_utc, local_day, subject_trunc,
                 byte_size, attachment_count),
            )
            row_id = cur.lastrowid
            conn.execute("COMMIT")
        except RateLimitExceeded:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()

    yield Reservation(row_id=row_id, recipient=recipient_lc,
                      local_day=local_day, limit=limit)


def finalize(
    db_path: Path,
    reservation: Reservation,
    new_status: str,
    message_id: str | None = None,
) -> int:
    """Update the reservation row. Returns rowcount.

    If rowcount==0, the row was already reaped (TTL elapsed during a slow
    send). The caller can use this signal to log a warning — the message
    may have been sent successfully but the audit row is now stuck as
    `unknown_post_send` with no message_id. (Codex F4.)
    """
    if new_status not in {"sent", "dry_run", "failed_pre_send", "unknown_post_send"}:
        raise ValueError(f"invalid finalize status: {new_status!r}")
    conn = _connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE sends SET status=?, message_id=?, finalized_at_utc=? "
                "WHERE id=? AND status='reserved'",
                (
                    new_status,
                    message_id,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    reservation.row_id,
                ),
            )
            return cur.rowcount
    finally:
        conn.close()


class RateLimitExceeded(Exception):
    def __init__(self, *, recipient: str, limit: int, sent_today: int):
        super().__init__(f"rate limit exceeded for {recipient}: {sent_today}/{limit}")
        self.recipient = recipient
        self.limit = limit
        self.sent_today = sent_today
