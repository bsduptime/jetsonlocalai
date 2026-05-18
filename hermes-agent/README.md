# hermes-agent

David's personal [Hermes Agent](https://github.com/NousResearch/hermes-agent) install on the Jetson Orin AGX — an always-on AI assistant that knows him, remembers across sessions, can be reached over messaging channels (Telegram/WhatsApp/Slack), and can orchestrate development across his projects by invoking Claude Code on his behalf.

## Why this is here (and not in `content/`)

Hermes is **personal infrastructure**, not part of the content pipeline. The Winnow content stack uses [OpenClaw](https://github.com/openclaw/openclaw) instead — that decision is captured in `~/code/content/strategy/roadmap.md` (search "OpenClaw-vs-Hermes"). Hermes lives here in `jetsonlocalai/` alongside the other personal Jetson services (`chatterbox-tts-server/`, `remote-voice/`).

The two agents coexist cleanly on the same machine — different ports, different config dirs, different binaries. See `~/.claude/projects/-home-dbexpertai-code-jetsonlocalai/memory/project_agents_hermes_vs_openclaw.md` for the full coexistence matrix.

## Security architecture

The Jetson runs personal code, SSH keys, GitHub tokens, browser cookies, and a docker daemon — all reachable from `dbexpertai`'s user account. Hermes is an AI agent exposed to potentially adversarial input (prompt injection via web pages, emails, chat messages), and it can invoke shell tools. Running it as `dbexpertai` would mean a hijacked agent inherits `sudo` group, `docker` group, full home directory access — effectively root via container escape.

So Hermes runs under a dedicated, deprivileged user with systemd sandboxing on top:

| Layer | What it does |
|---|---|
| **Dedicated `hermes` user** | No `sudo`, no `docker`, no `adm`. Cannot escalate. |
| **POSIX ACLs on `~/dbexpertai/code/`** | `hermes` gets r/w on the code dir only — that's the working surface for agent orchestration. Everything else in `dbexpertai`'s home stays invisible. |
| **`chmod o+x /home/dbexpertai`** | Traversal-only. `hermes` can `cd` into `code/` but cannot `ls` the parent. |
| **systemd `ProtectHome=tmpfs`** | At process-namespace level, `/home` looks empty. Only the explicitly bind-mounted paths (`/home/hermes`, `/home/dbexpertai/code`, `/home/dbexpertai/.claude/credentials.json` read-only) exist for the process. SSH keys, `.codex/`, `.gnupg/`, browser cookies — not just hidden but **not in the filesystem namespace**. |
| **`ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, `RestrictSUIDSGID`, `LockPersonality`** | Standard hardening — read-only system dirs, no setuid escalation, isolated `/tmp`. |
| **Read-only bind-mount of Claude credentials** | Hermes can invoke `claude` and share David's auth, but cannot modify or exfiltrate the credentials file (it's read-only at the mount level). |
| **Auditd tripwires** | Any access to `~/.ssh/`, `~/.codex/`, `~/.claude/credentials.json`, any write outside the allowed paths, any setuid bit change — logged to `/var/log/audit/audit.log` (root-only). |
| **Cron snapshotter** (runs as `dbexpertai`) | Periodic `git log` deltas in code repos + new-file detection + outbound connection snapshots. Output to `/var/log/hermes-watch/` (root-readable). Hermes can't read or tamper with the logs. |

**What this is not:** stealth monitoring no skilled adversary could detect. An attacker running as `hermes` could observe that auditd is running (`ps -ef`) and infer monitoring exists — they just can't tamper with it or read what's been collected. For a prompt-injection-hijacked agent (the realistic threat), this is plenty. For a deliberate intelligent adversary, you'd add kernel-level instrumentation (eBPF/nftables).

**Accepted risks** (David's explicit choice):

- Agent can spawn `claude` and orchestrate dev work in `~/code/` — including potentially destructive operations on tracked files. Mitigated by: git-tracked code (recoverable via reflog), monitoring, and the ACL boundary (cannot touch anything outside `~/dbexpertai/code/`).
- Agent can make outbound HTTP calls (needed for OpenAI Codex OAuth, MCP servers, etc.). Mitigated by: outbound connection logging in the snapshotter.
- Tool execution is "host" mode (no Docker sandbox layer). Chosen for performance + Claude Code orchestration; the deprivileged user + systemd sandboxing carry the security weight instead.

## Install

Three steps. Phases 1 and 3 are scripts; phase 2 is interactive OAuth that David drives himself.

```bash
# Phase 1: deprivileged user + ACLs + Hermes reinstall under that user
sudo bash hermes-agent/setup-phase1.sh

# Phase 2: OAuth (interactive — opens browser flow, imports ~/.codex/auth.json)
sudo -u hermes -i
hermes setup       # accept Codex auth import; pick openai-codex
exit

# Phase 3: systemd unit + monitoring
sudo bash hermes-agent/setup-phase2.sh
```

After Phase 3, verify:

```bash
systemctl status hermes
curl http://127.0.0.1:8642/health
```

## File layout

```
hermes-agent/
├── README.md               # this file
├── setup-phase1.sh         # ✓ user/ACL/reinstall (run as root)
├── setup-phase2.sh         # ✓ systemd install + auditd + cron (run as root)
├── systemd/
│   └── hermes.service      # ✓ hardened unit file referenced by phase 2
└── monitoring/
    ├── auditd.rules        # ✓ audit tripwires
    └── hermes-watch.sh     # ✓ cron snapshotter (runs as dbexpertai)
```

## Runtime layout (on the Jetson, post-install)

```
/home/hermes/
├── .hermes/                    # Hermes state (config, memory, sessions, skills)
│   ├── config.yaml
│   ├── .env                    # secrets (Codex token via OAuth import)
│   ├── memories/MEMORY.md      # agent's persistent notes
│   ├── memories/USER.md        # David's profile (agent-managed)
│   ├── sessions/               # chat history
│   ├── logs/
│   └── hermes-agent/           # the actual code (cloned by installer)
├── .codex/auth.json            # copied from David's during Phase 1
├── .claude/credentials.json    # bind-mounted read-only from David's during Phase 3
└── .ssh/                       # generated fresh in Phase 3 (deploy keys for repos)

/etc/systemd/system/hermes.service          # the unit
/etc/audit/rules.d/50-hermes.rules          # tripwires
/var/log/audit/audit.log                    # auditd output (root-only)
/var/log/hermes-watch/                      # cron snapshotter output (root-only)
/etc/cron.d/hermes-watch                    # snapshotter schedule
```

## Project context (memory pointers)

- `project_agents_hermes_vs_openclaw.md` — why Hermes is personal vs. OpenClaw being for Winnow; coexistence matrix
- `project_remote_voice_bridge.md` — narration via Jetson → Mac (separate stack but adjacent)
- `project_tailnet_targets.md` — Mac is `100.82.188.1`; voice listener at `:18082`

All live in `~/.claude/projects/-home-dbexpertai-code-jetsonlocalai/memory/`.

## Reinstall from scratch

Everything in this directory is reproducible. To rebuild from zero:

1. `sudo userdel -r hermes` (wipes /home/hermes)
2. `sudo bash hermes-agent/setup-phase1.sh`
3. (interactive OAuth as above)
4. `sudo bash hermes-agent/setup-phase2.sh`

The only thing not captured here is the **content of David's Codex OAuth token** (sourced live from `~/dbexpertai/.codex/auth.json` at install time) and the **Claude credentials** (bind-mounted from David's `~/.claude/credentials.json` at runtime).
