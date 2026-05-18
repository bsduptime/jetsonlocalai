#!/usr/bin/env bash
# ============================================================================
# Hermes Agent — Phase 1: dedicated user + ACLs + reinstall
# ============================================================================
# Run with (from jetsonlocalai repo root):
#   sudo bash hermes-agent/setup-phase1.sh
#
# This script:
#   1. Removes the prior Hermes install under dbexpertai (nothing of value
#      there yet — no OAuth, no chat history, no custom config).
#   2. Creates a `hermes` system user. NOT in sudo, NOT in docker. Has its
#      own home, its own login shell.
#   3. Installs `acl` package + sets POSIX ACLs so `hermes` user has r/w on
#      /home/dbexpertai/code/ (only that — everything else stays invisible).
#   4. Makes /home/dbexpertai traversable (o+x) so hermes can chdir into code/.
#      Does NOT grant read access to /home/dbexpertai itself.
#   5. Copies /home/dbexpertai/.codex/auth.json into /home/hermes/.codex/
#      so the Hermes setup wizard can import the OAuth credentials.
#   6. Pre-creates /home/hermes/.claude/ as a mountpoint target for the
#      systemd bind-mount that'll share dbexpertai's Claude credentials
#      read-only (configured in Phase 2).
#   7. Reinstalls Hermes Agent as the `hermes` user via the upstream
#      installer (curl | bash). Same script as before, different user.
#
# After this completes, run:
#   sudo -u hermes -i
#   hermes setup   # interactive OAuth wizard (you drive)
# ============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

step() { echo; echo "=== $* ==="; }

# ----------------------------------------------------------------------------
step "1/7: Remove existing Hermes install under dbexpertai"
# ----------------------------------------------------------------------------
if [ -d /home/dbexpertai/.hermes ]; then
    echo "removing /home/dbexpertai/.hermes (no OAuth or chat history yet)"
    sudo -u dbexpertai rm -rf /home/dbexpertai/.hermes
else
    echo "/home/dbexpertai/.hermes already absent"
fi
if [ -f /home/dbexpertai/.local/bin/hermes ]; then
    sudo -u dbexpertai rm -f /home/dbexpertai/.local/bin/hermes
    echo "removed dbexpertai's hermes CLI symlink"
fi

# ----------------------------------------------------------------------------
step "2/7: Create hermes system user"
# ----------------------------------------------------------------------------
if id hermes &>/dev/null; then
    echo "user hermes already exists; verifying group membership"
else
    useradd --create-home --shell /bin/bash --comment "Hermes Agent" hermes
    echo "created user hermes"
fi
# Belt-and-suspenders: make sure hermes is NOT in sudo / docker / any
# privileged group, even if a prior run added them.
for grp in sudo docker adm wheel root; do
    if id -nG hermes | tr ' ' '\n' | grep -qx "$grp"; then
        gpasswd -d hermes "$grp" || true
        echo "removed hermes from $grp group"
    fi
done
echo "hermes groups: $(id -nG hermes)"

# ----------------------------------------------------------------------------
step "3/7: Install ACL tooling + grant hermes r/w on /home/dbexpertai/code"
# ----------------------------------------------------------------------------
if ! command -v setfacl &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq acl
fi
# Recursive ACLs for existing files + default ACLs for new files
setfacl -R  -m u:hermes:rwX /home/dbexpertai/code
setfacl -R -d -m u:hermes:rwX /home/dbexpertai/code
echo "ACLs applied on /home/dbexpertai/code (sample):"
getfacl /home/dbexpertai/code 2>/dev/null | sed -n '1,8p'

# ----------------------------------------------------------------------------
step "4/7: Allow hermes to traverse into /home/dbexpertai (without read)"
# ----------------------------------------------------------------------------
# o+x lets non-members chdir() into the directory but NOT list its contents.
# So hermes can `cd /home/dbexpertai/code` but cannot `ls /home/dbexpertai`.
chmod o+x /home/dbexpertai
echo "/home/dbexpertai is now $(stat -c '%a' /home/dbexpertai) (711 = traverse-only for others)"

# ----------------------------------------------------------------------------
step "5/7: Copy Codex OAuth credentials for Hermes wizard import"
# ----------------------------------------------------------------------------
if [ ! -f /home/dbexpertai/.codex/auth.json ]; then
    echo "warning: /home/dbexpertai/.codex/auth.json not found — skipping copy"
    echo "         hermes setup wizard will fall back to a fresh OAuth device flow"
else
    install -d -o hermes -g hermes -m 700 /home/hermes/.codex
    install -o hermes -g hermes -m 600 \
        /home/dbexpertai/.codex/auth.json /home/hermes/.codex/auth.json
    echo "copied Codex auth.json into /home/hermes/.codex/"
fi

# ----------------------------------------------------------------------------
step "6/7: Pre-create /home/hermes/.claude/ for systemd bind-mount target"
# ----------------------------------------------------------------------------
install -d -o hermes -g hermes -m 700 /home/hermes/.claude
# Placeholder file the systemd unit will overlay with dbexpertai's real
# .credentials.json (read-only bind-mount, configured in Phase 2).
# Filename is `.credentials.json` (leading dot) to match what the `claude`
# CLI actually writes — NOT the conventional `credentials.json`.
touch /home/hermes/.claude/.credentials.json
chown hermes:hermes /home/hermes/.claude/.credentials.json
chmod 600 /home/hermes/.claude/.credentials.json
echo "/home/hermes/.claude/ ready for bind-mount target"

# ----------------------------------------------------------------------------
step "7a/7: Pre-install system dependencies (so hermes user is never prompted for sudo)"
# ----------------------------------------------------------------------------
# The Hermes upstream installer detects missing system tools and tries to
# `sudo apt install` them.  But the `hermes` user has no sudo by design —
# that's the whole point of the dedicated-user model.  So we install the
# nice-to-have deps system-wide HERE, as root, before invoking the installer
# as hermes.  The installer then finds them present and doesn't prompt.
apt-get update -qq
apt-get install -y -qq ripgrep ffmpeg
echo "system deps present: $(command -v rg) $(command -v ffmpeg)"

# ----------------------------------------------------------------------------
step "7b/7: Clean any partial Hermes install under /home/hermes"
# ----------------------------------------------------------------------------
# If a previous run of this script got interrupted mid-install (e.g. blocked
# on the sudo-for-ripgrep prompt before we added 7a), there may be a partial
# ~/.hermes tree.  Wipe it so the installer starts clean.
if [ -d /home/hermes/.hermes ]; then
    echo "removing partial /home/hermes/.hermes from prior interrupted run"
    rm -rf /home/hermes/.hermes
fi
if [ -f /home/hermes/.local/bin/hermes ]; then
    rm -f /home/hermes/.local/bin/hermes
fi

# ----------------------------------------------------------------------------
step "7c/7: Install Hermes Agent as the hermes user"
# ----------------------------------------------------------------------------
echo "running upstream installer as hermes (this takes a few minutes)..."
# `< /dev/null` answers EOF to any unexpected interactive prompt rather than
# blocking forever.  Combined with pre-installing ripgrep/ffmpeg above, the
# install should run fully non-interactively.
sudo -u hermes -H bash -lc \
    'curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash' \
    < /dev/null 2>&1 | tail -30

echo
echo "verifying install..."
sudo -u hermes -H -i bash -lc 'which hermes && hermes --version'

# ----------------------------------------------------------------------------
echo
echo "========================================================================"
echo " Phase 1 complete."
echo "========================================================================"
echo
echo " Next (interactive, YOU drive):"
echo "   sudo -u hermes -i"
echo "   hermes setup       # accept the Codex auth.json import; pick openai-codex"
echo "   exit"
echo
echo " Then tell Claude Code: 'OAuth done' and we'll proceed with Phase 2"
echo " (systemd unit with sandboxing + auditd + monitoring)."
echo "========================================================================"
