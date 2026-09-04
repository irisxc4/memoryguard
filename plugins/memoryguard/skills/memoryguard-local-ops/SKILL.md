---
name: memoryguard-local-ops
description: Install, configure, repair, diagnose, and govern local PyPI agent-memguard on Windows. Use for Codex MCP or Hook setup, package verification, V2 control-home issues, or MemoryGuard memory-policy questions.
---

# MemoryGuard local operations

Use this skill for the repository's `agent-memguard` package (the current
checkout declares version `0.7.9`). Scope is local Windows installation,
configuration, repair, diagnostics, and governed memory operations.

This is a skills-only plugin. It does not bundle `agent-memguard`, add a
remote MCP server, or silently edit a user's host configuration. Run commands
only when the user asked for the corresponding operation; use read-only checks
first when diagnosing.

## Windows install

1. Check the interpreter and package manager:

   ```powershell
   py -3 --version
   py -3 -m pip --version
   py -3 -c "import sys; print(sys.executable)"
   ```

   `agent-memguard` requires Python 3.10 or newer. If the `py` launcher is not
   available, use `python` consistently and verify that it resolves to the
   interpreter that will run the MCP process.

2. Install or upgrade the PyPI package:

   ```powershell
   py -3 -m pip install --upgrade agent-memguard
   ```

   The optional desktop console is a separate extra:

   ```powershell
   py -3 -m pip install --upgrade "agent-memguard[gui]"
   ```

   For a source checkout, use a non-editable install (`py -3 -m pip install
   .`). Do not point a live Codex MCP process at `src` or use `pip install -e`
   as its runtime. The provider installer can select or build a content-keyed,
   non-editable runtime snapshot when it detects a local/editable install.

3. Verify the exact interpreter and package:

   ```powershell
   memoryguard --version
   py -3 -c "import importlib.metadata as m; print(m.version('agent-memguard'))"
   py -3 -m memoryguard.mcp_server --help
   memoryguard doctor
   ```

   If `memoryguard` resolves to another Python installation, use that
   interpreter's console-script path or repair `PATH`; do not assume that a
   successful `pip` command configured Codex.

## Codex configuration

### Preferred provider path

If the MemoryGuard MCP tools are already available, call:

```text
memoryguard_provider_install(provider="codex")
```

Inspect its result. It is idempotent and is the canonical path for MCP,
redirect rules, and a supported user-level Hook. Do not pass a made-up
`agent_instance_id` or `share_group_id`; the trusted MCP binding is
authoritative. A result that says `configured_pending_runtime` means restart
or reload Codex, then verify the runtime receipt. `trust_required` means open
Codex `/hooks` and trust the exact MemoryGuard Hook hash; never bypass that
trust boundary.

If the MCP tool is unavailable but the package is installed, the packaged CLI
repair path is:

```powershell
memoryguard provider repair codex
```

This rebuilds the global provider integration from the canonical V2 data-home
binding. It can fail closed when no verified binding exists, more than one
Codex instance is detected, the control home is ambiguous, or V2 is not
active. Do not mint an identity or select a group from a profile path to force
the repair through.

### Manual MCP fallback

Use manual wiring only when the provider path is unavailable and the user
explicitly wants it. In the Windows user Codex config
`%USERPROFILE%\.codex\config.toml`, the documented minimal section is:

```toml
[mcp_servers.memoryguard]
command = "python"
args = ["-X", "utf8", "-m", "memoryguard.mcp_server"]
```

If `python` is not the interpreter verified above, set `command` to the full
path printed by `py -3 -c "import sys; print(sys.executable)"`; keep the
`-X utf8 -m memoryguard.mcp_server` arguments. Preserve unrelated TOML
tables. Manual MCP wiring alone does not create a trusted binding, redirect
rules, or a Hook; run the provider path later and restart Codex.

After any configuration change, restart Codex and run:

```powershell
memoryguard doctor
memoryguard mcp-status
memoryguard hooks status --provider codex
```

`mcp-status` reporting that the stdio server is not running is normal: stdio
starts on demand. Treat `configured, awaiting runtime receipt` as incomplete
until a restarted host produces evidence. A Hook status of unsupported or not
configured is not success.

## Codex boundary

On Windows, MemoryGuard's Codex lifecycle integration is an external,
best-effort compatibility shim. Codex remains lifecycle authority. The shim
uses conservative verified thread/workspace evidence for lifecycle cleanup;
an ordinary turn boundary is not proof that a conversation ended. It does not
patch, fix, or guarantee Codex upstream behavior. Never report “Codex fixed” or
“Hook guaranteed” from a config write alone; distinguish configured,
runtime-verified, trusted, and unsupported states.

## Repair and diagnosis

Use this read-only order before changing anything:

```powershell
memoryguard --version
memoryguard doctor
memoryguard hooks status --provider codex
memoryguard mcp-status
```

Useful interpretations:

- `doctor` checks Python `>=3.10`, package import, MCP module import, the
  selected control workspace, active bindings, provider adapters, host hooks,
  and optional GUI availability. A missing optional GUI dependency is not an
  MCP failure.
- `v2_upgrade_required`, `v2_not_active`, or
  `v2_manifest_state_unavailable` means provider mutation is gated. First
  inspect without writing:

  ```powershell
  memoryguard upgrade --preview
  ```

  Run `memoryguard upgrade` only after the user approves the migration. Then
  rerun provider repair and verification.
- `multiple_provider_instances_detected`, `active_binding_not_found`, or
  `verified_v2_control_home_ambiguous` is a fail-closed identity/control-home
  problem. Gather the reported IDs and paths; do not guess, merge, or delete
  profiles.
- A configured MCP entry is not proof that the host connected. Verify with a
  restarted host and the Hook status/runtime receipt.

After an approved repair:

```powershell
memoryguard provider repair codex
memoryguard doctor
memoryguard hooks status --provider codex
memoryguard mcp-status
```

`memoryguard provider repair all` changes every detected provider and requires
an explicit request. Provider repair is idempotent and should preserve
non-MemoryGuard config and hooks; do not hand-edit away other providers.

## Data home and governance

- The canonical Windows user data home defaults to `%LOCALAPPDATA%\MemoryGuard`.
  `MEMORYGUARD_HOME` is the explicit data-home override. Do not casually set
  `MEMORYGUARD_WORKSPACE` or point runtime commands at a project-local legacy
  `.memoryguard` tree; follow the V2 resolver and migration output.
- Authorize a project source explicitly when the user wants it indexed:

  ```powershell
  memoryguard source add .
  ```

- Treat V2 as the active control plane. Use `memoryguard upgrade --preview`
  for zero-write inspection and preserve migration evidence on failed gates.
  Do not manually delete old stores, snapshots, hooks, or backups to “fix” a
  status.
- Long-term memory writes go through `memoryguard_memory_write`; do not write
  host-native memory files (`%USERPROFILE%\.codex\memories`, `memory.md`, or
  similar) to bypass governance. Use `memoryguard_memory_search` and
  `memoryguard_memory_read` for retrieval.
- At the start of a new task, call `memoryguard_context_bootstrap` once with
  the task unless the host Hook already supplied this turn's bootstrap packet.
  Do not repeat it in the same task. History is separate raw evidence, not
  automatic long-term context; retrieve it progressively with history search,
  timeline, then explicitly authorized read.
- `memoryguard_memory_write` defaults to `injection_policy="relevant"`.
  Use `injection_policy="always"` only when the user explicitly asks for a
  mandatory/default rule. Facts, preferences, and ordinary procedures remain
  relevant. Keep `priority` bounded; never widen `audience` or overwrite
  trusted IDs without explicit scope.
- Updates preserve governed state. Deletes are soft deletes; use the MemoryGuard
  path for restore/delete decisions. Deduplication, supersede, conflict, and
  quarantine outcomes are evidence to inspect, not reasons to write a second
  copy manually.

## Privacy and local-only boundary

Current release documentation describes local SQLite storage and optional
usage telemetry that stays local and does not upload data. It says telemetry
does not store conversation bodies, account names, raw source paths, or
instance identifiers. Remote model or embedding operations remain explicit
opt-in behavior. Do not promise a privacy policy or terms URL that is not
published by this repository.

When reporting a problem, prefer command status, error codes, and non-reversible
digests over copying memory bodies, prompts, account data, or full config files.
