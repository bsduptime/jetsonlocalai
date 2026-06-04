# hermes-greeninvoice

Privilege-separated broker for the [GreenInvoice / Morning](https://www.greeninvoice.co.il/api-docs/)
API. Lets Hermes **draft** invoices, **issue** real ones (rate-limited, with
an explicit confirmation), **retrieve** documents, and **create/update**
clients — without ever holding the API key.

Companion to [`hermes-mailer`](../mail-relay/) and built on the same pattern:
a hardened `DynamicUser` systemd daemon owns the credentials and policy;
Hermes' plugin is a thin Unix-socket client. Full design in
[PROTOCOL.md](./PROTOCOL.md).

## Pieces

```
invoice-relay/
  hermes_greeninvoice/            # the daemon package (stdlib only)
    config.py      errors.py      audit.py
    ratelimit.py   # hourly + daily caps per (caller, action class)
    auth.py        # JWT token cache (/account/token)
    apiclient.py   # GreenInvoice HTTP client (throttle, retry, re-auth)
    validate.py    # input validation + request-body builders
    handler.py     # dispatch + policy enforcement
    daemon.py      # UDS server (SO_PEERCRED caller resolution)
  hermes_greeninvoice_client.py   # thin client (reference copy)
  systemd/hermes-greeninvoice.service
  setup-hermes-greeninvoice.sh
  .env.example
  tests/

../plugins/greeninvoice/          # the Hermes plugin (thin client)
  plugin.yaml  __init__.py  schemas.py  handler.py  _client.py
../install-greeninvoice-plugin.sh
```

## Tools exposed to Hermes

| tool | what | gated |
|------|------|-------|
| `gi_draft_invoice` | render a preview (no record, no email) | loose |
| `gi_issue_invoice` | create a real 305/320/400 document | **tight + confirm** |
| `gi_get_document` / `gi_search_documents` / `gi_document_download_links` | retrieve | no |
| `gi_create_client` / `gi_update_client` | client write (no delete) | loose |
| `gi_get_client` / `gi_search_clients` | client read | no |
| `gi_quota` | remaining budget + env + dry-run state | no |

### Intended flow

1. `gi_search_clients` → resolve who's being billed (or `gi_create_client`).
2. `gi_draft_invoice` → preview; Hermes shows/sends it to David (mailer/Telegram).
3. On David's explicit go-ahead, `gi_issue_invoice` with `confirm: true`
   (and `email_to_client: true` to also email the client).
4. When a `305` is later paid, issue a `400` receipt linked via
   `linkedDocumentId`.

## Document types

`305` tax invoice · `320` invoice+receipt · `400` receipt (these three are
issuable). Drafts can also preview `300` proforma and `10` price quote.
No credit notes, no deletes.

## Rate limits (defaults, per caller)

| class | hourly | daily |
|-------|--------|-------|
| issue | 3 | 10 |
| draft | 20 | 60 |
| client_write | 20 | 100 |

Reads are unlimited. Override any cap via `GI_LIMIT_<CLASS>_PER_HOUR/DAY` in
`.env`. The cap lives in the daemon, so a prompt-injected Hermes cannot
exceed it.

## Install

```bash
# 1) the broker daemon (holds the key)
sudo bash hermes-agent/invoice-relay/setup-hermes-greeninvoice.sh

# 2) put GreenInvoice creds in place, stay in sandbox + dry-run to start
sudoedit /etc/hermes-greeninvoice/.env     # GI_API_KEY_ID / GI_API_KEY_SECRET
#   keep GI_ENV=sandbox; flip GI_DRY_RUN=false when ready
sudo systemctl restart hermes-greeninvoice

# 3) the Hermes plugin (thin client, no creds)
sudo bash hermes-agent/install-greeninvoice-plugin.sh
sudo systemctl restart hermes
```

Default is **sandbox + dry-run**: the daemon validates, rate-limits, and
audits every call but makes no live API request until you set
`GI_DRY_RUN=false`.

### Enabling + delivering previews

A symlinked plugin is only *discovered* — Hermes needs an explicit enable, and
plugin tools register **per session**, not at boot:

```bash
sudo -u hermes -i hermes plugins enable greeninvoice
sudo systemctl restart hermes
# then start a NEW session (/new in Telegram) — gi_* tools appear only inside
# a session, so `hermes tools list` (run outside one) won't show them.
```

Draft/receipt previews are written to `/run/hermes-greeninvoice/previews`.
Hermes refuses to attach files outside its media allowlist, so the plugin
installer adds that dir to `HERMES_MEDIA_ALLOW_DIRS` via a `hermes.service.d`
drop-in. To email previews via the mailer instead, add the dir to that
plugin's `EMAIL_ATTACHMENT_ALLOWED_PREFIXES`.

## Tests

```bash
cd hermes-agent/invoice-relay
python3 -m pytest -q          # no credentials needed (dry-run + fakes)
```

## Logs

```bash
journalctl -u hermes-greeninvoice -f
sudo tail -f /var/lib/hermes-greeninvoice/audit.log
```
