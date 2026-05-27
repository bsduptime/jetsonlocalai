#!/usr/bin/env bash
# ============================================================================
# hermes-mailer — installer
# ============================================================================
# Usage:
#   sudo bash hermes-agent/mail-relay/setup-hermes-mailer.sh
#
# What it does (idempotent — safe to re-run):
#   1. Install Python deps system-wide (pyyaml, resend if requested).
#   2. Symlink the hermes_mailer/ package into /usr/local/lib/hermes-mailer/
#      so the systemd unit's PYTHONPATH works regardless of where the
#      repo lives.
#   3. Create the `hermes-mailer-clients` group; add `hermes` to it.
#      That's what gates UDS access — only group members can connect.
#   4. Seed /etc/hermes-mailer/.env and /etc/hermes-mailer/allowlist.yaml
#      from the templates (only if missing). Set ACLs so dbexpertai can
#      edit them.
#   5. Install + enable + start the systemd unit.
#   6. Remove the OBSOLETE in-process plugin config dir (~hermes/.hermes/
#      email-plugin/) — its .env held the API key in Elena's view, which
#      is exactly what this daemon is here to prevent.
# ============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PKG_SRC="$SCRIPT_DIR/hermes_mailer"

if [ ! -d "$PKG_SRC" ]; then
    echo "error: package source not found at $PKG_SRC" >&2
    exit 1
fi

step() { echo; echo "=== $* ==="; }

# ---------------------------------------------------------------------------
step "1/7: Install Python deps system-wide"
# ---------------------------------------------------------------------------
# pyyaml is hard-required for allowlist parsing.
apt-get update -qq
apt-get install -y -qq python3-yaml acl
# Resend is optional — only needed when EMAIL_TRANSPORT=resend. We install
# it via pip into a venv-free system path. Skip if pip isn't available;
# the daemon will return a structured error if Resend is selected without
# the SDK installed.
if command -v pip3 >/dev/null; then
    pip3 install --break-system-packages --quiet resend 2>&1 | tail -3 || true
fi

# ---------------------------------------------------------------------------
step "2/7: Stage hermes_mailer package at /usr/local/lib/hermes-mailer/"
# ---------------------------------------------------------------------------
install -d -o root -g root -m 755 /usr/local/lib/hermes-mailer
# Symlink so future repo updates flow through automatically. Refuse to
# clobber a real directory.
TARGET=/usr/local/lib/hermes-mailer/hermes_mailer
if [ -L "$TARGET" ]; then
    cur=$(readlink "$TARGET")
    if [ "$cur" = "$PKG_SRC" ]; then
        echo "symlink already correct: $TARGET -> $cur"
    else
        echo "warning: $TARGET points elsewhere ($cur). Updating."
        rm -f "$TARGET"
        ln -s "$PKG_SRC" "$TARGET"
    fi
elif [ -e "$TARGET" ]; then
    echo "error: $TARGET exists and is not a symlink. Refusing to clobber." >&2
    exit 1
else
    ln -s "$PKG_SRC" "$TARGET"
    echo "linked $TARGET -> $PKG_SRC"
fi
# Smoke-test the import path works.
PYTHONPATH=/usr/local/lib/hermes-mailer python3 -c \
    'import hermes_mailer; print("hermes_mailer", hermes_mailer.__version__)'

# ---------------------------------------------------------------------------
step "3/7: Create stable groups for UDS access + config access"
# ---------------------------------------------------------------------------
# Two groups gate two different things:
#   hermes-mailer-clients : who can connect() to the UDS socket
#   hermes-mailer-config  : who can read the credentials in /etc/hermes-mailer
# The DAEMON belongs to BOTH (via SupplementaryGroups in the unit).
# The HERMES user (Elena) belongs to ONLY the clients group — she can
# connect but cannot read the API key.
for g in hermes-mailer-clients hermes-mailer-config; do
    if ! getent group "$g" >/dev/null; then
        groupadd --system "$g"
        echo "created group $g"
    else
        echo "group $g already exists"
    fi
done
if id hermes >/dev/null 2>&1; then
    if ! id -nG hermes | tr ' ' '\n' | grep -qx hermes-mailer-clients; then
        gpasswd -a hermes hermes-mailer-clients
    fi
    # Belt-and-suspenders: hermes must NOT be in the config group.
    if id -nG hermes | tr ' ' '\n' | grep -qx hermes-mailer-config; then
        echo "WARNING: hermes is in hermes-mailer-config — REMOVING (would expose credentials)" >&2
        gpasswd -d hermes hermes-mailer-config || true
    fi
else
    echo "warning: hermes user does not exist yet (run setup-phase1.sh first)" >&2
fi

# ---------------------------------------------------------------------------
step "4/7: Seed /etc/hermes-mailer/.env and allowlist.yaml (if missing)"
# ---------------------------------------------------------------------------
# Dir + files owned root:hermes-mailer-config — the daemon is in that
# group (SupplementaryGroups in the unit) and can READ the contents.
# Mode 0640 on files means group can read but not write; David edits via
# a per-file ACL.
install -d -o root -g hermes-mailer-config -m 750 /etc/hermes-mailer
ENV_FILE=/etc/hermes-mailer/.env
ALLOW_FILE=/etc/hermes-mailer/allowlist.yaml

if [ ! -f "$ENV_FILE" ]; then
    install -m 640 -o root -g hermes-mailer-config "$SCRIPT_DIR/.env.example" "$ENV_FILE"
    echo "seeded $ENV_FILE — EDIT to add transport + creds"
else
    chgrp hermes-mailer-config "$ENV_FILE" 2>/dev/null || true
    chmod 640 "$ENV_FILE"
    echo "$ENV_FILE already exists; normalized owner/mode"
fi
if [ ! -f "$ALLOW_FILE" ]; then
    install -m 640 -o root -g hermes-mailer-config "$SCRIPT_DIR/allowlist.example.yaml" "$ALLOW_FILE"
    echo "seeded $ALLOW_FILE — EDIT to add contacts"
else
    chgrp hermes-mailer-config "$ALLOW_FILE" 2>/dev/null || true
    chmod 640 "$ALLOW_FILE"
    echo "$ALLOW_FILE already exists; normalized owner/mode"
fi

# ACL: let dbexpertai read+write these files without sudo. The daemon's
# read access is via group membership above, NOT via this ACL.
if command -v setfacl >/dev/null; then
    setfacl -m u:dbexpertai:rw "$ENV_FILE" "$ALLOW_FILE" 2>/dev/null || true
    echo "ACL: dbexpertai has rw on $ENV_FILE and $ALLOW_FILE"
else
    echo "warning: setfacl not installed — david will need sudo to edit" >&2
fi

# ---------------------------------------------------------------------------
step "5/7: Install + enable + start systemd unit"
# ---------------------------------------------------------------------------
install -m 644 "$SCRIPT_DIR/systemd/hermes-mailer.service" /etc/systemd/system/hermes-mailer.service
systemctl daemon-reload
systemctl enable hermes-mailer
systemctl restart hermes-mailer
sleep 3
systemctl --no-pager --lines=0 status hermes-mailer | head -8

# ---------------------------------------------------------------------------
step "6/7: Smoke-test the socket"
# ---------------------------------------------------------------------------
if [ -S /run/hermes-mailer/sock ]; then
    stat -c '  socket: %n owner=%U group=%G mode=%a' /run/hermes-mailer/sock
    # Try a minimal request as the hermes user.
    if id hermes >/dev/null 2>&1; then
        sudo -u hermes bash -c '
          printf "%s\n" "{\"v\":1,\"op\":\"send\",\"request_id\":\"smoke\",\"to\":\"smoke@example.com\",\"subject\":\"smoke\",\"body\":\"hi\"}" \
            | timeout 5 nc -U /run/hermes-mailer/sock 2>/dev/null
        ' || echo "  (nc test failed; this is OK if nc lacks -U support)"
    fi
else
    echo "warning: /run/hermes-mailer/sock not found — check 'journalctl -u hermes-mailer'" >&2
fi

# ---------------------------------------------------------------------------
step "7/7: Retire the in-process plugin config (security regression source)"
# ---------------------------------------------------------------------------
# The old in-process plugin stored credentials at ~hermes/.hermes/email-plugin/.env
# where Elena could read them. The daemon now holds those credentials at
# /etc/hermes-mailer/.env (unreachable from hermes user). Archive the old
# tree into a ROOT-OWNED backup dir.
#
# We use cp + rm rather than mv: with mv there's a brief window between
# the rename and the subsequent chown -R during which the directory still
# carries the old hermes ownership at the new path. A compromised hermes
# process with an existing open fd into the old tree could exploit it.
# cp into a freshly-created root-owned dir (mode 700) sidesteps that:
# the destination is already restricted, and we delete the original
# afterwards. The original's perms don't change during the copy, but
# nothing new becomes accessible.
OLD_DIR=/home/hermes/.hermes/email-plugin
if [ -d "$OLD_DIR" ]; then
    BACKUP_BASE=/var/backups/hermes-mailer
    install -d -o root -g root -m 700 "$BACKUP_BASE"
    ARCHIVE="$BACKUP_BASE/email-plugin.retired-$(date -u +%Y%m%dT%H%M%SZ)"
    install -d -o root -g root -m 700 "$ARCHIVE"
    # Copy preserving file content but NOT permissions/ownership — files
    # land owned root:root, default umask perms (will be tightened below).
    cp -r --no-preserve=mode,ownership "$OLD_DIR/." "$ARCHIVE/"
    chown -R root:root "$ARCHIVE"
    find "$ARCHIVE" -type f -exec chmod 600 {} +
    find "$ARCHIVE" -type d -exec chmod 700 {} +
    # Now delete the original. Any compromised process still holding an
    # open fd to a file in OLD_DIR sees what it saw before; no NEW data
    # is exposed.
    rm -rf "$OLD_DIR"
    echo "  archived old in-process plugin config to $ARCHIVE (root-only)"
    echo "  (review/delete after confirming daemon works end-to-end)"
fi

cat <<EOF

========================================================================
 hermes-mailer installed.
========================================================================

  Config:        /etc/hermes-mailer/.env             # transport + creds (mode 600 + dbexpertai ACL)
                 /etc/hermes-mailer/allowlist.yaml   # per-recipient limits
  State:         /var/lib/hermes-mailer/             # ratelimit.db + sent.log (root-only)
  Socket:        /run/hermes-mailer/sock             # mode 0660 group=hermes-mailer-clients
  Unit:          systemctl status hermes-mailer
  Logs:          journalctl -u hermes-mailer -f
  Audit log:     sudo tail -f /var/lib/hermes-mailer/sent.log

 Restart hermes (Elena) so the new plugin picks up:
   sudo systemctl restart hermes
   sudo -u hermes hermes plugins list      # mailer should be 'enabled'
   sudo -u hermes hermes tools list        # send_email should appear

 Test from the hermes user:
   sudo -u hermes printf '%s\n' '{"v":1,"op":"send","request_id":"t1","to":"YOU@yours.example","subject":"t","body":"hi"}' \\
     | nc -U /run/hermes-mailer/sock

========================================================================
EOF
