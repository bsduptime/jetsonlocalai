#!/usr/bin/env bash
# Format the external 2TB USB HDD to ext4 and auto-mount at /mnt/transcend.
# Per HANDOVER.md (2026-06-26). Run as: sudo bash scripts/format-transcend-hdd.sh
#
# NOTE: The handover predicted a "Transcend" drive, but the connected disk is a
# WDC WD20SPZX (WD Blue 2TB). It is the only ~1.8T external USB disk present and
# was exFAT-formatted on the Mac, matching the handover's described state. The
# mount label/dir name "transcend" is kept for continuity with the handover.
set -euo pipefail

DEV=/dev/sda
PART=/dev/sda1
MNT=/mnt/transcend
LABEL=transcend

echo ">>> Safety checks on $DEV ..."

# 1. Must exist and be a disk
[[ -b "$DEV" ]] || { echo "FATAL: $DEV is not a block device"; exit 1; }

# 2. Must NOT be the root disk
ROOT_DISK=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")
if [[ "$DEV" == *"$ROOT_DISK"* ]]; then
  echo "FATAL: $DEV is the ROOT disk ($ROOT_DISK). Aborting."; exit 1
fi

# 3. Must be roughly 2TB (between 1.5 and 2.2 TiB), guards against wrong target
BYTES=$(blockdev --getsize64 "$DEV")
TIB=$(awk "BEGIN{printf \"%.2f\", $BYTES/1099511627776}")
echo "    $DEV size = ${TIB} TiB"
awk "BEGIN{exit !($BYTES > 1.5*1099511627776 && $BYTES < 2.2*1099511627776)}" \
  || { echo "FATAL: $DEV size ${TIB} TiB outside expected ~2TB range. Aborting."; exit 1; }

# 4. Must be a USB-attached disk (transport)
TRAN=$(lsblk -dno TRAN "$DEV")
echo "    $DEV transport = ${TRAN:-unknown}"
[[ "$TRAN" == "usb" ]] || { echo "FATAL: $DEV is not USB-attached ($TRAN). Aborting."; exit 1; }

# 5. Nothing from this disk currently mounted
if mount | grep -q "^$DEV"; then
  echo "    Unmounting existing mounts on $DEV ..."
  umount "${DEV}"* 2>/dev/null || true
fi

echo ">>> All checks passed. Formatting $DEV -> ext4 in 5s (Ctrl-C to abort) ..."
sleep 5

# 6. Fresh GPT + one full-disk ext4 partition
wipefs -a "$DEV"
parted -s "$DEV" mklabel gpt mkpart primary ext4 0% 100%
# settle so $PART node appears
udevadm settle 2>/dev/null || sleep 2
mkfs.ext4 -F -L "$LABEL" "$PART"

# 7. Mount + ownership
mkdir -p "$MNT"
mount "$PART" "$MNT"
# Own it for the login user (the one who invoked sudo)
OWNER="${SUDO_USER:-$USER}"
chown -R "$OWNER:$OWNER" "$MNT"

# 8. Auto-mount on boot by UUID (nofail so a missing drive won't block boot)
UUID=$(blkid -s UUID -o value "$PART")
FSTAB_LINE="UUID=$UUID  $MNT  ext4  defaults,nofail,x-systemd.device-timeout=10  0  2"
if grep -q "$UUID" /etc/fstab; then
  echo "    fstab already has an entry for $UUID — skipping append"
else
  echo "$FSTAB_LINE" >> /etc/fstab
  echo "    Added to /etc/fstab: $FSTAB_LINE"
fi

# 9. Validate fstab parses and remounts cleanly
umount "$MNT"
mount -a
echo ">>> Done. Final state:"
df -h "$MNT"
echo ">>> Owner:"; stat -c '%U:%G' "$MNT"
