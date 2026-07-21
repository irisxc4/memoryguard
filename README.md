# MemoryGuard

> Local-first MCP memory backend and governance console for coding agents.
>
> 本地优先的 MCP 记忆后端与治理台，面向编程 Agent。([中文文档](README.zh-CN.md))

MemoryGuard lets multiple coding agents write to one shared memory layer, then
automatically organizes, quarantines, supersedes, and rolls back memories —
without turning the GUI into an approval queue.

## What It Does

MemoryGuard provides a local MCP stdio memory backend that coding agents
(Claude Code, Codex, Cursor, and others) can write to using the same memory
mechanisms they already use. Every write is auto-organized — classified,
deduplicated, superseded, quarantined, or compressed — so the shared memory
stays clean. A governance console lets you observe, edit, merge, roll back,
and resolve conflicts after the fact, not by approving every write.

（中文：MemoryGuard 提供本地 MCP 记忆后端，Agent 按原有方式写入，系统自动整理，
GUI 事后治理——无需手动批准每次写入。）

## Features

- **MCP memory backend** — 6 core tools: read / search / write / update / delete / status
- **Auto-organize** — classify / dedup / supersede / conflict / quarantine / derive / compress
- **Multi-agent shared memory groups** — multiple agents write to one governed memory
- **GUI governance console** — observe, edit, merge, lock, restore, roll back (not an approval queue)
- **Provider adapters** — one-command setup for Claude Code / Codex / Cursor
- **Rollback and version history** — all governance actions are versioned and reversible
- **No account, no server, no telemetry** — everything stays local

## Quick Start

### Install

**One-line install (recommended):**

```bash
pip install git+https://github.com/irisxc4/memoryguard.git
```

With GUI support (optional, for desktop window):

```bash
pip install "git+https://github.com/irisxc4/memoryguard.git#egg=memoryguard[gui]"
```

**Or from source:**

```bash
git clone https://github.com/irisxc4/memoryguard.git
cd memoryguard
pip install -e .
```

Verify the installation:

```bash
memoryguard doctor
```

### Configure your Agent

MemoryGuard ships provider adapters that write the MCP config and instruction
file for you. You can also configure manually — see the install guides:

- [Claude Code](docs/install-claude-code.md)
- [Codex](docs/install-codex.md)
- [Cursor](docs/install-cursor.md)

<details>
<summary>Manual MCP config (all agents use the same server)</summary>

**Claude Code** — `.mcp.json` (project root):

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

**Codex** — `~/.codex/config.toml`:

```toml
[mcp_servers.memoryguard]
command = "python"
args = ["-m", "memoryguard.mcp_server"]
```

**Cursor** — `~/.cursor/mcp.json`:

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

</details>

### Use

```bash
memoryguard doctor            # diagnose installation and environment
memoryguard mcp-status        # query MCP memory backend status
python -m memoryguard.mcp_server  # start the MCP stdio server (run by your agent)
memoryguard audit .           # read-only workspace scan, generate report
memoryguard open .            # open the latest report
```

Once your agent is configured, it writes memories through the
`memoryguard_memory_write` MCP tool. MemoryGuard auto-organizes every write.
Open the GUI to observe and govern the results.

## How It Works

MemoryGuard has three layers:

| Layer | Role |
|---|---|
| **Data layer** | Agent native memory, files, external MCP, documents — provides raw evidence, no governance |
| **Memory layer** | MemoryGuard MCP shared memory backend — the single shared source of truth; agents write here; auto-organize runs on every write |
| **Governance layer** | GUI + CLI — observe, edit, merge, lock, restore, roll back after the fact |

```
Agent writes memory
  → memoryguard_memory_write (MCP tool)
  → raw event
  → auto-organize (classify / dedup / supersede / conflict / quarantine / derive / compress)
  → active shared memory
  → GUI governance (observe / correct / override / roll back)
```

## Commands

| Command | Description |
|---|---|
| `audit [path]` | Read-only scan, generate report |
| `open [path]` | Open latest report in a window |
| `explain <finding_id>` | Explain a finding's evidence and risk |
| `plan <finding_ids...>` | Generate minimal fix plan (no write) |
| `apply <plan_id>` | Apply a plan: backup + patch + rescan |
| `verify` | Rescan and compare before/after |
| `undo <change_id>` | Restore from backup and re-verify |
| `source <action>` | Manage authorized sources (list / add / remove / preview) |
| `scan` | Read-only scan, build coverage ledger |
| `import <action> <bundle>` | Offline import bundle (preview / create) |
| `memory <action>` | Memory build & release (build-plan / build-apply / verify / rollback) |
| `doctor` | Diagnose installation and environment |
| `mcp-status` | Query MCP memory backend status |

## MCP Tools

### Memory Backend (6 core tools)

| Tool | Description |
|---|---|
| `memoryguard_memory_read` | Read a single memory record by ID |
| `memoryguard_memory_search` | Search memories by query, kind, or status |
| `memoryguard_memory_write` | Write a new memory; auto-organizes on write |
| `memoryguard_memory_update` | Update a memory (body / kind / status) |
| `memoryguard_memory_delete` | Soft-delete a memory |
| `memoryguard_memory_status` | Get shared memory group status |

### Audit & Scan

| Tool | Description |
|---|---|
| `memoryguard_audit` | Read-only workspace scan |
| `memoryguard_explain` | Explain a finding's evidence and risk |
| `memoryguard_list_sources` | List authorized sources |
| `memoryguard_scan_summary` | Scan + coverage ledger |
| `memoryguard_neuron_graph` | Read neuron graph projection |
| `memoryguard_import_preview` | Preview an import bundle |
| `memoryguard_build_plan` | Generate a memory build plan (no write) |

### Agent Binding

| Tool | Description |
|---|---|
| `memoryguard_binding_create` | Bind an agent to a share group |
| `memoryguard_binding_list` | List agent bindings |

### External MCP

| Tool | Description |
|---|---|
| `memoryguard_external_mcp_list` | List imported external MCP descriptors |
| `memoryguard_external_mcp_import` | Import an external MCP descriptor (L0–L4 classification) |

### Document & Discovery

| Tool | Description |
|---|---|
| `memoryguard_extract_memories` | Extract memory segments from a source file (read-only preview) |
| `memoryguard_accept_candidates` | Accept extracted candidates and write to shared memory |

### Semantic & Provider

| Tool | Description |
|---|---|
| `memoryguard_semantic_check` | Check text for semantic duplicates / conflicts |
| `memoryguard_provider_install` | Install provider adapter (Claude / Codex / Cursor) |

## Governance GUI

The GUI is a **governance console, not an approval queue**. Agents write
memories through MCP; MemoryGuard auto-organizes them. The GUI then shows you:

- Recent raw writes from agents
- Auto-organize results (classify / dedup / compress)
- Supersede chains (what was covered and why)
- Conflict queue (memories that need human arbitration)
- Quarantine queue (secrets / tokens / credentials isolated automatically)
- Derived memories (procedures / preferences from repeated behavior)

Governance actions: **edit, merge, split, lock, restore, delete, roll back**.
Every action is versioned and reversible.

## FAQ

**Does MemoryGuard require a server or account?**
No. It runs as a local MCP stdio server. No account, no cloud, no telemetry.

**Does it fully replace my agent's native memory?**
MemoryGuard redirects writes to the MCP backend where the agent supports it.
Some agents cannot fully disable native memory — MemoryGuard uses a
redirected / observed / unsupported classification instead of pretending
everything can be turned off.

**Does the GUI approve every write?**
No. The MCP backend accepts writes and auto-organizes them. The GUI only
governs the results afterward.

**Is my memory uploaded anywhere?**
No. All data stays in a local SQLite database under `.memoryguard/`.

**Can I roll back changes?**
Yes. All governance actions are versioned. You can restore, un-supersede, and
roll back to any previous version.

## Roadmap

- **Now (open-source core):** local MCP memory backend + auto-organize + GUI governance + provider adapters + rollback. This repository.
- **Later:** enhanced governance features (decay, derivation, governance reports). No timeline committed.
- **Later:** team and enterprise features. No timeline committed.

We do not promise dates for future features. The open-source core is designed
to be fully usable on its own.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). By submitting a PR you agree to the
[CLA](CLA.md).
