#!/usr/bin/env bash
# ============================================================================
# hermes-household-board — installer (systemd service + config skeleton)
# ============================================================================
# Run from the jetsonlocalai repo root:
#   sudo bash hermes-agent/install-household-board.sh
#
# Needs TWO manual values in /etc/hermes-household-board/.env afterwards:
#   HOUSEHOLD_BOT_TOKEN        (a NEW bot from @BotFather — not Elena's)
#   HOUSEHOLD_ALLOWED_CHAT_IDS (the family chat id(s))
# The service fails closed until both are set. Idempotent.
# ============================================================================
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "error: run as root (sudo)" >&2; exit 1; }

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC="$SCRIPT_DIR/household-board"
[ -f "$SRC/board.py" ] || { echo "error: $SRC/board.py missing" >&2; exit 1; }
id hermes &>/dev/null || { echo "error: hermes user missing" >&2; exit 1; }

CONF=/etc/hermes-household-board
install -d -m 750 -o root -g hermes "$CONF"
if [ ! -f "$CONF/.env" ]; then
    install -m 640 -o root -g hermes "$SRC/.env.example" "$CONF/.env"
    echo "created $CONF/.env — EDIT IT (token + allowed chat ids)"
else
    echo "$CONF/.env exists — leaving it alone"
fi

# Unit file: rewrite ExecStart to point at THIS checkout.
UNIT=/etc/systemd/system/hermes-household-board.service
sed "s|ExecStart=.*|ExecStart=/usr/bin/python3 $SRC/board.py|" \
    "$SRC/systemd/hermes-household-board.service" > "$UNIT"
systemctl daemon-reload
systemctl enable hermes-household-board.service >/dev/null

cat <<EOF

 Installed. Next:
   1. @BotFather -> /newbot (e.g. "Family List") -> paste token into $CONF/.env
   2. Add the bot to the family group; get the chat id into $CONF/.env
      (privacy mode can stay ON — the board only needs /list + taps)
   3. systemctl start hermes-household-board && journalctl -fu hermes-household-board
   4. In the group: /list  -> the pinned tappable board appears
EOF
