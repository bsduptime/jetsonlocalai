#!/usr/bin/env bash
# ============================================================================
# Hermes vault plugin — installer
# ============================================================================
# Run with (from jetsonlocalai repo root):
#   sudo bash hermes-agent/install-vault-plugin.sh
#
# Idempotent. Safe to re-run. What it does:
#
#   1. Find the hermes user's home + Hermes home dir.
#   2. Symlink the plugin code into $HERMES_HOME/plugins/vault so Hermes
#      auto-discovers it on next start. Refuses to clobber a pre-existing
#      symlink that points somewhere else.
#
# Nothing to seed: the plugin is stateless. All state lives in the synced
# vault at /home/dbexpertai/obsidian-vault/agents/hermes/, which is set up
# by step 7 of projects/maclocalai/shared-memory-architecture.md.
# ============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_SRC="$SCRIPT_DIR/plugins/vault"

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
step "1/3: Verify vault ACL setup (step 7)"
# ----------------------------------------------------------------------------
VAULT_NS="/home/dbexpertai/obsidian-vault/agents/hermes"
if [ ! -d "$VAULT_NS" ]; then
    echo "error: $VAULT_NS does not exist — run step 7 of the architecture plan first" >&2
    exit 1
fi

if ! command -v getfacl &>/dev/null; then
    echo "error: getfacl not installed — cannot verify the vault write boundary. Aborting." >&2
    echo "Install with: apt-get install acl" >&2
    exit 1
fi

acl_out=$(getfacl -p "$VAULT_NS" 2>/dev/null)
problem=""
echo "$acl_out" | grep -q "^user:hermes:rwx" || problem="missing access ACL user:hermes:rwx"
echo "$acl_out" | grep -q "^default:user:hermes:rwx" || problem="${problem:+$problem; }missing default ACL default:user:hermes:rwx"
# Reject if the mask caps hermes's effective bits below rwx.
if echo "$acl_out" | grep -q "^user:hermes:rwx[[:space:]]*#effective:"; then
    eff=$(echo "$acl_out" | sed -n 's/^user:hermes:rwx[[:space:]]*#effective:\([rwx-]*\).*/\1/p')
    if [ "$eff" != "rwx" ]; then
        problem="${problem:+$problem; }mask caps hermes effective to '$eff' instead of rwx"
    fi
fi

if [ -n "$problem" ]; then
    echo "error: vault ACL not configured correctly — $problem" >&2
    echo "Run: setfacl -R -m u:hermes:rwX -m d:u:hermes:rwx $VAULT_NS" >&2
    echo "Then re-run this installer." >&2
    exit 1
fi
echo "ACL OK: access + default + effective rwx for hermes on $VAULT_NS"

# ----------------------------------------------------------------------------
step "2/3: Symlink plugin code into Hermes plugin dir"
# ----------------------------------------------------------------------------
HERMES_PLUGINS="$HERMES_HOME/plugins"
install -d -o hermes -g hermes -m 750 "$HERMES_PLUGINS"
SYMLINK="$HERMES_PLUGINS/vault"

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
step "3/4: Enable the vault plugin in Hermes' opt-in registry"
# ----------------------------------------------------------------------------
# Hermes plugins are opt-in by default — symlinking is not enough; the
# plugin must be flipped on in Hermes' registry before tools register at
# next start. Idempotent: re-running on an already-enabled plugin is a no-op.
if sudo -u hermes -i hermes plugins enable vault 2>&1 | tee /tmp/.vault-enable.log; then
    echo "plugin marked as enabled"
else
    # `hermes plugins enable` returning non-zero may just mean "already enabled".
    if grep -qiE "already.enabled|enabled" /tmp/.vault-enable.log; then
        echo "plugin already enabled"
    else
        echo "error: failed to enable vault plugin — check the output above" >&2
        rm -f /tmp/.vault-enable.log
        exit 1
    fi
fi
rm -f /tmp/.vault-enable.log

# ----------------------------------------------------------------------------
step "4/4: Print verification + next steps"
# ----------------------------------------------------------------------------
cat <<EOF

========================================================================
 Vault plugin installed AND enabled.
========================================================================

  Plugin code:    $PLUGIN_SRC
  Plugin link:    $SYMLINK
  Vault root:     /home/dbexpertai/obsidian-vault  (synced via Syncthing)
  Hermes write boundary:  /home/dbexpertai/obsidian-vault/agents/hermes/
                          (kernel-enforced via POSIX ACL)

 Tools exposed:
   - vault_session_brief    (call ONCE at session start)
   - vault_read
   - vault_write_observation  (append-only; agents/hermes/observations/)
   - vault_write_memory       (mutable; agents/hermes/memory/<relpath>.md)
   - vault_conflict_scan      (treat any results as errors)

 FINAL STEP — restart Hermes so the tools register:
   sudo systemctl restart hermes

 Verify after restart:
   sudo -u hermes -i hermes plugins list | grep vault   # status: enabled
   sudo -u hermes -i hermes tools list   | grep vault   # vault toolset listed

 Optional: paste the system-prompt block from
   $PLUGIN_SRC/README.md
 into Hermes' top-level system prompt for belt-and-suspenders.

========================================================================
EOF
