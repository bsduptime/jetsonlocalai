# Windows deploy brief — familycal on Tal's mini PC (for the on-box Claude)

You are running on Tal's Windows mini PC. Hermes + Claude + Tailscale are already
installed; Telegram is the channel. Goal: give Hermes the `familycal` calendar tool
(create events + invite/inform people), talking to a local `hermes_calendar` relay.
Start in **dry-run** (writes nothing), prove it end-to-end from Telegram, then go live
with Google.

## What's already portable (done in the code — don't re-solve)
- The Unix-only `grp` import is guarded (no-op off Linux).
- The relay↔plugin link supports **localhost TCP** — use it on Windows instead of a Unix
  socket: `HERMES_CALENDAR_SOCKET=tcp://127.0.0.1:8765` (pick any free port).
- All paths are env-driven (config.py), so point them at Windows dirs.

## There is NO systemd / POSIX groups / ACLs here
Run the relay as a plain background process (Task Scheduler at logon, NSSM service, or a
`pythonw` launcher). Token isolation via Unix groups doesn't apply — rely on the box being
single-household + Windows Firewall (bind 127.0.0.1 only, which the code does).

---

## Steps

### 1. Config dir + files
```
mkdir C:\ProgramData\hermes-calendar\state
copy calendar-relay\.env.example        C:\ProgramData\hermes-calendar\.env
copy calendar-relay\contacts.example.yaml C:\ProgramData\hermes-calendar\contacts.yaml
```
Edit `C:\ProgramData\hermes-calendar\.env`:
```
CALENDAR_DRY_RUN=true
CALENDAR_TZ=Asia/Jerusalem
HERMES_CALENDAR_SOCKET=tcp://127.0.0.1:8765
CALENDAR_CONTACTS_FILE=C:\ProgramData\hermes-calendar\contacts.yaml
CALENDAR_STATE_DIR=C:\ProgramData\hermes-calendar\state
```
Put Tal, Ellie, kids, any caregiver in `contacts.yaml` (real Telegram/emails).
(No PyYAML? config.py also accepts a `.json` contacts file — use that + point
CALENDAR_CONTACTS_FILE at it.)

### 2. Deps
```
python -m pip install pyyaml            # for contacts.yaml (skip if using JSON)
# google libs only when going live: python -m pip install -r calendar-relay\requirements.txt
```

### 3. Run the relay (background)
Set `PYTHONPATH` to the `calendar-relay` dir and load the .env vars, then:
`python -m hermes_calendar.server`. Wrap that in a `.bat`/Task so it survives reboot.
Confirm it prints `hermes-calendar [DRY-RUN] listening on tcp://127.0.0.1:8765`.

### 4. Install + ENABLE the plugin in Hermes
- Find Hermes' plugin dir on Windows (check the Hermes config / `hermes --help`).
- **Copy** `plugins\familycal\` into it (Windows symlinks need admin/dev-mode; copy is
  simpler). Re-copy whenever the plugin code changes.
- Make sure the Hermes process sees `HERMES_CALENDAR_SOCKET=tcp://127.0.0.1:8765` (set it
  as a machine/user env var so Hermes inherits it — the plugin's client reads it).
- **Enable it** (this is the step that bit us on the Jetson — a symlinked/copied plugin is
  only *discovered*, not on):
  ```
  hermes plugins enable familycal
  ```
  then restart Hermes. Verify: `hermes tools list` shows `🔌 Familycal`
  (`create_event`, `list_contacts`).

### 5. Test from Telegram
Ensure Hermes' Telegram channel is live (bot token set, bot in the *Family HQ* group with
**privacy mode off** or as admin so it reads messages). In a **fresh** Telegram message
(tools register per session), send:
> put climbing with Elon in our family calendar tomorrow 11am, notify him, let Lihi know she won't come but should know

Expect a dry-run reply: event on Family, Elon invited, Lihi optional.

### 6. Go live with Google (browser works on Windows)
```
python calendar-relay\setup-google-auth.py            # opens browser -> approve
python calendar-relay\setup-google-auth.py --list-calendars   # copy the Family id
```
Then in `.env`: `CALENDAR_DRY_RUN=false`, set `CALENDAR_GOOGLE_CREDENTIALS`,
`CALENDAR_GOOGLE_TOKEN`, `CALENDAR_FAMILY_ID`, and restart the relay. Share the family's
Google *Family* calendar with the account you authorized (or a dedicated agent account),
"Make changes to events."

## Gotchas (learned on the Jetson)
- **Enable, don't just install.** `hermes plugins enable familycal` + restart.
- **Fresh session** after enabling — tools register per Telegram session.
- **Env must reach BOTH** the relay process and the Hermes process.
- TCP relay: 127.0.0.1 only; confirm Windows Firewall doesn't expose 8765.

## Scope guard
This deploy is the **family/calendar** workstream — low-stakes, no patient data. **Do NOT**
wire Ellie's patient attendance/billing here; that's a separate PHI workstream that needs a
written data-handling agreement first (Israeli Protection of Privacy Law). Keep them apart.
