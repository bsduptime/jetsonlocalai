# chatterbox-tts-server (planned, not yet built)

Chatterbox TTS as a small HTTP server on the Jetson, callable over Tailscale by any client (Mac, iPhone, content pipeline, future home Jarvis voice satellites). Uses the Jetson's CUDA to get sub-200ms first-sound latency that Chatterbox can't currently deliver on Apple Silicon.

## Why this lives on the Jetson, not on Macs

Chatterbox's MPS (Apple GPU) support is currently broken — it falls back to CPU on Mac, giving 1-2s per utterance. On consumer CUDA GPUs Chatterbox does sub-200ms first sound and 6× real-time. The Jetson Orin AGX has the CUDA path Chatterbox was actually optimized for; Macs would have to wait until MPS support lands upstream.

Routing TTS through the Jetson via Tailscale ends up net-faster *and* higher quality than running it on the Mac. Plus it keeps the heavy local-inference workload off the Mac.

## Architecture sketch

```
[any client: Mac, iPhone, content pipeline, voice satellite]
    POST {text, voice_profile} via Tailscale
       ↓
[Jetson Orin AGX]
    chatterbox-tts-server (FastAPI or similar)
        ↓
    Chatterbox TTS (CUDA)
        ↓
    audio stream back to client
```

## Voice profiles

The server holds multiple cloned voice profiles, picked per-request:

- **`founder`** — David's voice. Used by content pipeline for narration (already recorded, samples in `content/videngine/voice-profiles/`).
- **`claude-assistant`** — a different reference voice for Claude responding back to David. Choose someone you'd enjoy hearing daily; most people find AI-as-themselves uncanny.
- *(extensible)* — add per-brand voices for the future per-user voice-agent product.

## Likely clients

| Client | Repo | Use case |
|---|---|---|
| Mac voice-replies | `maclocalai/voice-replies/` | Claude reads responses back in the terminal |
| iPhone Shortcut | n/a (a Shortcut config) | "Hey Siri, Jarvis" → voice answer |
| Content pipeline | `content/videngine/` | Narration generation (replaces / extends current local-Chatterbox call) |
| Future home Jarvis | `jetsonlocalai/voice-pipeline-host/` | Spoken responses through Sonos / voice satellites |

## Status

Not built. Next steps when picking this up:

1. `uv pip install chatterbox-tts` on the Jetson (CUDA path).
2. Wrap with a tiny FastAPI server exposing `POST /speak` with `{text, voice_profile, speed?}`.
3. Listen on a Tailscale-only address (no public exposure).
4. Add auth token shared with clients.
5. Optimize for streaming (start playing as audio generates, don't wait for full file).
6. Document voice-profile recording / registration flow.

## See also

- [`maclocalai/voice-replies/`](https://github.com/bsduptime/maclocalai) — the Mac client side that will be one of this server's first consumers
- `content/videngine/` — existing Chatterbox setup with the founder voice profile (the prior art for voice cloning in this stack)
