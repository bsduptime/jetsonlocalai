#!/usr/bin/env bash
# Tier 2 (fallback): archive the docker data-root instead of `docker save`
# (docker save deadlocks on this box's large weight-laden image layers).
# Captures ALL docker images/containers as on-disk storage, fully restorable.
# Minimizes downtime: tar to HDD (fast) -> restart docker -> then transfer to NAS.
# Run: sudo bash ~/tier2c-docker-dataroot.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

SRC_PARENT=/mnt/sdcard
SRC=docker                        # /mnt/sdcard/docker
LOCAL=/mnt/transcend/docker-dataroot.tar
SSH=(ssh -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new nas)
DEST=/volume1/jetson-backup/docker-dataroot
STAMP=$(date +%Y%m%d)

echo ">>> 1/6 stop containers + docker daemon (downtime starts) ..."
docker stop comfyui ai-toolkit 2>/dev/null || true
systemctl stop docker docker.socket
echo "    docker stopped at $(date +%T)"

echo ">>> 2/6 tar the data-root -> HDD (no compression; layers don't compress) ..."
du -sh "$SRC_PARENT/$SRC" 2>/dev/null || true
tar -C "$SRC_PARENT" -cf "$LOCAL" "$SRC"
echo "    tar size: $(du -h "$LOCAL" | cut -f1)"

echo ">>> 3/6 restart docker (downtime ends) ..."
systemctl start docker
sleep 3
docker ps --format '{{.Names}}: {{.Status}}' || true
echo "    docker back up at $(date +%T)"

echo ">>> 4/6 transfer archive -> NAS (docker already running again) ..."
"${SSH[@]}" "mkdir -p $DEST"
gzip -1 -c "$LOCAL" | "${SSH[@]}" "cat > $DEST/docker-dataroot-$STAMP.tar.gz"

echo ">>> 5/6 restore note ..."
"${SSH[@]}" "cat > $DEST/RESTORE.txt" <<'TXT'
Docker data-root cold archive (used because `docker save` deadlocks on the large
weight-laden images on this Jetson). Contains all docker images/containers as storage.
Restore on a rebuilt/repaired Jetson:
  sudo systemctl stop docker docker.socket
  sudo rm -rf /mnt/sdcard/docker
  scp docker-dataroot-DATE.tar.gz the Jetson, then:
    gunzip -c docker-dataroot-DATE.tar.gz | sudo tar -C /mnt/sdcard -xf -
  sudo systemctl start docker
  docker images   # comfyui:latest + ai-toolkit:env should be present
TXT

echo ">>> 6/6 verify + clean up local temp ..."
"${SSH[@]}" "ls -lh $DEST"
rm -f "$LOCAL"
echo "done. Downtime was only steps 1-3 (tar duration)."
