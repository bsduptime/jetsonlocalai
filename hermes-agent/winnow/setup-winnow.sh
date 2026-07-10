#!/usr/bin/env bash
# setup-winnow.sh — stand up the hermes-winnow instance (the Winnow business
# secretary) + its dedicated BUSINESS calendar relay. Mirrors the lihi-coach
# instance pattern with an even tighter boundary: no repos bound in at all.
#
# Usage:  sudo bash hermes-agent/winnow/setup-winnow.sh
#
# Interactive steps that remain AFTER this script (see README.md):
#   1. @BotFather bot token  -> /home/hermes-winnow/.hermes/.env
#   2. sudo -u hermes-winnow -i hermes setup        (LLM provider)
#   3. allow_from user IDs   -> /home/hermes-winnow/.hermes/config.yaml
#   4. Business-calendar Google OAuth -> /etc/hermes-calendar-winnow
#   5. systemctl enable --now hermes-calendar-winnow hermes-winnow

set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"      # .../hermes-agent/winnow
AGENT_DIR="$(dirname "$REPO_DIR")"                            # .../hermes-agent
W_USER=hermes-winnow

step() { echo; echo "=== $* ==="; }

step "1/8: dedicated user"
if ! id "$W_USER" &>/dev/null; then
    useradd --create-home --shell /bin/bash --comment "Hermes - Winnow business secretary" "$W_USER"
fi

step "2/8: install Hermes under ${W_USER} (upstream installer)"
if ! sudo -u "$W_USER" test -x "/home/${W_USER}/.local/bin/hermes"; then
    sudo -u "$W_USER" -i bash -c \
        'curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/install.sh | bash'
fi

step "3/8: HERMES_HOME + SOUL + config"
install -d -o "$W_USER" -g "$W_USER" -m 700 "/home/${W_USER}/.hermes"
install -o "$W_USER" -g "$W_USER" -m 600 \
    "${REPO_DIR}/SOUL.md" "/home/${W_USER}/.hermes/SOUL.md"
if [ ! -f "/home/${W_USER}/.hermes/config.yaml" ]; then
    install -o "$W_USER" -g "$W_USER" -m 600 \
        "${REPO_DIR}/config.yaml.template" "/home/${W_USER}/.hermes/config.yaml"
else
    echo "config.yaml exists — NOT overwriting"
fi
touch "/home/${W_USER}/.hermes/.env"
chown "$W_USER:$W_USER" "/home/${W_USER}/.hermes/.env"
chmod 600 "/home/${W_USER}/.hermes/.env"

step "4/8: plugins — greeninvoice + familycal ONLY (symlinked from this repo)"
PLUGINS="/home/${W_USER}/.hermes/plugins"
install -d -o "$W_USER" -g "$W_USER" -m 750 "$PLUGINS"
for p in greeninvoice familycal; do
    if [ ! -e "$PLUGINS/$p" ]; then
        ln -s "${AGENT_DIR}/plugins/$p" "$PLUGINS/$p"
        chown -h "$W_USER:$W_USER" "$PLUGINS/$p"
    fi
done
# repo must be world-traversable for the symlinks (already true for Elena's)
chmod o+x /home/dbexpertai /home/dbexpertai/code

step "5/8: greeninvoice broker — caller identity + socket access"
usermod -aG hermes-greeninvoice-clients "$W_USER"
GI_ENV=/etc/hermes-greeninvoice/.env
W_UID=$(id -u "$W_USER")
if [ -f "$GI_ENV" ] && ! grep -q "^CALLER_UID_winnow=" "$GI_ENV"; then
    echo "CALLER_UID_winnow=${W_UID}" >> "$GI_ENV"
    echo "registered caller 'winnow' (uid ${W_UID}) — restart hermes-greeninvoice to apply"
elif [ ! -f "$GI_ENV" ]; then
    echo "WARNING: $GI_ENV missing — is the greeninvoice broker installed?"
fi

step "6/8: business calendar relay — groups + config skeleton"
getent group hermes-calendar-winnow-clients >/dev/null || groupadd --system hermes-calendar-winnow-clients
getent group hermes-calendar-winnow-config  >/dev/null || groupadd --system hermes-calendar-winnow-config
usermod -aG hermes-calendar-winnow-clients "$W_USER"
install -d -m 750 -o root -g hermes-calendar-winnow-config /etc/hermes-calendar-winnow
if [ ! -f /etc/hermes-calendar-winnow/.env ]; then
    cat > /etc/hermes-calendar-winnow/.env <<'ENV'
# hermes-calendar-winnow — BUSINESS calendar relay. Separate creds from the
# family relay. Start in dry-run; flip after the Google OAuth step.
CALENDAR_DRY_RUN=true
CALENDAR_TZ=Asia/Jerusalem
# After setup-google-auth.py (run against THIS dir):
#CALENDAR_GOOGLE_CREDENTIALS=/etc/hermes-calendar-winnow/oauth-client.json
#CALENDAR_GOOGLE_TOKEN=/var/lib/hermes-calendar-winnow/gcal-token.json
#CALENDAR_FAMILY_ID=<business calendar id>
ENV
    chgrp hermes-calendar-winnow-config /etc/hermes-calendar-winnow/.env
    chmod 640 /etc/hermes-calendar-winnow/.env
fi
if [ ! -f /etc/hermes-calendar-winnow/contacts.yaml ]; then
    install -m 640 -g hermes-calendar-winnow-config \
        "${AGENT_DIR}/calendar-relay/contacts.example.yaml" /etc/hermes-calendar-winnow/contacts.yaml
    echo "EDIT /etc/hermes-calendar-winnow/contacts.yaml (business contacts)"
fi
# the relay code itself is shared — installed once by setup-hermes-calendar.sh
[ -d /usr/local/lib/hermes-calendar ] || \
    echo "WARNING: /usr/local/lib/hermes-calendar missing — run setup-hermes-calendar.sh first"

step "7/8: systemd units"
install -m 644 "${REPO_DIR}/systemd/hermes-winnow.service" /etc/systemd/system/
install -m 644 "${REPO_DIR}/systemd/hermes-calendar-winnow.service" /etc/systemd/system/
systemctl daemon-reload

step "8/8: done — interactive remainder"
cat <<EOF

 Instance staged. Remaining (see winnow/README.md):
   1. @BotFather -> /newbot ("Winnow Secretary") -> TELEGRAM_BOT_TOKEN=... into
      /home/${W_USER}/.hermes/.env
   2. sudo -u ${W_USER} -i hermes setup          # LLM provider
   3. Fill allow_from (David + Lihi user IDs) in /home/${W_USER}/.hermes/config.yaml
   4. Google OAuth for the BUSINESS calendar (config dir /etc/hermes-calendar-winnow)
   5. systemctl restart hermes-greeninvoice      # picks up CALLER_UID_winnow
      systemctl enable --now hermes-calendar-winnow hermes-winnow
   6. Create the "Winnow Management" Telegram group; add the bot + Lihi.
EOF
