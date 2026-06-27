#!/usr/bin/env bash
# Tier 0: relocate hermes-db-guard SQLite snapshots from the SD card to the HDD.
# Run: sudo bash ~/tier0-relocate-db-backups.sh
set -euo pipefail

SRC=/mnt/sdcard/hermes-db-backups
DST=/mnt/transcend/hermes-db-backups
REPO_SRC=/home/dbexpertai/code/jetsonlocalai/hermes-agent/monitoring/hermes-db-guard.sh
INSTALLED=/usr/local/sbin/hermes-db-guard

echo ">>> 1/5 Install updated guard script (BACKUP_ROOT -> HDD) ..."
install -m 755 -o root -g root "$REPO_SRC" "$INSTALLED"
grep -n "^BACKUP_ROOT=" "$INSTALLED"

echo ">>> 2/5 Create HDD backup root (root:dbexpertai 750) ..."
install -d -o root -g dbexpertai -m 750 "$DST"

echo ">>> 3/5 Migrate existing snapshots SD -> HDD (preserve history) ..."
if [ -d "$SRC" ] && [ -n "$(ls -A "$SRC" 2>/dev/null)" ]; then
  cp -a "$SRC"/. "$DST"/
  chown -R root:dbexpertai "$DST"
  echo "    migrated: $(du -sh "$DST" | cut -f1) now on HDD"
else
  echo "    nothing to migrate"
fi

echo ">>> 4/5 Run one guard tick now; confirm it writes to the HDD ..."
"$INSTALLED"
LATEST=$(ls -1t "$DST"/state.db/hourly/ 2>/dev/null | head -1)
echo "    newest snapshot on HDD: $DST/state.db/hourly/$LATEST"
[ -n "$LATEST" ] || { echo "FATAL: no snapshot written to HDD — leaving SD copy intact"; exit 1; }

echo ">>> 5/5 Remove the old SD backup dir (history already copied to HDD) ..."
rm -rf "$SRC"
echo "    removed $SRC"

echo
echo "=== Done. db-guard now backs up to the HDD. ==="
echo "--- SD freed ---"; df -h /mnt/sdcard | tail -1
echo "--- HDD backup footprint ---"; du -sh "$DST"
echo "--- next cron tick (every :00/:15/:30/:45) will write here automatically ---"
