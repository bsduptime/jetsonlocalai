#!/usr/bin/env bash
# Hermes Agent watch — periodic anomaly snapshotter.
#
# Runs as dbexpertai via /etc/cron.d/hermes-watch every 15 minutes.
# Output goes to /var/log/hermes-watch/ (dbexpertai-owned, mode 750).
# The hermes user cannot see /var/log/hermes-watch/ (no list permission)
# nor tamper with the logs (no write permission).
#
# What's captured (and why):
#   - Service health: is hermes still up?
#   - Resource use: cpu/mem/rss — runaway loops show up as sustained high.
#   - Listening sockets: only 127.0.0.1:8642 + 127.0.0.1:9119 expected;
#     anything else = misconfiguration or active attack.
#   - Outbound connections: unusual destinations signal exfil attempts.
#   - Git commits in ~/code/: commits the agent made that you didn't see.
#
# Review with: less /var/log/hermes-watch/*.log
# Tail live with: sudo journalctl -u hermes -f   (separate channel)

set -uo pipefail

LOG_DIR=/var/log/hermes-watch
TS=$(date -u +%Y%m%d-%H%M%S)

mkdir -p "$LOG_DIR"

# ----------------------------------------------------------------------------
# 1. Service health
# ----------------------------------------------------------------------------
{
    echo "[$TS] active=$(systemctl is-active hermes 2>&1) failed=$(systemctl is-failed hermes 2>&1)"
} >> "$LOG_DIR/health.log"

# ----------------------------------------------------------------------------
# 2. Resource usage of hermes processes
# ----------------------------------------------------------------------------
ps_out=$(ps -u hermes -o pid,pcpu,pmem,rss,etime,cmd --no-headers 2>/dev/null || true)
if [ -n "$ps_out" ]; then
    while IFS= read -r line; do
        echo "[$TS] $line"
    done <<< "$ps_out" >> "$LOG_DIR/proc.log"
fi

# ----------------------------------------------------------------------------
# 3. Listening sockets owned by hermes
# ----------------------------------------------------------------------------
ss_listen=$(ss -tlnp 2>/dev/null | awk -v uid="$(id -u hermes 2>/dev/null)" '
    NR>1 && $0 ~ "uid:" uid { print }
' || true)
if [ -n "$ss_listen" ]; then
    while IFS= read -r line; do
        echo "[$TS] LISTEN $line"
    done <<< "$ss_listen" >> "$LOG_DIR/net.log"
fi

# ----------------------------------------------------------------------------
# 4. Outbound established connections by hermes
# ----------------------------------------------------------------------------
ss_conn=$(ss -tnp state established 2>/dev/null | awk -v uid="$(id -u hermes 2>/dev/null)" '
    $0 ~ "uid:" uid { print $4 " -> " $5 }
' || true)
if [ -n "$ss_conn" ]; then
    while IFS= read -r line; do
        echo "[$TS] CONN $line"
    done <<< "$ss_conn" >> "$LOG_DIR/net.log"
fi

# ----------------------------------------------------------------------------
# 5. Git commit deltas in repos hermes can write to
# ----------------------------------------------------------------------------
for repo in /home/dbexpertai/code/*/; do
    [ -d "$repo/.git" ] || continue
    # Commits in the last 16 minutes (slightly more than cron's 15-min interval
    # to ensure we don't miss anything at the boundary).
    new_commits=$(git -C "$repo" log --since "16 minutes ago" --pretty=format:'%h %an %s' 2>/dev/null || true)
    if [ -n "$new_commits" ]; then
        repo_name=$(basename "$repo")
        while IFS= read -r commit; do
            echo "[$TS] $repo_name: $commit"
        done <<< "$new_commits" >> "$LOG_DIR/git-deltas.log"
    fi
done

# Rotate: keep last 30 days of logs (cron-rotated; cheap inline alternative)
find "$LOG_DIR" -name '*.log' -mtime +30 -delete 2>/dev/null || true

exit 0
