# Jetson storage & backup

How storage is tiered on this Jetson Orin AGX, and the 4-tier backup system that
protects it. Set up 2026-06-27 (see `HANDOVER.md` for the original task that started it).

Operational scripts live in [`scripts/storage/`](../scripts/storage) and
[`scripts/backup/`](../scripts/backup). Most need `sudo` (root crons / `/usr/local/sbin`).

## Storage tiers

| Tier | Device | Mount | Use | Notes |
|------|--------|-------|-----|-------|
| eMMC | `/dev/mmcblk0p1` (57 G) | `/` | OS, code, dev tools | Keep < 80%. Fast random *when not full*. |
| SD | `/dev/mmcblk1p1` (234 G) | `/mnt/sdcard` | Docker root, active DBs, ComfyUI models, hot random-I/O | ~2350 read IOPS / 0.4 ms; slow seq write (16 MB/s). |
| HDD | `/dev/sda1` (1.8 T ext4, label `transcend`) | `/mnt/transcend` | Model weights, video output, datasets, local backups | 107 MB/s write, **123 MB/s read**; terrible random (94 IOPS). USB3 Transcend StoreJet enclosure w/ WD Blue WD20SPZX. Auto-mounts via fstab UUID + `nofail`. |
| NAS | Synology `jetson@192.168.1.100` | `nas:/volume1/jetson-backup` | Off-box backups + cold archive | 2.8 T free, encrypted volume. **No SFTP/rsync for non-admin — use ssh+tar/cat.** |

**Rule of thumb:** big sequential files (video, model weights) → HDD; small/random/DBs → SD; OS → eMMC; off-box copies → NAS.
Benchmark any drive with [`scripts/storage/hdd-bench.sh`](../scripts/storage/hdd-bench.sh).

### HDD setup
[`scripts/storage/format-transcend-hdd.sh`](../scripts/storage/format-transcend-hdd.sh) — guarded ext4 format + mount + fstab
(aborts unless target is a ~2 TB USB disk that isn't root).

### ai-toolkit FLUX weights
64 GB of FLUX weights (FLUX.1-schnell, OpenFLUX.1) were trapped in the `ai-toolkit` container's
writable layer (HF cache `/data` was never bind-mounted). [`scripts/storage/docker-reclaim.sh`](../scripts/storage/docker-reclaim.sh)
copied them to `/mnt/transcend/ai-toolkit/data`, committed the env to image `ai-toolkit:env`, and freed ~63 GB on the SD.
Relaunch training (seldom) with `bash /mnt/sdcard/ai-toolkit/run-ai-toolkit.sh` — **use `--runtime nvidia`, not `--gpus all`**.

## Backup tiers

| Tier | What | Where | Schedule | Script |
|------|------|-------|----------|--------|
| 0 | Hermes SQLite snapshots | HDD `/mnt/transcend/hermes-db-backups` | every 15 min | `hermes-agent/monitoring/hermes-db-guard.sh` → `/usr/local/sbin/hermes-db-guard` |
| 1 | Weekly encrypted file backup | local restic repo `/mnt/transcend/restic-repo` + ssh+tar mirror to `nas:/volume1/jetson-backup/restic-repo` | Sun 03:30 | `scripts/backup/tier1-restic-setup.sh` + `tier1-fix-mirror.sh` |
| 2 | Docker images cold archive | `nas:/volume1/jetson-backup/docker-dataroot/` (data-root tar, ~9 GB gz) | one-time / on rebuild | `scripts/backup/tier2c-docker-dataroot.sh` |
| 3 | Bootable full-eMMC image | `nas:/volume1/jetson-backup/jetson-orin-bootable-*.img.gz` (keep 3) | 1st of month 02:00 | `scripts/backup/tier3-install-bootable-cron.sh` |

Design principle: **categorize by recreatability.** Back up irreplaceable data (configs, DBs, vault, LoRAs/outputs)
everywhere; exclude re-downloadable bulk (model weights, docker blobs, caches) from Tier 1; cold-archive the
hard-to-rebuild custom docker images once (Tier 2).

### Tier 1 — restic (encrypted, off-box)
Repo is **local on the HDD** (fast) and mirrored to the NAS. Password at `/etc/restic/password`
(**also in David's password manager — losing it = unrecoverable**). Includes `/etc`, `/usr/local/sbin`,
Hermes DBs, the DB snapshots, `/home/dbexpertai`, LoRAs/outputs, and a reinstall manifest; excludes
caches/venvs/node_modules/model-weights (`/etc/restic/excludes.txt`).

- Manual run: `sudo /usr/local/sbin/jetson-nas-backup`
- List: `sudo RESTIC_PASSWORD_FILE=/etc/restic/password restic -r /mnt/transcend/restic-repo snapshots`
- Restore: `... restic -r /mnt/transcend/restic-repo restore latest --target /tmp/restore`
- Jetson dead: ssh+tar the repo back from the NAS first, then restore.

### Tier 2 — docker images (workaround)
**`docker save` is broken on this box** — it deadlocks (client `Sl`, 0 bytes, never streams) on the large
weight-laden image layers, regardless of containers running/stopped, local vs network, or a daemon restart;
tiny images (alpine) save fine. Workaround: archive the whole docker **data-root** instead
(`tier2c-docker-dataroot.sh` stops docker, tars `/mnt/sdcard/docker`, restarts).
⚠️ The script `docker stop`s the containers, and `--restart unless-stopped` does **not** auto-restart a
*manually* stopped container after a daemon bounce — `docker start comfyui ai-toolkit` afterward.

### Tier 3 — bootable image
Full `dd` of `/dev/mmcblk0` (all boot partitions) → gzip → NAS. Restore per the `.info` file alongside each
image (minimal JetPack flash to seed the module bootloader, then `dd` the image back). Note this is **eMMC only**
— docker/models on the SD card are *not* in it (that's why Tier 2 exists).

## Synology gotchas (cost hours — don't relearn)
- **restic SFTP backend does NOT work** (DSM blocks SFTP writes / `rsync --server` for non-admin `jetson`).
  `ssh+tar` / `ssh+cat` work — the Tier 1 mirror uses incremental ssh+tar (restic data files are immutable +
  hash-named, so name-based add/delete sync is correct).
- Root→NAS SSH must pin the key: `ssh -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes` (the dbexpertai account
  offers multiple keys; scripted contexts need the explicit single key).
- NAS user `jetson` can't `mkdir` at `/volume1/` root — use the existing `/volume1/jetson-backup/` share.
- The NAS volume is encrypted; its recovery key is in David's password manager (regenerated 2026-06-27).
