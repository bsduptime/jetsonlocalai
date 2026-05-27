#!/usr/bin/env bash
# ============================================================================
# hermes-db-guard — periodic backup + lightweight integrity check for the
# Hermes SQLite databases (state.db and, if present, kanban.db).
#
# Runs as `dbexpertai` from /etc/cron.d/hermes-db-guard (15-minute cadence).
# Reads hermes' DB files with sudo, writes backups to
# /var/lib/hermes-db-backups/ (root-owned, mode 750, dbexpertai readable).
#
# Why this exists:
#   The kanban dispatcher in Hermes v0.14.0 has known WAL/FD-leak bugs that
#   can corrupt kanban.db (#30908, #30896, #30445, #29610). state.db has its
#   own corruption field-report (#5563, P1). Auto-recovery support upstream
#   is limited. This script gives us a tested hourly snapshot so worst-case
#   data loss is one hour, and a rolling 24-snapshot window for kanban.db
#   plus a 7-day window for state.db.
#
# What it does each tick:
#   1. Online .backup of state.db -> rolling hourly snapshot, plus a daily.
#   2. Online .backup of kanban.db (if present, even if dispatcher disabled).
#   3. PRAGMA quick_check on each DB. Failure -> append to alert log + voice
#      alert via jetson-voice-say (Mac listener). Does NOT auto-wipe or
#      auto-recover -- humans get notified, humans decide. This is by design:
#      a stuck restart loop is worse than a stale DB.
#   4. Rotates old backups (keeps last 24 hourly + 7 daily for state.db;
#      24 hourly for kanban.db).
#
# Dependencies: sqlite3. Installed via `sudo apt install -y sqlite3` at setup.
#
# Install:
#   sudo install -m 755 hermes-db-guard.sh /usr/local/sbin/hermes-db-guard
#   sudo install -d -o root -g dbexpertai -m 750 /var/lib/hermes-db-backups
#   sudo install -d -o root -g dbexpertai -m 750 /var/log/hermes-db-guard
#   sudo tee /etc/cron.d/hermes-db-guard >/dev/null <<'CRON'
#   # Hermes SQLite backup + integrity guard. Every 15 min as dbexpertai.
#   */15 * * * * dbexpertai /usr/local/sbin/hermes-db-guard
#   CRON
# ============================================================================
set -u

HERMES_HOME=/home/hermes/.hermes
BACKUP_ROOT=/var/lib/hermes-db-backups
LOG_DIR=/var/log/hermes-db-guard
ALERT_LOG="$LOG_DIR/alerts.log"
VOICE_HOOK=/home/dbexpertai/.claude/hooks/jetson-voice-say

ts()        { date -u +'%Y-%m-%dT%H:%M:%SZ'; }
hour_tag()  { date -u +'%Y%m%dT%H'; }
day_tag()   { date -u +'%Y%m%d'; }

mkdir -p "$BACKUP_ROOT" "$LOG_DIR" 2>/dev/null || true

log()       { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_DIR/guard.log"; }
alert()     {
    printf '[%s] ALERT %s\n' "$(ts)" "$*" >> "$ALERT_LOG"
    log "ALERT: $*"
    if [ -x "$VOICE_HOOK" ]; then
        "$VOICE_HOOK" "jetson hermes db guard: $*" >/dev/null 2>&1 &
    fi
}

# ------------------------------------------------------------------------
# backup_db <db-name> <retain-hourly> <retain-daily>
# Reads the live DB via `sudo sqlite3 .backup` (online, no lock contention
# with the running hermes process). On failure, logs but doesn't bail -- one
# bad tick shouldn't break the next.
# ------------------------------------------------------------------------
backup_db() {
    local name="$1"
    local retain_hourly="$2"
    local retain_daily="$3"
    local src="$HERMES_HOME/$name"
    local dest_dir="$BACKUP_ROOT/$name"

    if ! sudo -n test -f "$src"; then
        log "skip $name: not present"
        return 0
    fi

    sudo -n mkdir -p "$dest_dir/hourly" "$dest_dir/daily" 2>/dev/null || true
    sudo -n chmod 750 "$dest_dir" "$dest_dir/hourly" "$dest_dir/daily" 2>/dev/null || true
    sudo -n chgrp dbexpertai "$dest_dir" "$dest_dir/hourly" "$dest_dir/daily" 2>/dev/null || true

    local hourly_file="$dest_dir/hourly/$name.$(hour_tag).db"
    local daily_file="$dest_dir/daily/$name.$(day_tag).db"
    local tmp
    tmp=$(sudo -n mktemp "$dest_dir/.$name.XXXXXX.db")

    # SQLite .backup is the safe online-backup API. Read with sudo since
    # /home/hermes is mode 700.
    if sudo -n sqlite3 "$src" ".backup '$tmp'" 2>>"$LOG_DIR/guard.log"; then
        # quick_check on the FRESH BACKUP (not the live DB -- WAL writes
        # could otherwise race a sanity check against the original).
        local qc
        qc=$(sudo -n sqlite3 "$tmp" "PRAGMA quick_check;" 2>>"$LOG_DIR/guard.log")
        if [ "$qc" = "ok" ]; then
            sudo -n mv -f "$tmp" "$hourly_file"
            sudo -n cp -f "$hourly_file" "$daily_file"
            sudo -n chgrp dbexpertai "$hourly_file" "$daily_file" 2>/dev/null || true
            sudo -n chmod 640 "$hourly_file" "$daily_file" 2>/dev/null || true
            log "backed up $name: hourly=$hourly_file quick_check=ok"
        else
            sudo -n rm -f "$tmp" 2>/dev/null
            alert "$name quick_check FAILED on fresh backup: ${qc:-no output}. NOT rotating; the last known-good snapshot is preserved. Investigate manually."
        fi
    else
        sudo -n rm -f "$tmp" 2>/dev/null
        alert "$name .backup command failed; see guard.log"
    fi

    # Rotate hourly: keep the newest N.
    sudo -n ls -1t "$dest_dir/hourly/" 2>/dev/null | tail -n +"$((retain_hourly + 1))" |
        while read -r old; do sudo -n rm -f "$dest_dir/hourly/$old"; done
    sudo -n ls -1t "$dest_dir/daily/" 2>/dev/null | tail -n +"$((retain_daily + 1))" |
        while read -r old; do sudo -n rm -f "$dest_dir/daily/$old"; done
}

backup_db state.db   24 7
backup_db kanban.db  24 0

# Also check the LIVE files (a heads-up if hermes itself is producing bad
# writes between our snapshots). Only logs; doesn't act.
for db in state.db kanban.db; do
    if sudo -n test -f "$HERMES_HOME/$db"; then
        live_qc=$(sudo -n sqlite3 "$HERMES_HOME/$db" "PRAGMA quick_check;" 2>&1 |
                  head -1)
        if [ "$live_qc" != "ok" ]; then
            alert "LIVE $db quick_check: $live_qc"
        fi
    fi
done

exit 0
