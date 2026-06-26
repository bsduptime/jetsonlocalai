# Handover — Jetson storage expansion (2026-06-26)

Pick-up doc for continuing the storage work **on the Jetson**. Pull this repo on the
Jetson and keep going. Written from a Mac session; the hands-on part runs on the Jetson.

## Goal
The Jetson Orin AGX 64GB dev kit keeps running out of space. Add fast, larger storage —
**immediate fix with a free 2TB USB HDD**, bigger/faster NVMe upgrade later.

## Current state (measured this session)
- **Root `/` = `/dev/mmcblk0p1` (eMMC, 57 GB) is 97% full — 1.8 GB free.** ← the actual pain.
- Also a **238 GB SD card** = `/dev/mmcblk1p1` mounted at `/mnt/sdcard` (75% full, 57 GB free).
- Write speeds: eMMC **~15 MB/s** (crippled by being 97% full), SD card **~24.8 MB/s**,
  the 2TB USB HDD **~102 MB/s**. So the USB HDD is the fastest *writable* storage available
  and ~4× the SD card for big sequential files (video). Spinning HDD = great for big files,
  bad for OS/random — use it for **data**, keep the OS on eMMC.

## In progress — FINISH THIS (on the Jetson)
A free **2TB Transcend USB HDD** (David's, fastest of his spare external drives) is being
added as **bulk video/data storage**. It was wiped to exFAT on the Mac and is being moved to
a Jetson **USB 3** port. Format it ext4, mount it, auto-mount on boot, then move big files off
the eMMC.

> ⚠️ **VERIFY THE DEVICE FIRST.** The eMMC is `mmcblk0`, the SD card is `mmcblk1`. The USB
> drive will be `/dev/sdX` (likely `/dev/sda`, ~1.8 TiB, MODEL contains "Transcend"). Confirm
> with `lsblk` before `mkfs` — formatting the wrong disk wipes your system. Commands below
> assume `/dev/sda`; change it if `lsblk` shows otherwise.

```bash
# 1. Identify the USB drive (look for ~1.8T, TRAN/Transcend, NOT mmcblk*)
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL

# 2. Unmount anything auto-mounted from it
sudo umount /dev/sda1 2>/dev/null; sudo umount /dev/sda 2>/dev/null

# 3. Fresh GPT + one full-disk partition
sudo wipefs -a /dev/sda
sudo parted -s /dev/sda mklabel gpt mkpart primary ext4 0% 100%

# 4. ext4 filesystem, labelled
sudo mkfs.ext4 -L transcend /dev/sda1

# 5. Mount + take ownership
sudo mkdir -p /mnt/transcend
sudo mount /dev/sda1 /mnt/transcend
sudo chown -R "$USER:$USER" /mnt/transcend

# 6. Auto-mount on boot by UUID (nofail so a missing drive won't block boot)
UUID=$(sudo blkid -s UUID -o value /dev/sda1)
echo "UUID=$UUID  /mnt/transcend  ext4  defaults,nofail,x-systemd.device-timeout=10  0  2" | sudo tee -a /etc/fstab
sudo mount -a            # must complete with no errors

# 7. Verify
df -h /mnt/transcend
```

## Then: free the 97%-full eMMC
```bash
# See what's eating the eMMC (biggest dirs on root, not crossing into other mounts)
sudo du -xh / 2>/dev/null | sort -h | tail -40
# Move big data/models/datasets/video onto the HDD, e.g.:
#   mv ~/big-stuff /mnt/transcend/ && ln -s /mnt/transcend/big-stuff ~/big-stuff
# Goal: get `/` comfortably below ~80% so it stops choking.
```

## SSD upgrade — decided, do later (lower priority)
The Jetson M.2 slot is **PCIe Gen4 x4, 2280, NVMe-only**. Buying rules learned this session:
- **Gen4 not Gen5** (Gen5 runs capped at Gen4 ~7,000 MB/s here — wasted money).
- **2TB not 1TB**, **TLC not QLC**, **bare drive not tall heatsink** (under-board slot clearance),
  **NVMe not M.2 SATA** (slot won't detect SATA).
- Picks: **WD SN7100 2TB** (best value — TLC, DRAM-less but efficient, full endurance) or
  **Crucial T500 2TB / WD SN850X 2TB** (best — adds DRAM cache).
- **Plan: buy a 2TB TLC drive in Germany next month (~July 2026).** A 2026 NAND shortage has
  ~2–3×'d SSD prices everywhere (2TB ≈ €290–370 now). Booting from NVMe later = re-flash JetPack
  onto the NVMe; deferred — the HDD + freeing the eMMC solves the immediate problem.

## Jetson access
- SSH: `jetson` → 192.168.1.200 (LAN), `jetson_vpn` → 100.99.130.79 (Tailscale). User `dbexpertai`,
  key `~/.ssh/jetson_key`. **Jetson sudo needs a password** (no passwordless sudo) — that's why
  the format/mount steps above are for David to run, not headless.

## Note on the Transcend wipe
It was reformatted (exFAT) on the Mac to benchmark it. David had said "all to nuke," but wanted
to peek at the contents first — the reformat beat him to it. Old data is physically still on the
platters (only a quick-format + ~2GB test write touched it) so recovery via TestDisk/PhotoRec/Disk
Drill is *possible* but not easy (needs sudo + hours + no guarantee). Decision was: don't recover,
proceed to nuke→ext4 for the Jetson.
