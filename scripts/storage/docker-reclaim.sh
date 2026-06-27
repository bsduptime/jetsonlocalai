#!/usr/bin/env bash
# Reclaim ~64 GB on the SD card by moving the ai-toolkit container's trapped FLUX
# weights (/data, already copied + byte-verified to the HDD) out of the writable layer,
# while PRESERVING the ~5 GB pip training env via docker commit.
#
# Safe ordering: delete /data from the layer FIRST (shrinks it), THEN commit (small image),
# THEN rm the old container. The weights already live on the HDD and the new container
# binds them back at /data, so all env paths (HF/torch caches) stay valid.
set -euo pipefail

C=ai-toolkit
HDD_DATA=/mnt/transcend/ai-toolkit/data
RECIPE=/mnt/sdcard/ai-toolkit/run-ai-toolkit.sh

echo ">>> Pre-flight: confirm HDD copy is present and non-empty"
[ -d "$HDD_DATA/models/huggingface" ] || { echo "FATAL: $HDD_DATA missing — aborting"; exit 1; }
echo "    HDD /data size: $(du -sh "$HDD_DATA" | cut -f1)"

echo ">>> 1/5 Deleting /data from the container layer (weights are safe on HDD) ..."
docker start "$C" >/dev/null
docker exec "$C" rm -rf /data
echo "    container /data now: $(docker exec "$C" sh -c 'du -sh /data 2>/dev/null || echo gone')"

echo ">>> 2/5 Committing the training env (~5 GB) to image ai-toolkit:env ..."
docker stop "$C" >/dev/null
docker commit "$C" ai-toolkit:env >/dev/null
echo "    committed: $(docker images ai-toolkit:env --format '{{.Repository}}:{{.Tag}} {{.Size}}')"

echo ">>> 3/5 Removing the old bloated container ..."
docker rm "$C" >/dev/null
echo "    removed."

echo ">>> 4/5 Writing relaunch recipe to $RECIPE ..."
cat > "$RECIPE" <<'EOF'
#!/usr/bin/env bash
# Relaunch the ai-toolkit training container (seldom-used). FLUX weights live on the HDD.
# Usage: bash run-ai-toolkit.sh   then:  docker exec -it ai-toolkit bash
set -e
docker rm -f ai-toolkit 2>/dev/null || true
docker run -d --name ai-toolkit --runtime nvidia --restart unless-stopped \
  -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all \
  --entrypoint tail \
  -v /mnt/transcend/ai-toolkit/data:/data \
  -v /mnt/sdcard/ai-toolkit:/ai-toolkit \
  -v /mnt/sdcard/comfyui-models:/comfyui-models \
  -v /mnt/sdcard/huggingface_cache:/hf_cache \
  -v /mnt/sdcard/lora-training:/lora-training \
  ai-toolkit:env -f /dev/null
echo "ai-toolkit up. Weights on HDD at /mnt/transcend/ai-toolkit/data -> /data"
EOF
chmod +x "$RECIPE"
echo "    wrote $RECIPE"

echo ">>> 5/5 Result"
echo "--- SD card now ---"; df -h /mnt/sdcard | tail -1
echo "--- docker disk ---"; docker system df
echo ">>> Done. Relaunch training any time with: bash $RECIPE"
