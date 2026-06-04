#!/usr/bin/env bash
# ============================================================================
# hermes-greeninvoice — installer
# ============================================================================
# Usage:
#   sudo bash hermes-agent/invoice-relay/setup-hermes-greeninvoice.sh
#
# Idempotent — safe to re-run. What it does:
#   1. Install acl (the daemon itself needs no third-party Python deps).
#   2. COPY the hermes_greeninvoice/ package into
#      /usr/local/lib/hermes-greeninvoice/ (daemon runs ProtectHome=yes, so
#      it cannot see /home — must be a copy, not a symlink into the repo).
#   3. Create groups hermes-greeninvoice-clients (UDS access) and
#      hermes-greeninvoice-config (credential read); add `hermes` to clients
#      only — NEVER to config.
#   4. Seed /etc/hermes-greeninvoice/.env from the template (if missing) and
#      ACL it so dbexpertai can edit without sudo.
#   5. Install + enable + start the systemd unit; smoke-test the socket.
# ============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PKG_SRC="$SCRIPT_DIR/hermes_greeninvoice"

if [ ! -d "$PKG_SRC" ]; then
    echo "error: package source not found at $PKG_SRC" >&2
    exit 1
fi

step() { echo; echo "=== $* ==="; }

# ---------------------------------------------------------------------------
step "1/6: Install system deps (acl)"
# ---------------------------------------------------------------------------
# The daemon uses only the Python stdlib (urllib, sqlite3, zoneinfo). No pip.
apt-get update -qq
apt-get install -y -qq acl

# ---------------------------------------------------------------------------
step "2/6: Stage hermes_greeninvoice package at /usr/local/lib/hermes-greeninvoice/"
# ---------------------------------------------------------------------------
install -d -o root -g root -m 755 /usr/local/lib/hermes-greeninvoice
TARGET=/usr/local/lib/hermes-greeninvoice/hermes_greeninvoice
if [ -L "$TARGET" ]; then rm -f "$TARGET"; fi
if [ -d "$TARGET" ]; then rm -rf "$TARGET"; fi
cp -r "$PKG_SRC" "$TARGET"
chown -R root:root "$TARGET"
find "$TARGET" -type d -exec chmod 755 {} +
find "$TARGET" -type f -exec chmod 644 {} +
find "$TARGET" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
echo "installed package at $TARGET (copy from $PKG_SRC)"

PYTHONPATH=/usr/local/lib/hermes-greeninvoice python3 -c \
    'import hermes_greeninvoice; print("hermes_greeninvoice ok")'

# ---------------------------------------------------------------------------
step "3/6: Create groups for UDS access + config access"
# ---------------------------------------------------------------------------
for g in hermes-greeninvoice-clients hermes-greeninvoice-config; do
    if ! getent group "$g" >/dev/null; then
        groupadd --system "$g"
        echo "created group $g"
    else
        echo "group $g already exists"
    fi
done
if id hermes >/dev/null 2>&1; then
    if ! id -nG hermes | tr ' ' '\n' | grep -qx hermes-greeninvoice-clients; then
        gpasswd -a hermes hermes-greeninvoice-clients
    fi
    # Belt-and-suspenders: hermes must NOT be in the config group.
    if id -nG hermes | tr ' ' '\n' | grep -qx hermes-greeninvoice-config; then
        echo "WARNING: hermes is in hermes-greeninvoice-config — REMOVING (would expose the API key)" >&2
        gpasswd -d hermes hermes-greeninvoice-config || true
    fi
else
    echo "warning: hermes user does not exist yet (run setup-phase1.sh first)" >&2
fi

# ---------------------------------------------------------------------------
step "4/6: Seed /etc/hermes-greeninvoice/.env (if missing)"
# ---------------------------------------------------------------------------
install -d -o root -g hermes-greeninvoice-config -m 750 /etc/hermes-greeninvoice
if command -v setfacl >/dev/null; then
    setfacl -m u:dbexpertai:rx /etc/hermes-greeninvoice
    setfacl -d -m u:dbexpertai:rx /etc/hermes-greeninvoice
fi
ENV_FILE=/etc/hermes-greeninvoice/.env
if [ ! -f "$ENV_FILE" ]; then
    install -m 640 -o root -g hermes-greeninvoice-config "$SCRIPT_DIR/.env.example" "$ENV_FILE"
    echo "seeded $ENV_FILE — EDIT to add GreenInvoice API key id/secret"
else
    chgrp hermes-greeninvoice-config "$ENV_FILE" 2>/dev/null || true
    chmod 640 "$ENV_FILE"
    echo "$ENV_FILE already exists; normalized owner/mode"
fi
if command -v setfacl >/dev/null; then
    setfacl -m u:dbexpertai:rw "$ENV_FILE" 2>/dev/null || true
    echo "ACL: dbexpertai has rw on $ENV_FILE"
else
    echo "warning: setfacl not installed — david will need sudo to edit" >&2
fi

# ---------------------------------------------------------------------------
step "5/6: Install + enable + start systemd unit"
# ---------------------------------------------------------------------------
install -m 644 "$SCRIPT_DIR/systemd/hermes-greeninvoice.service" \
    /etc/systemd/system/hermes-greeninvoice.service
systemctl daemon-reload
systemctl enable hermes-greeninvoice
systemctl restart hermes-greeninvoice
sleep 3
systemctl --no-pager --lines=0 status hermes-greeninvoice | head -8

# ---------------------------------------------------------------------------
step "6/6: Smoke-test the socket"
# ---------------------------------------------------------------------------
if [ -S /run/hermes-greeninvoice/sock ]; then
    stat -c '  socket: %n owner=%U group=%G mode=%a' /run/hermes-greeninvoice/sock
    if id hermes >/dev/null 2>&1; then
        sudo -u hermes bash -c '
          printf "%s\n" "{\"v\":1,\"op\":\"quota\",\"request_id\":\"smoke\",\"args\":{}}" \
            | timeout 5 nc -U /run/hermes-greeninvoice/sock 2>/dev/null
        ' || echo "  (nc test failed; OK if nc lacks -U support)"
        echo
    fi
else
    echo "warning: socket not found — check 'journalctl -u hermes-greeninvoice'" >&2
fi

cat <<EOF

========================================================================
 hermes-greeninvoice installed.
========================================================================

  Config:   /etc/hermes-greeninvoice/.env         # API key + env + limits (mode 640 + dbexpertai ACL)
  State:    /var/lib/hermes-greeninvoice/         # ratelimit.db + audit.log (root-only)
  Socket:   /run/hermes-greeninvoice/sock         # mode 0660 group=hermes-greeninvoice-clients
  Unit:     systemctl status hermes-greeninvoice
  Logs:     journalctl -u hermes-greeninvoice -f
  Audit:    sudo tail -f /var/lib/hermes-greeninvoice/audit.log

 Default is GI_DRY_RUN=true (no live API calls). To go live:
   1. Edit /etc/hermes-greeninvoice/.env: set GI_API_KEY_ID / GI_API_KEY_SECRET,
      keep GI_ENV=sandbox, set GI_DRY_RUN=false.
   2. sudo systemctl restart hermes-greeninvoice
   3. Watch: journalctl -u hermes-greeninvoice -f

 Install the plugin into Hermes (separate step):
   sudo bash hermes-agent/install-greeninvoice-plugin.sh   # see README
   sudo systemctl restart hermes
   sudo -u hermes hermes tools list      # gi_* tools should appear

========================================================================
EOF
