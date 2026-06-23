#!/usr/bin/env bash
# ============================================================================
# Hermes voice plugin — installer
# ============================================================================
# Run with (from jetsonlocalai repo root):
#   sudo bash hermes-agent/install-voice-plugin.sh
#
# Idempotent. Safe to re-run. What it does:
#
#   1. Find the hermes user's home + Hermes home dir.
#   2. Symlink the plugin code into $HERMES_HOME/plugins/voice so Hermes
#      auto-discovers it on next start. Refuses to clobber a pre-existing
#      symlink that points somewhere else (operator must remove manually).
#
# Unlike the email/greeninvoice plugins there is NO config dir, NO secrets,
# and NO broker daemon: the voice tool just makes two HTTP POSTs (synthesize
# on localhost, play on the Mac listener). Topology defaults are baked into
# the plugin; override via the HERMES_VOICE_* env vars on the hermes unit if
# ever needed (see plugins/voice/README.md).
# ============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_SRC="$SCRIPT_DIR/plugins/voice"

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

step() { echo; echo "=== $* ==="; }

# ----------------------------------------------------------------------------
step "1/2: Symlink plugin code into Hermes plugin dir"
# ----------------------------------------------------------------------------
HERMES_PLUGINS="$HERMES_HOME/plugins"
install -d -o hermes -g hermes -m 750 "$HERMES_PLUGINS"
SYMLINK="$HERMES_PLUGINS/voice"

if [ -L "$SYMLINK" ]; then
    cur_target=$(readlink "$SYMLINK")
    if [ "$cur_target" = "$PLUGIN_SRC" ]; then
        echo "symlink already correct: $SYMLINK -> $cur_target"
    else
        echo "error: $SYMLINK exists and points to $cur_target — remove it manually if you want to relink" >&2
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

# ----------------------------------------------------------------------------
step "2/2: Print verification + next steps"
# ----------------------------------------------------------------------------
cat <<EOF

========================================================================
 Voice plugin installed.
========================================================================

  Plugin code:    $PLUGIN_SRC
  Plugin link:    $SYMLINK

 No config, no secrets, no daemon. Defaults (override via env on the unit):
   HERMES_VOICE_TTS_URL       http://localhost:18080
   HERMES_VOICE_LISTENER_URL  http://100.82.188.1:18082
   HERMES_VOICE_NAME          devnen-elena

 Reload Hermes so the plugin is picked up:
   sudo systemctl restart hermes
   sudo -u hermes -i hermes plugins enable voice   # if not auto-enabled
   # then start a NEW session — 'hermes tools list' runs outside a session
   # and won't show plugin tools.

========================================================================
EOF
