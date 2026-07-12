# Hermes multi-agent deployment on a hardened Windows box (client runbook)

Hard-won from the Fligelman-Neuman deploy (2026-07-12). This is the reusable
playbook for standing up a family/business Hermes deployment on a client's
**native Windows** mini PC (not WSL), reachable via Telegram. Read this before
the next one — most of it is non-obvious and cost hours live.

The architecture we deploy: **one Hermes instance per trust boundary** (family
assistant, business secretary, etc.), each a separate Windows account + bot +
allowlist; privilege-separated **broker/relay** services hold the real
credentials (Google, GreenInvoice); a stdlib **board bot** gives tappable lists.
See `winnow/README.md` and the client's `AGENT-STRUCTURE.md` for the topology.

---

## The three walls of a hardened Windows box (know these first)

1. **The `hermes` standard user is heavily locked down.** It CANNOT use the
   `ScheduledTasks` PowerShell cmdlets (CIM/WMI access denied) or `schtasks`
   to create tasks. It can run processes and write to its own dirs + granted
   ProgramData dirs. So: **the agent user cannot install its own services.**

2. **Processes spawned over SSH die when the SSH session closes.** Windows
   OpenSSH kills the child process tree. `Start-Process`, `cmd /c start /b`,
   detach tricks — all still die. **Anything long-running MUST be a scheduled
   task**, not an SSH-spawned process. (This wasted the most time — symptoms
   look like random crashes / "service unreachable".)

3. **Even an elevated admin cannot create a task that RUNS AS `hermes` via
   S4U** (needs SeTcbPrivilege, not granted to Administrators). Two ways around:
   - **SYSTEM tasks** — an elevated admin CAN create these. SYSTEM can read the
     hermes profile, bind localhost, and reach the internet. Use for the
     relays/brokers/board (they hold no user-identity requirement).
   - **hermes-password tasks** — reset the hermes password as admin
     (`net user hermes <pass>`), then `schtasks /RU hermes /RP <pass>`. Use for
     the **gateway** (keep it running as the sandboxed hermes user, not SYSTEM).

### Remote access facts that make this possible
- **Admin over SSH is elevated** (`ssh -i user_key user@box` → high-integrity
  token; `net session` succeeds). So the admin key can create tasks, control
  SYSTEM tasks (Stop/Start-ScheduledTask), reset passwords, and read/write any
  file — everything the deploy needs, remotely. The hermes key is for
  running things *as* hermes (e.g. `hermes.exe setup`, plugin enable).
- SSH is FLAKY on these boxes and warns about post-quantum — filter that noise;
  retry on drop; keep commands idempotent.

---

## Durability model (what runs how)

| Service | Runs as | Mechanism | Why |
|---|---|---|---|
| Calendar relay(s) | SYSTEM | scheduled task, ONSTART | holds Google token; localhost TCP; no user identity needed |
| GreenInvoice broker | SYSTEM | scheduled task, ONSTART | holds API keys; localhost TCP |
| Household board (Lisa) | SYSTEM | scheduled task, ONSTART | Telegram out + shared state file |
| Hermes gateway (the agent) | **hermes** | scheduled task /RU hermes /RP, ONSTART | KEEP the agent sandboxed — never SYSTEM |

Task settings that matter: `ExecutionTimeLimit = PT0S` (no auto-kill),
`RestartCount 999 / RestartInterval 1min`, `StartWhenAvailable`. Every
long-running thing launches via a tiny `run-*.cmd` in `C:\ProgramData\...`
that sets its env then execs — because **Windows user/machine env-var scoping
does NOT reliably reach a task's process; put config in the launcher cmd or in
the Hermes `.env`, never in `setx`/User-scope env vars.**

> **Gateway must NOT run as SYSTEM.** The entire security pitch to the client is
> that the agent runs as a locked-down user. SYSTEM is the opposite. Pay the
> password-reset step to keep the gateway as hermes.

---

## Config lives in `.env`, not env vars

The familycal/greeninvoice plugins read `HERMES_CALENDAR_SOCKET` /
`HERMES_GREENINVOICE_SOCKET` / `HOUSEHOLD_STATE_DIR` from the process env. On
Windows, setting these as User/Machine env vars did NOT reach the running
gateway. **Put them in the Hermes `.env`** (`%LOCALAPPDATA%\hermes\.env`) — the
gateway loads `.env` at startup regardless of scoping. Restart the gateway
(task) after editing `.env`.

Loopback TCP (no AF_UNIX on Windows): `tcp://127.0.0.1:8765` (calendar),
`tcp://127.0.0.1:8766` (greeninvoice). Set per instance.

### Shared list state (Tony ↔ board sync gotcha)
The household plugin (in the gateway) and the board (separate process) MUST
point at the SAME dir or "add via chat" and "tap on board" desync silently.
Use a shared, cross-user-ACL'd dir: `C:\ProgramData\hermes-household`
(`icacls ... /grant hermes:(OI)(CI)M Users:(OI)(CI)M`). Set
`HOUSEHOLD_STATE_DIR` there in BOTH the gateway `.env` AND the board launcher.
Restart the gateway so its plugin actually switches (the default is
`%LOCALAPPDATA%\hermes\household`, which is NOT the board's dir).

---

## Telegram specifics that bite

- **Two bots per family**: the assistant (LLM) and the board (Lisa). Create
  both in one BotFather sitting. See `TELEGRAM-SETUP.md`.
- **Privacy mode**: assistant bot → **OFF** (must read group messages;
  `/setprivacy` → Disable, or `/mybots` → Bot Settings → Group Privacy).
  Board bot → OFF is fine too and gives bare `/list`/`/add` in groups; the board
  is **deaf by code** (only acts on `/list`, `/add`, taps — drops everything
  else), so privacy-off leaks nothing behaviorally. Purist alternative: leave it
  ON and use `/list@BoardBot`.
- **A privacy change only applies to a group after the bot is removed+re-added
  or promoted to admin.** Promote the board to admin with ONLY "Pin messages"
  — this both applies privacy-off AND lets it pin the board. Two birds.
- **Command collision**: both bots hear a bare `/list`; the assistant replies
  "unknown command". Mitigate by using the pinned board (rarely type `/list`)
  and natural language for adds ("add milk"), or `/list@BoardBot`.
- **Verify from your side without touching the box**:
  - `getMe` → `can_read_all_group_messages` confirms the privacy toggle.
  - `getUpdates?timeout=0` returning **409 conflict** = a gateway IS polling
    (good). But do NOT poll repeatedly — your calls conflict with the board's
    own long-poll and cause its "terminated by other getUpdates" errors.

---

## Provider auth (ChatGPT subscription via openai-codex)

- Run `hermes setup` **as hermes** (`ssh -i hermes_key hermes@localhost` from
  the box, or on-console). Choose **openai-codex** ("Sign in with ChatGPT") —
  draws the subscription; NEVER an API key (metered), NEVER a Claude sub.
- SSH session can't pop a browser → it prints the auth URL. Open it in the
  box's desktop browser; the localhost callback lands on the box. The auth URL
  + local callback server are a matched pair per run — if the page 404s/refuses,
  the script had exited; rerun and use the FRESH url.
- **Blank-slate the toolset** (`hermes tools`): DISABLE `terminal`, `file`,
  `code_execution`, `computer_use`; ENABLE only `memory`, `clarify`, the
  plugins, and `web` if the client asks. This is the prompt-injection firewall
  and the selling point — verify with `hermes tools list`.
- **Billing check (Windows gotcha)**: after a few messages confirm at
  platform.openai.com/usage , /api-keys , /settings/organization/billing that
  NO api key was auto-created and NO per-token charges appear. The sub lives at
  chatgpt.com, separate.

---

## Google Calendar go-live

- `setup-google-auth.py` reads env vars (NOT flags):
  `CALENDAR_GOOGLE_CREDENTIALS` (desktop-app client json),
  `CALENDAR_GOOGLE_TOKEN` (output). Run as hermes; browser consent as the
  account that owns the family calendar.
- OAuth consent screen must be **Testing** with the signing-in account added as
  a **Test user**, else "app not verified / access blocked". (Consumer Gmail
  testing-mode refresh tokens expire ~7 days — token-longevity follow-up:
  Workspace internal app, or publish.)
- **Binding to the RIGHT calendar** is independent of who built the app: it's
  `CALENDAR_FAMILY_ID=<calendar id>` in the relay env + the calendar shared to
  the authed account as "Make changes to events". `--list-calendars` prints IDs;
  pick the `*writable*` family one. The relay only ever writes that ID.
- The relay loads contacts + calendar id **at startup**; to change either,
  stop the relay task, edit, start it (admin controls the SYSTEM task remotely).

## contacts.json — never ship placeholders
The relay resolves "invite Ellie / tell mom" via `contacts.json`
(name/email/aliases/default_role). **Ship a per-client file** — a leftover
staging file leaked another family's names onto the client box. Youngest kids
with no email: include by name with `"email": null` so the name resolves.

---

## Verification cheatsheet (all remote, no box visit)
- Relay up: `Test-NetConnection 127.0.0.1 -Port <p> -InformationLevel Quiet`.
- Gateway polling: Telegram `getUpdates?timeout=0` → 409 (once; don't spam).
- Calendar actually wrote: query Google API directly with the token for
  upcoming events on the family calendar id.
- Bot privacy: `getMe` → `can_read_all_group_messages`.
- Board alive: `C:\ProgramData\hermes-household\board.log` shows
  `household-board up: N allowed chat(s)` and no repeating conflict.

## Security TODO on every box
- The `hermes_key` (and any admin key) left on the box = standing remote-support
  access. Decide WITH the client: keep (documented) for support, or remove for
  strict posture. Record the hermes account password you set for the gateway
  task somewhere the client controls.
