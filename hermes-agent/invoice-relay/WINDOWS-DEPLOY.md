# Windows deploy brief — greeninvoice broker (for the on-box Claude)

You are running on a Windows mini PC (native, not WSL). Hermes is installed under
`%LOCALAPPDATA%\hermes` with its own bundled Python. Goal: run the privilege-separated
GreenInvoice broker (`hermes_greeninvoice`) as one user, let Hermes' `greeninvoice`
plugin talk to it over **localhost TCP**, and keep the API credentials out of the
agent's reach. Start in **dry-run**, prove the round-trip, then add sandbox creds —
production only after David signs off.

## What's already portable (done in the code — don't re-solve)
- The daemon and both thin clients speak `tcp://127.0.0.1:<port>` (CPython on
  Windows has no AF_UNIX). Set `HERMES_GREENINVOICE_SOCKET=tcp://127.0.0.1:8766`.
- POSIX-only bits (`grp`, `chown`, peercred) are guarded; preview PDFs are written
  with `O_BINARY`.
- The TCP listener refuses non-loopback binds, and refuses **every** request until a
  caller token is configured — fail closed, because live invoicing creds sit behind it.

## Identity over TCP = shared token (SO_PEERCRED doesn't exist here)
- Daemon side (`.env`): `GI_CALLER_TOKEN_elena=<secret>` (>= 16 chars,
  `python -c "import secrets; print(secrets.token_hex(24))"`).
- Hermes side: env `HERMES_GREENINVOICE_TOKEN=<same secret>` for the plugin.
- One token **per agent**: each gets its own audit identity and its own rate-limit
  buckets. Never share one token across agents.

## Privilege separation on this box
Run the **daemon as `user`** (the admin account) and **Hermes as `hermes`** — same
split as the calendar relay. The `.env` (API key + tokens) lives under a directory
only `user` can read (`icacls` deny for `hermes`); the `hermes` account only ever
holds its own caller token, never the GreenInvoice key.

## Ellie's practice = its OWN instance (do not mix with family)
Ellie's client list is therapy-practice data. When her billing goes live, run a
**second daemon instance** — separate port, separate dirs, separate GreenInvoice
account creds, separate tokens:
```
HERMES_GREENINVOICE_CONFIG_DIR=C:\ProgramData\gi-ellie
HERMES_GREENINVOICE_STATE_DIR=C:\ProgramData\gi-ellie\state
HERMES_GREENINVOICE_SOCKET=tcp://127.0.0.1:8767
```
vs. the family/expenses instance on 8766 under `C:\ProgramData\gi-family`. The
audit logs, rate limits, previews, and credentials then never share a file. Ellie's
instance stays `GI_DRY_RUN=true` until the written data-handling agreement is signed.

## Steps (family/expenses instance)

### 1. Dirs + config (as `user`)
```
mkdir C:\ProgramData\gi-family\state
copy invoice-relay\.env.example C:\ProgramData\gi-family\.env
```
Edit `C:\ProgramData\gi-family\.env`:
```
GI_ENV=sandbox
GI_DRY_RUN=true
HERMES_GREENINVOICE_SOCKET=tcp://127.0.0.1:8766
GI_CALLER_TOKEN_elena=<generated secret>
```
Lock it down: `icacls C:\ProgramData\gi-family /inheritance:d /remove hermes` (then
grant `user` full control). Verify `hermes` cannot type the file.

### 2. Run the daemon (as `user`, Hermes' bundled Python)
```
set HERMES_GREENINVOICE_CONFIG_DIR=C:\ProgramData\gi-family
set HERMES_GREENINVOICE_STATE_DIR=C:\ProgramData\gi-family\state
C:\Users\hermes\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -m hermes_greeninvoice.daemon
```
(from the repo's `invoice-relay\` dir, or with it on PYTHONPATH). Expect the startup
log line `socket=tcp://127.0.0.1:8766 dry_run=True`.

### 3. Wire the plugin (as `hermes`)
Copy `plugins\greeninvoice` into `C:\Users\hermes\AppData\Local\hermes\hermes\plugins`,
set for the Hermes process:
```
HERMES_GREENINVOICE_SOCKET=tcp://127.0.0.1:8766
HERMES_GREENINVOICE_TOKEN=<the same secret>
```
then `hermes plugins enable greeninvoice`, restart Hermes, `hermes tools list`.

### 4. Prove it
- From Telegram: ask for a quota check → expect the dry-run quota reply.
- Draft an invoice → expect a preview marked dry-run; **no** real document.
- Tamper test: unset `HERMES_GREENINVOICE_TOKEN`, retry → must be refused
  (`unknown_caller`). Put it back.

### 5. Persistence
Task Scheduler, at-logon (or NSSM): one task for the daemon (as `user`), matching
whatever already keeps the calendar relay + Hermes alive. Same pattern.

### 6. Status file
Write findings/progress to `C:\Users\USER\Documents\GREENINVOICE-STAGING-STATUS.md`
as you go — that file is the handshake channel back to David's Mac.

## Go-live checklist (do NOT do this unilaterally)
1. Sandbox creds in `.env` → `GI_DRY_RUN=false` with `GI_ENV=sandbox` → issue a
   sandbox invoice end-to-end.
2. Production creds + `GI_ENV=production` only when David explicitly says so.
3. Ellie's instance additionally waits for the signed data-handling agreement.
