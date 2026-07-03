# Sync

Update memory and repo docs based on what happened in this conversation, set the next-session pointer, then commit and push all outstanding work.

## Instructions

Follow these steps in order.

### Step 1: Review Conversation

Scan the full conversation for:

1. **Feedback given** — corrections, preferences, "don't do X", "yes that's right", "stop doing Y"
2. **New project context** — decisions made, architecture/infra changes, new workflows or daemons established, runtime-state changes (services, crons, ports, mounts)
3. **Reference information** — new external tools, URLs, APIs, hosts, or resources mentioned
4. **User preferences** — how David likes to work, communication style, what to avoid

### Step 2: Update Memory

Memory files live at the harness-managed path **outside the repo**:
`/home/dbexpertai/.claude/projects/-home-dbexpertai-code-jetsonlocalai/memory/`

Because they are outside the git tree, memory updates do **not** get committed by Step 4 — they persist via the harness. Do not try to move them into the repo or hunt for them under `.claude/`.

For each item found in Step 1:

1. **Check if a memory already exists** — read `MEMORY.md` (the index) and the relevant memory files
2. **If it exists and is still accurate** — skip
3. **If it exists but is outdated or wrong** — update the file (or delete it if it's now false)
4. **If new** — create a new memory file with proper frontmatter and add a one-line pointer to `MEMORY.md`

Memory file format (the harness adds `node_type`/`originSessionId` to `metadata` automatically — you only need `type`):
```markdown
---
name: {kebab-case-slug}
description: {one-line summary — used for recall}
metadata:
  type: {user | feedback | project | reference}
---

{the fact}

**Why:** {reason — for feedback/project}
**How to apply:** {when/where this guidance kicks in — for feedback/project}
```

Link related memories in the body with `[[their-name]]`.

**Rules:**
- Do NOT save things derivable from the repo (file paths, code structure, git history, conventions, CLAUDE.md content)
- Do NOT save ephemeral task state ("what we're doing right now") — that belongs in the Step 6 pointer, not durable memory
- DO save feedback, preferences, decisions, non-obvious runtime facts (which service holds a secret, which port, which mount, why a thing is disabled)
- Convert relative dates ("yesterday", "last week") to absolute dates before writing
- Keep `MEMORY.md` tight — one line per memory, consolidate near-duplicates

### Step 3: Update Repo Docs

Check whether the conversation changed anything that a tracked doc should reflect:

1. **READMEs / PROTOCOL / design docs** — e.g. `hermes-agent/**/README.md`, `PROTOCOL.md`, any `docs/` doc that describes behaviour that changed this session
2. **Install / setup scripts** — if a new field, service, config key, or deploy step was added, make sure the relevant `install-*.sh` / `setup-*.sh` and its inline docs still match
3. **CLAUDE.md** — this repo has no in-repo CLAUDE.md; the operative one is David's **global** `~/.claude/CLAUDE.md` (outside the repo). Only touch it if David explicitly asked — do not auto-edit global instructions.

For each modified doc: verify the change is intentional and stage it for commit. If nothing doc-worthy changed, say so and move on — do not invent doc churn.

### Step 4: Commit Uncommitted Work

Run `git status` and `git diff --stat`. Summarize what changed.

Stage what belongs: source, plugin/daemon code, `install-*.sh`, `.claude/` (commands live here and ARE tracked), and any docs updated in Step 3. Skip scratch files, local experiments, and anything under the scratchpad. Memory is out-of-repo (Step 2) so it will not appear here — that's expected.

**This repo commits straight to `main`** (see the `feedback_commit_straight_to_main` memory) — David is effectively sole author. Do NOT branch first. Create a well-described commit grouping related changes logically; split unrelated changes into separate commits. End the commit message with the standard co-author trailer.

### Step 5: Sync with Remote

1. `git stash` any remaining uncommitted changes (only if the tree is dirty)
2. `git pull --rebase origin main`
3. Clean rebase → `git stash pop` (if stashed) → `git push origin main`
4. Conflicts or a non-trivial merge → **stop and show David**: what conflicts, what the remote changes look like, and ask how to resolve.

**Important:**
- If push fails (protected branch, auth), report the error clearly.
- Never force-push unless David explicitly says to.
- If the pull pulls in significant remote changes, summarize them before pushing.

### Step 6: Update next-session pointer

Rewrite `project_next_session_plans.md` in memory (create it + add to `MEMORY.md` if missing) so a future session opening this project gets a clean, current pointer. It MUST include:

1. **Current state** — a 2–4 bullet dated snapshot of where things landed. What's wired, deployed, verified, or shipped; what's still pending a manual/sudo/hardware step.
2. **Live tracks** — the 1–3 candidate next tasks, each with a one-line description + a concrete entry point (file path, service name, command) + any blockers. If more than one, say which you'd pick first and why.
3. **Carried over** — things blocked on David (sudo deploys, hardware installs, app/account approvals) + stale uncommitted files + longer-horizon items. Keep these visible until resolved, even if unchanged from last session.
4. **"How to apply"** — one short paragraph telling a future session how to use this pointer (default next task, what to re-read before starting a track).

**Why this step exists:** Step 2 captures *durable* facts; this pointer captures *momentum* — what's half-done, what's blocked on what, what needs David's sudo. Without it the next session re-derives state or picks up the wrong thing. Mandatory on every `/sync`, even for a short session.

### Step 7: Summary

Print:

```
## Session Synced

**Memory updated:** (out-of-repo)
- {new/updated memory 1}
- {new/updated memory 2}

**Docs updated:**
- {file 1}: {what changed}   (or "none — no doc-worthy changes")

**Next-session pointer:**
- Top candidate: {task} — {one-line entry point}
- Blocked on David: {count} ({short list})

**Committed & pushed:**
- {N} files changed
- {commit message subject(s)}

**Git state:**
- {last 5 commits}
- {working-tree status}
```
