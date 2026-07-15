#!/usr/bin/env bash
# Install the file-business-expense skill for Elena.
#
#   sudo bash hermes-agent/install-expense-skill.sh
#
# Symlinks the repo skill into Elena's skills dir (same pattern as the greeninvoice
# plugin) so it stays version-controlled here. Hermes surfaces it by its description when
# David sends an invoice/receipt to file. This is WORKFLOW guidance only — the safety
# (David confirms the numbers before any write) lives in the confirmgate hook, not here.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

SRC=/home/dbexpertai/code/jetsonlocalai/hermes-agent/skills/file-business-expense
DEST_DIR=/home/hermes/.hermes/skills
LINK="$DEST_DIR/file-business-expense"

[[ -f "$SRC/SKILL.md" ]] || { echo "skill source missing at $SRC" >&2; exit 1; }
install -d -o hermes -g hermes "$DEST_DIR"

if [[ -e "$LINK" && ! -L "$LINK" ]]; then
    echo "$LINK exists and is not a symlink — leaving it alone" >&2; exit 1
fi
ln -sfn "$SRC" "$LINK"
chown -h hermes:hermes "$LINK"
echo "linked $LINK -> $SRC"

systemctl restart hermes
sleep 4
systemctl is-active --quiet hermes || { echo "hermes did NOT come back" >&2; exit 1; }

cat <<'EOF'

Installed. Send Elena a receipt and ask her to file it as a business expense.
Expected flow:
  she reads it -> uploads to Morning (OCR draft) -> checks the draft + dedup ->
  creates it OPEN -> you get the confirm-the-numbers prompt -> approve -> filed Open.

  journalctl -u hermes -f | grep --line-buffered -iE 'GI_EXPENSE_CONFIRM|create_expense'
EOF
