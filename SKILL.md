---
name: memoryguard
description: Local-first MCP memory backend and governance console for coding agents. Auto-organize, quarantine, supersede, and rollback shared memories across multiple agents.
version: 0.3.0
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

- `memoryguard_memory_read` - Read a single memory record by ID
- `memoryguard_memory_search` - Search memories by query, kind, or status
- `memoryguard_memory_write` - Write a new memory; auto-organizes on write
- `memoryguard_memory_update` - Update a memory (body / kind / status)
- `memoryguard_memory_delete` - Soft-delete a memory
- `memoryguard_memory_status` - Get shared memory group status
- `memoryguard_binding_create` - Bind an agent to a share group
- `memoryguard_binding_list` - List agent bindings
- `memoryguard_extract_memories` - Extract memory segments from a source file
- `memoryguard_accept_candidates` - Accept extracted candidates and write to shared memory
- `memoryguard_semantic_check` - Check text for semantic duplicates / conflicts
- `memoryguard_provider_install` - Install/repair global MCP + rules + verified
  Hook (Claude / Codex / Cursor; TRAE gets an explicit MCP+rules fallback)
- `memoryguard_build_and_enrich` - Build projection; **host Skill is the default enricher**
- `memoryguard_list_pending_enrichments` - List pending classify/translate tasks
- `memoryguard_apply_enrichments` - Apply host enrichment results (then rebuild)
- `memoryguard_enrichment_status` - Pending/applied queue counts

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
