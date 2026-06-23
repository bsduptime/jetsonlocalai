# voice plugin — Elena speaks to David's Mac

Gives Hermes (Elena) one tool, `speak_to_david`, that says a short message out
loud through the speakers on David's Mac — the same path the terminal sessions
use (`jetson-voice-say`), but callable by the agent itself.

## How it works

```
Elena (speak_to_david)
  └─ POST {"text", "voice":"devnen-elena"} → http://localhost:18080/synthesize  (WAV bytes, local)
       └─ POST WAV (audio/wav)             → http://100.82.188.1:18082/play       (afplay on the Mac, over Tailscale)
```

Two HTTP hops, stdlib `urllib` only. **No secrets, no broker daemon** — unlike
the mailer (which fronts a credential-holding `hermes-mailer.service`), voice
synthesis needs no credentials, and the hermes unit already permits `AF_INET`
egress, so the plugin makes the requests directly from Elena's process.

It does **not** shell out to `~/.claude/hooks/jetson-voice-say`: that path is
hidden from the hermes process by `ProtectHome=tmpfs`. The two hops are
reimplemented here so the tool is self-contained.

## Contract

- **One-way.** There is no microphone and no reply channel. The model is told
  never to ask a question it needs answered through this tool.
- **Best-effort delivery.** If synthesis succeeds but the Mac is asleep / off
  Tailscale, the tool returns `ok=true, delivered=false` — not an error.
- **Voice is fixed** to `devnen-elena` (Elena's reserved identity). It is not a
  tool parameter.
- **Synthesis failure** (local TTS server down/broken) returns
  `ok=false, error=synth_failed`.

Response shapes:

| situation                     | response                                                              |
|-------------------------------|----------------------------------------------------------------------|
| spoke + played on the Mac     | `{"ok":true,"delivered":true,"voice":"devnen-elena","chars":N}`      |
| spoke, Mac unreachable        | `{"ok":true,"delivered":false,...}`                                  |
| bad input                     | `{"ok":false,"error":"invalid_input","reason":...}`                  |
| TTS server down               | `{"ok":false,"error":"synth_failed","reason":...,"detail":...}`      |

## Config (optional env overrides)

Defaults match `jetson-voice-say` and need no configuration. To override, set
on the hermes unit (e.g. a systemd drop-in):

- `HERMES_VOICE_TTS_URL` (default `http://localhost:18080`)
- `HERMES_VOICE_LISTENER_URL` (default `http://100.82.188.1:18082`)
- `HERMES_VOICE_NAME` (default `devnen-elena`)

## Install

```sh
sudo bash hermes-agent/install-voice-plugin.sh
sudo systemctl restart hermes
sudo -u hermes -i hermes plugins enable voice   # if not auto-enabled
# then start a NEW session (/new) — tools list outside a session won't show it
```

## Tests

```sh
cd hermes-agent/plugins/voice && python3 -m pytest -q
```

The two network hops are monkeypatched; tests never touch the real TTS server
or the Mac.
