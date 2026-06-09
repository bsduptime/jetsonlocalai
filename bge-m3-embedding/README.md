# jetsonlocalai — bge-m3 embedding service

Persistent, OpenAI-compatible HTTP embedding service backed by **BAAI/bge-m3**,
exposing **dense + sparse (lexical) + ColBERT** vectors. Reachable over Tailscale
(e.g. the `ytengine` channel-optimizer on the Mac calls it).

## Layout (fully self-contained on the SD card)

```
/mnt/sdcard/jetsonlocalai/
  venv/                 # dedicated python3.10 venv — owns its deps, nothing external can break it
  server.py             # FastAPI app
  requirements.txt      # pinned to the proven aarch64 reference env
  bge-m3-embedding.service   # copy of the installed systemd unit
```

Weights load from the local HF cache — **no download**:
`HF_HOME=/mnt/sdcard/.cache/huggingface`, `HF_HUB_OFFLINE=1`.

## Endpoints (port 11435, bound 0.0.0.0)

- `POST /v1/embeddings` — OpenAI-compatible, **dense only**
  `{input: str|list[str], model}` → `{data:[{embedding,index}], model, usage}`
- `POST /embed` — rich hybrid endpoint
  `{input, return_dense, return_sparse, return_colbert}` →
  per input `{dense:[...], sparse:{token_id: weight}, colbert:[[...]]}`
- `GET /health` — `{status, model_loaded}`

## Service

```bash
sudo systemctl status bge-m3-embedding
sudo systemctl restart bge-m3-embedding
journalctl -u bge-m3-embedding -f
```

The model is loaded **once** at startup. CUDA is unavailable with the CPU
aarch64 torch wheel, so `use_fp16` auto-disables on CPU (override with
`BGE_FP16=1`). Tunables via env: `BGE_BATCH_SIZE`, `BGE_MAX_LENGTH`, `BGE_PORT`.

## Quick check

```bash
curl localhost:11435/v1/embeddings -H 'content-type: application/json' \
  -d '{"input":"hello","model":"bge-m3"}'

curl localhost:11435/embed -H 'content-type: application/json' \
  -d '{"input":["hello world"],"return_dense":true,"return_sparse":true,"return_colbert":true}'
```

Tailscale: `http://100.99.130.79:11435` (host `dbexpertAI`).

## Repo ↔ runtime

This directory is the version-controlled **source**. The **runtime** lives at
`/mnt/sdcard/jetsonlocalai/` (with its own `venv/` and pip caches, intentionally not
in git). To redeploy after editing source here:

```bash
cp bge-m3-embedding/{server.py,requirements.txt} /mnt/sdcard/jetsonlocalai/
sudo cp bge-m3-embedding/bge-m3-embedding.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart bge-m3-embedding
```

To rebuild the venv from scratch:
`python3.10 -m venv /mnt/sdcard/jetsonlocalai/venv && /mnt/sdcard/jetsonlocalai/venv/bin/python -m pip install -r requirements.txt`
(torch 2.2.2 is the PyPI CPU aarch64 wheel — no special index needed).
