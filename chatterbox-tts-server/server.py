#!/usr/bin/env python3
"""Chatterbox TTS HTTP server (OpenAI-compatible /v1/audio/speech).

Runs on the Jetson Orin AGX via the existing videngine venv
(/mnt/sdcard/videngine/venv) which already has torch+CUDA+chatterbox+fastapi.

Voices: place reference WAV files in $VOICES_DIR (one per voice). The
voice name is the filename stem. The 'default' voice uses Chatterbox's
built-in voice with no reference file.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import soundfile as sf
import torch
from chatterbox.tts import ChatterboxTTS
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("chatterbox-server")

VOICES_DIR = Path(os.environ.get("VOICES_DIR", "./voices"))
AUTH_TOKEN = os.environ.get("CHATTERBOX_API_TOKEN")  # optional bearer

app = FastAPI(title="chatterbox-tts-server", version="0.1.0")

log.info("Loading Chatterbox model (first request will warm CUDA)...")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MODEL = ChatterboxTTS.from_pretrained(device=DEVICE)
log.info("Loaded on %s (sr=%d)", DEVICE, MODEL.sr)


def discover_voices() -> dict[str, str | None]:
    voices: dict[str, str | None] = {"default": None}
    if VOICES_DIR.exists():
        for p in sorted(VOICES_DIR.glob("*.wav")):
            voices[p.stem] = str(p)
    return voices


VOICES = discover_voices()
log.info("Voices available: %s", list(VOICES.keys()))


def check_auth(authorization: str | None) -> None:
    if not AUTH_TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if authorization[len("Bearer "):].strip() != AUTH_TOKEN:
        raise HTTPException(401, "invalid token")


class SpeechRequest(BaseModel):
    # OpenAI-compatible. `model` is accepted but ignored (we always use Chatterbox).
    model: str = "chatterbox"
    input: str
    voice: str = "default"
    response_format: str = "wav"  # only wav supported currently
    speed: float = 1.0  # ignored — Chatterbox doesn't expose speed control


@app.get("/health")
def health() -> dict:
    return {"ok": True, "device": DEVICE, "voices": list(VOICES.keys())}


@app.get("/v1/voices")
def list_voices() -> dict:
    return {"voices": list(VOICES.keys())}


@app.post("/v1/audio/speech")
def speak(
    req: SpeechRequest,
    authorization: str | None = Header(default=None),
) -> Response:
    check_auth(authorization)
    text = req.input.strip()
    if not text:
        raise HTTPException(400, "empty input")
    if req.voice not in VOICES:
        raise HTTPException(
            400,
            f"unknown voice {req.voice!r}; available: {list(VOICES.keys())}",
        )
    if req.response_format != "wav":
        raise HTTPException(
            400, "only response_format=wav is supported currently"
        )

    ref = VOICES[req.voice]
    log.info("speak voice=%s chars=%d", req.voice, len(text))
    kwargs = {"audio_prompt_path": ref} if ref else {}
    wav = MODEL.generate(text, **kwargs)
    wav_np = wav.squeeze().cpu().numpy()

    buf = io.BytesIO()
    sf.write(buf, wav_np, MODEL.sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return Response(content=buf.read(), media_type="audio/wav")
