# hermes-calendar relay

The daemon behind the `familycal` Hermes plugin. Same shape as `mail-relay`:
the plugin (running inside the deprivileged agent) talks to this over a Unix
socket; **credentials live here, never in the agent.**

## What it does
Turns a `create_event` request into either:
- **dry-run** (default): renders exactly what it *would* create + who it *would*
  invite/inform, writing nothing and sending nothing; or
- **live** (`CALENDAR_DRY_RUN=false` + Google creds): a real Google Calendar
  event with real invites.

Participant vs. FYI semantics:
- `role=participant` → required attendee (normal invite).
- `role=fyi` → **optional** attendee — on their calendar, notified, but marked
  "not expected to attend" ("she won't come but should know").

## Layout
```
calendar-relay/
├── hermes_calendar/
│   ├── config.py      # env + contacts + calendars
│   ├── event.py       # datetime + attendee resolution -> a plan (pure)
│   ├── transport.py   # DryRunTransport (default) + GoogleTransport (live)
│   └── server.py      # UDS daemon + request dispatch (handle_request is unit-testable)
├── setup-google-auth.py   # one-time OAuth + `--list-calendars`
├── requirements.txt       # google libs (LIVE only; dry-run is stdlib-only)
├── .env.example
├── contacts.example.yaml
└── systemd/hermes-calendar.service
```

## Protocol (one JSON line per connection, over the UDS)
Request: `{"v":1,"op":"create_event","request_id":"…","title":…,"start":"YYYY-MM-DDTHH:MM",
"duration_minutes":60,"calendar":"family","attendees":[{"ref":"elon","role":"participant","notify":true}]}`
or `{"v":1,"op":"contacts","request_id":"…"}`.
Response: `{"v":1,"ok":true,"dry_run":true,"event":{…},"invited":[…],"informed":[…],
"unresolved":[…],"summary":"…","calendar_url":null,"event_id":null}` — or
`{"ok":false,"error":…,"reason":…}`. The handler NEVER raises; errors are JSON.

## Run standalone (dev)
```bash
CALENDAR_DRY_RUN=true HERMES_CALENDAR_SOCKET=/tmp/hcal.sock \
CALENDAR_CONTACTS_FILE=contacts.example.yaml \
python3 -m hermes_calendar.server
```

## Try it end-to-end without the Jetson
See `~/code/aiconsulting/demo/` — `calendar_demo.py` drives the real plugin
client + this relay over a real socket, and `SETUP-LIVE.md` is the go-live runbook.
