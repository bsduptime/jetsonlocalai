#!/usr/bin/env bash
# ============================================================================
# Hermes familycal plugin — installer (the agent-side shim only)
# ============================================================================
# Run from the jetsonlocalai repo root, AFTER setup-hermes-calendar.sh:
#   sudo bash hermes-agent/install-calendar-plugin.sh
#
# The plugin is a thin client — all config/creds live with the relay in
# /etc/hermes-calendar (installed by setup-hermes-calendar.sh). This script
# only symlinks the plugin into Elena's plugin dir so she discovers the
# create_event + list_contacts tools. Idempotent.
# ============================================================================
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "error: run as root (sudo)" >&2; exit 1; }

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_SRC="$SCRIPT_DIR/plugins/familycal"
[ -d "$PLUGIN_SRC" ] || { echo "error: plugin source not found at $PLUGIN_SRC" >&2; exit 1; }
id hermes &>/dev/null || { echo "error: hermes user missing — run setup-phase1.sh first" >&2; exit 1; }

HERMES_HOME="$(getent passwd hermes | cut -d: -f6)/.hermes"
[ -d "$HERMES_HOME" ] || { echo "error: $HERMES_HOME missing — run setup-phase1.sh first" >&2; exit 1; }

HERMES_PLUGINS="$HERMES_HOME/plugins"
install -d -o hermes -g hermes -m 750 "$HERMES_PLUGINS"
SYMLINK="$HERMES_PLUGINS/familycal"

if [ -L "$SYMLINK" ]; then
    [ "$(readlink "$SYMLINK")" = "$PLUGIN_SRC" ] || { echo "error: $SYMLINK points elsewhere — remove it manually" >&2; exit 1; }
    echo "symlink already correct: $SYMLINK -> $PLUGIN_SRC"
elif [ -e "$SYMLINK" ]; then
    echo "error: $SYMLINK exists but is not a symlink — refusing to clobber" >&2; exit 1
else
    ln -s "$PLUGIN_SRC" "$SYMLINK"; chown -h hermes:hermes "$SYMLINK"
    echo "linked $SYMLINK -> $PLUGIN_SRC"
fi

cat <<EOF

 familycal plugin linked. Now restart Elena so she loads it:
   sudo systemctl restart hermes
   sudo -u hermes -i hermes tools list      # expect create_event + list_contacts

 (The relay must already be running: systemctl is-active hermes-calendar)
EOF
