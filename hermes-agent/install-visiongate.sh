#!/usr/bin/env bash
# visiongate — deploy the local vision guardrail on the expense-upload path.
#
#   sudo bash hermes-agent/install-visiongate.sh
#
# The plugin CODE needs no install step: greeninvoice is already symlinked into
# Hermes' plugin dir, so visiongate.py/hooks.py are visible the moment hermes restarts.
# What this script does is fix the ENVIRONMENT the gate depends on:
#
#   1. Point ollama.service at the model store's real location. The models were moved
#      SD -> HDD; the unit still says /mnt/sdcard/ollama, which no longer exists, so on
#      the next boot ollama would come up with an empty store and re-download ~15 GB.
#   2. Cap the default context window. Ollama sizes the KV cache to a model's FULL
#      native context by default: for qwen3-vl:4b that allocated ~49 GB for a 5 GB model
#      and hard-froze this box. A per-request `num_ctx` overrides this, so the cap is a
#      floor for safety, not a ceiling on capability.
#   3. Enable ollama.service so the gate survives a reboot (it is currently a
#      hand-started `nohup ollama serve`, which does not).
#   4. Restart hermes so the hooks register.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

MODELS=/mnt/transcend/ollama
MODEL_NAME=qwen3-vl:4b-instruct-q8_0

[[ -d "$MODELS" ]] || { echo "model store missing: $MODELS" >&2; exit 1; }

echo "==> ollama.service -> $MODELS"
install -d /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment="OLLAMA_MODELS=$MODELS"
Environment="OLLAMA_HOST=127.0.0.1:11434"
# Never allocate a model's full native context: qwen3-vl:4b defaults to ~49 GB of KV
# cache for a 5 GB model and has frozen this box. Callers that need more pass num_ctx.
Environment="OLLAMA_CONTEXT_LENGTH=4096"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
EOF

echo "==> reconciling the stale OLLAMA_MODELS=/sdcard/ollama (missing /mnt)"
sed -i 's#^OLLAMA_MODELS=.*#OLLAMA_MODELS="'"$MODELS"'"#' /etc/environment || true

echo "==> stopping any hand-started 'ollama serve'"
pkill -x ollama 2>/dev/null || true
sleep 2

systemctl daemon-reload
systemctl enable --now ollama.service
sleep 5

echo "==> verifying the model is servable"
curl -fsS --max-time 5 http://127.0.0.1:11434/api/version >/dev/null \
  || { echo "ollama did not come up" >&2; exit 1; }
curl -fsS http://127.0.0.1:11434/api/tags | grep -q "$MODEL_NAME" \
  || { echo "model $MODEL_NAME not in the store at $MODELS" >&2; exit 1; }

echo "==> restarting hermes so visiongate's hooks register"
systemctl restart hermes
sleep 3
systemctl is-active --quiet hermes || { echo "hermes did not come back" >&2; exit 1; }

cat <<'EOF'

visiongate is live.

  Verify:   journalctl -u hermes -n 30 --no-pager | grep visiongate
            (expect: "visiongate: hooks registered (model=qwen3-vl:4b-instruct-q8_0)")

  Behaviour:
    - Drop an image in Telegram -> Elena's message is annotated with a local,
      untrusted-labelled classification (kind / tax_document / confidence / language).
    - Ask her to file a real receipt as an expense -> it uploads silently.
    - Ask her to file anything that is NOT an invoice/receipt -> Hermes asks YOU for
      approval in Telegram first. There is no way for the model to skip that prompt.

  Escape hatch (operator only, NOT reachable by the model):
    GI_VISIONGATE=0 in hermes' environment disables the gate entirely.

  NOTE: the first classification after a restart cold-loads the model (~90s). It runs
  in the background and never stalls the gateway; the upload gate simply asks you for
  approval until the model is warm.
EOF
