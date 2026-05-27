"""JSON schemas exposed to the LLM via ctx.register_tool().

The `description` fields are the load-bearing prompt mechanism: they're what
the model sees when it decides whether/when/how to call a tool. So we lean
into them — every contract rule from shared-memory-architecture.md is
restated inside the description of the relevant tool.
"""

from __future__ import annotations

VAULT_SESSION_BRIEF = {
    "name": "vault_session_brief",
    "description": (
        "Call this ONCE at the start of every new chat session, before any other "
        "vault tool. Returns the contents of INDEX.md, areas/schedule.md, and the "
        "last 7 days of daily/*.md notes (you can override the window via `days`). "
        "Each returned section carries a staleness flag — if `last_compiled` in the "
        "file's frontmatter is older than 14 days, the brief returns a warning and "
        "you MUST mention to David that the underlying state files may be stale. "
        "If the brief returns conflict files in `conflicts`, treat them as a hard "
        "error: stop and ask David to resolve them before proceeding."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "How many days back to include from daily/ (rolling window). Default 7.",
                "minimum": 1,
                "maximum": 31,
                "default": 7,
            },
        },
        "required": [],
    },
}

VAULT_READ = {
    "name": "vault_read",
    "description": (
        "Read any file under the shared vault at "
        "/home/dbexpertai/obsidian-vault. Path must be relative to the vault "
        "root (e.g. 'projects/maclocalai/shared-memory-architecture.md' or "
        "'areas/customers/acme.md'). Symlinks are resolved and any path that "
        "escapes the vault root is rejected. Returns the file body plus parsed "
        "frontmatter and a `stale` flag — if `last_compiled` in the frontmatter "
        "is older than `staleness_warning_after_days` (default 14), `stale=true` "
        "and you MUST warn David that the file may be out of date before relying "
        "on its contents."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to vault root. No leading slash. No '..'.",
            },
        },
        "required": ["path"],
    },
}

VAULT_WRITE_OBSERVATION = {
    "name": "vault_write_observation",
    "description": (
        "Write a single new observation file to agents/hermes/observations/. "
        "The handler generates the filename: YYYY-MM-DD-HHMM-<slug>.md based on "
        "the current local time and the slug you provide. Observations are "
        "APPEND-ONLY — one observation per file, never overwrite. If a file with "
        "the same timestamp+slug already exists (race-of-the-minute), the tool "
        "returns an error and you should adjust the slug. Use this for: things "
        "David said in this session that future sessions should know, decisions "
        "made, customer/people facts learned, etc. Do NOT use this for ephemeral "
        "session state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": (
                    "Kebab-case slug, 1-50 chars, lowercase alnum + dashes. "
                    "Examples: 'customer-acme-contract-signed', 'preference-dark-mode'."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown body of the observation. Frontmatter is auto-generated; don't include `---` fences.",
            },
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of source references (urls, vault paths, chat ids) recorded in frontmatter.",
                "default": [],
            },
        },
        "required": ["slug", "body"],
    },
}

VAULT_WRITE_MEMORY = {
    "name": "vault_write_memory",
    "description": (
        "Write or overwrite a file under agents/hermes/memory/. Subdirectories "
        "are allowed (e.g. 'people/alice.md', 'customers/acme.md', "
        "'preferences.md'). Use this for compiled/curated state that gets "
        "updated over time (vs. vault_write_observation which is append-only "
        "log entries). The handler auto-sets `last_compiled` in the file's "
        "frontmatter to today's date. Path must be relative to "
        "agents/hermes/memory/, must end in .md, and each component must be "
        "lowercase alnum + dash/underscore."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "relpath": {
                "type": "string",
                "description": "Path relative to agents/hermes/memory/, e.g. 'people/alice.md'. Must end in .md.",
            },
            "body": {
                "type": "string",
                "description": "Markdown body. Frontmatter is auto-managed; don't include `---` fences.",
            },
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of source references for the frontmatter.",
                "default": [],
            },
        },
        "required": ["relpath", "body"],
    },
}

VAULT_CONFLICT_SCAN = {
    "name": "vault_conflict_scan",
    "description": (
        "Scan the vault for Syncthing conflict files (`*.sync-conflict-*`). "
        "By default scans only agents/hermes/ — the namespace you write to. "
        "If any are returned, the contract requires you to TREAT THEM AS "
        "ERRORS, not data: stop what you're doing and surface them to David. "
        "Conflicts inside agents/hermes/ usually mean the same observation file "
        "was modified on two devices simultaneously; David should review and "
        "delete the loser before continuing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["hermes", "vault"],
                "description": "'hermes' = scan only agents/hermes/ (default). 'vault' = scan the whole vault.",
                "default": "hermes",
            },
        },
        "required": [],
    },
}
