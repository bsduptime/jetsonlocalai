# vault — Hermes vault plugin

Implements the contract from `projects/maclocalai/shared-memory-architecture.md`
(v4+). Code enforces path containment; tool descriptions encode the contract
rules as system-prompt instructions to the model.

## Tools

| Tool | Purpose | Write boundary |
|---|---|---|
| `vault_session_brief` | Bundle INDEX.md + areas/schedule.md + last 7 days of `daily/*.md` (rolling). Called once per chat session. | read-only |
| `vault_read` | Read any file under the vault root, with frontmatter parse + staleness flag. | read-only |
| `vault_write_observation` | One-observation-per-file write to `agents/hermes/observations/YYYY-MM-DD-HHMM-<slug>.md`. Append-only — refuses overwrite via `O_EXCL`. | `agents/hermes/observations/` only |
| `vault_write_memory` | Mutable write to `agents/hermes/memory/<relpath>.md`. Subdirs allowed. Auto-updates `last_compiled`. | `agents/hermes/memory/` only |
| `vault_conflict_scan` | Glob `*.sync-conflict-*` under `agents/hermes/` (default) or whole vault. | read-only |

## Defense in depth

1. **Handler code**: every write resolves the path via `Path.resolve()` and asserts it stays under the per-tool write root (`OBSERVATIONS_DIR` or `MEMORY_DIR`). Path-traversal (`../`), absolute paths, and symlink escapes all fail before any `open()` call.
2. **Open flags**: writes use `O_NOFOLLOW` so a malicious symlink under the write dir can't redirect to elsewhere on the filesystem.
3. **Kernel ACL** (step 7 of the architecture plan): Hermes runs as `hermes` (UID 2010). `agents/hermes/` has `setfacl u:hermes:rwX`; everywhere else in the vault, `hermes` has only the `other` bits (`r-x` on dirs, `r--` on files). Any contract violation that bypasses the handler still hits `EACCES`.
4. **Tool descriptions**: every schema description restates the contract rule, so the LLM sees the constraint at decision time, not just at call time.

## Install

```bash
sudo bash hermes-agent/install-vault-plugin.sh
```

The installer does three things:
1. Verifies the `agents/hermes/` ACL (access + default + effective rwx) and refuses to proceed if anything is wrong.
2. Symlinks this dir into `/home/hermes/.hermes/plugins/vault`.
3. Runs `hermes plugins enable vault` — Hermes plugins are opt-in by default; symlinking alone won't register the tools.

After the script finishes, **you still need to restart Hermes** so the tools actually attach to running sessions:

```bash
sudo systemctl restart hermes
sudo -u hermes -i hermes plugins list | grep vault   # status: enabled
sudo -u hermes -i hermes tools list   | grep vault   # vault toolset listed
```

## System-prompt addition

The tool descriptions themselves carry the contract. For belt-and-suspenders,
you may also want to add this paragraph to Hermes' top-level system prompt
(wherever your Hermes config keeps it):

> You have a shared knowledge vault under `/home/dbexpertai/obsidian-vault`,
> accessed exclusively through the `vault` toolset. At the start of every new
> chat session, call `vault_session_brief` ONCE to ingest the current state
> of the vault before doing anything else. Any tool may return a `stale`
> flag or `warnings` array — when it does, mention to David that the
> underlying state may be out of date. If `vault_conflict_scan` returns any
> conflicts, treat them as a hard error: stop and ask David to resolve.
> Write observations (append-only, one fact per file) with
> `vault_write_observation`; write/update compiled state (people, customers,
> preferences) with `vault_write_memory`. Never try to write anywhere else
> in the vault — the kernel will refuse.

## Tests

```bash
cd hermes-agent/plugins/vault
python3 -m pytest tests/
```

## What's deliberately out of scope

- No write access to `daily/`, `inbox/`, `decisions/`, or any other vault
  area. The daily compile and conflict surfacing happen in a separate
  Jetson cron (step 5 of the architecture plan), run as `dbexpertai`.
- No git operations. Versioning is Syncthing Staggered Versioning (step 2).
- No retroactive editing of observations. They're append-only by design;
  to correct a previous observation, write a new one that supersedes it.
