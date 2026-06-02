# voice-replies (Jetson side)

The Jetson-side half of the cross-machine voice narration loop. The Mac-side
client lives in the sister repo: [`maclocalai/voice-replies/`](https://github.com/bsduptime/maclocalai/tree/main/voice-replies).

This stack exists here because **jetsonlocalai is the only repo present on both
the Mac and the Jetson**, so it's the natural owner of anything that must be
identical on both machines.

## What it installs

```bash
bash install.sh
```

| Asset | Destination | Platforms |
|---|---|---|
| `voice-profiles` | `~/.claude/voice-profiles` | both (Mac + Jetson) |
| `jetson-voice-say` | `~/.claude/hooks/voice-say` | Linux / Jetson only |

- **`voice-profiles`** — the canonical, machine-agnostic `repo: voice` map.
  When a session narrates via `voice-say`, the voice is chosen from this map by
  the current repo (git-toplevel basename), so each project sounds the same on
  every machine and parallel sessions are distinguishable by ear. Unmapped
  repos fall back to `devnen-eli`; an explicit voice arg always overrides.
  Installed **create-if-missing** so your curated edits survive re-runs (delete
  it and re-run to reset to the repo defaults).

- **`jetson-voice-say`** — synthesises TTS on the Jetson (`localhost:18080`) and
  ships the WAV to the Mac listener (`100.82.188.1:18082`) for playback. Same
  arg shape as the Mac `voice-say` so the CLAUDE.md narration rules transfer
  unchanged. Installed as `~/.claude/hooks/voice-say` on the Jetson only — on
  the Mac that path is owned by the maclocalai wrapper, so this installer never
  touches it there.

## Editing voices

Edit `voice-profiles`, commit, push to main, then `git pull` on the Jetson and
re-run nothing (the resolver re-reads the file on every call). To redeploy the
file after a reset, delete `~/.claude/voice-profiles` and re-run `install.sh`.

Audition a voice:

```bash
afplay <(curl -s http://192.168.1.200:18080/previews/<voice>.wav)   # macOS
curl -s http://192.168.1.200:18080/voices | python3 -m json.tool    # list all 36
```
