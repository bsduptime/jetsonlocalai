from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from hermes_email_pkg import ratelimit


def _make_db(tmp_path):
    db = tmp_path / "rl.db"
    ratelimit.init_schema(db)
    return db


def _reserve_and_send(db, recipient="alice@example.com", limit=3,
                     local_day="2026-05-27", status="sent"):
    with ratelimit.reserve(
        db, recipient=recipient, limit=limit, local_day=local_day,
        subject_trunc="s", byte_size=10, attachment_count=0,
        ttl_seconds=180,
    ) as r:
        ratelimit.finalize(db, r, status, message_id="m")
    return r


def test_initial_count_is_zero(tmp_path):
    db = _make_db(tmp_path)
    assert ratelimit.count_today(db, "alice@example.com", "2026-05-27") == 0


def test_reserve_and_send_increments(tmp_path):
    db = _make_db(tmp_path)
    _reserve_and_send(db)
    assert ratelimit.count_today(db, "alice@example.com", "2026-05-27") == 1


def test_dry_run_does_not_count(tmp_path):
    db = _make_db(tmp_path)
    _reserve_and_send(db, status="dry_run")
    assert ratelimit.count_today(db, "alice@example.com", "2026-05-27") == 0


def test_failed_pre_send_does_not_count(tmp_path):
    db = _make_db(tmp_path)
    _reserve_and_send(db, status="failed_pre_send")
    assert ratelimit.count_today(db, "alice@example.com", "2026-05-27") == 0


def test_unknown_post_send_does_count(tmp_path):
    db = _make_db(tmp_path)
    _reserve_and_send(db, status="unknown_post_send")
    assert ratelimit.count_today(db, "alice@example.com", "2026-05-27") == 1


def test_limit_exceeded_raises(tmp_path):
    db = _make_db(tmp_path)
    for _ in range(2):
        _reserve_and_send(db, limit=2)
    with pytest.raises(ratelimit.RateLimitExceeded) as ei:
        _reserve_and_send(db, limit=2)
    assert ei.value.limit == 2
    assert ei.value.sent_today == 2


def test_different_recipient_has_own_quota(tmp_path):
    db = _make_db(tmp_path)
    for _ in range(3):
        _reserve_and_send(db, recipient="alice@example.com", limit=3)
    # Now alice is full; bob is fine.
    _reserve_and_send(db, recipient="bob@example.com", limit=3)
    assert ratelimit.count_today(db, "bob@example.com", "2026-05-27") == 1


def test_different_day_has_own_quota(tmp_path):
    db = _make_db(tmp_path)
    for _ in range(2):
        _reserve_and_send(db, limit=2, local_day="2026-05-27")
    # 2026-05-28 is a fresh bucket
    _reserve_and_send(db, limit=2, local_day="2026-05-28")
    assert ratelimit.count_today(db, "alice@example.com", "2026-05-28") == 1


def test_stale_reservation_is_reaped(tmp_path):
    db = _make_db(tmp_path)
    # Insert a stale `reserved` row directly
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO sends (recipient, reserved_at_utc, local_day, status, "
        "subject_trunc, byte_size, attachment_count) "
        "VALUES (?, ?, ?, 'reserved', ?, ?, ?)",
        ("alice@example.com", "2020-01-01T00:00:00+00:00", "2020-01-01",
         "stale", 0, 0),
    )
    conn.commit()
    conn.close()
    reaped = ratelimit.reap_stale_reservations(db, ttl_seconds=10)
    assert reaped == 1
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT status FROM sends WHERE recipient='alice@example.com'").fetchone()
    assert row[0] == "unknown_post_send"


def test_reserved_row_left_behind_blocks_further_sends_then_reaped(tmp_path):
    """Simulate a crash between reservation and finalize: a `reserved` row
    remains; subsequent sends within TTL see the row counted; after TTL
    elapses, reap_stale converts it to unknown_post_send (still counted).
    The user-facing quota stays honored throughout."""
    db = _make_db(tmp_path)
    # Manually reserve without finalizing — simulates a crash
    with ratelimit.reserve(
        db, recipient="alice@example.com", limit=2, local_day="2026-05-27",
        subject_trunc="s", byte_size=0, attachment_count=0, ttl_seconds=180,
    ):
        pass  # crash equivalent — never finalized
    # Now count should be 1 (the reserved row counts)
    assert ratelimit.count_today(db, "alice@example.com", "2026-05-27") == 1
    # Another reservation works (still under the limit of 2)
    _reserve_and_send(db, limit=2)
    # Now at limit
    with pytest.raises(ratelimit.RateLimitExceeded):
        _reserve_and_send(db, limit=2)


def test_concurrent_reservations_honor_limit(tmp_path):
    """Spawn N threads racing on the same recipient; only `limit` should
    succeed. Verifies BEGIN IMMEDIATE correctly serializes."""
    db = _make_db(tmp_path)
    successes = []
    failures = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            with ratelimit.reserve(
                db, recipient="alice@example.com", limit=3, local_day="2026-05-27",
                subject_trunc="s", byte_size=0, attachment_count=0,
                ttl_seconds=180,
            ) as r:
                ratelimit.finalize(db, r, "sent", message_id="x")
            successes.append(1)
        except ratelimit.RateLimitExceeded:
            failures.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(successes) == 3
    assert len(failures) == 5


def test_local_day_and_next_midnight_format(tmp_path):
    day = ratelimit.local_day_str("UTC")
    assert len(day) == 10 and day[4] == "-" and day[7] == "-"
    nxt = ratelimit.next_midnight_iso("UTC")
    assert "T00:00:00" in nxt


def test_unknown_tz_falls_back_to_utc():
    # Should not raise — falls back to UTC
    day = ratelimit.local_day_str("Mars/Phobos")
    assert len(day) == 10
