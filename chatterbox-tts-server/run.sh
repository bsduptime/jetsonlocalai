#!/usr/bin/env bash
# Launch the Chatterbox TTS server on the Jetson via the videngine venv.
#
# Uses /mnt/sdcard/videngine/venv (already has torch+CUDA+chatterbox+fastapi).
# Override HOST, PORT, VOICES_DIR, HF_HOME, CHATTERBOX_API_TOKEN via env.

set -euo pipefail

cd "$(dirname "$0")"

VENV=/mnt/sdcard/videngine/venv
if [ ! -x "$VENV/bin/python" ]; then
  echo "error: videngine venv not found at $VENV"
  echo "  expected: $VENV/bin/python"
  exit 1
fi

export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-18080}"
export HF_HOME="${HF_HOME:-/mnt/sdcard/.cache/huggingface}"
export VOICES_DIR="${VOICES_DIR:-$(pwd)/voices}"
mkdir -p "$VOICES_DIR"

VOICE_COUNT=$(ls "$VOICES_DIR" 2>/dev/null | grep -c '\.wav$' || true)
AUTH_DESC="none (set CHATTERBOX_API_TOKEN to require a bearer)"
if [ -n "${CHATTERBOX_API_TOKEN:-}" ]; then
  AUTH_DESC="bearer (CHATTERBOX_API_TOKEN set)"
fi

cat <<EOF
chatterbox-tts-server starting on ${HOST}:${PORT}
  venv:       $VENV
  HF cache:   $HF_HOME
  voices:     $VOICES_DIR ($VOICE_COUNT reference files + 'default')
  auth:       $AUTH_DESC

First request will warm CUDA (~5-10s). Subsequent requests are fast.
EOF

# Invoke uvicorn via the venv's python (not the bin/uvicorn shebang) — that
# way we're guaranteed to use the videngine venv's site-packages, not whatever
# Python the uvicorn shebang happens to point at on this machine.
exec "$VENV/bin/python" -m uvicorn server:app --host "$HOST" --port "$PORT"
