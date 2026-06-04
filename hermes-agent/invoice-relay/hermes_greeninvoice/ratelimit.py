"""SQLite-backed rate limiter, keyed by (caller, action_class).

Two windows enforced together:
  - hourly: rolling 3600s window (count rows with reserved_epoch > now-3600)
  - daily:  calendar day in the configured tz (count rows with local_day == today)

A call is admitted only if BOTH windows are under their cap. The same
reserve -> (do the side-effecting API call) -> finalize pattern as
hermes-mailer is used so that:
  - concurrent connects can't oversend (BEGIN IMMEDIATE + re-count under lock),
  - a crash between reserve and finalize leaves a `reserved` row that the
    reaper later reclassifies to `unknown` (still counts against the cap —
    conservative; we'd rather under-issue than over-issue).

Counted statuses (count toward the cap): reserved, committed, unknown.
A `failed_pre_send` finalize frees the slot — the API call was rejected
before any document was created, so it shouldn't burn quota.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_COUNTED_STATUSES = ("reserved", "committed", "unknown")
HOUR_SECONDS = 3600

_init_lock = threading.Lock()


@dataclass
class Reservation:
    row_id: int
    caller: str
    action_class: str
    local_day: str
    per_hour: int
    per_day: int


class RateLimitExceeded(Exception):
    def __init__(self, *, caller: str, action_class: str, window: str,
                 limit: int, used: int):
        super().__init__(
            f"rate limit exceeded for {caller}/{action_class} "
            f"({window}): {used}/{limit}"
        )
        self.caller = caller
        self.action_class = action_class
        self.window = window     # "hour" | "day"
        self.limit = limit
        self.used = used


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
                CREATE TABLE IF NOT EXISTS calls (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    caller           TEXT    NOT NULL,
                    action_class     TEXT    NOT NULL,
                    op               TEXT    NOT NULL,
                    reserved_at_utc  TEXT    NOT NULL,
                    reserved_epoch   INTEGER NOT NULL,
                    finalized_at_utc TEXT,
                    local_day        TEXT    NOT NULL,
                    status           TEXT    NOT NULL,
                    request_id       TEXT,
                    detail           TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_calls_class_epoch
                    ON calls(caller, action_class, reserved_epoch, status);
                CREATE INDEX IF NOT EXISTS idx_calls_class_day
                    ON calls(caller, action_class, local_day, status);
                CREATE INDEX IF NOT EXISTS idx_calls_status_reserved
                    ON calls(status, reserved_at_utc);
                """
            )
            conn.commit()
        finally:
            conn.close()


# ---- time helpers --------------------------------------------------------

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


# ---- reaper --------------------------------------------------------------

def reap_stale_reservations(db_path: Path, ttl_seconds: int) -> int:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE calls SET status='unknown', finalized_at_utc=? "
                "WHERE status='reserved' AND reserved_at_utc < ?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), cutoff),
            )
            return cur.rowcount
    finally:
        conn.close()


# ---- counting ------------------------------------------------------------

def _counts(conn: sqlite3.Connection, *, caller: str, action_class: str,
            local_day: str, hour_cutoff_epoch: int) -> tuple[int, int]:
    """Return (used_this_hour, used_today) for counted statuses."""
    placeholders = ",".join("?" * len(_COUNTED_STATUSES))
    used_hour = conn.execute(
        f"SELECT count(*) FROM calls "
        f"WHERE caller=? AND action_class=? AND reserved_epoch > ? "
        f"AND status IN ({placeholders})",
        (caller, action_class, hour_cutoff_epoch, *_COUNTED_STATUSES),
    ).fetchone()[0]
    used_day = conn.execute(
        f"SELECT count(*) FROM calls "
        f"WHERE caller=? AND action_class=? AND local_day=? "
        f"AND status IN ({placeholders})",
        (caller, action_class, local_day, *_COUNTED_STATUSES),
    ).fetchone()[0]
    return used_hour, used_day


def usage(db_path: Path, *, caller: str, action_class: str,
          local_day: str) -> tuple[int, int]:
    """Read-only (used_this_hour, used_today). Used by the quota op."""
    hour_cutoff = int(time.time()) - HOUR_SECONDS
    conn = _connect(db_path)
    try:
        return _counts(conn, caller=caller, action_class=action_class,
                       local_day=local_day, hour_cutoff_epoch=hour_cutoff)
    finally:
        conn.close()


# ---- reserve / finalize --------------------------------------------------

@contextmanager
def reserve(
    db_path: Path,
    *,
    caller: str,
    action_class: str,
    op: str,
    per_hour: int,
    per_day: int,
    local_day: str,
    request_id: str,
    detail: str,
    ttl_seconds: int,
):
    """Reserve a slot for one gated call. Raises RateLimitExceeded if
    either window is full. Yields a Reservation to finalize afterwards."""
    reap_stale_reservations(db_path, ttl_seconds)
    now_epoch = int(time.time())
    hour_cutoff = now_epoch - HOUR_SECONDS
    conn = _connect(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            used_hour, used_day = _counts(
                conn, caller=caller, action_class=action_class,
                local_day=local_day, hour_cutoff_epoch=hour_cutoff,
            )
            if used_hour >= per_hour:
                conn.execute("ROLLBACK")
                raise RateLimitExceeded(
                    caller=caller, action_class=action_class,
                    window="hour", limit=per_hour, used=used_hour,
                )
            if used_day >= per_day:
                conn.execute("ROLLBACK")
                raise RateLimitExceeded(
                    caller=caller, action_class=action_class,
                    window="day", limit=per_day, used=used_day,
                )
            now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            cur = conn.execute(
                "INSERT INTO calls "
                "(caller, action_class, op, reserved_at_utc, reserved_epoch, "
                " local_day, status, request_id, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)",
                (caller, action_class, op, now_utc, now_epoch, local_day,
                 request_id, detail[:200] if detail else None),
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
        row_id=row_id, caller=caller, action_class=action_class,
        local_day=local_day, per_hour=per_hour, per_day=per_day,
    )


def finalize(db_path: Path, reservation: Reservation, new_status: str,
             detail: str | None = None) -> int:
    if new_status not in {"committed", "failed_pre_send", "unknown"}:
        raise ValueError(f"invalid finalize status: {new_status!r}")
    conn = _connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE calls SET status=?, finalized_at_utc=?, "
                "detail=COALESCE(?, detail) "
                "WHERE id=? AND status='reserved'",
                (
                    new_status,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    detail[:200] if detail else None,
                    reservation.row_id,
                ),
            )
            return cur.rowcount
    finally:
        conn.close()
