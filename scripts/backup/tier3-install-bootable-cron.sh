#!/usr/bin/env bash
# Tier 3: install a cron-safe monthly bootable full-disk image of the eMMC -> NAS.
# Hardened version of ~/code/backup-jetson-bootable.sh (pinned root key, non-interactive).
# Run: sudo bash ~/tier3-install-bootable-cron.sh   (installs only; run an image separately)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

echo "=== install /usr/local/sbin/jetson-bootable-backup ==="
cat > /usr/local/sbin/jetson-bootable-backup <<'OUTER'
#!/usr/bin/env bash
# Monthly bootable full-disk image of the eMMC -> NAS. Cron-safe, non-interactive.
# Full dd of /dev/mmcblk0 (all boot partitions) | gzip | ssh cat -> NAS. Keeps last 3.
set -uo pipefail
NAS_SSH=(ssh -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new nas)
DEST=/volume1/jetson-backup
DEV=/dev/mmcblk0
STAMP=$(date +%Y%m%d-%H%M%S)
NAME=jetson-orin-bootable-$STAMP
LOG=/var/log/jetson-bootable-backup.log
MAX=3
exec >>"$LOG" 2>&1
echo "===== $(date) bootable backup start ====="
"${NAS_SSH[@]}" "mkdir -p $DEST" || { echo "NAS unreachable — abort"; exit 1; }
SIZE=$(blockdev --getsize64 $DEV); GB=$((SIZE/1024/1024/1024))
echo "imaging $DEV (${GB}GB, crash-consistent live dd) -> $DEST/$NAME.img.gz"
dd if=$DEV bs=4M status=none | gzip -1 | "${NAS_SSH[@]}" "cat > $DEST/$NAME.img.gz"
rc=${PIPESTATUS[0]}; [ "$rc" -eq 0 ] || { echo "dd/stream failed rc=$rc"; exit 1; }
"${NAS_SSH[@]}" "cat > $DEST/$NAME.info" <<INFO
Jetson Orin AGX bootable image
Date: $(date)
Device: $DEV (${GB}GB, full eMMC incl. bootloader partitions)
Hostname: $(hostname)
Kernel: $(uname -r)
JetPack: $(dpkg -l 2>/dev/null | grep nvidia-jetpack | awk '{print $3}')
Restore: gunzip -c $NAME.img.gz | sudo dd of=/dev/mmcblk0 bs=4M status=progress && sudo sync && reboot
INFO
"${NAS_SSH[@]}" "cd $DEST && ls -t jetson-orin-bootable-*.img.gz 2>/dev/null | tail -n +$((MAX+1)) | xargs -r rm -f"
"${NAS_SSH[@]}" "cd $DEST && ls -t jetson-orin-bootable-*.info 2>/dev/null | tail -n +$((MAX+1)) | xargs -r rm -f"
SZ=$("${NAS_SSH[@]}" "du -h $DEST/$NAME.img.gz | cut -f1")
echo "===== $(date) done, compressed size=$SZ ====="
OUTER
chmod 755 /usr/local/sbin/jetson-bootable-backup

echo "=== install monthly cron (1st of month, 02:00) ==="
cat > /etc/cron.d/jetson-bootable-monthly <<'EOF'
# Monthly bootable full-disk image of the eMMC -> NAS. Runs as root.
0 2 1 * * root /usr/local/sbin/jetson-bootable-backup
EOF
chmod 644 /etc/cron.d/jetson-bootable-monthly

echo
echo "Installed. Monthly image runs 1st of each month at 02:00."
echo "To run one NOW (30-60 min): sudo /usr/local/sbin/jetson-bootable-backup &"
echo "  watch progress: tail -f /var/log/jetson-bootable-backup.log"
