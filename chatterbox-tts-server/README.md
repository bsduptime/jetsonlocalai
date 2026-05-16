# chatterbox-tts-server

Tiny FastAPI wrapper around [Chatterbox TTS](https://github.com/resemble-ai/chatterbox) that exposes an **OpenAI-compatible `/v1/audio/speech`** endpoint. Designed to run on the Jetson Orin AGX and serve any client (Mac voice-replies, iPhone Shortcuts, content pipeline narration, future voice satellites) over Tailscale.

## Why this exists

Chatterbox produces high-quality, expressive voice (beats ElevenLabs in blind tests at 63.75%) and supports voice cloning, but its MPS (Apple GPU) support is currently broken — it falls back to CPU on Macs and runs at 1-2s per utterance. On consumer CUDA GPUs it does **sub-200ms first sound**. The Jetson has CUDA. So we serve Chatterbox from the Jetson and let Mac clients (and others) POST text and get audio back.

## Prerequisites

- Jetson Orin AGX with the existing `videngine` venv at `/mnt/sdcard/videngine/venv` (already has torch with CUDA, chatterbox-tts, fastapi, uvicorn). No new installs needed.
- Chatterbox model weights cached at `/mnt/sdcard/.cache/huggingface/hub/models--ResembleAI--chatterbox` (already downloaded by videngine).
- Tailscale running on the Jetson if you want remote access.
- **`setuptools<81` pinned in the venv** (videngine's `setup-jetson.sh` does this). Newer setuptools silently breaks perth — `perth.PerthImplicitWatermarker` ends up `None` and chatterbox crashes on init with a confusing `TypeError`. The pin is the real fix; the DummyWatermarker monkey-patch in `server.py` is a belt-and-suspenders fallback for venvs where the pin slipped.

## Performance on Jetson Orin AGX (CUDA)

Measured by David against the videngine venv:

- **Cold model load**: ~16 s
- **Generation**: ~6-8 s per short line (1-2 s of audio). Sampling caps at 1000 tokens.
- **Output sample rate**: 24000 Hz (`model.sr`). Resample with `ffmpeg -ar 22050` if mixing into a 22050 Hz pipeline.

## Voice profiles — what already exists, what to add

Existing reference clips (David's voice — for content pipeline narration, **not** Claude responses):

```
content/videngine/voice-profiles/samples/
  steady.wav
  drive.wav
  reflective-calm.wav
  …
```

To add a **new** voice (e.g. `claude-assistant`):

- 12 s of clean speech, mono, 22050 Hz WAV, no music, no reverb.
- For long source recordings, use `content/videngine/scripts/make_voice_profiles.py` — it extracts the best 12s segment automatically.
- Drop the resulting WAV into this stack's `voices/` folder; the filename stem becomes the voice name in API calls.

## Reproducibility note

Generation is **non-deterministic** — same text + same reference produces different cadence each run. If you need stability (regression tests, audio mixing alignment), set `torch.manual_seed(N)` before `.generate()`.

## Reference integration

`content/videngine/src/videngine/stages/intro_outro.py` is the proven Chatterbox integration — wraps load + generate + save behind config. Look there for any "how do they do X" question before reinventing.

## Run it

```bash
bash run.sh
```

That's it. It uses the videngine venv automatically. First request warms CUDA (~5-10 seconds); subsequent requests are fast.

### Configuration (override via env)

| Var | Default | What |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address. Use `tailscale ip -4` output to bind only to the Tailscale interface. |
| `PORT` | `18080` | Server port. |
| `VOICES_DIR` | `./voices` | Directory of reference WAV files (one per voice profile). |
| `HF_HOME` | `/mnt/sdcard/.cache/huggingface` | Where the Chatterbox model is cached. |
| `CHATTERBOX_API_TOKEN` | *(unset)* | If set, requires `Authorization: Bearer <token>` on requests. |

## API

### `POST /v1/audio/speech` — OpenAI-compatible

```bash
curl -X POST http://JETSON:18080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input": "Hello from the Jetson.", "voice": "default"}' \
  --output reply.wav
```

Body (subset of OpenAI's TTS request shape):

| Field | Type | Default | Notes |
|---|---|---|---|
| `input` | string | required | The text to synthesize. |
| `voice` | string | `"default"` | A voice name from `GET /v1/voices`. `default` uses Chatterbox's built-in voice with no reference. |
| `model` | string | `"chatterbox"` | Accepted for OpenAI compatibility, ignored — server always uses Chatterbox. |
| `response_format` | string | `"wav"` | Only `wav` is supported currently. |
| `speed` | float | `1.0` | Ignored — Chatterbox doesn't expose speed control. |

Returns: `audio/wav` (16-bit PCM, mono, Chatterbox's sample rate).

### `GET /v1/voices`

Returns the list of available voice names. Always includes `default`. Other names come from the WAV files in `$VOICES_DIR`.

### `GET /health`

Liveness probe. Returns `{ok, device, voices}`.

## Voice profiles

Drop reference WAV files into `voices/` (gitignored — they're personal). The filename stem becomes the voice name.

- **Recommended for the reference**: 12-second clean clip, 22050 Hz, mono, 16-bit PCM. See `content/videngine/voice-profiles/recording-guide.md` for the recording protocol and `content/videngine/scripts/make_voice_profiles.py` for the extraction pipeline.
- **Don't reuse `founder.m4a` for Claude.** Most people find AI-in-their-own-voice uncanny long-term. Pick a different reference for the `claude-assistant` voice. The `founder` voice stays for narration in the content pipeline.

To convert an existing m4a/mp3 to the right WAV format:

```bash
ffmpeg -i input.m4a -acodec pcm_s16le -ar 22050 -ac 1 voices/claude-assistant.wav
```

## Tailscale-only binding (optional)

Bind only to the Tailscale interface so the server isn't reachable on the LAN:

```bash
HOST=$(tailscale ip -4) bash run.sh
```

Pair with `CHATTERBOX_API_TOKEN` for a second auth layer if you're nervous.

## Clients

Designed-in callers:

- **`maclocalai/voice-replies/` Tier 2** — Mac client POSTs each Claude response here; plays the returned WAV.
- **Content pipeline narration** — replaces in-process Chatterbox calls in `content/videngine/stages/intro_outro.py` so the pipeline can stay device-light.
- **Future voice satellites / iPhone Shortcuts** — anything that speaks OpenAI TTS protocol works without code changes.

## Running as a service (later)

For now `run.sh` is manual. A systemd unit + auto-start config will land here once we've validated the manual workflow.
