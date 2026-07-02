# Deploying the calendar tool onto Elena (the Jetson)

Gives Elena `create_event` + `list_contacts`. Same isolation model as
hermes-mailer: a `hermes-calendar` relay daemon holds any credentials; Elena
reaches it over a group-gated UDS and never sees the Google token.

**Dry-run by default** — perfect for the demo (Elena parses your sentence into a
real structured event + participant/FYI split; nothing is written to a real
calendar). Going live with Google is the last section.

---

## 0. Gate checks (do these FIRST — they decide if the demo is viable)
1. **Can you talk to Elena right now?** Send her a message on Telegram ("hi").
   If she replies, her channel + model auth are good → proceed. If not, fix that
   first (a plugin install won't help if you can't reach her).
2. Confirm the service user exists: `id hermes` and `systemctl is-active hermes`.

## 1. Get the code onto the Jetson
Either it was rsynced to `~/code/jetsonlocalai/hermes-agent/{plugins/familycal,
calendar-relay}`, or pull it. Verify:
```bash
ls ~/code/jetsonlocalai/hermes-agent/plugins/familycal/plugin.yaml
ls ~/code/jetsonlocalai/hermes-agent/calendar-relay/setup-hermes-calendar.sh
```

## 2. Install the relay (sudo — needs your password)
```bash
cd ~/code/jetsonlocalai
sudo bash hermes-agent/calendar-relay/setup-hermes-calendar.sh
# -> installs deps, copies the package to /usr/local/lib, creates the
#    clients/config groups, seeds /etc/hermes-calendar, starts the service.
systemctl is-active hermes-calendar          # expect: active
ls -l /run/hermes-calendar/sock              # group should be hermes-calendar-clients
```

## 3. Install the plugin into Elena + restart her (sudo)
```bash
sudo bash hermes-agent/install-calendar-plugin.sh
sudo systemctl restart hermes
sudo -u hermes -i hermes tools list          # expect: create_event, list_contacts
```

## 4. Put in your real people (no sudo — ACL lets dbexpertai edit)
```bash
$EDITOR /etc/hermes-calendar/contacts.yaml   # real emails for Lihi, Elon, kids
```

## 5. Demo — talk to Elena
On Telegram, send her something like:
> put climbing with Elon in our family calendar for tomorrow at 11am and notify
> him. also let Lihi know — she won't come but she should know.

She calls `create_event`; the relay (dry-run) replies with the plan: event on the
Family calendar, Elon invited (participant), Lihi informed (optional attendee).

### Watch it work / troubleshoot
```bash
journalctl -u hermes-calendar -f            # relay activity + dry-run dumps
sudo -u hermes -i hermes tools list         # is the tool registered?
cat /var/lib/hermes-calendar/events.log     # audit line per event (may need sudo)
```
- **Elena says she has no such tool** → she wasn't restarted, or the plugin
  symlink is missing (`ls ~hermes/.hermes/plugins/familycal`).
- **daemon_unreachable** → relay not running / socket group wrong. Check
  `systemctl status hermes-calendar` and that `hermes` is in
  `hermes-calendar-clients` (`id hermes`), then `sudo systemctl restart hermes`.

---

## Going live (real Google writes + invites)
Do this once the dry-run demo works. The OAuth *client secret* is read-only in
`/etc`; the auto-refreshed *user token* lives in the writable state dir so the
daemon can rotate it — and it stays unreadable to Elena (state dir is owned by
the relay's dynamic user, and Elena isn't in the config group).

```bash
# 1. libs + client secret
sudo bash hermes-agent/calendar-relay/setup-hermes-calendar.sh --live
sudo install -m 640 -o root -g hermes-calendar-config \
     ~/secrets/gcal-oauth-client.json /etc/hermes-calendar/gcal-oauth-client.json

# 2. bootstrap the user token INTO the state dir, as root, once:
sudo CALENDAR_GOOGLE_CREDENTIALS=/etc/hermes-calendar/gcal-oauth-client.json \
     CALENDAR_GOOGLE_TOKEN=/var/lib/hermes-calendar/gcal-token.json \
     python3 hermes-agent/calendar-relay/setup-google-auth.py
sudo CALENDAR_GOOGLE_CREDENTIALS=/etc/hermes-calendar/gcal-oauth-client.json \
     CALENDAR_GOOGLE_TOKEN=/var/lib/hermes-calendar/gcal-token.json \
     python3 hermes-agent/calendar-relay/setup-google-auth.py --list-calendars  # get the Family id

# 3. flip live in /etc/hermes-calendar/.env:
#      CALENDAR_DRY_RUN=false
#      CALENDAR_GOOGLE_CREDENTIALS=/etc/hermes-calendar/gcal-oauth-client.json
#      CALENDAR_GOOGLE_TOKEN=/var/lib/hermes-calendar/gcal-token.json
#      CALENDAR_FAMILY_ID=...@group.calendar.google.com
sudo systemctl restart hermes-calendar
```
(The Jetson is headless — `run_local_server` prints a URL to open in a browser on
another machine on the same network, or use SSH port-forwarding for the callback.)

## Why this is safe to tell a doctor
The agent never holds the credential. Compromise Elena via prompt injection and
she still can't read the Google token — she can only *ask* the broker to create
an event, and the broker enforces contacts + a daily cap. Same pattern that keeps
patient data safe in `cliniclocalai`.
