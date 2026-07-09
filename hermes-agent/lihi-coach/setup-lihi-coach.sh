#!/usr/bin/env bash
# setup-lihi-coach.sh — stand up the hermes-lihi instance (Noga, Lihi's CMO
# coach) alongside the main hermes instance. Mirrors setup-phase1/2 of the
# main install, with a NARROWER access boundary: only the marketing repo.
#
# Usage:  sudo bash hermes-agent/lihi-coach/setup-lihi-coach.sh
#
# Interactive steps that remain AFTER this script (see README):
#   1. Create the Telegram bot via @BotFather → put token in
#      /home/hermes-lihi/.hermes/.env  (TELEGRAM_BOT_TOKEN=...)
#   2. sudo -u hermes-lihi -i hermes setup   (LLM provider OAuth/API key)
#   3. Fill the two TODO Telegram user IDs in config.yaml
#   4. sudo systemctl enable --now hermes-lihi

set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKETING=/home/dbexpertai/code/marketing-liram-heshev
COACH_USER=hermes-lihi

step() { echo; echo "=== $* ==="; }

step "1/7: dedicated user"
if ! id "$COACH_USER" &>/dev/null; then
    useradd --create-home --shell /bin/bash --comment "Hermes - Lihi CMO coach" "$COACH_USER"
fi

step "2/7: ACLs — marketing repo ONLY (both current files and defaults)"
command -v setfacl >/dev/null || apt-get install -y -qq acl
setfacl -R  -m "u:${COACH_USER}:rwX" "$MARKETING"
setfacl -R -d -m "u:${COACH_USER}:rwX" "$MARKETING"
# dbexpertai keeps rw on everything the coach creates
setfacl -R -d -m "u:dbexpertai:rwX" "$MARKETING"
# traversal into ~/code (o+x on /home/dbexpertai already set for main hermes)
chmod o+x /home/dbexpertai /home/dbexpertai/code

step "3/7: git identity + shared-repo safety for the coach"
sudo -u "$COACH_USER" -H git config --global user.name  "Noga (coach agent)"
sudo -u "$COACH_USER" -H git config --global user.email "noga-coach@jetson.local"
sudo -u "$COACH_USER" -H git config --global --replace-all safe.directory "$MARKETING" "$MARKETING"
git config --global --replace-all safe.directory "$MARKETING" "$MARKETING" || true

step "4/7: install Hermes under ${COACH_USER} (upstream installer)"
if ! sudo -u "$COACH_USER" test -x "/home/${COACH_USER}/.local/bin/hermes"; then
    sudo -u "$COACH_USER" -i bash -c \
        'curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/install.sh | bash'
fi

step "5/7: HERMES_HOME + SOUL + config"
install -d -o "$COACH_USER" -g "$COACH_USER" -m 700 "/home/${COACH_USER}/.hermes"
install -o "$COACH_USER" -g "$COACH_USER" -m 600 \
    "${REPO_DIR}/SOUL.md" "/home/${COACH_USER}/.hermes/SOUL.md"
if [ ! -f "/home/${COACH_USER}/.hermes/config.yaml" ]; then
    install -o "$COACH_USER" -g "$COACH_USER" -m 600 \
        "${REPO_DIR}/config.yaml.template" "/home/${COACH_USER}/.hermes/config.yaml"
else
    echo "config.yaml exists — NOT overwriting; merge ${REPO_DIR}/config.yaml.template manually"
fi
touch "/home/${COACH_USER}/.hermes/.env"
chown "$COACH_USER:$COACH_USER" "/home/${COACH_USER}/.hermes/.env"
chmod 600 "/home/${COACH_USER}/.hermes/.env"
# separate API server port so it never collides with the main instance (8642)
grep -q API_SERVER_PORT "/home/${COACH_USER}/.hermes/.env" || \
    echo "API_SERVER_PORT=8643" >> "/home/${COACH_USER}/.hermes/.env"

step "6/7: systemd unit"
install -m 644 "${REPO_DIR}/systemd/hermes-lihi.service" /etc/systemd/system/hermes-lihi.service
systemctl daemon-reload

step "7/7: jetson-stt service (transcription backend)"
# idempotent: reinstall the unit every run so edits propagate
install -m 644 /mnt/sdcard/jetson-stt/jetson-stt.service /etc/systemd/system/jetson-stt.service
systemctl daemon-reload
# stop any manually-started dev instance so the port is free. Kill by port,
# not by cmdline pattern — the dev server's cmdline is a relative
# "./venv/bin/python server.py", identical in shape to the bge-m3 service.
fuser -k 11436/tcp 2>/dev/null || true
sleep 1
systemctl enable --now jetson-stt
systemctl restart jetson-stt

echo
echo "Done. Remaining manual steps:"
echo "  1. @BotFather → new bot → TELEGRAM_BOT_TOKEN=... in /home/${COACH_USER}/.hermes/.env"
echo "  2. sudo -u ${COACH_USER} -i hermes setup   (choose LLM provider)"
echo "  3. Fill TODO user IDs in /home/${COACH_USER}/.hermes/config.yaml"
echo "  4. sudo systemctl enable --now hermes-lihi && systemctl status hermes-lihi"
