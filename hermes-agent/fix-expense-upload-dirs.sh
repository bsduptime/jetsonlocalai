#!/usr/bin/env bash
# Let Elena upload a received Telegram photo/PDF to Morning for an expense.
#
#   sudo bash hermes-agent/fix-expense-upload-dirs.sh
#
# WHY: gi_upload_expense_file confines uploads to an allowlist of directories (so the
# agent can't be tricked into uploading /etc/passwd). That allowlist was
# HERMES_MEDIA_ALLOW_DIRS=/run/hermes-greeninvoice/previews — ONLY the generated-invoice
# preview dir. A receipt David sends in Telegram lands in ~/.hermes/image_cache (or
# ~/.hermes/cache/images), which wasn't allowed, so every expense upload was rejected.
#
# Fix: set GI_UPLOAD_ALLOWED_DIRS (takes precedence over HERMES_MEDIA_ALLOW_DIRS for
# greeninvoice uploads only) to the media caches where inbound attachments land, plus the
# previews dir. The upload handler still enforces: regular file, extension in
# {pdf,png,jpg,jpeg,webp,heic,heif,gif}, size cap, O_NOFOLLOW + /proc/self/fd re-check —
# so this only widens WHERE a media file may come from, not WHAT may be uploaded.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

HOME_H=/home/hermes/.hermes
DIRS="$HOME_H/cache:$HOME_H/image_cache:/run/hermes-greeninvoice/previews"

DROPIN=/etc/systemd/system/hermes.service.d/greeninvoice-upload-dirs.conf
install -d "$(dirname "$DROPIN")"
cat > "$DROPIN" <<EOF
[Service]
# Directories Elena may upload an expense file FROM. Covers inbound Telegram media
# (~/.hermes/cache/images and the legacy ~/.hermes/image_cache) plus invoice previews.
Environment="GI_UPLOAD_ALLOWED_DIRS=$DIRS"
EOF

systemctl daemon-reload
systemctl restart hermes
sleep 4
systemctl is-active --quiet hermes || { echo "hermes did NOT come back" >&2; exit 1; }

echo "GI_UPLOAD_ALLOWED_DIRS = $DIRS"
systemctl show hermes -p Environment | tr ' ' '\n' | grep GI_UPLOAD_ALLOWED_DIRS || true
cat <<'EOF'

Now ask Elena to file the receipt again (resend it or just say "file it"). The upload
should succeed this time, then she pulls the OCR draft, dedupes, and creates the expense
Open -> you get the confirm-the-numbers prompt.

  journalctl -u hermes -f | grep --line-buffered -iE 'GI_EXPENSE_CONFIRM|upload|create_expense'
EOF
