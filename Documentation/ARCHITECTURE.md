# Local Agent Architecture

**Status as of:** Aug 2026 — safe/review/restricted tiers all built and verified.
**Stack:** LangGraph + Ollama (`gemma4:e4b`), fully local, no paid APIs.

## 1. The goal

Not a one-off script — this is the foundation for a sellable agent platform.
The pitch to a customer isn't "it's smart," it's: *you know exactly what it
can do, what it can't, and you have a complete record of everything it did.*
Every design decision below exists in service of that, not as academic
caution.

## 2. Core principle

**The model never acts directly.** It can only *request* an action. Every
request passes through a permission check before anything runs. The model's
job is to think; `security.py`'s job is to decide; the tool's job is to
execute only what was approved. Those three responsibilities are kept in
three different places on purpose, so each can be reviewed independently.

## 3. The three permission tiers

| Tier | Tools | Behavior | Why |
|---|---|---|---|
| **Safe** | `list_files`, `read_file` | Auto-approved, no gate | Read-only, worst case is wasted effort, not damage |
| **Review** | `write_file` | Auto-checked against stricter rules (extension allowlist, size cap, sandbox containment, overwrite logging) | Can destroy data via overwrite, but scoped and logged |
| **Restricted** | `delete_file` | Pauses for real human approval via `interrupt()` | Irreversible; a mistake here has real cost |

This tiering is the actual product idea: most actions run with zero
friction, and only the genuinely consequential ones stop and ask. Not
"everything needs a human" — that wouldn't be sellable. Only the narrow
slice where being wrong is expensive.

## 4. The sandbox

A single folder: `data/agent_sandbox/`, under a shared `data/` directory
next to the code (`DATA_DIR = Path(__file__).parent / "data"`,
`SANDBOX_DIR = DATA_DIR / "agent_sandbox"` — deliberately project-relative,
not the original `/tmp/agent_sandbox`, which resolved to an unpredictable
location on Windows). All runtime-generated state — sandbox contents, logs,
checkpoint db — lives under `data/`, kept separate from the source `.py`
files at project root. `DATA_DIR` is defined once in `security.py`; every
other module that needs it (`main.py`, `pending_runs.py`) imports it rather
than recomputing its own path, the same single-definition discipline
`SANDBOX_DIR` already followed.

- It's a **boundary**, not a list of approved files. Anything inside is
  fair game; anything outside is refused, unconditionally.
- Containment is checked via `pathlib` component-wise parent comparison
  (`SANDBOX_DIR not in resolved.parents`), **not string-prefix matching** —
  this specifically defeats decoy folder names like `agent_sandbox_evil`
  that would fool a naive `startswith()` check.
- **Flat only, no subfolders.** `write_file` explicitly rejects nested
  paths (`resolved.parent != SANDBOX_DIR`) as a *separate* check from
  containment — a nested path can still resolve inside the sandbox and
  pass containment, so flatness needed its own check. Deliberately scoped
  out for now: folder creation is a different capability than file writing
  and hasn't been reviewed as its own thing yet.
- All three file tools (`list_files`, `read_file`, `write_file`,
  `delete_file`) take **bare filenames only, never paths** — the tool
  constructs the full sandbox path internally. This wasn't the original
  design; it's the fix for a real bug (see §7).

## 5. Files

```
main.py            — the graph: agent -> permission_gate -> [tool_execution |
                      human_approval] -> agent -> end. Also: run()/resume()
                      helpers, checkpointer setup.
security.py        — the only place permission decisions are made. Allowlist
                      tiers, sandbox containment, extension/size checks,
                      audit_log(), DATA_DIR and SANDBOX_DIR (single
                      definitions, imported elsewhere).
tools.py            — tool definitions: what the agent CAN do, independent of
                      what it's permitted to do.
pending_runs.py     — discovery layer for paused approvals (see §6); imports
                      DATA_DIR from security.py rather than defining its own.

data/                — all runtime-generated state, separate from source.
  agent_sandbox/      — the sandbox itself.
  audit.log           — one JSON line per tool-call attempt, allowed or not.
  pending_runs.log    — one JSON line per pending/resolved approval.
  checkpoints.db      — SQLite-backed LangGraph checkpointer; durable graph
                        state, survives process crashes.

archive/              — superseded audit.log snapshots from earlier
                        debugging sessions. Not read by the running system;
                        kept for reference only.
```

## 6. The approval flow (restricted tier)

1. Model requests `delete_file`.
2. `permission_gate` sees it's restricted-tier, checks sandbox containment
   and file existence up front — if the delete could never succeed anyway
   (file doesn't exist), it's denied immediately and **never reaches a
   human**. No point interrupting someone to approve something impossible.
3. If it could succeed: `mark_pending()` writes to `pending_runs.log`
   **before** `interrupt()` pauses — order matters, since this is the
   record that needs to survive a crash at the worst possible moment.
4. `human_approval` node prints the tool, filename, and the file's current
   content (so you can see exactly what you'd lose), then `interrupt()`s
   and waits for y/n.
5. On resume: `mark_resolved()` updates the pending record; approved goes
   to `tool_execution`, denied returns a rejection to the model as a normal
   tool result.
6. Every step — pending, approved, denied — is logged to both
   `audit.log` (the permanent record) and `pending_runs.log` (the
   discovery index, which drops resolved entries from `list_pending()`).

**Discovery without prior knowledge:** a fresh process with zero
information can call `pending_runs.list_pending()` and get back every
still-open approval — thread_id, tool, and args — with no dependency on
having seen the original terminal output. Verified end to end: killed a
process mid-pause, found the pending request cold in a new process, resumed
it correctly, confirmed it dropped off the pending list once resolved.

## 7. Bugs found and fixed along the way (worth remembering why)

- **The `path="."` bug.** `list_files` originally took a `path` argument
  defaulting to the sandbox. When asked generically to "list the sandbox,"
  the model guessed `path="."`, which resolved relative to the *process's*
  working directory, not the sandbox — correctly blocked, but useless.
  **Fix:** removed the argument entirely. `list_files` takes nothing, always
  looks at the sandbox. Same root cause and same fix later applied to
  `read_file`, which had the identical bug and caused a full task failure
  (model gave up and reported the file "didn't exist" rather than
  retrying) before being caught and fixed the same way.
- **Lesson generalized:** if a tool has a parameter the model has no
  reliable way to get right, the fix is removing the model's ability to
  guess — not better prompting. Prompting is inherently probabilistic;
  removing the parameter is structural.

## 8. What's deliberately *not* built yet, and why

- **Nested folders / directory creation** — scoped out on purpose (§4).
  Would need its own review (folder-name validation, depth limits) before
  being safe to add.
- **`checkpoints.db` retention/pruning** — confirmed to grow unbounded (one
  delete-with-approval run alone produced 7 checkpoint rows, 21 write
  rows). Not fixed: this is a volume problem that matters at continuous
  production usage, not during hand-testing. `LangGraph`'s checkpointer
  classes expose `.delete_thread()` for this when it's actually needed.
- **Network/external-facing tools** — the sandbox model is filesystem-only;
  an API-calling or messaging tool would need a completely different
  containment approach (rate limits, destination allowlists, etc.), not an
  extension of the current one.
- **General human-approval UI** — right now "approval" is a terminal
  `input()` prompt. Fine for solo development, not something a customer
  would use. Would need a real interface before this is demo-able to
  anyone but you.

## 9. Verification history (what's actually been proven, not just built)

- Tool-calling reliability: `gemma4:e4b` produces well-formed `tool_calls`
  reliably; no structured-output flakiness observed.
- Sandbox containment: resistant to posix-style traversal (`../../../`),
  Windows-style traversal (`..\..\`), and decoy sibling-folder names.
- Two full realistic tasks completed end to end (clean article + a
  deliberately messy, noisy status report with a buried high-priority
  issue) — summaries were accurate, correctly prioritized, and didn't
  hallucinate or leak irrelevant content.
- Graceful handling of a genuinely missing file — honest failure, no
  hallucinated output, no unexpected tool calls.
- Full restricted-tier flow — approve, deny, and doomed-request-denied-early
  — all verified with real audit.log traces.
- Durability — pending approval survived an uncontrolled process crash
  (`EOFError` immediately after the pre-pause checkpoint write, arguably a
  harsher test than a clean kill signal) and resumed correctly in a
  completely separate process.
- Regression-checked repeatedly after non-behavioral changes: after the
  tiering refactor (status string changed from bool to enum), after the
  SQLite checkpointer swap, and after the `data/`/`archive/` folder
  reorganization — each time confirming the safe-tier read/write flow was
  unaffected. The reorg check also confirmed `audit.log` and
  `pending_runs.log` continued their existing history at the new location
  rather than resetting.

## 10. Suggested next steps, in rough priority order

1. Decide on a second restricted-tier or review-tier action once a real
   use case calls for one — don't design more tiers speculatively.
2. If moving toward anything customer-facing, replace the terminal
   `input()` approval with a real interface.
3. Revisit `checkpoints.db` retention once running continuously rather
   than in individual test sessions.
4. If nested folder support becomes a real need, design its own
   validation (name allowlist, depth cap) rather than folding it into the
   existing flat-sandbox write path.
