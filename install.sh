#!/usr/bin/env bash
# jetsonlocalai top-level installer.
# Lists available stacks and dispatches to each one's install.sh.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

stacks=()
for dir in "$ROOT_DIR"/*/; do
  if [ -f "${dir}install.sh" ]; then
    stacks+=("$(basename "$dir")")
  fi
done

if [ ${#stacks[@]} -eq 0 ]; then
  cat <<'EOF'
No stacks shipped yet — this repo is just getting started.

Candidates being designed (not yet built):
  - openclaw/             multi-tenant agent gateway
  - nemotron-heartbeat/   local Nemotron monitoring agent
  - voice-pipeline-host/  home Jarvis host (HA + Wyoming + STT/TTS)
  - backups/              per-user writable-folder backup recipes

Watch the repo for updates: https://github.com/bsduptime/jetsonlocalai
EOF
  exit 0
fi

echo "Available stacks:"
for i in "${!stacks[@]}"; do
  echo "  $((i+1)). ${stacks[$i]}"
done
echo "  a. all"
echo "  q. quit"
echo ""
read -r -p "Pick one or more (e.g. '1', '1 2', 'a'): " choice

run_stack() {
  local name="$1"
  echo ""
  echo "=== Installing stack: $name ==="
  bash "$ROOT_DIR/$name/install.sh"
}

case "$choice" in
  q|Q) exit 0 ;;
  a|A)
    for s in "${stacks[@]}"; do run_stack "$s"; done
    ;;
  *)
    for n in $choice; do
      idx=$((n-1))
      if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#stacks[@]}" ]; then
        run_stack "${stacks[$idx]}"
      else
        echo "Skipping invalid selection: $n"
      fi
    done
    ;;
esac
