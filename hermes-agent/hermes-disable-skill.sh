#!/usr/bin/env bash
# Disable a bundled Hermes skill for Elena.
#
#   sudo bash hermes-agent/hermes-disable-skill.sh ocr-and-documents
#   sudo bash hermes-agent/hermes-disable-skill.sh ocr-and-documents --undo
#
# WHY (the case that prompted this):
#
# Hermes ships a skill `ocr-and-documents`, described as "extracting text from PDFs,
# scanned documents, images ... using OCR". A receipt photo matches that description
# perfectly, so Hermes surfaces the skill, and the skill instructs the agent to
# `pip install pymupdf` / `marker-pdf` / etc. Elena duly went off and started probing for
# easyocr/pytesseract/paddleocr/torch and asking to run execute_code.
#
# That is wrong here on every axis:
#   * She can already SEE the image — Hermes passes it to the model natively
#     (OpenAI, image_input_mode=auto). She read a crumpled Hebrew receipt's merchant,
#     tax ID, VAT and total straight off the photo, unaided.
#   * For expenses she must NOT extract fields locally anyway. The flow is
#     gi_upload_expense_file -> Morning's server-side OCR -> gi_search_expense_drafts ->
#     gi_create_expense. Morning does the OCR, and it does it on the stored document,
#     which is what the tax authority needs.
#   * pip-installing a torch/OCR stack into the hermes venv at runtime is a good way to
#     break the agent that runs the household.
#
# Removing the skill is structural; telling her "please don't" in a prompt is not.
# There is no `hermes skills disable` subcommand (only install/list), so we write the
# `skills.disabled` config key directly — the same key hermes_cli/skills_config.py uses.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
SKILL="${1:-}"
[[ -n "$SKILL" ]] || { echo "usage: $0 <skill-name> [--undo]" >&2; exit 1; }
UNDO=""
[[ "${2:-}" == "--undo" ]] && UNDO=1

CFG=/home/hermes/.hermes/config.yaml
[[ -f "$CFG" ]] || { echo "no config at $CFG" >&2; exit 1; }

cp -a "$CFG" "$CFG.bak.$(date +%s)"

python3 - "$CFG" "$SKILL" "${UNDO:-}" <<'PY'
import sys, yaml
cfg_path, skill, undo = sys.argv[1], sys.argv[2], sys.argv[3]
with open(cfg_path) as fh:
    cfg = yaml.safe_load(fh) or {}
skills = cfg.setdefault("skills", {})
disabled = list(skills.get("disabled") or [])
if undo:
    disabled = [s for s in disabled if s != skill]
    print(f"re-enabled: {skill}")
elif skill not in disabled:
    disabled.append(skill)
    print(f"disabled: {skill}")
else:
    print(f"already disabled: {skill}")
skills["disabled"] = sorted(disabled)
with open(cfg_path, "w") as fh:
    yaml.safe_dump(cfg, fh, sort_keys=False, allow_unicode=True)
print("skills.disabled =", skills["disabled"])
PY

chown hermes:hermes "$CFG"
systemctl restart hermes
sleep 4
systemctl is-active --quiet hermes || {
    echo "hermes did NOT come back — restore with the .bak file next to $CFG" >&2
    exit 1
}
echo "hermes restarted OK"
