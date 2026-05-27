#!/usr/bin/env bash
# ============================================================================
# setup-db-guard — install hermes-db-guard cron (runs as root).
#
# v2: dropped the sudoers-fragment + run-as-dbexpertai design; that bit us
# on multi-arg sudo patterns and dbexpertai-not-being-in-the-write-group
# for the log dir. Now the cron entry runs the guard as root and the only
# group access dbexpertai needs is READ to browse backups + logs.
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

echo "=== 1/6: install sqlite3 ==="
if ! command -v sqlite3 >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq sqlite3
fi
sqlite3 --version | head -1

echo "=== 2/6: install hermes-db-guard ==="
install -m 755 -o root -g root \
    "$SCRIPT_DIR/hermes-db-guard.sh" /usr/local/sbin/hermes-db-guard

echo "=== 3/6: create backup + log dirs (root-owned, dbexpertai-readable) ==="
install -d -o root -g dbexpertai -m 750 /var/lib/hermes-db-backups
install -d -o root -g dbexpertai -m 750 /var/log/hermes-db-guard
# If a prior v1 install left these with wrong ownership/perms, normalize:
chown -R root:dbexpertai /var/lib/hermes-db-backups /var/log/hermes-db-guard
chmod 750 /var/lib/hermes-db-backups /var/log/hermes-db-guard

echo "=== 4/6: remove obsolete v1 sudoers fragment if present ==="
if [ -f /etc/sudoers.d/hermes-db-guard ]; then
    rm -f /etc/sudoers.d/hermes-db-guard
    echo "  removed /etc/sudoers.d/hermes-db-guard (no longer needed)"
fi

echo "=== 5/6: install cron schedule (every 15 min as ROOT) ==="
cat > /etc/cron.d/hermes-db-guard <<'EOF'
# Hermes SQLite backup + integrity guard. Installed by hermes-agent/monitoring/setup-db-guard.sh.
# Runs as root every 15 min. Output to /var/log/hermes-db-guard/.
*/15 * * * * root /usr/local/sbin/hermes-db-guard
EOF
chmod 644 /etc/cron.d/hermes-db-guard

echo "=== 6/6: run one tick now so we have a baseline backup ==="
/usr/local/sbin/hermes-db-guard
echo

echo "========================================================================"
echo " hermes-db-guard installed."
echo "========================================================================"
echo
echo " Verify:"
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
