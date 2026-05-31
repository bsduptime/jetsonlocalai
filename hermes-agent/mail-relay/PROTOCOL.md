# hermes-mailer — protocol + design contract

A privilege-separated email broker. The daemon owns the credentials and the
state; callers (Elena's mailer plugin today, future Winnow agents tomorrow)
talk to it over a Unix domain socket and never see the API key.

## Why

The previous design (in-process plugin) had Elena's runtime hold the Resend
key. A prompt-injected Elena could read the `.env`, bypass the plugin, and
call Resend directly. The plugin's allowlist + rate limit became moot for
that traffic.

This design moves the credential **out of Elena's process**. She talks to a
socket; the daemon enforces policy and is the only thing that can touch
Resend. Resend itself does not support per-API-key daily caps — so the cap
has to be enforced in a process the caller cannot bypass.

## Trust boundary

```
┌──────────────────────────────────────┐   ┌────────────────────────────────┐
│ hermes user                          │   │ DynamicUser hermes-mailer      │
│ (Elena's runtime)                    │   │ (privileged broker)            │
│                                      │   │                                │
│   mailer plugin (handler.py)         │   │  daemon.py — UDS server        │
│   reads /tmp/... attachments         │   │  reads /etc/hermes-mailer/.env │
│   path-validates                     │   │  enforces:                     │
│   base64-encodes content              ──▶│    • allowlist[caller]         │
│   sends JSON over UDS                │   │    • rate-limit[caller, day]   │
│                                      │ ◀ │    • magic-byte attach check   │
│   does NOT have read access to       │   │    • header injection check    │
│   /etc/hermes-mailer/.env            │   │    • size caps                 │
│                                      │   │  calls Resend/SMTP             │
└──────────────────────────────────────┘   │  writes /var/lib/hermes-mailer/│
                                           │    ratelimit.db                │
                                           │    sent.log                    │
                                           └────────────────────────────────┘
```

Filesystem layout enforces the boundary:

- `/etc/hermes-mailer/.env` — mode 600 owned by hermes-mailer:hermes-mailer.
  ACL grants `dbexpertai` read+write (David edits without sudo). **No
  access for `hermes` user.**
- `/etc/hermes-mailer/allowlist.yaml` — same perms.
- `/var/lib/hermes-mailer/` — mode 700 hermes-mailer-only. Holds
  `ratelimit.db`, `sent.log`, `dryrun/*.eml`.
- `/run/hermes-mailer/sock` — UDS, mode 0660 owner=hermes-mailer
  group=hermes-mailer-clients. `hermes` user is a member of that group,
  so Elena can `connect()`. Anyone outside the group cannot.

The systemd unit uses `DynamicUser=yes`, so the `hermes-mailer` user is
transient (created by systemd each start) and has no login shell, no
home directory, no persistent UID — no way for an attacker to socially
engineer access as that user.

## Protocol

JSONL over a Unix stream socket. The client writes ONE JSON object
followed by `\n`, the daemon reads it, processes, and writes ONE JSON
response object followed by `\n`. Connections are short-lived
(one-request, one-response, close).

Why not HTTP/JSON-RPC: simpler, smaller surface, no protocol parser to
exploit, easy to test with `nc -U`. Why not a Python-pickled object:
pickle is unsafe across trust boundaries.

### Request envelope

```json
{
  "v": 1,
  "op": "send",
  "request_id": "<opaque-string-from-client>",
  "to": "alice@example.com",
  "subject": "...",
  "body": "...",
  "body_html": null,
  "attachments": [
    {
      "filename": "report.pdf",
      "content_b64": "JVBERi0xLjQK...",
      "claimed_mime": "application/pdf"
    }
  ]
}
```

- `v` (required, int) — protocol version. Daemon rejects unknown versions.
- `op` (required, string) — operation. `"send"` (send an email) and
  `"contacts"` (read the caller's contact directory) are implemented.
  Future: `"quota"` (read remaining-today only), `"verify"` (dry-validate
  an envelope). A `contacts` request carries only `v`, `op`, and
  `request_id` — no other fields.
- `request_id` (required, string, ≤64 chars [a-zA-Z0-9_-]) — for log
  correlation. Daemon echoes it in the response.
- `to`, `subject`, `body`, `body_html`, `attachments` — same semantics
  as the original `send_email` tool.

### `caller` is derived from SO_PEERCRED, NOT from the request

The daemon reads the connecting process's UID via `SO_PEERCRED`. It
resolves UID → username → caller identity. This makes spoofing
impossible (the kernel attests to the connecting UID).

Mapping (initial):

| UID/username   | Caller identity |
|----------------|-----------------|
| `hermes`       | `elena`         |
| anything else  | rejected — "unknown_caller" |

Future Winnow agents would each have their own dedicated UID; the table
just grows. Multi-tenant scoping is then a config edit, not a code
change.

### `op = "contacts"`

A read-only directory lookup. No send, no rate-limit reservation — the
allowlist is still the hard gate on `send`, so this is pure convenience for
resolving a human name/alias to an allowlisted address.

Request:
```json
{"v": 1, "op": "contacts", "request_id": "..."}
```

Response (`remaining_today` is `null` if the rate-limit DB couldn't be
read; entries are sorted by `email`):
```json
{
  "v": 1,
  "request_id": "...",
  "ok": true,
  "contacts": [
    {
      "email": "yoram@dbexpert.ai",
      "name": "Yoram",
      "aliases": ["yoram"],
      "note": "co-founder dbexpert.ai",
      "daily_limit": 5,
      "remaining_today": 5
    }
  ],
  "resets_at": "2026-05-28T00:00:00+02:00"
}
```

The directory is derived from the same per-caller allowlist `send` uses, so
it can never list a recipient `send` would reject. It exposes no secrets —
only the operator's own contact handles.

### Response envelope

Success:
```json
{
  "v": 1,
  "request_id": "...",
  "ok": true,
  "status": "sent",
  "to": "alice@example.com",
  "message_id": "re_xxx",
  "remaining_today": 4,
  "limit": 5,
  "resets_at": "2026-05-28T00:00:00+02:00"
}
```

Not-allowed:
```json
{"v": 1, "request_id":"...", "ok": false,
 "error": "not_allowed",
 "reason": "not_in_allowlist" | "rate_limit_exceeded",
 "to": "..." }
```

Invalid input:
```json
{"v": 1, "request_id":"...", "ok": false,
 "error": "invalid_input",
 "reason": "<machine-token>", "detail": "<safe-token>"}
```

Transport failure:
```json
{"v": 1, "request_id":"...", "ok": false,
 "error": "transport_failed",
 "reason": "pre_send" | "post_send_unknown", "to": "..."}
```

Protocol error:
```json
{"v": 1, "request_id": "...", "ok": false,
 "error": "protocol",
 "reason": "unknown_op" | "version_mismatch" | "request_too_large" | "malformed_json" |
           "unknown_caller" | "missing_field",
 "detail": "..."}
```

### Resource limits

- Per-request max bytes: **30 MiB** (covers 25 MiB total attachments with
  base64 overhead + envelope). Read with a hard cap; longer is dropped
  with `request_too_large`.
- Per-connection deadline: **60 s** (covers slow Resend tail latency).
- Concurrent connections: capped at **8** to bound memory.

## State partitioning by caller (future-proofing)

All state is keyed by `caller`. Today only `caller="elena"` exists, but
the schemas already have the column:

```sql
CREATE TABLE sends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    caller          TEXT    NOT NULL,
    recipient       TEXT    NOT NULL,
    -- ... rest unchanged from the plugin's ratelimit.py
);
CREATE INDEX idx_sends_caller_recipient_day
    ON sends(caller, recipient, local_day, status);
```

Allowlist file layout future-proofed:

```
/etc/hermes-mailer/
  .env                      # transport + Resend key
  allowlist.yaml            # convenience: single-file form for the single-caller case
  allowlists/               # multi-caller form (used if /etc/hermes-mailer/allowlist.yaml absent)
    elena.yaml
    winnow-agent.yaml
    ...
```

Today's loader: read `allowlist.yaml`, treat its `contacts:` as
`caller=elena`. Future loader: read `allowlists/<caller>.yaml`. The
schema of each file is unchanged.

Audit log records `caller` on every row. Today every row has `elena`.
Tomorrow they vary.

## What stays in the client (Elena's process)

The client owns the trust boundary on the **path side**:

- Path validation: must be absolute, must resolve under
  `EMAIL_ATTACHMENT_ALLOWED_PREFIXES` (default `/tmp/`), must be a
  regular file. The daemon never sees a path.
- Open-once, fstat (`S_ISREG`), read-exact-size, never grows.
- Base64-encode and ship.

This split means:
- The daemon doesn't need filesystem read access into Elena's territory.
  Stronger isolation.
- A compromised client can't trick the daemon into reading
  `/etc/hermes-mailer/.env` as an "attachment" — because the daemon
  receives bytes, never a path.
- The daemon still does content-level validation: magic bytes, size,
  extension/mime match. So a lying client (one that bypassed the
  client-side path check) cannot smuggle e.g. an `.exe` past the daemon
  by claiming `content_b64` is "totally just a PDF, trust me bro."

## Failure modes worth calling out

| Failure | Consequence | Mitigation |
|---|---|---|
| Daemon dead | Client's send returns `transport_failed`/`daemon_unreachable` | systemd restart on failure (Restart=on-failure, RestartSec=10) |
| Daemon hung on a slow Resend response | New connections queue; per-connection deadline kicks in at 60s | Concurrent-connection cap prevents fan-out |
| Client OOMs on a 25 MB base64 attachment | Per-attachment cap is 10 MiB anyway; total cap 25 MiB; daemon enforces both | — |
| systemd kills daemon mid-send | Reservation row sits as `reserved` in the DB; reaper on next start reclassifies to `unknown_post_send` (counts against quota — conservative) | Same pattern as plugin v2; tested |
| Caller is hermes but Elena got compromised | Elena can call the socket but cannot exceed her quota, cannot send to non-allowlisted recipients, cannot read the key | This is the whole point |

## What can still go wrong (honest limits)

- **A leaked Resend key (via the daemon side)** still allows unlimited
  sends FROM the daemon. The daemon process MUST be hardened (and is,
  via systemd ProtectSystem/NoNewPrivileges/PrivateNetwork=no/etc.).
- **Resend account compromise** is out of scope. Use 2FA on the Resend
  dashboard.
- **Resend-side rate limit ≠ our per-caller cap.** Multiple callers can
  collectively exceed Resend's per-account limits. With one caller
  (Elena, 20/day), this is fine. With many callers, raise the Resend
  plan or queue.
