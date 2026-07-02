#!/usr/bin/env bash
# ============================================================================
# hermes-calendar — installer  (mirrors setup-hermes-mailer.sh)
# ============================================================================
# Usage:
#   sudo bash hermes-agent/calendar-relay/setup-hermes-calendar.sh          # dry-run capable
#   sudo bash hermes-agent/calendar-relay/setup-hermes-calendar.sh --live   # also pip-install Google libs
#
# Idempotent. What it does:
#   1. Install deps (python3-yaml, acl; google libs only with --live).
#   2. COPY the hermes_calendar package to /usr/local/lib/hermes-calendar/
#      (copy, not symlink — the daemon runs ProtectHome=yes so /home is hidden).
#   3. Create groups: hermes-calendar-clients (Elena connects) +
#      hermes-calendar-config (daemon reads creds). Add hermes to clients only.
#   4. Seed /etc/hermes-calendar/{.env,contacts.yaml} from templates (if missing),
#      root:hermes-calendar-config 0640, with an ACL so dbexpertai can edit them.
#   5. Install + enable + start the systemd unit.
#
# The familycal PLUGIN (the agent-side shim) is installed separately by
# install-calendar-plugin.sh — it just symlinks the plugin into Elena's dir.
# ============================================================================
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "error: run as root (sudo)" >&2; exit 1; }

LIVE=0; [ "${1:-}" = "--live" ] && LIVE=1
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PKG_SRC="$SCRIPT_DIR/hermes_calendar"
[ -d "$PKG_SRC" ] || { echo "error: package source not found at $PKG_SRC" >&2; exit 1; }

step() { echo; echo "=== $* ==="; }

step "1/6: deps"
apt-get update -qq
apt-get install -y -qq python3-yaml acl
if [ "$LIVE" = "1" ]; then
    if command -v pip3 >/dev/null; then
        pip3 install --quiet -r "$SCRIPT_DIR/requirements.txt" 2>&1 | tail -3 || \
            echo "  warning: pip install of google libs failed; live mode will error clearly"
    fi
fi

step "2/6: stage package at /usr/local/lib/hermes-calendar/"
install -d -o root -g root -m 755 /usr/local/lib/hermes-calendar
TARGET=/usr/local/lib/hermes-calendar/hermes_calendar
rm -rf "$TARGET"
cp -r "$PKG_SRC" "$TARGET"
chown -R root:root "$TARGET"
find "$TARGET" -type d -exec chmod 755 {} +
find "$TARGET" -type f -exec chmod 644 {} +
find "$TARGET" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
PYTHONPATH=/usr/local/lib/hermes-calendar python3 -c \
    'import hermes_calendar; print("hermes_calendar import OK")'

step "3/6: groups (clients = connect; config = read creds)"
for g in hermes-calendar-clients hermes-calendar-config; do
    getent group "$g" >/dev/null || { groupadd --system "$g"; echo "created group $g"; }
done
if id hermes >/dev/null 2>&1; then
    id -nG hermes | tr ' ' '\n' | grep -qx hermes-calendar-clients || gpasswd -a hermes hermes-calendar-clients
    if id -nG hermes | tr ' ' '\n' | grep -qx hermes-calendar-config; then
        echo "WARNING: hermes is in hermes-calendar-config — REMOVING (would expose the Google token)" >&2
        gpasswd -d hermes hermes-calendar-config || true
    fi
else
    echo "warning: hermes user missing — run setup-phase1.sh first" >&2
fi

step "4/6: seed /etc/hermes-calendar/{.env,contacts.yaml}"
install -d -o root -g hermes-calendar-config -m 750 /etc/hermes-calendar
if command -v setfacl >/dev/null; then
    setfacl -m u:dbexpertai:rx /etc/hermes-calendar
    setfacl -d -m u:dbexpertai:rx /etc/hermes-calendar
fi
for pair in ".env:.env.example" "contacts.yaml:contacts.example.yaml"; do
    dst="/etc/hermes-calendar/${pair%%:*}"; src="$SCRIPT_DIR/${pair##*:}"
    if [ ! -f "$dst" ]; then
        install -m 640 -o root -g hermes-calendar-config "$src" "$dst"
        echo "seeded $dst — EDIT it"
    else
        chgrp hermes-calendar-config "$dst" 2>/dev/null || true; chmod 640 "$dst"
        echo "$dst exists; normalized owner/mode"
    fi
    command -v setfacl >/dev/null && setfacl -m u:dbexpertai:rw "$dst" 2>/dev/null || true
done

step "5/6: install + start systemd unit"
cp "$SCRIPT_DIR/systemd/hermes-calendar.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hermes-calendar

step "6/6: verify"
sleep 1
systemctl is-active hermes-calendar && echo "service active" || { journalctl -u hermes-calendar -n 20 --no-pager; exit 1; }
ls -l /run/hermes-calendar/sock 2>/dev/null || echo "note: socket not visible yet (check journalctl -u hermes-calendar)"

cat <<EOF

========================================================================
 hermes-calendar relay installed (DRY-RUN by default).
========================================================================
  Code:     /usr/local/lib/hermes-calendar/hermes_calendar
  Config:   /etc/hermes-calendar/.env          <- tz, live Google creds
            /etc/hermes-calendar/contacts.yaml  <- people + emails (edit as dbexpertai)
  Socket:   /run/hermes-calendar/sock  (group hermes-calendar-clients)

 Next:
  1) Install the plugin into Elena, then restart her:
       sudo bash hermes-agent/install-calendar-plugin.sh
       sudo systemctl restart hermes
       sudo -u hermes -i hermes tools list     # expect create_event + list_contacts
  2) Edit /etc/hermes-calendar/contacts.yaml with real emails (no sudo needed).
  3) Talk to Elena (Telegram) and ask her to create an event.
  For LIVE Google writes, see JETSON-DEPLOY.md (§ going live).
========================================================================
EOF
