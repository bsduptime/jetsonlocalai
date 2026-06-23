#!/usr/bin/env bash
# ============================================================================
# hermes-db-guard — periodic backup + lightweight integrity check for the
# Hermes SQLite databases (state.db and, if present, kanban.db).
#
# Runs as ROOT from /etc/cron.d/hermes-db-guard (15-minute cadence). Root
# context removes the sudo-from-cron complexity — see git log for the v1
# version that tried to run as dbexpertai+narrow-sudoers and broke on
# multi-arg command patterns.
#
# Backups land in /mnt/sdcard/hermes-db-backups/ (root:dbexpertai mode 750:
# david can list + read them without sudo; only root can write/delete). The
# SD card keeps snapshots off the eMMC root. Logs land in
# /var/log/hermes-db-guard/ (same ownership).
#
# Why this exists:
#   Hermes v0.14.0 has known WAL/FD-leak kanban corruption bugs (upstream
#   #30908, #30896, #30445, #29610) and a P1 state.db corruption field
#   report (#5563). Upstream auto-recovery is limited. This script gives
#   us tested snapshots so worst-case data loss is one hour.
#
# What it does each tick:
#   1. SQLite online .backup of state.db -> rolling hourly snapshot + daily.
#   2. Same for kanban.db if present (even if dispatcher disabled).
#   3. PRAGMA quick_check on the FRESH BACKUP (not the live DB).
#   4. Rotates: keep last 24 hourly + 7 daily for state.db; 24 hourly only
#      for kanban.db.
#   5. Voice-alerts via jetson-voice-say (run as dbexpertai) on genuine
#      integrity failure. Transient SQLite locks (the DB is momentarily busy
#      while hermes writes) are logged but NOT spoken — they clear on their
#      own and aren't data loss. Does NOT auto-wipe or auto-recover --
#      humans get notified, humans decide.
# ============================================================================
set -u

HERMES_HOME=/home/hermes/.hermes
BACKUP_ROOT=/mnt/sdcard/hermes-db-backups
LOG_DIR=/var/log/hermes-db-guard
ALERT_LOG="$LOG_DIR/alerts.log"
VOICE_HOOK=/home/dbexpertai/.claude/hooks/jetson-voice-say

ts()        { date -u +'%Y-%m-%dT%H:%M:%SZ'; }
hour_tag()  { date -u +'%Y%m%dT%H'; }
day_tag()   { date -u +'%Y%m%d'; }

# Ensure dirs (idempotent; safe to recreate every tick).
mkdir -p "$BACKUP_ROOT" "$LOG_DIR" 2>/dev/null || true
chown root:dbexpertai "$BACKUP_ROOT" "$LOG_DIR" 2>/dev/null || true
chmod 750 "$BACKUP_ROOT" "$LOG_DIR" 2>/dev/null || true

log() {
    printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_DIR/guard.log"
}
# A real problem worth interrupting David for (DB corruption, or a backup
# failure that isn't a transient lock). Logs to alerts.log + guard.log AND
# speaks via the Mac voice bridge.
alert() {
    printf '[%s] ALERT %s\n' "$(ts)" "$*" >> "$ALERT_LOG"
    log "ALERT: $*"
    # Voice hook runs as dbexpertai so it can hit the wrapper script in
    # that user's ~/.claude/hooks/ tree. The spoken line deliberately leads
    # with "DB backup guard" (not "hermes"/"agent") and states the agent is
    # still up, so a TTS readout isn't misheard as "the Hermes agent failed".
    if [ -x "$VOICE_HOOK" ]; then
        sudo -u dbexpertai "$VOICE_HOOK" \
            "Jetson DB backup guard: $*. The Hermes agent itself is still running." \
            >/dev/null 2>&1 &
    fi
}

# A transient / benign condition (e.g. a momentary SQLite lock while hermes is
# writing). Recorded for review but NOT spoken — locks clear on their own and
# don't mean data loss, so they shouldn't wake David.
warn() {
    printf '[%s] WARN %s\n' "$(ts)" "$*" >> "$ALERT_LOG"
    log "WARN: $*"
}

# True when a sqlite error/quick_check string is a transient lock or busy
# state rather than actual corruption.
is_lock() {
    case "$1" in
        *"is locked"*|*"locked"*|*"database is busy"*|*"is busy"*) return 0 ;;
        *) return 1 ;;
    esac
}

backup_db() {
    local name="$1"
    local retain_hourly="$2"
    local retain_daily="$3"
    local src="$HERMES_HOME/$name"
    local dest_dir="$BACKUP_ROOT/$name"

    if [ ! -f "$src" ]; then
        log "skip $name: not present"
        return 0
    fi

    mkdir -p "$dest_dir/hourly" "$dest_dir/daily"
    chown -R root:dbexpertai "$dest_dir"
    chmod 750 "$dest_dir" "$dest_dir/hourly" "$dest_dir/daily"

    local hourly_file="$dest_dir/hourly/$name.$(hour_tag).db"
    local daily_file="$dest_dir/daily/$name.$(day_tag).db"
    local tmp
    tmp=$(mktemp "$dest_dir/.$name.XXXXXX.db")

    # SQLite .backup is the safe online-backup API (no lock contention
    # with the running hermes process). Capture stderr so we can tell a
    # transient lock apart from a genuine failure.
    local backup_err
    backup_err=$(sqlite3 "$src" ".backup '$tmp'" 2>&1 >/dev/null)
    if [ $? -eq 0 ]; then
        local qc
        qc=$(sqlite3 "$tmp" "PRAGMA quick_check;" 2>>"$LOG_DIR/guard.log")
        if [ "$qc" = "ok" ]; then
            mv -f "$tmp" "$hourly_file"
            cp -f "$hourly_file" "$daily_file"
            chmod 640 "$hourly_file" "$daily_file"
            chown root:dbexpertai "$hourly_file" "$daily_file"
            log "backed up $name: hourly=$hourly_file quick_check=ok"
        elif is_lock "$qc"; then
            rm -f "$tmp"
            warn "$name quick_check hit a transient lock on fresh backup: ${qc}. Not rotating this tick; will retry next run."
        else
            rm -f "$tmp"
            alert "$name quick_check FAILED on fresh backup: ${qc:-no output}. NOT rotating; the last known-good snapshot is preserved."
        fi
    else
        rm -f "$tmp"
        if is_lock "$backup_err"; then
            warn "$name .backup hit a transient lock: ${backup_err}. Will retry next run."
        else
            alert "$name .backup command failed: ${backup_err:-see guard.log}"
        fi
    fi

    # Rotate hourly + daily: keep the newest N.
    ls -1t "$dest_dir/hourly/" 2>/dev/null | tail -n +"$((retain_hourly + 1))" |
        while read -r old; do rm -f "$dest_dir/hourly/$old"; done
    if [ "$retain_daily" -gt 0 ]; then
        ls -1t "$dest_dir/daily/" 2>/dev/null | tail -n +"$((retain_daily + 1))" |
            while read -r old; do rm -f "$dest_dir/daily/$old"; done
    fi
}

backup_db state.db   24 7
backup_db kanban.db  24 0

# Heads-up check on the LIVE files between snapshots.
for db in state.db kanban.db; do
    if [ -f "$HERMES_HOME/$db" ]; then
        live_qc=$(sqlite3 "$HERMES_HOME/$db" "PRAGMA quick_check;" 2>&1 | head -1)
        if [ "$live_qc" = "ok" ]; then
            : # healthy
        elif is_lock "$live_qc"; then
            # Momentary lock while hermes writes — benign, clears itself.
            warn "LIVE $db quick_check transient lock: $live_qc"
        else
            alert "LIVE $db quick_check: $live_qc"
        fi
    fi
done

exit 0
