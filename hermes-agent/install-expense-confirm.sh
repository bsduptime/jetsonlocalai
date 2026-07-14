#!/usr/bin/env bash
# Deploy the expense-confirm gate and give Elena her eyes back. Supersedes
# visiongate-standdown.sh (which this leaves in place as history).
#
#   sudo bash hermes-agent/install-expense-confirm.sh
#
# What this sets up:
#   - Elena confirms the NUMBERS with David before gi_create_expense writes to the real
#     Morning books (Hermes human-approval prompt in Telegram). No vision model.
#   - ocr-and-documents re-enabled so Elena can read receipts (tesseract) her own way.
#   - visiongate fully out of the loop: the plugin no longer imports it (GI_VISIONGATE and
#     the observe drop-in are now irrelevant; we clear them to avoid confusion).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

CFG=/home/hermes/.hermes/config.yaml
[[ -f "$CFG" ]] || { echo "no config at $CFG" >&2; exit 1; }
cp -a "$CFG" "$CFG.bak.$(date +%s)"

echo "==> re-enabling ocr-and-documents (Elena's tesseract reading)"
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

echo "==> clearing stale visiongate drop-ins (the gate no longer uses a vision model)"
rm -f /etc/systemd/system/hermes.service.d/visiongate.conf \
      /etc/systemd/system/hermes.service.d/visiongate-observe.conf 2>/dev/null || true
# GI_EXPENSE_CONFIRM defaults ON in code; nothing to set. To DISABLE (operator only):
#   add a drop-in with Environment="GI_EXPENSE_CONFIRM=0" and restart.

systemctl daemon-reload
systemctl restart hermes
sleep 4
systemctl is-active --quiet hermes || { echo "hermes did NOT come back — restore from $CFG.bak.*" >&2; exit 1; }

cat <<'EOF'

Done. The expense-confirm gate is live and Elena has tesseract back.

TEST IT:
  1. Send Elena a receipt photo, ask her to "file this as a business expense".
  2. She reads it, uploads to Morning (OCR draft), then calls gi_create_expense.
  3. Hermes stops and asks YOU in Telegram to confirm the numbers (supplier, amount,
     VAT, date). Approve -> the expense is created OPEN (not reported to tax).
  4. Watch the gate:  journalctl -u hermes -f | grep --line-buffered GI_EXPENSE_CONFIRM

The gate announces itself on its first hook call (not at startup), so it shows up the
first time Elena tries to create an expense.

KNOWN LIMITATION (decide before adding Lihi): the confirm prompt goes to the chat. In
David's DM that's David. In a shared group, any authorized member could approve. The gate
checks the PAYLOAD, not who approves.
EOF
