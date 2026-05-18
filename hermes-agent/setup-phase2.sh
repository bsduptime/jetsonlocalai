#!/usr/bin/env bash
# ============================================================================
# Hermes Agent — Phase 2: hardened systemd unit + audit tripwires + monitoring
# ============================================================================
# Run with (from jetsonlocalai repo root):
#   sudo bash hermes-agent/setup-phase2.sh
#
# Prerequisites:
#   - Phase 1 has been run (hermes user exists, ACLs set, Hermes installed)
#   - `hermes setup` has been run interactively (OAuth wired up, model picked)
#   - You can verify both by checking `id hermes` succeeds and
#     `sudo -u hermes -i hermes status` shows Model + Provider
#
# This script:
#   1. Installs /etc/systemd/system/hermes.service (hardened unit)
#   2. Installs auditd if missing, then writes /etc/audit/rules.d/50-hermes.rules
#   3. Installs /usr/local/sbin/hermes-watch (snapshotter script)
#   4. Installs /etc/cron.d/hermes-watch (15-min schedule)
#   5. Creates /var/log/hermes-watch/ (dbexpertai-owned, mode 750)
#   6. Enables + starts the hermes service
#   7. Smoke-tests health + dashboard endpoints
# ============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

step() { echo; echo "=== $* ==="; }

# Resolve repo root from script location so relative paths work regardless
# of cwd when invoked.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

# ----------------------------------------------------------------------------
step "0/7: Prerequisite check"
# ----------------------------------------------------------------------------
if ! id hermes &>/dev/null; then
    echo "error: hermes user does not exist. Run setup-phase1.sh first." >&2
    exit 1
fi
if [ ! -x /home/hermes/.local/bin/hermes ]; then
    echo "error: /home/hermes/.local/bin/hermes not found. Run setup-phase1.sh first." >&2
    exit 1
fi
echo "prerequisites OK"

# ----------------------------------------------------------------------------
step "1/7: Install hardened systemd unit"
# ----------------------------------------------------------------------------
# Stop any Hermes-default-installed gateway service from a prior `hermes
# gateway install` attempt — we're replacing it with our hardened version.
for unit in hermes hermes-gateway hermes.service hermes-gateway.service; do
    if systemctl is-enabled "$unit" &>/dev/null; then
        echo "disabling pre-existing unit: $unit"
        systemctl disable --now "$unit" || true
    fi
done

install -m 644 "$SCRIPT_DIR/systemd/hermes.service" /etc/systemd/system/hermes.service
echo "installed /etc/systemd/system/hermes.service"
systemctl daemon-reload

# ----------------------------------------------------------------------------
step "2/7: Install + configure auditd"
# ----------------------------------------------------------------------------
if ! command -v auditctl &>/dev/null; then
    echo "installing auditd..."
    apt-get update -qq
    apt-get install -y -qq auditd
fi
install -m 640 "$SCRIPT_DIR/monitoring/auditd.rules" /etc/audit/rules.d/50-hermes.rules
echo "installed /etc/audit/rules.d/50-hermes.rules"
augenrules --load
systemctl enable --now auditd
echo "audit rules loaded:"
auditctl -l | grep -E 'hermes|setuid|setgid' || echo "  (no rules currently visible — auditd will pick them up after restart)"

# ----------------------------------------------------------------------------
step "3/7: Install snapshotter script"
# ----------------------------------------------------------------------------
install -m 755 "$SCRIPT_DIR/monitoring/hermes-watch.sh" /usr/local/sbin/hermes-watch
echo "installed /usr/local/sbin/hermes-watch"

# ----------------------------------------------------------------------------
step "4/7: Create log directory (dbexpertai-owned, hermes-inaccessible)"
# ----------------------------------------------------------------------------
install -d -o dbexpertai -g dbexpertai -m 750 /var/log/hermes-watch
echo "created /var/log/hermes-watch/ (mode 750, owned by dbexpertai)"
# Mode 750 means hermes (not in dbexpertai group) cannot list, read, or
# write the directory. Tamper-proof from the agent's perspective.

# ----------------------------------------------------------------------------
step "5/7: Install cron schedule (15-min snapshots as dbexpertai)"
# ----------------------------------------------------------------------------
cat > /etc/cron.d/hermes-watch <<'EOF'
# Hermes Agent watch — periodic anomaly snapshotter.
# Runs as dbexpertai every 15 minutes. Output to /var/log/hermes-watch/.
# Installed by hermes-agent/setup-phase2.sh.
*/15 * * * * dbexpertai /usr/local/sbin/hermes-watch
EOF
chmod 644 /etc/cron.d/hermes-watch
echo "installed /etc/cron.d/hermes-watch"

# ----------------------------------------------------------------------------
step "6/7: Enable + start hermes service"
# ----------------------------------------------------------------------------
systemctl enable hermes
systemctl start hermes
sleep 3
echo "service status:"
systemctl --no-pager --lines=0 status hermes | head -8

# ----------------------------------------------------------------------------
step "7/7: Smoke-test endpoints"
# ----------------------------------------------------------------------------
# Give the gateway a few seconds to bind ports
sleep 5
echo
echo "checking 127.0.0.1:8642/health (gateway API)..."
if curl -sf --max-time 5 http://127.0.0.1:8642/health >/dev/null 2>&1; then
    echo "  ✓ /health responding"
else
    echo "  ✗ /health not responding yet — check 'journalctl -u hermes -f'"
fi
echo "checking 127.0.0.1:9119 (dashboard)..."
if curl -sf --max-time 5 http://127.0.0.1:9119/ -o /dev/null 2>&1; then
    echo "  ✓ dashboard responding"
else
    echo "  ✗ dashboard not responding yet (may need HERMES_DASHBOARD=1 in env)"
fi

echo
echo "========================================================================"
echo " Phase 2 complete."
echo "========================================================================"
echo
echo " Verify:"
echo "   systemctl status hermes"
echo "   journalctl -u hermes -f                          # live logs"
echo "   sudo cat /var/log/hermes-watch/health.log        # snapshotter (root or dbexpertai)"
echo "   sudo auditctl -l                                 # active audit rules"
echo "   sudo ausearch -k hermes-ssh-access               # any ssh-key access events"
echo "   sudo ausearch -k hermes-codex-access             # any codex auth access events"
echo "========================================================================"
