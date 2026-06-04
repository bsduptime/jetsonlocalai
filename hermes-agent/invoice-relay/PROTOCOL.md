# hermes-greeninvoice — protocol + design contract

A privilege-separated GreenInvoice (Morning) broker. The daemon owns the
API key and the rate-limit state; callers (Hermes today, future agents
tomorrow) talk to it over a Unix domain socket and never see the key.

## Why

If Hermes' runtime held the GreenInvoice API key, a prompt-injected Hermes
could read it from disk and call `api.greeninvoice.co.il` directly —
issuing unlimited real, irreversible tax documents. A plugin-level rate
limit becomes moot for that traffic.

This design moves the credential **out of Hermes' process**. She talks to
a socket; the daemon enforces policy and is the only thing that can touch
GreenInvoice. The per-action caps therefore live in a process the caller
cannot bypass. (GreenInvoice has no per-key daily issue cap of its own.)

## Trust boundary

```
┌──────────────────────────────────┐   ┌────────────────────────────────────┐
│ hermes user (Hermes runtime)     │   │ DynamicUser hermes-greeninvoice    │
│                                  │   │ (privileged broker)                │
│  greeninvoice plugin (handler)   │   │  daemon.py — UDS server            │
│  forwards args over UDS          ──▶│  reads /etc/hermes-greeninvoice/.env│
│                                  │ ◀ │  enforces:                         │
│  does NOT have read access to    │   │    • caller via SO_PEERCRED        │
│  /etc/hermes-greeninvoice/.env   │   │    • per-action rate limit         │
│                                  │   │    • issue confirmation gate       │
│                                  │   │    • input validation/build        │
│                                  │   │  holds + refreshes the JWT         │
│                                  │   │  calls GreenInvoice over HTTPS     │
│                                  │   │  writes /var/lib/.../ratelimit.db  │
└──────────────────────────────────┘   │              + audit.log           │
                                       └────────────────────────────────────┘
```

Filesystem layout enforces the boundary:

- `/etc/hermes-greeninvoice/.env` — mode 0640 root:hermes-greeninvoice-config.
  Daemon reads via that group. ACL grants `dbexpertai` rw (edit without
  sudo). **No access for `hermes`.**
- `/var/lib/hermes-greeninvoice/` — mode 700, daemon-only. `ratelimit.db`,
  `audit.log`.
- `/run/hermes-greeninvoice/sock` — UDS, mode 0660, group
  hermes-greeninvoice-clients. `hermes` is a member, so the plugin can
  `connect()`. Nobody else can.

`DynamicUser=yes` means the `hermes-greeninvoice` user is transient — no
persistent UID, no login shell, no home — so it can't be socially
engineered into.

## Protocol

JSONL over a Unix stream socket. The client writes ONE JSON object + `\n`;
the daemon reads it, processes, writes ONE JSON response + `\n`; closes.
One request per connection.

### Request envelope

```json
{ "v": 1, "op": "issue_invoice", "request_id": "<opaque>", "args": { ... } }
```

- `v` (required, int) — protocol version. Unknown versions rejected.
- `op` (required, string) — one of:
  `draft_invoice`, `issue_invoice`, `get_document`, `search_documents`,
  `document_download_links`, `create_client`, `update_client`,
  `get_client`, `search_clients`, `quota`.
- `request_id` (required, string, ≤64 chars) — echoed in the response.
- `args` (object) — op-specific; validated + rebuilt server-side. The
  daemon never forwards a caller-supplied raw GreenInvoice body; it builds
  the body from whitelisted fields so a caller can't smuggle a different
  document type or extra fields.

### `caller` is derived from SO_PEERCRED, NOT from the request

The daemon reads the connecting process's UID via `SO_PEERCRED` and maps
it to a caller identity. Spoofing is impossible — the kernel attests to
the UID. Default mapping: `hermes` → `elena`; everything else rejected as
`unknown_caller`. Add `CALLER_UID_<name>=<uid>` to grow the table.

All rate-limit state is keyed by `caller`, so a new caller cannot consume
another's quota and cannot mint itself a fresh one.

## Operations

| op | side effect | action class | rate-limited |
|----|-------------|--------------|--------------|
| `draft_invoice` | none (preview only) | draft | loose (20/h, 60/d) |
| `issue_invoice` | **creates a real document** | issue | tight (3/h, 10/d) + `confirm` |
| `get_document` | none | — | no |
| `search_documents` | none | — | no |
| `document_download_links` | none | — | no |
| `create_client` | creates a client | client_write | loose (20/h, 100/d) |
| `update_client` | updates a client | client_write | loose |
| `get_client` | none | — | no |
| `search_clients` | none | — | no |
| `quota` | none | — | no |

Document types: **305** tax invoice, **320** invoice+receipt, **400**
receipt — the only *issuable* types. Drafts may additionally preview
**300** proforma and **10** price quote. No credit notes, no deletes
through this broker.

### `issue_invoice` confirmation gate

`issue_invoice` is rejected with `not_allowed / confirmation_required`
unless `args.confirm === true`. Defense in depth: the model must take a
deliberate, explicit step to create an irreversible document, on top of
the rate limit and Hermes' own human-in-the-loop.

### Email distribution

`issue_invoice` distributes to the client only when `args.email_to_client
=== true` AND `args.client.emails` is non-empty. Otherwise the daemon
forces the document's client `emails` to `[]`, so no email goes out.
Drafts never email. "Send the draft to David" is **not** done here — the
daemon returns the preview artifact and Hermes delivers it via the mailer
or Telegram, as asked.

### Preview PDFs are spooled to a file, not returned inline

`/documents/preview` returns the rendered PDF as base64 in `result.file`.
Returning that over the socket would dump tens of KB of base64 into the
agent's context every draft. So the daemon decodes it, writes it to
`/run/hermes-greeninvoice/previews/<request_id>.pdf` (tmpfs, under the
RuntimeDirectory), chgrp's it to `hermes-greeninvoice-clients` mode 0640 so
the `hermes` user can read it, and replaces `result.file` with
`result.preview_pdf_path` + `result.preview_pdf_bytes`. The agent hands that
path to its delivery channel (mailer attachment / Telegram). Files are
ephemeral: pruned by age (`GI_PREVIEW_RETENTION_SECONDS`, default 1h) and
count (`GI_PREVIEW_MAX_FILES`, default 50), and cleared on reboot. To attach
a preview via the mailer, add `/run/hermes-greeninvoice/previews/` to that
plugin's `EMAIL_ATTACHMENT_ALLOWED_PREFIXES`.

### Response envelope

Success:
```json
{ "v":1, "request_id":"...", "ok":true, "op":"issue_invoice",
  "dry_run":false, "result": { ...GreenInvoice response... },
  "rate": { "action_class":"issue", "per_hour":3, "per_day":10,
            "used_hour":1, "used_day":1, "remaining_hour":2,
            "remaining_day":9, "resets_at":"2026-06-05T00:00:00+03:00" } }
```

Policy rejection:
```json
{ "v":1, "request_id":"...", "ok":false, "error":"not_allowed",
  "reason":"rate_limit_exceeded" | "confirmation_required",
  "op":"issue_invoice", "window":"hour", "limit":3, "used":3, "rate": {...} }
```

Invalid input:
```json
{ "v":1, "request_id":"...", "ok":false, "error":"invalid_input",
  "reason":"<machine-token>", "detail":"<field>", "op":"..." }
```

Upstream failure:
```json
{ "v":1, "request_id":"...", "ok":false, "error":"upstream_failed",
  "reason":"api_error" | "network_error", "status":4xx|5xx|null, "op":"..." }
```

Protocol error:
```json
{ "v":1, "request_id":"...", "ok":false, "error":"protocol",
  "reason":"unknown_op" | "version_mismatch" | "request_too_large" |
            "malformed_json" | "unknown_caller", "detail":"..." }
```

## Rate limiting

Per `(caller, action_class)`, two windows enforced together:

- **hourly** — rolling 3600s window (rows with `reserved_epoch > now-3600`).
- **daily** — calendar day in `GI_LIMIT_TZ` (rows with `local_day == today`).

A call is admitted only if BOTH are under cap. Same
reserve → (API call) → finalize pattern as hermes-mailer:

1. `BEGIN IMMEDIATE`, re-count under lock, insert a `reserved` row, commit.
2. Perform the GreenInvoice call.
3. Finalize: `committed` on success; `failed_pre_send` (frees the slot) on
   a clean 4xx; `unknown` (keeps the slot — conservative) on a network
   error or 5xx, where a document *might* have been created.

Counted statuses: `reserved`, `committed`, `unknown`. A crash between
reserve and finalize leaves a `reserved` row that the reaper reclassifies
to `unknown` after `GI_RESERVATION_TTL_SECONDS` — it keeps counting, so we
under-issue rather than over-issue. In **dry-run**, gated ops still reserve
+ finalize `committed`, so the limiter is exercised end-to-end with no creds.

### Resource limits

- Per-request max bytes: **1 MiB** (JSON only; no attachments).
- Per-connection deadline: **35 s** (above the 30 s upstream HTTP timeout).
- Concurrent connections: **4**.

## Failure modes

| Failure | Consequence | Mitigation |
|---|---|---|
| Daemon dead | plugin returns `transport_failed / daemon_unreachable` | systemd `Restart=on-failure` |
| Ambiguous upstream (network/5xx mid-issue) | reservation finalized `unknown` → counts against quota | conservative; David reconciles in dashboard |
| Crash between reserve+finalize | `reserved` row reaped to `unknown` | counts against quota |
| hermes compromised | can call the socket but can't exceed quota, can't issue without `confirm`, can't read the key | the whole point |

## Honest limits

- A leaked key on the **daemon** side still allows unlimited issuing FROM
  the daemon. The unit is hardened (DynamicUser, ProtectSystem=strict,
  NoNewPrivileges, locked-down syscalls + address families).
- GreenInvoice account compromise is out of scope — use 2FA on the
  dashboard.
- The `confirm` flag is set by the model; its protection is *forcing a
  deliberate path*, not cryptographic. The real ceiling on damage is the
  rate limit, which the model cannot influence.
