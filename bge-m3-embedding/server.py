#!/usr/bin/env python3
"""
bge-m3 embedding service (OpenAI-compatible + rich hybrid endpoint).

Backed by BAAI/bge-m3 via FlagEmbedding's BGEM3FlagModel, exposing:
  - dense vectors           (1024-d)
  - sparse / lexical_weights (token_id -> weight)
  - ColBERT vectors          (per-token contextual vectors)

Self-contained project living under /mnt/sdcard/jetsonlocalai with its own
SD-card venv. Weights load from the local HF cache (HF_HOME) with no download.

Endpoints:
  GET  /                 -> service banner
  GET  /health          -> {"status": "ok", "model_loaded": bool}
  POST /v1/embeddings   -> OpenAI-compatible, dense only
  POST /embed           -> rich: dense + sparse + colbert (ytengine uses this)
"""
import os
import logging
from typing import List, Optional, Union, Dict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bge-m3")

# ---------------------------------------------------------------------------
# Environment / config (resolved before heavy imports so logs are clear)
# ---------------------------------------------------------------------------
MODEL_NAME = os.environ.get("BGE_MODEL", "BAAI/bge-m3")
HOST = os.environ.get("BGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BGE_PORT", "11435"))
BATCH_SIZE = int(os.environ.get("BGE_BATCH_SIZE", "12"))
MAX_LENGTH = int(os.environ.get("BGE_MAX_LENGTH", "8192"))
# use_fp16 is a GPU optimisation. On this Jetson the proven torch is the CPU
# aarch64 wheel (cuda build None), so default-on but auto-disable without CUDA
# to avoid the slow / partially-unsupported CPU-fp16 path. Override with
# BGE_FP16=1/0 to force.
_FP16_ENV = os.environ.get("BGE_FP16")

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from FlagEmbedding import BGEM3FlagModel

CUDA = torch.cuda.is_available()
if _FP16_ENV is not None:
    USE_FP16 = _FP16_ENV.strip() not in ("0", "false", "False", "")
else:
    USE_FP16 = CUDA  # fp16 only pays off on GPU; CPU stays fp32

# ---------------------------------------------------------------------------
# Model singleton (loaded once at startup)
# ---------------------------------------------------------------------------
MODEL: Optional[BGEM3FlagModel] = None


def load_model() -> BGEM3FlagModel:
    log.info(
        "Loading %s (use_fp16=%s, cuda=%s, HF_HOME=%s, offline=%s)",
        MODEL_NAME, USE_FP16, CUDA,
        os.environ.get("HF_HOME"), os.environ.get("HF_HUB_OFFLINE"),
    )
    m = BGEM3FlagModel(MODEL_NAME, use_fp16=USE_FP16)
    log.info("Model loaded.")
    return m


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------
class OpenAIEmbeddingRequest(BaseModel):
    input: Union[str, List[str]]
    model: Optional[str] = MODEL_NAME
    # accepted for OpenAI client compatibility; ignored
    encoding_format: Optional[str] = None
    user: Optional[str] = None
    dimensions: Optional[int] = None


class EmbedRequest(BaseModel):
    input: Union[str, List[str]]
    return_dense: bool = True
    return_sparse: bool = True
    return_colbert: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _as_list(x: Union[str, List[str]]) -> List[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, list) and all(isinstance(i, str) for i in x):
        return x
    raise HTTPException(status_code=400, detail="`input` must be a string or list of strings")


def _to_floats(arr) -> List[float]:
    if isinstance(arr, np.ndarray):
        return arr.astype(float).tolist()
    if torch.is_tensor(arr):
        return arr.float().cpu().tolist()
    return [float(v) for v in arr]


def _sparse_to_dict(weights) -> Dict[str, float]:
    # FlagEmbedding returns a dict (token_id_str -> weight); values may be
    # numpy floats. Normalise keys to str and values to python float.
    out: Dict[str, float] = {}
    for k, v in dict(weights).items():
        out[str(k)] = float(v)
    return out


def _colbert_to_floats(vecs) -> List[List[float]]:
    if isinstance(vecs, np.ndarray):
        return vecs.astype(float).tolist()
    if torch.is_tensor(vecs):
        return vecs.float().cpu().tolist()
    return [_to_floats(v) for v in vecs]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="bge-m3 embedding service", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    global MODEL
    MODEL = load_model()


@app.get("/")
def root():
    return {
        "service": "bge-m3 embedding service",
        "model": MODEL_NAME,
        "endpoints": ["/v1/embeddings", "/embed", "/health"],
        "cuda": CUDA,
        "use_fp16": USE_FP16,
    }


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": MODEL is not None}


@app.post("/v1/embeddings")
def openai_embeddings(req: OpenAIEmbeddingRequest):
    if MODEL is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    texts = _as_list(req.input)
    out = MODEL.encode(
        texts,
        batch_size=BATCH_SIZE,
        max_length=MAX_LENGTH,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )
    dense = out["dense_vecs"]
    data = [
        {"object": "embedding", "index": i, "embedding": _to_floats(dense[i])}
        for i in range(len(texts))
    ]
    total = sum(len(t.split()) for t in texts)
    return {
        "object": "list",
        "data": data,
        "model": req.model or MODEL_NAME,
        "usage": {"prompt_tokens": total, "total_tokens": total},
    }


@app.post("/embed")
def embed(req: EmbedRequest):
    if MODEL is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    texts = _as_list(req.input)
    out = MODEL.encode(
        texts,
        batch_size=BATCH_SIZE,
        max_length=MAX_LENGTH,
        return_dense=req.return_dense,
        return_sparse=req.return_sparse,
        return_colbert_vecs=req.return_colbert,
    )

    results = []
    dense = out.get("dense_vecs") if req.return_dense else None
    sparse = out.get("lexical_weights") if req.return_sparse else None
    colbert = out.get("colbert_vecs") if req.return_colbert else None

    for i in range(len(texts)):
        item: Dict[str, object] = {"index": i}
        if req.return_dense and dense is not None:
            item["dense"] = _to_floats(dense[i])
        if req.return_sparse and sparse is not None:
            item["sparse"] = _sparse_to_dict(sparse[i])
        if req.return_colbert and colbert is not None:
            item["colbert"] = _colbert_to_floats(colbert[i])
        results.append(item)

    return {"model": MODEL_NAME, "data": results}


if __name__ == "__main__":
    import uvicorn

    log.info("Starting uvicorn on %s:%s", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
