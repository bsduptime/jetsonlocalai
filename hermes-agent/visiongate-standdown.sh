#!/usr/bin/env bash
# Stand visiongate down and give Elena her eyes back.
#
#   sudo bash hermes-agent/visiongate-standdown.sh
#
# DECISION (David, 2026-07-15): Elena already reads receipts better than the local
# qwen3-vl classifier we built — she uses tesseract (the `ocr-and-documents` skill) plus
# her own reasoning, and got merchant / tax ID / VAT / total right every time. visiongate
# was solving a problem she doesn't have, and disabling her OCR skill to run a weaker
# replacement actively blinded her. So: let her do it her way.
#
# This script:
#   1. RE-ENABLES ocr-and-documents (undoes the disable that broke her vision).
#   2. DISABLES visiongate (GI_VISIONGATE=0) — the upload gate stops intercepting; the
#      code stays in place, parked, not deleted (see the banner in visiongate.py).
#
# What safety remains WITHOUT visiongate (and why that's fine):
#   - The broker still creates every expense as OPEN (status 10), NEVER auto-reported to
#     tax. A mis-filed image is a reviewable Open draft, not a tax record.
#   - gi_close_expense (report to tax, irreversible) is still confirm-gated + rate-limited.
#   - Elena reads the image herself and decides — which was the original ask.
# The residual risk visiongate closed (a confused upload of a non-invoice) now costs one
# Open draft that gets caught in the monthly review. That is proportionate.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

CFG=/home/hermes/.hermes/config.yaml
[[ -f "$CFG" ]] || { echo "no config at $CFG" >&2; exit 1; }
cp -a "$CFG" "$CFG.bak.$(date +%s)"

echo "==> re-enabling ocr-and-documents (restoring Elena's tesseract vision)"
python3 - "$CFG" <<'PY'
import sys, yaml
p = sys.argv[1]
cfg = yaml.safe_load(open(p)) or {}
sk = cfg.setdefault("skills", {})
sk["disabled"] = sorted(s for s in (sk.get("disabled") or []) if s != "ocr-and-documents")
yaml.safe_dump(cfg, open(p, "w"), sort_keys=False, allow_unicode=True)
print("   skills.disabled =", sk["disabled"])
PY
chown hermes:hermes "$CFG"

echo "==> disabling visiongate (GI_VISIONGATE=0; code parked, not removed)"
install -d /etc/systemd/system/hermes.service.d
cat > /etc/systemd/system/hermes.service.d/visiongate.conf <<'EOF'
[Service]
# visiongate stood down 2026-07-15 — Elena reads receipts better with tesseract.
# Delete this drop-in (and daemon-reload + restart) to bring the gate back.
Environment="GI_VISIONGATE=0"
EOF
# Clear the observe-mode drop-in if present — it's meaningless with the gate off.
rm -f /etc/systemd/system/hermes.service.d/visiongate-observe.conf 2>/dev/null || true

systemctl daemon-reload
systemctl restart hermes
sleep 4
systemctl is-active --quiet hermes || { echo "hermes did not come back — restore from $CFG.bak.*" >&2; exit 1; }

echo
echo "Done. Elena has tesseract back and visiongate is off."
echo "Send her a receipt and let her handle it her way."
echo
echo "To bring visiongate back later:"
echo "  sudo rm /etc/systemd/system/hermes.service.d/visiongate.conf && sudo systemctl daemon-reload && sudo systemctl restart hermes"
