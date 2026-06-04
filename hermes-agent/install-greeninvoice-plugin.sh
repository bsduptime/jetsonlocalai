#!/usr/bin/env bash
# ============================================================================
# Hermes greeninvoice plugin — installer
# ============================================================================
# Run (from jetsonlocalai repo root):
#   sudo bash hermes-agent/install-greeninvoice-plugin.sh
#
# Idempotent. Unlike the email plugin, this plugin holds NO config and NO
# credentials — all of that lives in the hermes-greeninvoice daemon (install
# it first with invoice-relay/setup-hermes-greeninvoice.sh). So this script
# only symlinks the plugin code into the Hermes plugin dir. The plugin
# reaches the daemon over /run/hermes-greeninvoice/sock; the hermes user's
# membership in hermes-greeninvoice-clients (granted by the daemon installer)
# is what authorizes the connection.
# ============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_SRC="$SCRIPT_DIR/plugins/greeninvoice"

if [ ! -d "$PLUGIN_SRC" ]; then
    echo "error: plugin source not found at $PLUGIN_SRC" >&2
    exit 1
fi
if ! id hermes &>/dev/null; then
    echo "error: hermes user does not exist. Run setup-phase1.sh first." >&2
    exit 1
fi

HERMES_HOME_ROOT=$(getent passwd hermes | cut -d: -f6)
HERMES_HOME="$HERMES_HOME_ROOT/.hermes"
if [ ! -d "$HERMES_HOME" ]; then
    echo "error: $HERMES_HOME does not exist — run setup-phase1.sh first" >&2
    exit 1
fi

# Warn (don't fail) if the daemon side isn't installed yet.
if [ ! -S /run/hermes-greeninvoice/sock ]; then
    echo "note: /run/hermes-greeninvoice/sock not present — install/start the daemon" >&2
    echo "      with invoice-relay/setup-hermes-greeninvoice.sh, or tools will return" >&2
    echo "      daemon_unreachable until it's running." >&2
fi
if id hermes &>/dev/null && ! id -nG hermes | tr ' ' '\n' | grep -qx hermes-greeninvoice-clients; then
    echo "note: hermes is not yet in hermes-greeninvoice-clients — run the daemon" >&2
    echo "      installer so the plugin can connect() to the socket." >&2
fi

HERMES_PLUGINS="$HERMES_HOME/plugins"
install -d -o hermes -g hermes -m 750 "$HERMES_PLUGINS"
SYMLINK="$HERMES_PLUGINS/greeninvoice"

if [ -L "$SYMLINK" ]; then
    cur_target=$(readlink "$SYMLINK")
    if [ "$cur_target" = "$PLUGIN_SRC" ]; then
        echo "symlink already correct: $SYMLINK -> $cur_target"
    else
        echo "error: $SYMLINK exists and points to $cur_target — remove it manually to relink" >&2
        exit 1
    fi
elif [ -e "$SYMLINK" ]; then
    echo "error: $SYMLINK exists but is not a symlink — refusing to clobber" >&2
    exit 1
else
    ln -s "$PLUGIN_SRC" "$SYMLINK"
    chown -h hermes:hermes "$SYMLINK"
    echo "linked $SYMLINK -> $PLUGIN_SRC"
fi

cat <<EOF

========================================================================
 greeninvoice plugin installed.
========================================================================

  Plugin code:  $PLUGIN_SRC
  Plugin link:  $SYMLINK
  Daemon:       systemctl status hermes-greeninvoice
  Socket:       /run/hermes-greeninvoice/sock

 The plugin holds no credentials. All policy + the API key live in the
 hermes-greeninvoice daemon. No extra Python deps (stdlib only).

 Reload Hermes so the plugin is picked up:
   sudo systemctl restart hermes
   sudo -u hermes -i hermes plugins list
   sudo -u hermes -i hermes tools list   # should show gi_draft_invoice, gi_issue_invoice, ...

========================================================================
EOF
