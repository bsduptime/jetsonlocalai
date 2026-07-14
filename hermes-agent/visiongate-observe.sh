#!/usr/bin/env bash
# visiongate observe mode — on/off.
#
#   sudo bash hermes-agent/visiongate-observe.sh on     # nothing uploads without a human yes
#   sudo bash hermes-agent/visiongate-observe.sh off    # normal: confident receipts upload silently
#
# WHY THIS EXISTS: the hermes-greeninvoice broker runs against the LIVE Morning account
# (GI_ENV=production, GI_DRY_RUN=false — there is no sandbox account). Until the
# classifier has been benchmarked on real Hebrew receipts, "the model was confident" is
# not sufficient authority to write to David's actual books. In observe mode EVERY
# upload — including one the model is 99% sure is a receipt — stops and asks him in
# Telegram first.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
MODE="${1:-}"
[[ "$MODE" == "on" || "$MODE" == "off" ]] || { echo "usage: $0 on|off" >&2; exit 1; }

DROPIN=/etc/systemd/system/hermes.service.d/visiongate.conf
install -d "$(dirname "$DROPIN")"

if [[ "$MODE" == "on" ]]; then
    cat > "$DROPIN" <<'EOF'
[Service]
Environment="VISIONGATE_OBSERVE=1"
EOF
else
    cat > "$DROPIN" <<'EOF'
[Service]
Environment="VISIONGATE_OBSERVE=0"
EOF
fi

systemctl daemon-reload
systemctl restart hermes
sleep 3
systemctl is-active --quiet hermes || { echo "hermes did not come back" >&2; exit 1; }

echo "visiongate observe mode: $MODE"
echo
# The gate announces itself on its FIRST HOOK CALL, not at startup — plugin
# registration runs before Hermes attaches its logging handlers, so anything logged
# there is swallowed. So there is nothing to check until you send an image.
echo "To confirm the gate is live, send Elena an image and watch for a verdict:"
echo "  journalctl -u hermes -f | grep --line-buffered VISIONGATE"
