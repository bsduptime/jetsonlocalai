# lihi-coach — Noga, Lihi's CMO coach (dedicated Hermes instance)

A second, independent Hermes instance on the Jetson serving as **Lihi's
personal coach** for her CMO role at Liram-Heshev. Private Telegram bot,
Hebrew-first persona ("נוגה"), voice-note transcription (Hebrew+English),
and a daily coaching cadence — with the marketing strategy repo
(`~/code/marketing-liram-heshev`) as its working memory.

Design assessment + decisions: session of 2026-07-09 (dedicated bot ·
Hebrew-first · full-read with tagged sensitivity per the repo's
`AGENT-ACCESS.md` · open-source/permissive stack).

## Architecture

```
Lihi (Telegram: text + voice notes + meeting recordings)
        │
        ▼
hermes-lihi instance (user=hermes-lihi, sandboxed, port 8643)
  ├── SOUL.md persona: coach loops (morning top-3 / check-in filing /
  │   evening review / weekly Tzadok 1-pager), drift guard, boundaries
  ├── sandbox: ONLY /home/dbexpertai/code/marketing-liram-heshev bound in
  │   (no other repos, no Claude credentials, no claude CLI)
  ├── stt.provider=jetson → /mnt/sdcard/jetson-stt/stt-client
  │        │
  │        ▼
  │   jetson-stt.service (127.0.0.1:11436, user=dbexpertai)
  │     whisper-large-v3-turbo CT2, CPU int8, ~1.9x realtime
  │     detect (stock model) → route: he→ivrit-ai fine-tune, else stock
  └── files everything into the marketing repo + git commit (no push)
```

## Licenses (all permissive — verified 2026-07-09)

| Component | License |
|---|---|
| Hermes Agent (NousResearch) | Apache-2.0 |
| faster-whisper + CTranslate2 | MIT |
| ivrit-ai/whisper-large-v3-turbo-ct2 | Apache-2.0 |
| deepdml/faster-whisper-large-v3-turbo-ct2 (stock conversion) | MIT |
| FastAPI / uvicorn | MIT / BSD-3 |

The only non-open component is the **LLM inference provider** chosen at
`hermes setup` (OpenAI Codex OAuth like Elena, or an Anthropic/OpenRouter
key) — commercial API, David's account, David's choice at install time.

## Validated facts this build rests on

- Hermes's Telegram adapter downloads incoming voice/audio to cache and the
  agent transcribes via the built-in `transcribe_audio` tool; STT backend is
  pluggable (`stt.providers.<name>: type: command`). Verified in upstream
  source.
- The ivrit fine-tune's language detection is degraded (says `he` @ 1.00 on
  pure English) and it injects Hebrew tokens into English speech — measured
  here, matches the model card. Hence detect-on-stock-then-route in
  jetson-stt.
- CPU int8 turbo ≈ **1.9× realtime** on the Orin (12 threads): a 1-min voice
  note ≈ 30 s; a 1-h meeting ≈ 30 min (acceptable async; CUDA build of
  CTranslate2 is the upgrade path if it isn't).
- Telegram Bot API caps bot file downloads at ~20 MB — opus voice notes of
  an hour fit; large mp3/m4a uploads may not. Boundary documented in SOUL
  behavior ("say so"), not worked around.

## Deploy (David, with sudo)

```bash
sudo bash hermes-agent/lihi-coach/setup-lihi-coach.sh
# then the 4 manual steps it prints:
#  1. @BotFather → bot token into /home/hermes-lihi/.hermes/.env
#  2. sudo -u hermes-lihi -i hermes setup      # LLM provider
#  3. Telegram user IDs (Lihi + David) into config.yaml (allow_from)
#  4. sudo systemctl enable --now hermes-lihi
```

Smoke test: send the bot a Hebrew voice note → expect a Hebrew reply that
categorizes it and names the file it was saved to; check `git log` in the
marketing repo for a `coach:` commit.

## Repo-side pieces (in marketing-liram-heshev)

- `AGENT-ACCESS.md` — binding sensitivity tiers (context-only topics never
  reach shareable output).
- `coach/STATE.md` — live anchors (deadlines, deliverables); the coach reads
  it daily and keeps it current.
- `coach/{daily,weekly,logs}/` — filing structure (three living logs seeded).

## Relationship to other agents

- **Elena (main hermes)** — David's assistant; unchanged. Different user,
  port (8642 vs 8643), bot, HERMES_HOME. Elena's broad `~/code` ACL predates
  this and is unchanged.
- **The "Seeing agent"** (morning campaign reports for Tzadok) is a SEPARATE
  future build — a work deliverable of Lihi's, not part of her private coach.
