#!/usr/bin/env bash
# voice-replies (Jetson side) — installs the per-repo narration assets that
# this repo owns:
#
#   1. voice-profiles   -> ~/.claude/voice-profiles   (both platforms)
#        The canonical, machine-agnostic repo->voice map. Lives here because
#        jetsonlocalai is the only repo present on BOTH the Mac and the Jetson,
#        so a given repo speaks in the same voice everywhere.
#
#   2. jetson-voice-say -> ~/.claude/hooks/voice-say   (Linux / Jetson only)
#        Synthesises TTS locally and ships the WAV to the Mac listener. Becomes
#        the narration trigger path so CLAUDE.md's voice rules work unchanged.
#        NOT installed on macOS — there the Mac wrapper from maclocalai owns
#        ~/.claude/hooks/voice-say.
#
# Safe to re-run. voice-profiles is create-if-missing so curated edits survive.

set -euo pipefail

step() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }

THIS_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
HOOKS_DIR="${CLAUDE_DIR}/hooks"
mkdir -p "${HOOKS_DIR}"

# --- 1. Shared voice-profiles map (both platforms) --------------------------
PROFILES_DEST="${CLAUDE_DIR}/voice-profiles"
if [ -f "${PROFILES_DEST}" ]; then
  step "Keeping existing voice profiles at ${PROFILES_DEST} (not overwriting your edits)"
  echo "    (delete it and re-run to reset to this repo's defaults)"
else
  step "Installing voice profiles to ${PROFILES_DEST}"
  cp "${THIS_DIR}/voice-profiles" "${PROFILES_DEST}"
fi

# --- 2. Jetson narration wrapper (Linux only) -------------------------------
if [ "$(uname)" = "Linux" ]; then
  VOICE_SAY_DEST="${HOOKS_DIR}/voice-say"
  step "Installing jetson-voice-say to ${VOICE_SAY_DEST}"
  cp "${THIS_DIR}/jetson-voice-say" "${VOICE_SAY_DEST}"
  chmod +x "${VOICE_SAY_DEST}"
  cat <<EOF

Done (Jetson). Narration on this box now:
  - synthesises locally (http://localhost:18080)
  - ships the WAV to the Mac listener for playback
  - picks the voice per repo from ${PROFILES_DEST}

Start a fresh Claude Code session here to pick it up.
EOF
else
  cat <<EOF

Done (macOS). Only the shared voice-profiles map was installed here — the Mac
voice-say wrapper itself comes from the maclocalai repo:
  bash ~/code/maclocalai/voice-replies/install.sh

Start a fresh Claude Code session to pick up profile changes.
EOF
fi
