#!/usr/bin/env bash
# Make Elena see inbound images with her OWN model (gpt-5.4), natively.
#
#   sudo bash hermes-agent/hermes-vision-native.sh          # set native
#   sudo bash hermes-agent/hermes-vision-native.sh --undo   # back to auto
#
# WHY (diagnosed 2026-07-15 from the live config + journal):
#   Elena's main model is gpt-5.4 (vision-capable), but the `auxiliary.vision` task is
#   provider: auto / model: '' with providers: {} — unconfigured. Hermes' image router
#   (agent/image_routing.py) in the default `auto` mode only attaches images NATIVELY when
#   the main model is *recognized* as vision-capable; gpt-5.4 isn't in its metadata, so it
#   falls back to the text `vision_analyze` path — which then crashes with
#   "No LLM provider configured for task=vision". Net effect: Elena can't see any image.
#
#   Setting agent.image_input_mode: native forces the router to attach the pixels to
#   gpt-5.4 directly (image_routing.py:436  if mode_cfg == "native": return "native"),
#   bypassing the capability lookup. OpenAI's adapter handles image_url parts, so gpt-5.4
#   sees the receipt itself — no auxiliary provider, no tesseract, no OCR skill, no new
#   data egress beyond the OpenAI you already use.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

CFG=/home/hermes/.hermes/config.yaml
[[ -f "$CFG" ]] || { echo "no config at $CFG" >&2; exit 1; }
MODE="native"; [[ "${1:-}" == "--undo" ]] && MODE="auto"

cp -a "$CFG" "$CFG.bak.$(date +%s)"

python3 - "$CFG" "$MODE" <<'PY'
import sys, yaml
path, mode = sys.argv[1], sys.argv[2]
cfg = yaml.safe_load(open(path)) or {}
agent = cfg.setdefault("agent", {})
if not isinstance(agent, dict):
    raise SystemExit("config 'agent:' is not a mapping — aborting, edit by hand")
agent["image_input_mode"] = mode
yaml.safe_dump(cfg, open(path, "w"), sort_keys=False, allow_unicode=True)
print(f"   agent.image_input_mode = {mode}")
PY
chown hermes:hermes "$CFG"

systemctl restart hermes
sleep 4
systemctl is-active --quiet hermes || { echo "hermes did NOT come back — restore from $CFG.bak.*" >&2; exit 1; }

cat <<EOF

agent.image_input_mode = $MODE. hermes restarted.

Now send Elena a receipt. She should describe it directly (gpt-5.4 sees the pixels) with
NO vision_analyze call and NO browser/code improvising. If she still can't see it, gpt-5.4
via your OpenAI setup may not accept image parts — tell me and we'll look at the adapter.

Watch:  journalctl -u hermes -f | grep --line-buffered -iE 'vision|GI_EXPENSE_CONFIRM'
  - GOOD: no "No LLM provider configured for task=vision" errors.
  - Then ask her to file it -> the confirm-the-numbers prompt fires.
EOF
