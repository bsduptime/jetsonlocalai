#!/usr/bin/env bash
# ============================================================================
# setup-db-guard — install hermes-db-guard cron + give dbexpertai NOPASSWD
# sudo for the narrow set of commands the guard needs (sqlite3 read of
# /home/hermes, mv/rm/cp under /var/lib/hermes-db-backups).
#
# Usage:
#   sudo bash hermes-agent/monitoring/setup-db-guard.sh
# ============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

echo "=== 1/5: install sqlite3 ==="
if ! command -v sqlite3 >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq sqlite3
fi
sqlite3 --version | head -1

echo "=== 2/5: install hermes-db-guard ==="
install -m 755 -o root -g root \
    "$SCRIPT_DIR/hermes-db-guard.sh" /usr/local/sbin/hermes-db-guard

echo "=== 3/5: create backup + log dirs (dbexpertai-readable) ==="
install -d -o root -g dbexpertai -m 750 /var/lib/hermes-db-backups
install -d -o root -g dbexpertai -m 750 /var/log/hermes-db-guard

echo "=== 4/5: narrow NOPASSWD sudo for the guard ==="
# The guard runs as dbexpertai and needs to: read /home/hermes/*.db,
# write under /var/lib/hermes-db-backups, exec sqlite3 against hermes paths.
# We DO NOT grant blanket sudo — only the specific commands.
cat > /etc/sudoers.d/hermes-db-guard <<'EOF'
# Installed by hermes-agent/monitoring/setup-db-guard.sh
# Narrow privileges for /usr/local/sbin/hermes-db-guard to read hermes
# SQLite DBs and maintain backups under /var/lib/hermes-db-backups.
dbexpertai ALL=(root) NOPASSWD: /usr/bin/sqlite3 /home/hermes/.hermes/state.db *
dbexpertai ALL=(root) NOPASSWD: /usr/bin/sqlite3 /home/hermes/.hermes/kanban.db *
dbexpertai ALL=(root) NOPASSWD: /usr/bin/sqlite3 /var/lib/hermes-db-backups/*
dbexpertai ALL=(root) NOPASSWD: /usr/bin/test -f /home/hermes/.hermes/*
dbexpertai ALL=(root) NOPASSWD: /bin/mkdir -p /var/lib/hermes-db-backups/*
dbexpertai ALL=(root) NOPASSWD: /bin/mv -f /var/lib/hermes-db-backups/*
dbexpertai ALL=(root) NOPASSWD: /bin/cp -f /var/lib/hermes-db-backups/*
dbexpertai ALL=(root) NOPASSWD: /bin/rm -f /var/lib/hermes-db-backups/*
dbexpertai ALL=(root) NOPASSWD: /bin/ls -1t /var/lib/hermes-db-backups/*
dbexpertai ALL=(root) NOPASSWD: /bin/chmod 750 /var/lib/hermes-db-backups/*
dbexpertai ALL=(root) NOPASSWD: /bin/chmod 640 /var/lib/hermes-db-backups/*
dbexpertai ALL=(root) NOPASSWD: /bin/chgrp dbexpertai /var/lib/hermes-db-backups/*
dbexpertai ALL=(root) NOPASSWD: /bin/mktemp /var/lib/hermes-db-backups/*
EOF
chmod 440 /etc/sudoers.d/hermes-db-guard
# visudo -c validates the new fragment
visudo -c -f /etc/sudoers.d/hermes-db-guard

echo "=== 5/5: install cron schedule (every 15 min as dbexpertai) ==="
cat > /etc/cron.d/hermes-db-guard <<'EOF'
# Hermes SQLite backup + integrity guard. Installed by hermes-agent/monitoring/setup-db-guard.sh.
# See /usr/local/sbin/hermes-db-guard for what it does.
*/15 * * * * dbexpertai /usr/local/sbin/hermes-db-guard
EOF
chmod 644 /etc/cron.d/hermes-db-guard

echo
echo "========================================================================"
echo " hermes-db-guard installed."
echo "========================================================================"
echo
echo " Verify by running ONE tick manually:"
echo "   /usr/local/sbin/hermes-db-guard"
echo "   ls -la /var/lib/hermes-db-backups/state.db/hourly/"
echo "   tail /var/log/hermes-db-guard/guard.log"
echo
echo " Restore (worst case):"
echo "   sudo systemctl stop hermes"
echo "   sudo cp /var/lib/hermes-db-backups/state.db/hourly/<latest>.db \\"
echo "          /home/hermes/.hermes/state.db"
echo "   sudo chown hermes:hermes /home/hermes/.hermes/state.db"
echo "   sudo rm -f /home/hermes/.hermes/state.db-wal /home/hermes/.hermes/state.db-shm"
echo "   sudo systemctl start hermes"
echo "========================================================================"
