#!/usr/bin/env bash
# ============================================================================
# Tier 1: weekly ENCRYPTED file-level backup of the irreplaceable Jetson set.
#   - restic repo lives LOCALLY on the HDD (fast backups + fast local restore)
#   - the encrypted repo is then MIRRORED off-box to the NAS via rsync-over-ssh
#     (the SSH shell path works; Synology's SFTP ACL blocks restic-sftp).
# Run once: sudo bash ~/tier1-restic-setup.sh
# ============================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "must run as root (sudo)"; exit 1; }

REPO=/mnt/transcend/restic-repo                       # local encrypted repo
NAS_MIRROR=/volume1/jetson-backup/restic-repo         # off-box copy on the NAS
PWFILE=/etc/restic/password
PW_SRC="/etc/restic/password.seed"

echo "=== 1/8 restic present + latest ==="
command -v restic >/dev/null || { apt-get update -qq; apt-get install -y -qq restic; }
restic self-update 2>/dev/null || true
restic version

echo "=== 2/8 password file ==="
install -d -m 700 /etc/restic
if [ ! -s "$PWFILE" ]; then
  [ -s "$PW_SRC" ] || { echo "FATAL: no password at $PW_SRC and $PWFILE empty"; exit 1; }
  install -m 600 /dev/null "$PWFILE"; cat "$PW_SRC" > "$PWFILE"
fi
echo "  $PWFILE ready ($(wc -c <"$PWFILE") bytes)"

echo "=== 3/8 root -> NAS SSH (for the rsync mirror) ==="
install -d -m 700 /root/.ssh
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' -C "root@jetson-backup" -f /root/.ssh/id_ed25519
grep -q '^Host nas$' /root/.ssh/config 2>/dev/null || cat >> /root/.ssh/config <<'EOF'
Host nas
    HostName 192.168.1.100
    User jetson
    IdentityFile /root/.ssh/id_ed25519
    StrictHostKeyChecking accept-new
EOF
chmod 600 /root/.ssh/config
PUB=$(cat /root/.ssh/id_ed25519.pub)
sudo -u dbexpertai ssh -o BatchMode=yes nas \
  "grep -qF '$PUB' ~/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> ~/.ssh/authorized_keys"
ssh -o BatchMode=yes nas 'echo "  NAS reachable as $(whoami)@$(hostname)"'

echo "=== 4/8 init LOCAL encrypted repo on the HDD ==="
export RESTIC_REPOSITORY="$REPO" RESTIC_PASSWORD_FILE="$PWFILE"
restic cat config >/dev/null 2>&1 && echo "  repo already initialized" || { restic init; echo "  repo initialized at $REPO"; }

echo "=== 5/8 exclude list ==="
cat > /etc/restic/excludes.txt <<'EOF'
**/.cache
**/.npm
**/.local/lib
**/.local/share/Trash
**/.vscode
**/.mozilla
**/.codex
**/.cargo
**/.rustup
**/node_modules
**/__pycache__
**/*.pyc
**/.venv
**/venv
**/.stversions
/home/dbexpertai/code/content/.git
/home/dbexpertai/code/content/output
EOF
echo "  wrote /etc/restic/excludes.txt"

echo "=== 6/8 install backup runner ==="
cat > /usr/local/sbin/jetson-nas-backup <<EOF
#!/usr/bin/env bash
# Weekly encrypted file-level backup -> local HDD repo, then rsync mirror -> NAS.
set -uo pipefail
export RESTIC_REPOSITORY="$REPO"
export RESTIC_PASSWORD_FILE="$PWFILE"
LOG=/var/log/jetson-nas-backup; mkdir -p "\$LOG"
exec >>"\$LOG/backup.log" 2>&1
echo "===== \$(date -u +%FT%TZ) backup start ====="

MAN=/var/backups/jetson-manifest; mkdir -p "\$MAN"
dpkg --get-selections > "\$MAN/dpkg-selections.txt" 2>/dev/null || true
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' > "\$MAN/docker-images.txt" 2>/dev/null || true
( crontab -l -u root 2>/dev/null; echo "# --- dbexpertai ---"; crontab -l -u dbexpertai 2>/dev/null ) > "\$MAN/crontabs.txt" || true
ls -1 /etc/cron.d > "\$MAN/cron.d-list.txt" 2>/dev/null || true
pip3 freeze > "\$MAN/pip3-freeze.txt" 2>/dev/null || true

restic backup --verbose --exclude-file=/etc/restic/excludes.txt \\
  /etc /usr/local/sbin /home/hermes/.hermes /mnt/transcend/hermes-db-backups \\
  /home/dbexpertai /mnt/sdcard/lora-training /mnt/sdcard/ai-toolkit/output \\
  /mnt/sdcard/ai-toolkit/config /mnt/sdcard/comfyui-models/loras "\$MAN"
echo "--- retention (7 daily, 8 weekly, 12 monthly) ---"
restic forget --keep-daily 7 --keep-weekly 8 --keep-monthly 12 --prune

echo "--- mirror encrypted repo off-box to NAS via rsync ---"
rsync -a --delete -e ssh "$REPO/" "nas:$NAS_MIRROR/"
echo "===== \$(date -u +%FT%TZ) backup done ====="
EOF
chmod 755 /usr/local/sbin/jetson-nas-backup
echo "  installed /usr/local/sbin/jetson-nas-backup"

echo "=== 7/8 weekly cron (Sun 03:30) ==="
cat > /etc/cron.d/jetson-nas-backup <<'EOF'
# Weekly encrypted backup: local HDD restic repo + rsync mirror to NAS. Runs as root.
30 3 * * 0 root /usr/local/sbin/jetson-nas-backup
EOF
chmod 644 /etc/cron.d/jetson-nas-backup

echo "=== 8/8 first backup now ==="
ssh -o BatchMode=yes nas "mkdir -p $NAS_MIRROR"
/usr/local/sbin/jetson-nas-backup
echo
echo "--- local repo snapshots ---"; restic snapshots 2>/dev/null | tail -6
echo "--- local repo size ---"; du -sh "$REPO" 2>/dev/null
echo "--- NAS mirror size ---"; ssh -o BatchMode=yes nas "du -sh $NAS_MIRROR" 2>/dev/null
echo
echo "================================================================"
echo " Tier 1 ready: local repo $REPO  +  NAS mirror nas:$NAS_MIRROR"
echo " Manual run : sudo /usr/local/sbin/jetson-nas-backup"
echo " Restore    : sudo RESTIC_PASSWORD_FILE=$PWFILE restic -r $REPO restore latest --target /tmp/restore"
echo " If Jetson dies: rsync the repo back from nas:$NAS_MIRROR, then restic restore."
echo "================================================================"
