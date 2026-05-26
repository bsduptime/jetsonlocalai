# mailer — Hermes email plugin

`send_email` tool for the Hermes Agent. Sends email to a **pre-approved
allowlist** of contacts only, with a **per-recipient daily rate limit**
enforced by the plugin itself (not by trusting the model). Attachments are
restricted to PDF, Markdown, image, audio, and CSV — validated by magic
bytes, not just extension.

Dry-run is the default. Nothing goes out until you set
`EMAIL_DRY_RUN=false` and a transport.

## Why a plugin instead of an MCP server

Same-process call is cheaper, easier to harden (no separate network surface),
and lets us reuse Hermes' own filesystem sandbox. The trade-off is that the
plugin runs in the same Python interpreter as the agent — but everything
load-bearing for security (allowlist, rate limit, attachment validation) is
implemented as guard rails the model cannot edit at runtime.

## Threat model in one paragraph

The realistic adversary is **prompt injection** flowing in through any
input Hermes ingests (chat channels, scraped web pages, incoming emails).
A compromised agent could try to spam recipients, exfiltrate files as
attachments, inject SMTP headers, or sneak through a polyglot file. The
plugin counters each of those: hard allowlist, per-recipient daily cap,
absolute-path + magic-byte attachment validation under a tight allowed
prefix, CRLF/NUL header sanitization, and a structured audit log.

## Install

```bash
sudo bash hermes-agent/install-email-plugin.sh

# install runtime deps inside the hermes user's environment
sudo -u hermes -i pip install --user pyyaml
sudo -u hermes -i pip install --user resend     # only if EMAIL_TRANSPORT=resend
```

The installer creates `~hermes/.hermes/email-plugin/` with:

| Path                                              | Mode | Purpose                                            |
|---------------------------------------------------|------|----------------------------------------------------|
| `~hermes/.hermes/email-plugin/.env`               | 600  | Plugin-private secrets + transport selection       |
| `~hermes/.hermes/email-plugin/allowlist.yaml`     | 600  | Per-recipient daily limits — edit to add contacts  |
| `~hermes/.hermes/email-plugin/state/ratelimit.db` | 600  | SQLite rate-limit ledger                           |
| `~hermes/.hermes/email-plugin/state/sent.log`     | 600  | JSONL audit log                                    |
| `~hermes/.hermes/email-plugin/state/dryrun/*.eml` | 600  | Rendered messages in dry-run mode                  |
| `~hermes/.hermes/plugins/mailer`                  | link | Symlink into this repo                             |

David's UID gets an ACL on `email-plugin/` so editing `.env` and
`allowlist.yaml` does **not** need `sudo`. The state dir stays
`hermes`-only.

After install:

```bash
sudo systemctl restart hermes
sudo -u hermes -i hermes plugins list   # should show: mailer
sudo -u hermes -i hermes tools list     # should show: send_email
```

## Configure

Edit `~hermes/.hermes/email-plugin/.env`. The full set of keys, with
defaults:

| Key                                  | Default     | Notes                                                  |
|--------------------------------------|-------------|--------------------------------------------------------|
| `EMAIL_TRANSPORT`                    | `dry_run`   | `dry_run`, `resend`, or `smtp`.                        |
| `EMAIL_DRY_RUN`                      | `true`      | Safety belt — `true` forces dry-run regardless of transport. |
| `EMAIL_FROM`                         | (required when not dry-run) | RFC 5322 mailbox.                |
| `EMAIL_REPLY_TO`                     | unset       | Optional Reply-To.                                     |
| `EMAIL_LIMIT_TZ`                     | `local`     | `local` (system tz) or an IANA name like `Europe/Berlin`. |
| `EMAIL_MAX_ATTACHMENT_BYTES`         | `10485760`  | 10 MiB per file.                                       |
| `EMAIL_MAX_TOTAL_BYTES`              | `26214400`  | 25 MiB per email.                                      |
| `EMAIL_ATTACHMENT_ALLOWED_PREFIXES`  | `/tmp/`     | Colon-separated absolute prefixes for attachment paths. |
| `EMAIL_RESERVATION_TTL_SECONDS`      | `180`       | Stale-reservation reaper window.                       |
| `RESEND_API_KEY`                     | —           | From resend.com.                                       |
| `SMTP_HOST` / `SMTP_PORT`            | —           | `587` default.                                         |
| `SMTP_USERNAME` / `SMTP_PASSWORD`    | —           | App-password recommended for Gmail.                    |
| `SMTP_STARTTLS`                      | `true`      | Set false for implicit-TLS port 465.                   |

Edit `~hermes/.hermes/email-plugin/allowlist.yaml`. Changes are picked up
on the next tool call — no restart.

```yaml
contacts:
  - email: alice@example.com
    daily_limit: 5
    note: "best friend"
  - email: bob@example.com
    daily_limit: 2
```

Validation rules: `email` must be a syntactically valid address;
`daily_limit` must be an integer in `[1, 100]`; duplicates rejected;
lookup is case-insensitive on the whole address.

## Going live

1. Pick a transport and put credentials in `.env`. Smoke-test:
   ```bash
   sudo -u hermes -i python -c "
   from hermes_email_pkg.handler import send_email
   import json
   print(send_email({'to':'YOUR-OWN-ADDRESS','subject':'test','body':'hi'}))
   "
   ```
   In dry-run this writes a `.eml` to `state/dryrun/`; inspect it.

2. Flip `EMAIL_DRY_RUN=false` in `.env`.

3. Restart hermes if it had already loaded the config (`systemctl restart hermes`).

## Response shapes

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
 "to": "alice@example.com", "limit": 5, "sent_today": 5,
 "resets_at": "…"}

// bad input (path, magic, header, missing field)
{"ok": false, "error": "invalid_input",
 "reason": "<machine-readable token>", "detail": "<safe context>"}

// transport problem after allowlist+rate-limit passed
{"ok": false, "error": "transport_failed",
 "reason": "pre_send" | "post_send_unknown", "to": "…"}
```

`reason` strings are stable machine tokens. `detail` is field names or
fixed tokens — never echoes attacker-supplied data into the response.

## How the rate limit works

Fixed-window per local day. Reservation pattern with five statuses:

| Status            | Counts toward today's cap?                     |
|-------------------|-----------------------------------------------|
| `reserved`        | yes (while fresh)                              |
| `sent`            | yes                                            |
| `dry_run`         | no                                             |
| `failed_pre_send` | no — transport rejected before bytes left host |
| `unknown_post_send` | yes — transport call started, outcome unknown (conservative) |

A reservation older than `EMAIL_RESERVATION_TTL_SECONDS` is reclassified
to `unknown_post_send` on plugin load and before each new reservation,
so a crash-mid-send doesn't permanently DoS a recipient.

## How attachment validation works

```
absolute path  →  Path.resolve(strict=True)  →  must start with allowed prefix
              →  extension is on the allow list
              →  lstat says S_ISREG (no FIFOs, devices, sockets)
              →  open with O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC
              →  fstat re-confirms S_ISREG
              →  read exactly st.st_size bytes
              →  one final read returns 0 (file didn't grow)
              →  magic bytes match extension
              →  total across all attachments fits under EMAIL_MAX_TOTAL_BYTES
```

The agent must stage attachments under `/tmp/` (or the configured
prefix). Anything resolved outside is rejected.

## Audit log

`state/sent.log` is JSONL. Each line records: timestamp, event type,
outcome, recipient, truncated subject (80 chars), attachment basenames
(no full paths), byte count, message id, transport. **Bodies are never
logged.** File mode 600 in a 700 directory — only `hermes` and `root`
can read.

## Tests

```bash
cd hermes-agent/plugins/mailer
python3 -m pytest tests/
```

92 tests covering: dotenv parser, header validation (RFC + injection),
allowlist loading + cache-on-parse-fail, attachment validation
(magic-byte, FIFO rejection, symlink-escape, oversize, all 12 extensions),
rate-limit reservation + reap + concurrent races, all three transports
(DryRun real, Resend + SMTP mocked), and the handler end-to-end.

## What's deliberately out of scope

- DKIM / SPF / DMARC — domain-side, not plugin's job.
- Bounce processing / reply handling — not in scope.
- HTML sanitization — `body_html` is passed through as-is; the agent
  constructs it.
- Cross-day-boundary "evasion" of the cap — fixed window at reservation
  time means each calendar day's cap is honored independently. A burst
  straddling midnight counts each send against the day it was attempted.
