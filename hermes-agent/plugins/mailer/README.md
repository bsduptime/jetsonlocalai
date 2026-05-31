# mailer — Hermes email plugin

`send_email` tool for the Hermes Agent (Elena). Sends email to a
**pre-approved allowlist** of contacts only, with a **per-recipient daily
rate limit**, magic-byte attachment validation, and header-injection
defenses.

**This plugin is a thin client.** As of the privilege-separation refactor,
none of the load-bearing security policy runs in Elena's process anymore.
The plugin opens a Unix socket to the **`hermes-mailer` daemon**
(`../../mail-relay/`), ships a JSON envelope, and returns the daemon's
response. The daemon — running as a transient systemd `DynamicUser` — owns
the credentials and enforces allowlist, rate limit, attachment content
validation, and header sanitization. Elena never sees the Resend/SMTP key.

See [`../../mail-relay/README.md`](../../mail-relay/README.md) for the
daemon, and [`../../mail-relay/PROTOCOL.md`](../../mail-relay/PROTOCOL.md)
for the wire protocol, trust boundary, and threat model.

## Architecture

```
┌─────────────────────────────┐         ┌──────────────────────────────┐
│ hermes user (Elena)         │   UDS   │ DynamicUser hermes-mailer     │
│                             │  JSONL  │ (privileged broker)           │
│  plugins/mailer (this pkg)  │ ──────▶ │  • allowlist[caller]          │
│   • validate field types    │         │  • rate-limit[caller, day]    │
│   • read + path-validate     │ ◀────── │  • magic-byte attach check    │
│     /tmp attachments         │         │  • header-injection check     │
│   • base64-encode bytes      │         │  • size caps                  │
│   • ship JSON over socket    │         │  • Resend / SMTP transport    │
│  NO access to the API key   │         │  reads /etc/hermes-mailer/.env │
└─────────────────────────────┘         └──────────────────────────────┘
```

The trust split: the **client** owns the *path* side of attachments
(resolve under an allowed prefix, `S_ISREG`, open-once, read-exact-size)
and ships only bytes — the daemon never sees a filesystem path. The
**daemon** owns the *content* side (magic bytes, size caps, extension/MIME
match) plus everything the model must not be able to bypass.

## Why a plugin (client) instead of an MCP server

A same-process tool call is cheaper than a network round-trip and reuses
Hermes' plugin contract directly. The security trade-off of running in
Elena's interpreter is resolved by moving the secret and the policy out to
the daemon: even a fully prompt-injected Elena can only do what the daemon
permits.

## Threat model in one paragraph

The realistic adversary is **prompt injection** flowing in through any
input Hermes ingests (chat channels, scraped pages, incoming mail). A
compromised agent could try to spam recipients, exfiltrate files as
attachments, inject SMTP headers, or smuggle a polyglot file. Because the
allowlist, per-recipient daily cap, attachment content validation, and
header sanitization all live in the daemon — a process Elena cannot edit at
runtime and whose credentials she cannot read — none of those attacks
succeed even if the model is hijacked. The full threat model lives in
[`PROTOCOL.md`](../../mail-relay/PROTOCOL.md).

## Install

The daemon and the plugin are installed together by the daemon's setup
script (it installs the systemd unit, seeds `/etc/hermes-mailer/`, creates
the socket group, and links this plugin into Hermes' plugin dir):

```bash
sudo bash hermes-agent/mail-relay/setup-hermes-mailer.sh
```

Then reload Hermes so the tool is registered:

```bash
sudo systemctl restart hermes
sudo -u hermes -i hermes plugins list   # should show: mailer
sudo -u hermes -i hermes tools list     # should show: send_email
```

> **Note:** the older `hermes-agent/install-email-plugin.sh` predates the
> privilege-separation refactor — it seeds an in-process config tree under
> `~hermes/.hermes/email-plugin/` that this thin-client plugin no longer
> reads. Use `setup-hermes-mailer.sh` instead.

## Configure

There are two config surfaces, on opposite sides of the trust boundary.

### Daemon side — `/etc/hermes-mailer/` (the daemon reads these; Elena cannot)

| Path                              | Mode | Purpose                                           |
|-----------------------------------|------|---------------------------------------------------|
| `/etc/hermes-mailer/.env`         | 600  | Transport selection + Resend/SMTP credentials     |
| `/etc/hermes-mailer/allowlist.yaml` | 600 | Per-recipient daily limits — edit to add contacts |
| `/var/lib/hermes-mailer/ratelimit.db` | —  | SQLite rate-limit ledger (daemon-only)            |
| `/var/lib/hermes-mailer/sent.log` | —    | JSONL audit log (daemon-only)                     |
| `/run/hermes-mailer/sock`         | 0660 | UDS; group `hermes-mailer-clients` (Elena is in it) |

David's UID gets an ACL on `/etc/hermes-mailer/`, so editing `.env` and
`allowlist.yaml` does **not** need `sudo`. Transport keys (`.env` keys like
`EMAIL_TRANSPORT`, `EMAIL_DRY_RUN`, `EMAIL_FROM`, `RESEND_API_KEY`,
`SMTP_*`, `EMAIL_LIMIT_TZ`, the size caps, `EMAIL_RESERVATION_TTL_SECONDS`)
are documented in the daemon README.

Allowlist (`/etc/hermes-mailer/allowlist.yaml`) — changes picked up on the
**next request, no restart**:

```yaml
contacts:
  - email: alice@example.com
    daily_limit: 5
    name: Alice
    aliases: ["alice", "best friend"]
    note: "best friend"
  - email: bob@example.com
    daily_limit: 2
```

Validation rules: `email` must be a syntactically valid address;
`daily_limit` must be an integer in `[1, 100]`; duplicates rejected; lookup
is case-insensitive on the whole address. A parse error keeps serving the
last-known-good list (no deny-all storm mid-edit). For future multi-tenant
use, per-caller files at `/etc/hermes-mailer/allowlists/<caller>.yaml` take
precedence if present.

`name` and `aliases` are **optional** and exist only so the agent can
resolve a spoken handle to an address via `list_contacts` (below). Aliases
are lowercased; an alias may not collide with another contact's alias or
email. They are not a security boundary — the email is still the only thing
the daemon sends to or rate-limits.

### Client side — env vars in Elena's process (describe Elena's filesystem)

| Key                                  | Default  | Notes                                              |
|--------------------------------------|----------|----------------------------------------------------|
| `EMAIL_ATTACHMENT_ALLOWED_PREFIXES`  | `/tmp/`  | Colon-separated absolute prefixes attachments must resolve under. |
| `EMAIL_MAX_ATTACHMENT_BYTES`         | `10485760` | Per-file cap applied client-side before shipping (10 MiB). |
| `HERMES_MAILER_SOCKET`               | `/run/hermes-mailer/sock` | Daemon socket path.                   |

These live on the client because they describe where Elena may stage files,
not daemon policy. The daemon independently re-enforces its own caps.

## The `send_email` tool

Input fields (see `schemas.py`):

| Field        | Required | Notes                                                       |
|--------------|----------|-------------------------------------------------------------|
| `to`         | yes      | Bare recipient address (no display name). Must be on the allowlist. |
| `subject`    | yes      | No newlines. Max 200 chars.                                 |
| `body`       | yes      | Plain-text body.                                            |
| `body_html`  | no       | Optional HTML alternative; `body` is still the text/plain part. |
| `attachments`| no       | List of absolute paths under an allowed prefix. PDF/Markdown/image/audio/CSV, validated by magic bytes. |

Dry-run is the daemon's default — nothing actually leaves the host until the
operator sets `EMAIL_DRY_RUN=false` and a transport in
`/etc/hermes-mailer/.env`. In dry-run the daemon renders an `.eml` into its
state dir for inspection.

## The `list_contacts` tool

Takes no arguments. Returns the caller's contact directory so the agent can
resolve a name or alias to an address **before** calling `send_email` — it's
what makes "send this to Yoram" or "email it to me" work without the user
typing an address. Read-only: it sends nothing and reserves no quota.

```json
{"ok": true, "resets_at": "2026-05-28T00:00:00+02:00",
 "contacts": [
   {"email": "yoram@dbexpert.ai", "name": "Yoram", "aliases": ["yoram"],
    "note": "co-founder dbexpert.ai", "daily_limit": 5, "remaining_today": 5}
 ]}
```

The directory is derived from the same allowlist `send_email` enforces, so
it can never name a recipient that `send_email` would then reject.
`remaining_today` is `null` if the rate-limit DB couldn't be read. The
agent should match the user's wording against `name`/`aliases`/`email`, pass
the matched `email` to `send_email`, and — if nothing matches — say so
rather than guess an address.

## Response shapes

The handler always returns a JSON string and never raises. The daemon's
protocol-version key (`v`) is stripped before returning to the agent.

```json
// success
{"ok": true, "status": "sent",
 "to": "alice@example.com", "message_id": "…",
 "remaining_today": 4, "limit": 5,
 "resets_at": "2026-05-28T00:00:00+02:00"}

// not on allowlist
{"ok": false, "error": "not_allowed", "reason": "not_in_allowlist",
 "to": "evil@example.com"}

// per-day cap hit
{"ok": false, "error": "not_allowed", "reason": "rate_limit_exceeded",
 "to": "alice@example.com", "limit": 5, "sent_today": 5, "resets_at": "…"}

// bad input (path, magic bytes, header, missing field)
{"ok": false, "error": "invalid_input",
 "reason": "<machine-readable token>", "detail": "<safe context>"}

// transport problem after allowlist+rate-limit passed (daemon side)
{"ok": false, "error": "transport_failed",
 "reason": "pre_send" | "post_send_unknown", "to": "…"}

// daemon unreachable (socket missing / refused / timeout) — client side
{"ok": false, "error": "transport_failed",
 "reason": "daemon_unreachable", "detail": "<reason>: <socket path>"}
```

`reason` strings are stable machine tokens. `detail` is field names or fixed
tokens — never echoes attacker-supplied data back into the response.

## How the rate limit works (daemon side)

Fixed-window per local day, reservation pattern. A reservation older than
`EMAIL_RESERVATION_TTL_SECONDS` is reclassified to `unknown_post_send` so a
crash mid-send can't permanently DoS a recipient. Counting rules and the
full state table are documented in
[`PROTOCOL.md`](../../mail-relay/PROTOCOL.md) and the daemon README.

## How attachment validation works

Path side (this plugin, `_client.py`):

```
absolute path → Path.resolve(strict=True) → must start with allowed prefix
             → lstat S_ISREG (no FIFOs, devices, sockets)
             → open O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC
             → fstat re-confirms S_ISREG
             → read exactly st.st_size bytes; one more read must return 0
             → base64-encode and ship bytes (never the path)
```

Content side (daemon): magic bytes match the claimed extension, per-file and
per-email size caps. A client that lied about a path still can't smuggle a
disallowed type past the daemon, because the daemon validates the bytes.

## Audit log

`/var/lib/hermes-mailer/sent.log` is JSONL, written by the daemon. Each line
records timestamp, caller, event, outcome, recipient, truncated subject (80
chars), attachment basenames (no full paths), byte count, message id, and
transport. **Bodies are never logged.** Readable only by the daemon user and
root.

## Tests

```bash
# client shim (this package)
cd hermes-agent/plugins/mailer && python3 -m pytest tests/

# daemon policy (allowlist, rate-limit, attachments, protocol, transports)
cd hermes-agent/mail-relay && python3 -m pytest tests/
```

The plugin's tests cover the thin-client behavior: field-type
pre-validation, client-side attachment path validation, envelope framing,
and the `daemon_unreachable` path. The policy-enforcement tests
(allowlist, rate-limit, magic-byte, header injection, transports) live with
the daemon under `mail-relay/tests/`.

## What's deliberately out of scope

- DKIM / SPF / DMARC — domain-side, not the broker's job.
- Bounce processing / reply handling.
- HTML sanitization — `body_html` is passed through as-is.
- Cross-day-boundary cap evasion — the fixed window honors each calendar
  day's cap independently.
