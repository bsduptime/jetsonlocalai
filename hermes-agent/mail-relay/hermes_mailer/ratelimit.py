"""SQLite-backed rate limiter, caller-keyed.

Lifted from the plugin's ratelimit.py with one schema change: `caller` is
now a NOT NULL column on every row, and the count query filters by
(caller, recipient, local_day, status).

Same reservation pattern (BEGIN IMMEDIATE + INSERT reserved + check
count + send + UPDATE) so concurrent connects to the daemon don't
oversend even within a single caller, and don't cross-pollinate across
callers.
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
    caller: str
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
                    caller           TEXT    NOT NULL,
                    recipient        TEXT    NOT NULL,
                    reserved_at_utc  TEXT    NOT NULL,
                    finalized_at_utc TEXT,
                    local_day        TEXT    NOT NULL,
                    status           TEXT    NOT NULL,
                    message_id       TEXT,
                    subject_trunc    TEXT,
                    byte_size        INTEGER,
                    attachment_count INTEGER DEFAULT 0,
                    request_id       TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sends_caller_recipient_day_status
                    ON sends(caller, recipient, local_day, status);
                CREATE INDEX IF NOT EXISTS idx_sends_status_reserved_at
                    ON sends(status, reserved_at_utc);
                """
            )
            conn.commit()
        finally:
            conn.close()


def _resolve_tz(tz_name: str):
    if tz_name == "local" or not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
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
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE sends SET status='unknown_post_send', finalized_at_utc=? "
                "WHERE status='reserved' AND reserved_at_utc < ?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), cutoff),
            )
            return cur.rowcount
    finally:
        conn.close()


def count_today(db_path: Path, *, caller: str, recipient: str,
                local_day: str) -> int:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"SELECT count(*) FROM sends "
            f"WHERE caller=? AND recipient=? AND local_day=? "
            f"AND status IN ({','.join('?' * len(_COUNTED_STATUSES))})",
            (caller, recipient.lower(), local_day, *_COUNTED_STATUSES),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


@contextmanager
def reserve(
    db_path: Path,
    *,
    caller: str,
    recipient: str,
    limit: int,
    local_day: str,
    subject_trunc: str,
    byte_size: int,
    attachment_count: int,
    request_id: str,
    ttl_seconds: int,
):
    reap_stale_reservations(db_path, ttl_seconds)
    recipient_lc = recipient.lower()
    conn = _connect(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = conn.execute(
                f"SELECT count(*) FROM sends "
                f"WHERE caller=? AND recipient=? AND local_day=? "
                f"AND status IN ({','.join('?' * len(_COUNTED_STATUSES))})",
                (caller, recipient_lc, local_day, *_COUNTED_STATUSES),
            ).fetchone()[0]
            if count >= limit:
                conn.execute("ROLLBACK")
                raise RateLimitExceeded(
                    caller=caller, recipient=recipient_lc,
                    limit=limit, sent_today=count,
                )
            now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            cur = conn.execute(
                "INSERT INTO sends "
                "(caller, recipient, reserved_at_utc, local_day, status, "
                " subject_trunc, byte_size, attachment_count, request_id) "
                "VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?, ?)",
                (caller, recipient_lc, now_utc, local_day, subject_trunc,
                 byte_size, attachment_count, request_id),
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
    yield Reservation(
        row_id=row_id, caller=caller, recipient=recipient_lc,
        local_day=local_day, limit=limit,
    )


def finalize(db_path: Path, reservation: Reservation, new_status: str,
             message_id: str | None = None) -> int:
    if new_status not in {"sent", "dry_run", "failed_pre_send", "unknown_post_send"}:
        raise ValueError(f"invalid finalize status: {new_status!r}")
    conn = _connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE sends SET status=?, message_id=?, finalized_at_utc=? "
                "WHERE id=? AND status='reserved'",
                (
                    new_status, message_id,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    reservation.row_id,
                ),
            )
            return cur.rowcount
    finally:
        conn.close()


class RateLimitExceeded(Exception):
    def __init__(self, *, caller: str, recipient: str, limit: int, sent_today: int):
        super().__init__(
            f"rate limit exceeded for {caller}/{recipient}: {sent_today}/{limit}"
        )
        self.caller = caller
        self.recipient = recipient
        self.limit = limit
        self.sent_today = sent_today
