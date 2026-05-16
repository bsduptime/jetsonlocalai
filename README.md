# jetsonlocalai

Reproducible setup scripts for running AI workloads locally on NVIDIA Jetson devices (primarily the **Jetson Orin AGX**, which is what these are tested on). No subscriptions, no cloud round-trips, no data leaving your network.

Each subfolder is a self-contained stack with its own `install.sh` and notes.

## Why device-class-specific?

Different hardware has fundamentally different constraints and strengths. A "local AI" repo that tries to cover Macs, Jetsons, NUCs, gaming PCs, and Raspberry Pis ends up serving none of them well. Each device class deserves purpose-built configs that lean into what that hardware actually does best.

Sister repo for Apple Silicon Macs: **[maclocalai](https://github.com/bsduptime/maclocalai)**.

## Available stacks

*Stacks will appear here as they're built.*

Candidates being designed (see the parent project notes — not yet shipped):

- **`openclaw/`** — vendored OpenClaw agent gateway, configured for multi-tenant per-user agents with OAuth-Codex auth (ChatGPT subscription pass-through) and BYO-API-key paths.
- **`nemotron-heartbeat/`** — NVIDIA Nemotron 3 Nano Omni running locally as a heartbeat/monitoring agent. ~$0/customer/month for proactive checks vs frontier-API alternatives.
- **`voice-pipeline-host/`** — "home Jarvis" host stack: Home Assistant + Wyoming + faster-whisper STT + Piper/Kokoro TTS + voice satellites (ESPHome). Pair with a button on your phone or room mics.
- **`backups/`** — restic / rsync recipes for per-user writable folders, off-Jetson destination (Backblaze B2, Hetzner Storage Box, etc.).

## Requirements

- **NVIDIA Jetson Orin AGX** (other Orin models may work for lighter stacks; documented per-stack)
- **JetPack** installed (current major version recommended)
- Linux comfort — these scripts assume you can SSH into the Jetson and run them
- LAN access to whatever clients you want to talk to it from

## Quick start

```bash
git clone https://github.com/bsduptime/jetsonlocalai.git
cd jetsonlocalai
bash install.sh
```

`install.sh` is a menu — pick which stacks to install.

Or jump straight to a stack:

```bash
bash <stack-name>/install.sh
```

## License

MIT — see [LICENSE](LICENSE).

---

*Jetson is a trademark of NVIDIA Corporation. This project is not affiliated with NVIDIA.*
