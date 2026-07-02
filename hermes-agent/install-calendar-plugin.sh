#!/usr/bin/env bash
# ============================================================================
# Hermes calendar plugin — installer  (mirrors install-email-plugin.sh)
# ============================================================================
# Run from the jetsonlocalai repo root:
#   sudo bash hermes-agent/install-calendar-plugin.sh
#
# Idempotent. Sets up the plugin-private config/state dir, seeds .env +
# contacts.yaml from templates (only if missing), and symlinks the plugin
# code into Hermes' plugin dir. The relay DAEMON is a separate service —
# install it after this (see the note at the end / calendar-relay/README.md).
# ============================================================================
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "error: run as root (sudo)" >&2; exit 1; }

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_SRC="$SCRIPT_DIR/plugins/familycal"
[ -d "$PLUGIN_SRC" ] || { echo "error: plugin source not found at $PLUGIN_SRC" >&2; exit 1; }
id hermes &>/dev/null || { echo "error: hermes user missing — run setup-phase1.sh first" >&2; exit 1; }

HERMES_HOME="$(getent passwd hermes | cut -d: -f6)/.hermes"
[ -d "$HERMES_HOME" ] || { echo "error: $HERMES_HOME missing — run setup-phase1.sh first" >&2; exit 1; }

step() { echo; echo "=== $* ==="; }

step "1/4: plugin-private config dir"
PDIR="$HERMES_HOME/calendar-plugin"
install -d -o hermes -g hermes -m 750 "$PDIR"
if command -v setfacl &>/dev/null; then
    setfacl -m u:dbexpertai:rwx "$PDIR"
    setfacl -d -m u:dbexpertai:rwX "$PDIR"
    echo "ACLs set: dbexpertai can edit $PDIR without sudo"
fi

step "2/4: seed .env + contacts.yaml (only if missing)"
[ -f "$PDIR/.env" ] || { install -m 600 -o hermes -g hermes \
    "$SCRIPT_DIR/calendar-relay/.env.example" "$PDIR/.env"; echo "seeded .env — EDIT for tz/live creds"; }
[ -f "$PDIR/contacts.yaml" ] || { install -m 600 -o hermes -g hermes \
    "$SCRIPT_DIR/calendar-relay/contacts.example.yaml" "$PDIR/contacts.yaml"; echo "seeded contacts.yaml — EDIT to add people"; }

step "3/4: state dir (hermes-only)"
install -d -o hermes -g hermes -m 700 "$PDIR/state"
install -d -o hermes -g hermes -m 700 "$PDIR/state/dryrun"

step "4/4: symlink plugin into Hermes' plugin dir"
HERMES_PLUGINS="$HERMES_HOME/plugins"
install -d -o hermes -g hermes -m 750 "$HERMES_PLUGINS"
SYMLINK="$HERMES_PLUGINS/familycal"
if [ -L "$SYMLINK" ]; then
    [ "$(readlink "$SYMLINK")" = "$PLUGIN_SRC" ] || { echo "error: $SYMLINK points elsewhere — remove it manually" >&2; exit 1; }
    echo "symlink already correct"
elif [ -e "$SYMLINK" ]; then
    echo "error: $SYMLINK exists but is not a symlink — refusing to clobber" >&2; exit 1
else
    ln -s "$PLUGIN_SRC" "$SYMLINK"; chown -h hermes:hermes "$SYMLINK"; echo "linked $SYMLINK"
fi

cat <<EOF

========================================================================
 Calendar plugin installed (DRY-RUN by default — safe).
========================================================================
  Plugin:   $PLUGIN_SRC  ->  $SYMLINK
  Config:   $PDIR/.env            <- tz, live Google creds
            $PDIR/contacts.yaml   <- people + emails
  State:    $PDIR/state           <- dryrun artifacts, audit log

 Next:
  1) Install the relay daemon service (holds Google creds, isolated from
     the agent — same pattern as mail-relay):
       sudo cp hermes-agent/calendar-relay/systemd/hermes-calendar.service \\
               /etc/systemd/system/
       # edit WorkingDirectory to this repo's calendar-relay path, then:
       sudo systemctl daemon-reload && sudo systemctl enable --now hermes-calendar
  2) For LIVE Google writes:  pip install -r calendar-relay/requirements.txt
     then run calendar-relay/setup-google-auth.py and set the live env vars.
  3) Reload Hermes:  sudo systemctl restart hermes
     sudo -u hermes -i hermes tools list   # expect create_event + list_contacts
========================================================================
EOF
