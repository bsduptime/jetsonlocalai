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

# ----------------------------------------------------------------------------
# Allowlist the daemon's preview dir for Hermes' Telegram/media delivery.
# Draft previews are written to /run/hermes-greeninvoice/previews, but Hermes
# refuses to attach files outside its media allowlist. Add that dir via a
# systemd drop-in on the hermes gateway — but NEVER clobber an existing
# HERMES_MEDIA_ALLOW_DIRS value (warn instead, so the operator can merge).
# ----------------------------------------------------------------------------
PREVIEWS_DIR=/run/hermes-greeninvoice/previews
DROPIN_DIR=/etc/systemd/system/hermes.service.d
DROPIN="$DROPIN_DIR/greeninvoice-previews.conf"
CUR_ENV=$(systemctl show hermes -p Environment 2>/dev/null | sed 's/^Environment=//' | tr ' ' '\n')
if printf '%s\n' "$CUR_ENV" | grep -q "HERMES_MEDIA_ALLOW_DIRS=.*$PREVIEWS_DIR"; then
    echo "media allowlist already includes $PREVIEWS_DIR (ok)"
elif printf '%s\n' "$CUR_ENV" | grep -q "^HERMES_MEDIA_ALLOW_DIRS="; then
    echo "WARNING: hermes already sets HERMES_MEDIA_ALLOW_DIRS without $PREVIEWS_DIR." >&2
    echo "         Append it manually (keep the existing value) in a drop-in, e.g.:" >&2
    echo "         HERMES_MEDIA_ALLOW_DIRS=<existing>:$PREVIEWS_DIR" >&2
else
    install -d "$DROPIN_DIR"
    printf '[Service]\nEnvironment="HERMES_MEDIA_ALLOW_DIRS=%s"\n' "$PREVIEWS_DIR" > "$DROPIN"
    systemctl daemon-reload
    echo "wrote $DROPIN — allowlists $PREVIEWS_DIR for Hermes (restart hermes to apply)"
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

 Enable + load the plugin (a symlink alone is NOT enough — Hermes needs an
 explicit enable, and plugin tools register per SESSION, not at boot):
   sudo -u hermes -i hermes plugins enable greeninvoice
   sudo systemctl restart hermes
   sudo -u hermes -i hermes plugins list           # greeninvoice -> enabled
   # then start a NEW session (e.g. /new in Telegram) — the gi_* tools only
   # appear inside a session, so 'hermes tools list' (run outside one) won't
   # show them even when everything is correct.

 Preview/receipt PDFs are written to /run/hermes-greeninvoice/previews and
 this script allowlists that dir for Hermes media delivery (above). Without
 it, Hermes can read the PDF but refuses to attach it to Telegram.

========================================================================
EOF
