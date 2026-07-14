#!/usr/bin/env bash
# Set how the gateway treats a message that arrives while Elena is mid-turn.
#
#   sudo bash hermes-agent/hermes-busy-mode.sh queue      # wait, then handle it (recommended)
#   sudo bash hermes-agent/hermes-busy-mode.sh interrupt  # stop the current task (hermes default)
#   sudo bash hermes-agent/hermes-busy-mode.sh steer      # inject mid-run; TEXT ONLY
#
# WHY THIS MATTERS FOR VISIONGATE, not just for tidiness:
#
# The `pre_gateway_dispatch` hook — the one that classifies an inbound image — lives
# inside `_handle_message`. But when a session is BUSY, the adapter diverts the message
# to `_handle_active_session_busy_message` (gateway/run.py:7167), which never calls
# `_handle_message`. So in the default `interrupt` mode, an image sent while Elena is
# still working on the previous one IS NEVER CLASSIFIED and produces no verdict.
#
# The upload gate itself is unaffected (pre_tool_call classifies on demand at tool-call
# time, so nothing can reach Morning unchecked) — but the advisory annotation and the
# audit line are silently lost, which is exactly the thing we want to watch.
#
# `queue` makes the message wait and then re-enter the normal hooked path.
# Do NOT use `steer` for this: it only carries text, and falls back to queue whenever
# images are attached, so it buys nothing here.
#
# Note `/busy <mode>` exists but is CLI/TUI-only — it is NOT a gateway command, so
# sending it to Elena in Telegram just gets "unknown command".
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
MODE="${1:-}"
case "$MODE" in
    queue|interrupt|steer) ;;
    *) echo "usage: $0 queue|interrupt|steer" >&2; exit 1 ;;
esac

DROPIN=/etc/systemd/system/hermes.service.d/busy-mode.conf
install -d "$(dirname "$DROPIN")"
cat > "$DROPIN" <<EOF
[Service]
Environment="HERMES_GATEWAY_BUSY_INPUT_MODE=$MODE"
EOF

systemctl daemon-reload
systemctl restart hermes
sleep 4
systemctl is-active --quiet hermes || { echo "hermes did not come back" >&2; exit 1; }

echo "busy_input_mode = $MODE"
echo
echo "visiongate announces itself on its first hook call (not at startup), so send an"
echo "image to confirm it is live:"
echo "  journalctl -u hermes -f | grep --line-buffered VISIONGATE"
