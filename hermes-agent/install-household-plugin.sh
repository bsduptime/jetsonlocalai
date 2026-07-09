#!/usr/bin/env bash
# ============================================================================
# Hermes household plugin — installer
# ============================================================================
# Run from the jetsonlocalai repo root:
#   sudo bash hermes-agent/install-household-plugin.sh
#
# No relay, no credentials: the plugin keeps its lists as JSON under
# $HERMES_HOME/household (created on first write by the hermes user).
# This script only symlinks the plugin into Elena's plugin dir and enables
# it. Idempotent.
# ============================================================================
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "error: run as root (sudo)" >&2; exit 1; }

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_SRC="$SCRIPT_DIR/plugins/household"
[ -d "$PLUGIN_SRC" ] || { echo "error: plugin source not found at $PLUGIN_SRC" >&2; exit 1; }
id hermes &>/dev/null || { echo "error: hermes user missing — run setup-phase1.sh first" >&2; exit 1; }

HERMES_HOME="$(getent passwd hermes | cut -d: -f6)/.hermes"
[ -d "$HERMES_HOME" ] || { echo "error: $HERMES_HOME missing — run setup-phase1.sh first" >&2; exit 1; }

HERMES_PLUGINS="$HERMES_HOME/plugins"
install -d -o hermes -g hermes -m 750 "$HERMES_PLUGINS"
SYMLINK="$HERMES_PLUGINS/household"

if [ -L "$SYMLINK" ]; then
    [ "$(readlink "$SYMLINK")" = "$PLUGIN_SRC" ] || { echo "error: $SYMLINK points elsewhere — remove it manually" >&2; exit 1; }
    echo "symlink already correct: $SYMLINK -> $PLUGIN_SRC"
elif [ -e "$SYMLINK" ]; then
    echo "error: $SYMLINK exists but is not a symlink — refusing to clobber" >&2; exit 1
else
    ln -s "$PLUGIN_SRC" "$SYMLINK"; chown -h hermes:hermes "$SYMLINK"
    echo "linked $SYMLINK -> $PLUGIN_SRC"
fi

echo "enabling household for the hermes user…"
sudo -u hermes -i hermes plugins enable household \
    || echo "  (enable failed — run manually: sudo -u hermes -i hermes plugins enable household)"

cat <<EOF

 household plugin linked + enabled. Now restart Elena so she loads it:
   sudo systemctl restart hermes
   sudo -u hermes -i hermes tools list      # expect shopping_add/remove/list/clear

 Smoke test from any chat with Elena:
   "add milk and dog food to the shopping list" -> then "what's on the list?"
EOF
