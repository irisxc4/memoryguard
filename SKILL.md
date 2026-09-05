---
name: memoryguard
description: Local-first MCP memory backend and governance console for coding agents. Auto-organize, quarantine, supersede, and rollback shared memories across multiple agents.
version: 0.4.0
author: irisxc4
homepage: https://github.com/irisxc4/memoryguard
repository: https://github.com/irisxc4/memoryguard
license: MIT
tags:
  - mcp
  - memory
  - agent
  - governance
  - claude-code
  - cursor
  - codex
  - local-first
  - sqlite
---

# MemoryGuard

Shared memory for coding agents, without shared-memory chaos.

## What It Does

MemoryGuard provides a local MCP stdio memory backend that coding agents
(Claude Code, Codex, Cursor) can write to. Every write is auto-organized:
classified, deduplicated, superseded, quarantined, or compressed.

## Install

```bash
pip install agent-memguard
```

## First activation — install the host Hook automatically

Installing a plain Skill does not itself execute code. On the **first Skill
activation after installation**, the host Agent must proactively complete this
idempotent setup without asking the user to edit JSON/TOML:

1. Identify the current host as exactly one of `claude`, `codex`, `cursor`, or
   `trae`. Do not install into every detected Agent.
2. Call `memoryguard_provider_install(provider=<current host>)`. Do not pass a
   different `agent_instance_id`; the trusted MCP binding is authoritative.
3. Inspect the returned `hook` object:
   - `configured=true`: setup is complete.
   - `status=configured_pending_runtime`: restart/reload the host and verify
     after the first prompt.
   - Codex `trust_required=true`: tell the user to open `/hooks` and trust the
     exact MemoryGuard hook hash. Never bypass Hook trust automatically.
   - `supported=false`: keep MCP + always-on rules active and report that this
     host has no verified lifecycle Hook seam.
4. Re-running the setup is a repair operation. It replaces only
   MemoryGuard-owned handlers and must not duplicate or delete other hooks.

If the MCP install tool is not available but a trusted binding already exists,
run:

```bash
memoryguard hooks ensure --provider <current-host> \
  --workspace <memoryguard-control-workspace> \
  --agent-id <trusted-agent-id>
```

Do not claim that Skill installation alone created a permanent Hook. The Hook
is active only after the user-level host configuration is written and a runtime
receipt is observed.

## Mandatory rules

- Use `memoryguard_memory_write(injection_policy="always", priority=...)` only
  when the user explicitly asks for a long-term mandatory rule (for example,
  “must”, “always”, or a default rule). Facts, preferences, and ordinary
  procedures remain `relevant`; never promote every procedure to mandatory.
- `memoryguard_memory_update` can switch `injection_policy`/`priority`, but it
  cannot change lifecycle status or restore a deleted record. The GUI can switch
  a rule back to on-demand or perform governed delete/restore actions.
- Bootstrap injects mandatory rules in an independent budget before relevant
  recall. Sensitive or over-limit mandatory packages fail closed.

## Hook runtime contract

- Claude and Codex: `UserPromptSubmit` injects one bounded, relevant,
  active-only long-term context packet per turn.
- Claude and Codex subagents receive the same policy plus a task-relevant
  bounded packet through `SubagentStart`; tool guards also apply inside them.
- Cursor: `sessionStart` injects session policy/context. Because
  `beforeSubmitPrompt` cannot inject dynamic context, the first non-MemoryGuard
  tool call is denied until `memoryguard_context_bootstrap` succeeds.
- `PreToolUse` blocks visible writes to known native-memory paths and other
  memory MCP writers. Manual MemoryGuard GUI delete/restore remains available.
- `PostToolUse` records successful bootstrap/write receipts without storing raw
  prompts or memory bodies.
- `Stop` continues at most once when an explicit durable preference/correction
  has no MemoryGuard write receipt. Never save the whole conversation.
- Hook modes are `enforce` (default), `observe`, and `paused`.

## History is evidence, not long-term memory

- **Rules & habits** govern long-term records. Mandatory rules must be scoped
  to their intended Agent, project, provider, or runtime role; never treat a
  shared group as permission to force every member.
- **Conversation history** is a separate, local raw-evidence archive. It is
  isolated per Agent by default and is never injected by
  `memoryguard_context_bootstrap` or added to the neuron graph automatically.
- Retrieve history progressively: `memoryguard_history_search` (IDs and
  summaries only), then `memoryguard_history_timeline`, then
  `memoryguard_history_read` for an authorized raw turn/session.
- `memoryguard_history_extract_preview` is read-only. It may suggest
  evidence-backed candidates but must not write long-term memory; use the
  normal governed memory write path only after explicit selection.
- Verified Hook seams may archive bounded user prompts and exposed assistant
  finals directly into the history database. They honor privacy/disable flags,
  never infer missing content, and cannot block the host conversation when
  archival fails.

## MCP Configuration

Add to your agent's MCP config:

```json
{
  "mcpServers": {
    "memoryguard": {
      "command": "python",
      "args": ["-m", "memoryguard.mcp_server"]
    }
  }
}
```

## Tools

The default MCP discovery surface exposes nine day-to-day tools:

- `memoryguard_context_bootstrap` - Load bounded mandatory rules and relevant memory context
- `memoryguard_memory_search` - Search governed memories by query, lifecycle status,
  and bounded limit. `kind` is not forwarded as an MCP search filter; semantic
  duplicate/conflict checks are a separate advanced governance operation.
- `memoryguard_memory_read` - Read one governed memory record
- `memoryguard_memory_write` - Write and organize a governed memory
- `memoryguard_memory_update` - Update the body, kind, recall policy, or priority
  of one known memory. It does not change lifecycle status or restore deleted
  records; restoration is a GUI governance action.
- `memoryguard_memory_delete` - Soft-delete a governed memory
- `memoryguard_memory_status` - Inspect shared-memory status
- `memoryguard_audit` - Run a read-only local governance audit
- `memoryguard_explain` - Explain one audit finding and its evidence

Advanced governance remains available through the GUI and CLI, including rule
lifecycle, bindings and shared groups, source scanning, CodeGraph, knowledge and
history review, provider controls, external MCP import, and maintenance.
Previously advertised advanced MCP names remain callable for compatibility when
an installed client invokes an exact name, but they are not returned by the
default `tools/list`. Use the live MCP surface as the source of truth; do not
assume that a compatibility-only name is part of default discovery.

## Host AI enrichment (Skill interface — automatic)

**You (the host agent running this Skill / MCP tools) ARE the LLM for enrichment.**
Installing MemoryGuard + enabling MCP connects this path. Do not ask the user to
pick Codex CLI unless they are in the multi-agent GUI and chose a CLI engine.

### Required auto loop (no user confirmation for each step)

When the user asks to build / rebuild projection or enrich memories:

1. Call `memoryguard_build_and_enrich` (default `enrich_mode=host`).
2. If response has `host_action_required` or `pending_tasks`:
   - Classify + translate each task yourself (`kind` / `title` / `body` / `confidence`).
   - Call `memoryguard_apply_enrichments` with the results.
   - Call `memoryguard_build_and_enrich` again to refresh the neuron graph.
3. Do **not** stop after step 1 when pending remains. Do **not** tell the user
   “only Codex is available” — the chat model is already connected via Skill/MCP.

### Multi-agent GUI popup (separate path)

When building from the MemoryGuard GUI in **share-group / multi-agent** mode, the
UI **must** show a picker: host Skill / Cursor Agent CLI / Codex / Claude / ….
That picker is for choosing the sync engine in the GUI process. Skill/MCP calls
still default to `host` and auto-run the loop above.

Optional `enrich_mode=cli` + `llm_agent` / `llm_cli` is only for an explicit CLI
choice from that popup or headless automation.

Do **not** require a separate GUI “AI 整理” button. Rebuild projection to re-run enrich.

## Privacy

No account. No server. No telemetry. All data stays in local SQLite under `.memoryguard/`.
