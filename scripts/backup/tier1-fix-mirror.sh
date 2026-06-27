#!/usr/bin/env bash
# Replace the (Synology-blocked) rsync mirror with an incremental ssh+tar mirror.
# Run: sudo bash ~/tier1-fix-mirror.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

echo "=== install /usr/local/sbin/jetson-repo-mirror ==="
cat > /usr/local/sbin/jetson-repo-mirror <<'EOF'
#!/usr/bin/env bash
# Incremental off-box mirror of the local restic repo -> NAS via ssh+tar.
# Synology blocks rsync-over-ssh for non-admin users, but ssh+tar works, and
# restic's data files are immutable + hash-named (only added or pruned), so a
# name-based add/delete sync is correct and ships only new pack files.
set -uo pipefail
LOCAL=/mnt/transcend/restic-repo
REMOTE=/volume1/jetson-backup/restic-repo
SSH=(ssh -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new nas)

"${SSH[@]}" "mkdir -p '$REMOTE'"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
( cd "$LOCAL" && find . -type f | sort ) > "$tmp/local.list"
"${SSH[@]}" "cd '$REMOTE' && find . -type f 2>/dev/null | sort" > "$tmp/remote.list"
comm -23 "$tmp/local.list" "$tmp/remote.list" > "$tmp/add.list"
comm -13 "$tmp/local.list" "$tmp/remote.list" > "$tmp/del.list"
nadd=$(wc -l < "$tmp/add.list"); ndel=$(wc -l < "$tmp/del.list")
echo "mirror: +$nadd new, -$ndel stale"
if [ "$nadd" -gt 0 ]; then
  tar -C "$LOCAL" -cf - -T "$tmp/add.list" | "${SSH[@]}" "tar -C '$REMOTE' -xf - && echo '  new files extracted'"
fi
if [ "$ndel" -gt 0 ]; then
  sed "s#^\./#$REMOTE/#" "$tmp/del.list" | "${SSH[@]}" "xargs -d '\n' rm -f -- && echo '  stale files removed'"
fi
echo "mirror done: $("${SSH[@]}" "du -sh '$REMOTE' | cut -f1")"
EOF
chmod 755 /usr/local/sbin/jetson-repo-mirror

echo "=== rewrite runner to call the new mirror (drop rsync) ==="
cat > /usr/local/sbin/jetson-nas-backup <<'EOF'
#!/usr/bin/env bash
# Weekly encrypted file-level backup -> local HDD repo, then ssh+tar mirror -> NAS.
set -uo pipefail
export RESTIC_REPOSITORY=/mnt/transcend/restic-repo
export RESTIC_PASSWORD_FILE=/etc/restic/password
LOG=/var/log/jetson-nas-backup; mkdir -p "$LOG"
exec >>"$LOG/backup.log" 2>&1
echo "===== $(date -u +%FT%TZ) backup start ====="

MAN=/var/backups/jetson-manifest; mkdir -p "$MAN"
dpkg --get-selections > "$MAN/dpkg-selections.txt" 2>/dev/null || true
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' > "$MAN/docker-images.txt" 2>/dev/null || true
( crontab -l -u root 2>/dev/null; echo "# --- dbexpertai ---"; crontab -l -u dbexpertai 2>/dev/null ) > "$MAN/crontabs.txt" || true
ls -1 /etc/cron.d > "$MAN/cron.d-list.txt" 2>/dev/null || true
pip3 freeze > "$MAN/pip3-freeze.txt" 2>/dev/null || true

restic backup --verbose --exclude-file=/etc/restic/excludes.txt \
  /etc /usr/local/sbin /home/hermes/.hermes /mnt/transcend/hermes-db-backups \
  /home/dbexpertai /mnt/sdcard/lora-training /mnt/sdcard/ai-toolkit/output \
  /mnt/sdcard/ai-toolkit/config /mnt/sdcard/comfyui-models/loras "$MAN"
echo "--- retention (7 daily, 8 weekly, 12 monthly) ---"
restic forget --keep-daily 7 --keep-weekly 8 --keep-monthly 12 --prune
echo "--- off-box mirror to NAS ---"
/usr/local/sbin/jetson-repo-mirror
echo "===== $(date -u +%FT%TZ) backup done ====="
EOF
chmod 755 /usr/local/sbin/jetson-nas-backup

echo "=== run the first off-box mirror now (8.7G -> NAS, a couple minutes) ==="
/usr/local/sbin/jetson-repo-mirror
echo
echo "=== verify: local vs NAS file counts match ==="
LC=$(find /mnt/transcend/restic-repo -type f | wc -l)
RC=$(ssh -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes nas 'find /volume1/jetson-backup/restic-repo -type f | wc -l')
echo "local files: $LC   NAS files: $RC"
[ "$LC" = "$RC" ] && echo "✅ MIRROR COMPLETE — counts match" || echo "⚠️ count mismatch — check log"
ssh -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes nas 'du -sh /volume1/jetson-backup/restic-repo'
