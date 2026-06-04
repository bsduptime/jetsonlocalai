"""Rate limiter: hourly + daily windows, reserve/finalize semantics."""

from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_greeninvoice import ratelimit


def _db(tmp_path):
    p = tmp_path / "rl.db"
    ratelimit.init_schema(p)
    return p


def _reserve_commit(db, *, per_hour=3, per_day=10, op="issue_invoice",
                    action_class="issue"):
    with ratelimit.reserve(
        db, caller="elena", action_class=action_class, op=op,
        per_hour=per_hour, per_day=per_day,
        local_day=ratelimit.local_day_str("local"),
        request_id="r", detail="", ttl_seconds=120,
    ) as resv:
        ratelimit.finalize(db, resv, "committed")


def test_hourly_cap_blocks(tmp_path):
    db = _db(tmp_path)
    for _ in range(3):
        _reserve_commit(db, per_hour=3, per_day=100)
    with pytest.raises(ratelimit.RateLimitExceeded) as ei:
        _reserve_commit(db, per_hour=3, per_day=100)
    assert ei.value.window == "hour"
    assert ei.value.limit == 3
    assert ei.value.used == 3


def test_daily_cap_blocks_even_when_hour_ok(tmp_path):
    db = _db(tmp_path)
    # Backdate the hourly window by making all but the cap old, but keep
    # them in today's local_day so the daily counter still sees them.
    for _ in range(5):
        _reserve_commit(db, per_hour=1000, per_day=5)
    with pytest.raises(ratelimit.RateLimitExceeded) as ei:
        _reserve_commit(db, per_hour=1000, per_day=5)
    assert ei.value.window == "day"


def test_failed_pre_send_frees_slot(tmp_path):
    db = _db(tmp_path)
    local_day = ratelimit.local_day_str("local")
    # Reserve then finalize as failed_pre_send — should NOT count.
    with ratelimit.reserve(
        db, caller="elena", action_class="issue", op="issue_invoice",
        per_hour=1, per_day=10, local_day=local_day,
        request_id="r", detail="", ttl_seconds=120,
    ) as resv:
        ratelimit.finalize(db, resv, "failed_pre_send")
    used_hour, used_day = ratelimit.usage(
        db, caller="elena", action_class="issue", local_day=local_day)
    assert used_hour == 0 and used_day == 0
    # And a fresh reserve at per_hour=1 still succeeds.
    _reserve_commit(db, per_hour=1, per_day=10)


def test_unknown_status_counts(tmp_path):
    db = _db(tmp_path)
    local_day = ratelimit.local_day_str("local")
    with ratelimit.reserve(
        db, caller="elena", action_class="issue", op="issue_invoice",
        per_hour=3, per_day=10, local_day=local_day,
        request_id="r", detail="", ttl_seconds=120,
    ) as resv:
        ratelimit.finalize(db, resv, "unknown")
    used_hour, _ = ratelimit.usage(
        db, caller="elena", action_class="issue", local_day=local_day)
    assert used_hour == 1


def test_callers_isolated(tmp_path):
    db = _db(tmp_path)
    local_day = ratelimit.local_day_str("local")
    for _ in range(3):
        _reserve_commit(db, per_hour=3, per_day=10)
    # A different caller has its own fresh quota.
    with ratelimit.reserve(
        db, caller="winnow", action_class="issue", op="issue_invoice",
        per_hour=3, per_day=10, local_day=local_day,
        request_id="r", detail="", ttl_seconds=120,
    ) as resv:
        ratelimit.finalize(db, resv, "committed")
    assert ratelimit.usage(db, caller="winnow", action_class="issue",
                           local_day=local_day)[0] == 1


def test_action_classes_isolated(tmp_path):
    db = _db(tmp_path)
    local_day = ratelimit.local_day_str("local")
    for _ in range(3):
        _reserve_commit(db, per_hour=3, per_day=10, action_class="issue")
    # draft has its own bucket; not blocked by issue exhaustion.
    with ratelimit.reserve(
        db, caller="elena", action_class="draft", op="draft_invoice",
        per_hour=3, per_day=10, local_day=local_day,
        request_id="r", detail="", ttl_seconds=120,
    ) as resv:
        ratelimit.finalize(db, resv, "committed")
    assert ratelimit.usage(db, caller="elena", action_class="draft",
                           local_day=local_day)[0] == 1


def test_reaper_reclassifies_stale_reserved(tmp_path):
    db = _db(tmp_path)
    # Insert a reserved row with an old reserved_at_utc directly.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO calls (caller, action_class, op, reserved_at_utc, "
        "reserved_epoch, local_day, status) "
        "VALUES ('elena','issue','issue_invoice','2000-01-01T00:00:00+00:00', ?, ?, 'reserved')",
        (int(time.time()) - 99999, ratelimit.local_day_str("local")),
    )
    conn.commit()
    conn.close()
    n = ratelimit.reap_stale_reservations(db, ttl_seconds=120)
    assert n == 1
    row = sqlite3.connect(str(db)).execute(
        "SELECT status FROM calls").fetchone()
    assert row[0] == "unknown"
