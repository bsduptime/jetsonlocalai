# hermes-mailer — privilege-separated email broker

A small daemon that owns the email credentials (Resend / SMTP) and
enforces per-caller policy. Hermes' mailer plugin (Elena's process) talks
to this daemon over a Unix socket and **never sees the API key**.

See [`PROTOCOL.md`](./PROTOCOL.md) for the design contract, threat model,
and protocol reference.

## Why this exists

The original [in-process mailer plugin](../plugins/mailer/) stored the
Resend key in Elena's read scope. A prompt-injected Elena could read
the `.env`, bypass the plugin, and call Resend directly — the allowlist
and per-recipient rate limit become moot for that traffic.

This daemon moves the credential **out of Elena's process** entirely.
She talks to a socket; the daemon enforces policy and is the only thing
that can reach Resend. Resend itself does not support per-API-key daily
caps — so the cap has to be enforced in a process the caller cannot
bypass.

## Install

```bash
sudo bash hermes-agent/mail-relay/setup-hermes-mailer.sh
```

The setup script:

- Creates two stable groups: `hermes-mailer-clients` (who can connect
  to the UDS) and `hermes-mailer-config` (who can read the credentials).
  Adds `hermes` to clients only; the daemon is in both.
- Installs the systemd unit (`DynamicUser=yes` — transient broker user,
  no /etc/passwd entry, no shell, no home).
- Seeds `/etc/hermes-mailer/.env` and `/etc/hermes-mailer/allowlist.yaml`
  from the templates (only if missing). Sets ACL so `dbexpertai` can
  edit without sudo.
- Creates `/var/lib/hermes-mailer/` (state — DB + audit log, root only)
  and `/run/hermes-mailer/sock` (UDS, mode 0660 group=hermes-mailer-clients).
- Archives the old in-process plugin config to `/var/backups/hermes-mailer/`
  (root-only) so the old credential file is no longer in Elena's reach.

After install, edit `/etc/hermes-mailer/.env` to pick a transport and
add credentials, plus `/etc/hermes-mailer/allowlist.yaml` to add
contacts. **No daemon restart needed** — allowlist reloads per request
and `.env` is read on each tool invocation (via the systemd unit's
Environment block + the daemon's process env).

Then restart hermes so the new (thin-client) plugin picks up:

```bash
sudo systemctl restart hermes
sudo -u hermes hermes plugins list      # mailer should be "enabled"
sudo -u hermes hermes tools list        # send_email should appear
```

## File layout

| Path | Owner / mode | Purpose |
|---|---|---|
| `/etc/hermes-mailer/.env` | `root:hermes-mailer-config 0640` + dbexpertai ACL | Transport + credentials |
| `/etc/hermes-mailer/allowlist.yaml` | same | Single-caller (elena) allowlist |
| `/etc/hermes-mailer/allowlists/<caller>.yaml` | same | Per-caller multi-tenant form (future) |
| `/var/lib/hermes-mailer/ratelimit.db` | `dynamic 0600` | SQLite reservation DB |
| `/var/lib/hermes-mailer/sent.log` | `dynamic 0600` | JSONL audit (caller, recipient, etc.) |
| `/var/lib/hermes-mailer/dryrun/*.eml` | `dynamic 0600` | Rendered messages in dry-run |
| `/run/hermes-mailer/sock` | `dynamic 0660 hermes-mailer-clients` | UDS — Elena connects here |
| `/usr/local/sbin/hermes-mailer` | — | (none — daemon runs via `python3 -m hermes_mailer.daemon`) |
| `/usr/local/lib/hermes-mailer/hermes_mailer/` | symlink → repo | Package source |

## Operational

- **Status**: `systemctl status hermes-mailer`
- **Logs**: `journalctl -u hermes-mailer -f`
- **Audit log**: `sudo tail -f /var/lib/hermes-mailer/sent.log`
- **Restart**: `sudo systemctl restart hermes-mailer`
- **Test from hermes user**:
  ```bash
  sudo -u hermes printf '%s\n' '{"v":1,"op":"send","request_id":"t","to":"YOU@yours.example","subject":"t","body":"hi"}' \
    | nc -U /run/hermes-mailer/sock
  ```

## Multi-caller usage (future Winnow agents)

Today only Elena (UID of `hermes` user) is a recognized caller. To add
another local agent:

1. Pick a caller name (lowercase, underscore, ≤30 chars — e.g. `winnow_agent`).
2. Note that agent's Unix UID.
3. Add to `/etc/hermes-mailer/.env`:
   ```
   CALLER_UID_winnow_agent=<uid>
   ```
4. Create `/etc/hermes-mailer/allowlists/winnow_agent.yaml` with the
   contacts that agent may email.
5. Add the agent's user to the `hermes-mailer-clients` group so it can
   connect.
6. Restart the daemon: `sudo systemctl restart hermes-mailer`.

Rate limits are partitioned by caller automatically. Elena hitting her
cap on `alice@example.com` does NOT affect `winnow_agent`'s quota for
the same recipient.

## Tests

```bash
cd hermes-agent/mail-relay
python3 -m pytest tests/
```

17 tests covering: protocol envelope, malformed JSON, version mismatch,
unknown op, header injection, attachment magic-byte enforcement on a
lying client, rate-limit exhaustion, per-caller quota isolation,
attachment basename sanitization, dry-run-belt overrides transport,
slow-drip starvation defense, oversized request rejection.

## Hardening

The systemd unit applies:

- `DynamicUser=yes` — transient broker user, no persistent UID.
- `ProtectHome=yes` — `/home` invisible to the process.
- `ProtectSystem=strict` — `/usr` `/etc` `/boot` read-only.
- `ConfigurationDirectory=hermes-mailer` (auto-readable to the daemon).
- `StateDirectory=hermes-mailer` (auto-writable to the daemon, mode 0700).
- `RuntimeDirectory=hermes-mailer` for the UDS.
- `NoNewPrivileges=true`, `LockPersonality=true`, `RestrictRealtime=true`,
  `RestrictNamespaces=true`, `RestrictSUIDSGID=true`.
- `CapabilityBoundingSet=` / `AmbientCapabilities=` — zero caps.
- `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`.
- `SystemCallFilter=@system-service` minus `@mount @debug @cpu-emulation
  @obsolete @raw-io @reboot @swap`.
- `MemoryMax=512M`, `TasksMax=32`, `OOMScoreAdjust=200`.

## Failure modes

| Failure | Caller sees | Recovery |
|---|---|---|
| Daemon down / restarting | `transport_failed / daemon_unreachable` | systemd `Restart=on-failure` kicks back up in 10s |
| Daemon hung on slow Resend | per-connection 60s deadline → `read_timeout` | concurrency cap (MAX_CONCURRENT=4) bounds fan-out |
| Resend rejects (bad From / quota / auth) | `transport_failed / pre_send` | check `journalctl -u hermes-mailer` for the upstream error |
| Resend acks then drops | `transport_failed / post_send_unknown`, quota IS burned | conservative; intentional — see PROTOCOL.md |
| Disk full → state.db can't write | reservation fails; send rejected | hermes-db-guard cron + watchman on /var |
