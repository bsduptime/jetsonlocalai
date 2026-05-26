#!/usr/bin/env bash
# ============================================================================
# Hermes email plugin — installer
# ============================================================================
# Run with (from jetsonlocalai repo root):
#   sudo bash hermes-agent/install-email-plugin.sh
#
# Idempotent. Safe to re-run. What it does:
#
#   1. Find the hermes user's home + Hermes home dir.
#   2. Create the plugin-private config + state directory at
#      $HERMES_HOME/email-plugin/, owned by hermes:hermes mode 750, with
#      an ACL that grants dbexpertai read/write so David can edit `.env`
#      and `allowlist.yaml` without sudo.
#   3. Seed `.env` and `allowlist.yaml` from the templates ONLY if they
#      don't exist yet (so re-running the script never clobbers David's
#      edits).
#   4. Create the state subdirectory (mode 700, hermes-only) for the
#      sqlite rate-limit DB and the audit log.
#   5. Symlink the plugin code into $HERMES_HOME/plugins/email so Hermes
#      auto-discovers it on next start. Refuses to clobber a pre-existing
#      symlink that points somewhere else (operator must remove manually).
# ============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_SRC="$SCRIPT_DIR/plugins/mailer"

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
step "1/5: Create plugin-private config dir"
# ----------------------------------------------------------------------------
PDIR="$HERMES_HOME/email-plugin"
install -d -o hermes -g hermes -m 750 "$PDIR"

if command -v setfacl &>/dev/null; then
    # David needs to edit the .env and allowlist.yaml directly.
    setfacl -m u:dbexpertai:rwx "$PDIR"
    setfacl -d -m u:dbexpertai:rwX "$PDIR"
    echo "ACLs set: dbexpertai has rwX on $PDIR (and default for new files)"
else
    echo "warning: setfacl not installed — David will need sudo to edit config" >&2
fi

# ----------------------------------------------------------------------------
step "2/5: Seed .env and allowlist.yaml (only if missing)"
# ----------------------------------------------------------------------------
ENV_FILE="$PDIR/.env"
ALLOWLIST_FILE="$PDIR/allowlist.yaml"

if [ ! -f "$ENV_FILE" ]; then
    install -m 600 -o hermes -g hermes \
        "$PLUGIN_SRC/.env.example" "$ENV_FILE"
    echo "seeded $ENV_FILE — EDIT THIS to add transport + creds"
else
    echo "$ENV_FILE already exists; leaving alone"
fi

if [ ! -f "$ALLOWLIST_FILE" ]; then
    install -m 600 -o hermes -g hermes \
        "$PLUGIN_SRC/allowlist.example.yaml" "$ALLOWLIST_FILE"
    echo "seeded $ALLOWLIST_FILE — EDIT THIS to add contacts"
else
    echo "$ALLOWLIST_FILE already exists; leaving alone"
fi

# ----------------------------------------------------------------------------
step "3/5: Create state dir (hermes-only, mode 700)"
# ----------------------------------------------------------------------------
STATE_DIR="$PDIR/state"
install -d -o hermes -g hermes -m 700 "$STATE_DIR"
install -d -o hermes -g hermes -m 700 "$STATE_DIR/dryrun"
echo "state at $STATE_DIR (mode 700)"

# ----------------------------------------------------------------------------
step "4/5: Symlink plugin code into Hermes plugin dir"
# ----------------------------------------------------------------------------
HERMES_PLUGINS="$HERMES_HOME/plugins"
install -d -o hermes -g hermes -m 750 "$HERMES_PLUGINS"
SYMLINK="$HERMES_PLUGINS/mailer"

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
step "5/5: Print verification + next steps"
# ----------------------------------------------------------------------------
cat <<EOF

========================================================================
 Email plugin installed.
========================================================================

  Plugin code:    $PLUGIN_SRC
  Plugin link:    $SYMLINK
  Config dir:     $PDIR
  Edit:           $ENV_FILE            <-- transport + creds
                  $ALLOWLIST_FILE      <-- per-recipient limits
  State:          $STATE_DIR
  Audit log:      $STATE_DIR/sent.log

 Required Python deps inside the hermes virtualenv:
   sudo -u hermes -i pip install --user pyyaml
   # and ONE of:
   sudo -u hermes -i pip install --user resend     # if using EMAIL_TRANSPORT=resend

 Reload Hermes so the plugin is picked up:
   sudo systemctl restart hermes
   sudo -u hermes -i hermes plugins list
   sudo -u hermes -i hermes tools list   # should show send_email

========================================================================
EOF
