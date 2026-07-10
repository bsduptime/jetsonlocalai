# winnow — the Winnow business secretary (dedicated Hermes instance)

A third Hermes instance on the Jetson, for **Winnow business operations
only**: invoices/expenses (GreenInvoice) and the business calendar. Lives in
the "Winnow Management" Telegram group with David + Lihi — nobody else.
Family matters stay with Elena; this instance can't touch them, and Elena
can't touch the business calendar.

## Architecture

```
David + Lihi (Telegram group "Winnow Management"; user-ID allowlist, fail-closed)
        │
        ▼
hermes-winnow instance (user=hermes-winnow, sandboxed: NO repos bound in)
  ├── greeninvoice plugin ──UDS──► hermes-greeninvoice broker (existing)
  │     caller identity "winnow" (own rate buckets + audit trail);
  │     credentials stay in the broker — the agent never sees them
  ├── familycal plugin ──UDS──► hermes-calendar-winnow relay (NEW, own unit)
  │     business Google calendar, own OAuth creds/contacts — fully separate
  │     from the family calendar relay
  └── SOUL.md: secretary persona, draft→confirm→issue discipline
```

Separation is structural, not prompt-level: what this agent can ever do is
decided by which plugins are linked into ITS plugin dir, which sockets its
unit exposes, and the broker's own gates (issue caps 3/h 10/d, confirm
required, drafts free).

## Setup

```bash
sudo bash hermes-agent/winnow/setup-winnow.sh
```

Then the interactive remainder (~10 min):
1. **@BotFather** → `/newbot` ("Winnow Secretary") → token into
   `/home/hermes-winnow/.hermes/.env` (`TELEGRAM_BOT_TOKEN=...`).
2. `sudo -u hermes-winnow -i hermes setup` — LLM provider.
3. Fill David's + Lihi's Telegram user IDs in
   `/home/hermes-winnow/.hermes/config.yaml` (`allow_from`).
4. Business calendar Google OAuth: create/choose the **business** calendar,
   run `calendar-relay/setup-google-auth.py` against
   `/etc/hermes-calendar-winnow`, set `CALENDAR_DRY_RUN=false` when proven.
5. `sudo systemctl enable --now hermes-calendar-winnow hermes-winnow`
6. Create the "Winnow Management" group, add the bot + Lihi.

✅ Verify: in the group — "draft an invoice for <client> over 1,000 ILS" →
preview PDF, nothing issued; "what's on the business calendar this week?" →
calendar reply; a family question → the secretary has no tools for it.
