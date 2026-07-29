"""MCP stdio server（spec §1.2, §4.2）。

让宿主通过 MCP stdio 调用 MemoryGuard，获得结构化结果。
首期实现 MCP 2024-11-05 基础协议子集:
- initialize: 能力协商
- tools/list: 返回 memoryguard_audit/open/explain 工具
- tools/call: 执行工具，返回结构化结果

纯标准库实现，不依赖 MCP SDK。无网络。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .cli import run_audit, _load_report
from .report import render_html_report
from .schema import Report


# 写操作工具列表：执行前做本地参数预检，避免无效请求写入状态。
# 注：memoryguard_extract_memories 现为只读 preview（§8.5 两步流程步骤 1）。
#     memoryguard_accept_candidates 写入共享记忆（§8.5 步骤 2）。
_MUTATING_TOOLS = {
    "memoryguard_memory_write",
    "memoryguard_memory_update",
    "memoryguard_memory_delete",
    "memoryguard_binding_create",
    "memoryguard_external_mcp_import",
    "memoryguard_accept_candidates",
    "memoryguard_provider_install",
    "memoryguard_apply_enrichments",
}


# ---------------------------------------------------------------------------
# MCP 协议常量
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "memoryguard"
SERVER_VERSION = "0.1.0"

TOOLS = [
    {
        "name": "memoryguard_audit",
        "description": "Read-only scan of an Agent workspace: instructions, skills, memory, local RAG. Returns findings with evidence. No network, no writes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    {
        "name": "memoryguard_explain",
        "description": "Explain a finding's evidence, impact, suggestion, and confidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string", "description": "finding id from audit"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "memoryguard_list_sources",
        "description": "List authorized sources (project directory, selected folders, Obsidian vaults). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    {
        "name": "memoryguard_scan_summary",
        "description": "Run a read-only scan and return snapshot + coverage ledger. Proves scan completeness (unaccounted_count must be 0).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    {
        "name": "memoryguard_neuron_graph",
        "description": "Read the scoped neuron graph projection (read-only). Requires explicit agent_instance_id or share_group_id. Returns {empty: true, reason: 'not_built'|'missing_governance_scope'|...}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "mode": {"type": "string", "description": "native | reconstructed (default: reconstructed)"},
                "agent_instance_id": {"type": "string", "description": "single-agent governance scope"},
                "share_group_id": {"type": "string", "description": "MCP shared-memory scope (mutually exclusive with agent)"},
            },
        },
    },
    {
        "name": "memoryguard_import_preview",
        "description": "Preview an offline import bundle (ChatGPT/Claude/Gemini/Generic). Read-only detection + inventory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "bundle path (file or dir)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["path"],
        },
    },
    # --- v3.2 memory backend tools ---
    {
        "name": "memoryguard_memory_read",
        "description": "Read a single shared memory record by memory_id. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "memory record ID"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "memoryguard_memory_search",
        "description": "Search shared memory records by query, kind, or status. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search query"},
                "kind": {"type": "string", "description": "filter by kind: preference|fact|project|procedure|episode|correction"},
                "status": {"type": "string", "description": "filter by status: active (default)|low_confidence|shadowed|conflicted|quarantined|deleted"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "maximum results to return (default: 5 for conversation recall)"},
                "semantic": {"type": "string", "enum": ["off", "heuristic", "model"], "description": "optional semantic recall mode (default: off)"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
        },
    },
    {
        "name": "memoryguard_memory_write",
        "description": "Write a new memory record. Auto-organizes: classify, dedup, supersede, conflict, quarantine. Returns memory_id and auto_actions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "memory content"},
                "kind": {"type": "string", "description": "override kind (default: auto-classify). Valid: preference|fact|project|procedure|episode|correction"},
                "write_policy": {"type": "string", "description": "write policy: auto_accept (default) | auto_quarantine_on_risk | propose_only. propose_only creates a low_confidence candidate without modifying existing memories"},
                "metadata": {"type": "object", "description": "optional metadata from agent"},
                "idempotency_key": {"type": "string", "description": "optional retry key bound to content, metadata, kind and policy"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["body"],
        },
    },
    {
        "name": "memoryguard_memory_update",
        "description": "Update an existing memory record (body, kind, status). Records a DecisionEvent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "memory record ID"},
                "body": {"type": "string", "description": "new body"},
                "kind": {"type": "string", "description": "new kind"},
                "status": {"type": "string", "description": "new status"},
                "idempotency_key": {"type": "string", "description": "optional retry key bound to this target and payload"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "memoryguard_memory_delete",
        "description": "Soft-delete a memory record (status=deleted). Records a DecisionEvent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "memory record ID"},
                "idempotency_key": {"type": "string", "description": "optional retry key bound to this target"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "memoryguard_memory_status",
        "description": "Get shared memory group status: record counts, event counts, conflicts, quarantine. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
        },
    },
    {
        "name": "memoryguard_context_bootstrap",
        "description": (
            "Build one bounded, read-only long-term-memory context packet for a new task. "
            "Uses the trusted MCP identity/binding, includes active preferences plus "
            "task-relevant governed memories, omits sensitive/unsafe states, and never "
            "replaces or repeats the host's current conversation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "description": "current task or request; required",
                },
                "project_hint": {
                    "type": "string",
                    "description": "optional project/repository hint used only for relevance",
                },
                "max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 12,
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": 12000,
                    "default": 6000,
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    # --- v3.2 agent binding tools ---
    {
        "name": "memoryguard_binding_create",
        "description": "Bind an agent instance to a share_group. Creates an AgentBinding record (active). Read-only listing is via binding_list; unbind goes through CLI/GUI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_instance_id": {"type": "string", "description": "agent instance ID to bind"},
                "share_group_id": {"type": "string", "description": "share group ID to bind the agent into"},
                "mcp_server_name": {"type": "string", "description": "MCP server name (default: memoryguard)"},
                "native_memory_mode": {"type": "string", "description": "native memory mode: observed|redirected|unsupported (default: observed)"},
                "redirect_paths": {"type": "array", "items": {"type": "string"}, "description": "optional native memory redirect paths"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["agent_instance_id", "share_group_id"],
        },
    },
    {
        "name": "memoryguard_binding_list",
        "description": "List existing AgentBinding records. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_inactive": {"type": "boolean", "description": "include inactive bindings (default: true)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    # --- v3.2 external MCP descriptor tools ---
    {
        "name": "memoryguard_external_mcp_list",
        "description": "List imported external MCP server descriptors and their resources. Descriptor-level only (no live MCP client discovery). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    {
        "name": "memoryguard_external_mcp_import",
        "description": "Import a new external MCP descriptor (JSON). Classifies the server (L0-L4), persists it, and returns the detection result. Descriptor-level import only; does not call the live MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "descriptor_json": {"type": "string", "description": "JSON-encoded MCP descriptor {name|display_name, tools[], resources[], memory_entries[]}"},
                "server_id": {"type": "string", "description": "server ID (default: derived from descriptor name/display_name)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["descriptor_json"],
        },
    },
    # --- v3.2 document extraction tool (§8.5 两步流程) ---
    {
        "name": "memoryguard_extract_memories",
        "description": "Extract memory segments from a source file under an authorized source root (read-only preview). Returns candidate list with kind, risk_level, and preview. Does NOT write to shared memory. Use memoryguard_accept_candidates to write accepted candidates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "absolute or workspace-relative path to a source file under an authorized source root"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["source_path"],
        },
    },
    {
        "name": "memoryguard_accept_candidates",
        "description": "Accept extracted memory candidates through GovernanceEngine and write them to shared memory. Records governed automatic writes plus a DecisionEvent (action=accept_extract). Requires extract_id from a prior extract_memories call and explicit candidate_ids list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "extract_id": {"type": "string", "description": "extract_id returned by memoryguard_extract_memories preview"},
                "candidate_ids": {"type": "array", "items": {"type": "string"}, "description": "list of candidate_id values to accept (cannot be empty)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "share_group_id": {"type": "string", "description": "share group ID (default: default)"},
            },
            "required": ["extract_id", "candidate_ids"],
        },
    },
    # --- v3.2 semantic dedup tool ---
    {
        "name": "memoryguard_semantic_check",
        "description": "Check a new text against existing memories for semantic duplicates/conflicts (cross-lingual, paraphrase). Returns similar memories with similarity scores. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "new text to check"},
                "kind": {"type": "string", "description": "optional kind of the new memory, used for conflict detection"},
                "threshold": {"type": "number", "description": "similarity threshold (default: 0.85)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "share_group_id": {"type": "string", "description": "share group ID (default: default)"},
            },
            "required": ["text"],
        },
    },
    # --- v3.2 provider adapter tool ---
    {
        "name": "memoryguard_provider_install",
        "description": "Install/repair the provider's global MCP, redirect rules, and supported user-level lifecycle Hook (Claude/Codex/Cursor; TRAE reports MCP+rules fallback). Ensures the trusted Agent has a personal binding unless an explicit shared binding already exists. Requires admin capability; idempotent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "provider name: claude|codex|cursor|trae"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "agent_instance_id": {"type": "string", "description": "trusted Agent identity (normally from MEMORYGUARD_AGENT_ID)"},
            },
            "required": ["provider"],
        },
    },
    # --- v3.2 agent group resolution tool ---
    {
        "name": "memoryguard_resolve_group",
        "description": "Resolve which share_group_id an agent should write to, based on its AgentBinding. Read-only. Agents should call this before memory_write to know their group.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_instance_id": {"type": "string", "description": "agent instance ID to resolve"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["agent_instance_id"],
        },
    },
    # --- v3.3 host AI enrichment tools ---
    {
        "name": "memoryguard_list_pending_enrichments",
        "description": "List pending memory enrichment tasks. Skill path: after build_and_enrich returns host_action_required, YOU (host agent) must classify+translate each task and call apply_enrichments — do not ask the user to pick a CLI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "limit": {"type": "integer", "description": "max tasks to return (default: 50)"},
                "agent_instance_id": {"type": "string", "description": "filter by agent scope (optional)"},
                "share_group_id": {"type": "string", "description": "filter by share group scope (optional)"},
            },
        },
    },
    {
        "name": "memoryguard_apply_enrichments",
        "description": "Apply host-agent enrichment results to Memory IR / SharedMemoryStore. Each result: task_id, kind, title, body, confidence. After YOU enrich pending tasks, call this then memoryguard_build_and_enrich again to refresh the graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "results": {
                    "type": "array",
                    "description": "enrichment results to apply",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "kind": {"type": "string", "description": "preference|fact|project|procedure|episode|correction"},
                            "title": {"type": "string", "description": "translated/organized title"},
                            "body": {"type": "string", "description": "translated/organized body"},
                            "confidence": {"type": "number", "description": "0.0-1.0"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["task_id", "kind", "title", "body"],
                    },
                },
                "agent_instance_id": {"type": "string", "description": "scope filter (optional)"},
                "share_group_id": {"type": "string", "description": "share group scope (optional)"},
            },
            "required": ["results"],
        },
    },
    {
        "name": "memoryguard_enrichment_status",
        "description": "Check enrichment queue status: pending/applied counts. Primary enrich happens inside build_projection; use this to see residuals.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "agent_instance_id": {"type": "string", "description": "filter by agent scope (optional)"},
                "share_group_id": {"type": "string", "description": "filter by share group (optional)"},
            },
        },
    },
    # --- v3.3 build projection + auto enrich ---
    {
        "name": "memoryguard_build_and_enrich",
        "description": "Build memory projection. Default enrich_mode=host: YOU are the LLM. If pending_tasks / host_action_required, immediately classify+translate, call apply_enrichments, then call this again. Multi-agent GUI may pass enrich_mode=cli with a chosen Agent CLI. Do not require a separate AI-整理 button.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "agent_instance_id": {"type": "string", "description": "agent instance ID for scoped projection"},
                "mode": {"type": "string", "description": "projection mode: reconstructed (default) or native"},
                "share_group_id": {"type": "string", "description": "share group ID (optional)"},
                "enrich_mode": {"type": "string", "description": "host (default) | cli | auto | heuristic"},
                "llm_agent": {"type": "string", "description": "CLI agent id when enrich_mode=cli (codex|claude|cursor|…)"},
                "llm_cli": {"type": "string", "description": "CLI path when enrich_mode=cli"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行
# ---------------------------------------------------------------------------


def _mcp_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


def _preflight_mutating_tool(name: str, args: dict[str, Any], workspace: Path) -> dict[str, Any] | None:
    """Validate mutating requests before writing local state."""
    if name == "memoryguard_memory_write":
        if not str(args.get("body", "")).strip():
            return _mcp_error("body is required")
        return None

    if name in {"memoryguard_memory_update", "memoryguard_memory_delete"}:
        memory_id = str(args.get("memory_id", "")).strip()
        if not memory_id:
            return _mcp_error("memory_id is required")
        if name == "memoryguard_memory_update" and not any(args.get(k) for k in ("body", "kind", "status")):
            return _mcp_error("at least one update field is required: body, kind, or status")
        # 枚举校验在 preflight 做，非法值不落盘
        if name == "memoryguard_memory_update":
            from .schema_v3 import SharedMemoryStatus, MemoryKind
            _VALID_STATUSES = {s.value for s in SharedMemoryStatus}
            _VALID_KINDS = {k.value for k in MemoryKind}
            status_val = args.get("status")
            kind_val = args.get("kind")
            if status_val is not None and status_val not in _VALID_STATUSES:
                return _mcp_error(f"invalid status '{status_val}'. Valid: {sorted(_VALID_STATUSES)}")
            if kind_val is not None and kind_val not in _VALID_KINDS:
                return _mcp_error(f"invalid kind '{kind_val}'. Valid: {sorted(_VALID_KINDS)}")
        from .shared_memory_store import SharedMemoryStore

        group_id, access_err, _ = _resolve_access(
            args,
            workspace,
        )
        if access_err:
            return _mcp_error(access_err)
        try:
            store = SharedMemoryStore(workspace, group_id, read_only=True)
        except FileNotFoundError:
            return _mcp_error(f"group not found: {group_id}")
        target = store.get_record(memory_id)
        if target is None:
            return _mcp_error(f"memory not found: {memory_id}")
        return None

    if name == "memoryguard_binding_create":
        if not str(args.get("agent_instance_id", "")).strip():
            return _mcp_error("agent_instance_id is required")
        if not str(args.get("share_group_id", "")).strip():
            return _mcp_error("share_group_id is required")
        return None

    if name == "memoryguard_external_mcp_import":
        descriptor_raw = args.get("descriptor_json", "")
        try:
            descriptor = json.loads(descriptor_raw) if isinstance(descriptor_raw, str) else dict(descriptor_raw)
        except (ValueError, TypeError) as e:
            return _mcp_error(f"invalid descriptor_json: {e}")
        if not isinstance(descriptor, dict):
            return _mcp_error("descriptor_json must decode to an object")
        return None

    if name == "memoryguard_accept_candidates":
        extract_id = str(args.get("extract_id", "")).strip()
        if not extract_id:
            return _mcp_error("extract_id is required")
        candidate_ids = args.get("candidate_ids", [])
        if not candidate_ids or not isinstance(candidate_ids, list):
            return _mcp_error("candidate_ids must be a non-empty list")
        return None

    if name == "memoryguard_provider_install":
        provider = str(args.get("provider", "")).lower()
        if provider not in {"claude", "codex", "cursor", "trae"}:
            return _mcp_error(f"unknown provider '{provider}'. Supported: claude|codex|cursor|trae")
        return None

    return None


def _resolve_workspace(args: dict[str, Any]) -> Path:
    """解析稳定控制目录。

    用户级 MCP 会从任意项目启动，不能把宿主当前目录当作 MemoryGuard
    数据目录。显式参数仅供管理/测试覆盖；正常 Agent 连接走安装时写入的环境变量。
    """
    explicit = str(args.get("workspace", "") or "").strip()
    configured = os.environ.get("MEMORYGUARD_WORKSPACE", "").strip()
    return Path(explicit or configured or ".").expanduser().resolve()


def _resolve_memory_workspace(args: dict[str, Any]) -> Path:
    """共享记忆始终服从安装身份，防止对话参数把存储切到当前项目。"""
    configured = os.environ.get("MEMORYGUARD_WORKSPACE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _resolve_workspace(args)


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """执行工具，返回 MCP tool result。"""
    workspace = (
        _resolve_memory_workspace(args)
        if (
            name.startswith("memoryguard_memory_")
            or name == "memoryguard_context_bootstrap"
        )
        else _resolve_workspace(args)
    )

    # 写操作：先做本地参数预检，再执行
    if name in _MUTATING_TOOLS:
        preflight_err = _preflight_mutating_tool(name, args, workspace)
        if preflight_err is not None:
            return preflight_err

    if name == "memoryguard_audit":
        report = run_audit(workspace)
        # 写报告
        from .cli import ensure_layout, REPORTS_DIR

        ensure_layout(workspace)
        json_path = workspace / REPORTS_DIR / "report.json"
        html_path = workspace / REPORTS_DIR / "report.html"
        json_path.write_text(report.to_json(), encoding="utf-8")
        html_path.write_text(render_html_report(report), encoding="utf-8")
        return {
            "content": [
                {
                    "type": "text",
                    "text": _format_audit_text(report, str(html_path)),
                }
            ]
        }

    if name == "memoryguard_explain":
        report = _load_report(workspace)
        if report is None:
            return {"content": [{"type": "text", "text": "error: no report found"}], "isError": True}
        finding = next((f for f in report.findings if f.id == args.get("finding_id")), None)
        if finding is None:
            return {"content": [{"type": "text", "text": f"error: finding not found: {args.get('finding_id')}"}], "isError": True}
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Finding: {finding.id}\n"
                        f"  rule: {finding.rule_id}\n"
                        f"  severity: {finding.severity.value}\n"
                        f"  evidence: {finding.evidence}\n"
                        f"  impact: {finding.impact}\n"
                        f"  suggestion: {finding.suggestion}\n"
                        f"  confidence: {finding.confidence}\n"
                        f"  fixable: {finding.fixable}\n"
                    ),
                }
            ]
        }

    # --- v3 只读工具 ---

    if name == "memoryguard_list_sources":
        api = _get_governance_api(workspace)
        result = api.list_sources()
        text = f"Sources: {result['total']}\n"
        for s in result["sources"]:
            text += f"  - {s['root_id']}  {s['type']}  {s['display_name']}  scope={s['scope']}\n"
            text += f"      path: {s['path']}\n"
        return {"content": [{"type": "text", "text": text}]}

    if name == "memoryguard_scan_summary":
        api = _get_governance_api(workspace)
        result = api.scan_sources()
        cov = result["coverage"]
        text = (
            f"Snapshot: {result['snapshot_id']}\n"
            f"  created_at: {result['created_at']}\n"
            f"  source_objects: {result['source_object_count']}\n"
            f"  coverage: {cov['coverage_status']}\n"
            f"  candidates: {cov['candidate_count']}\n"
            f"  read: {cov['read']}  unsupported: {cov['unsupported']}  unreadable: {cov['unreadable']}\n"
            f"  skipped_by_policy: {cov['skipped_by_policy']}  unaccounted: {cov['unaccounted_count']}"
        )
        return {"content": [{"type": "text", "text": text}]}

    if name == "memoryguard_neuron_graph":
        api = _get_governance_api(workspace)
        agent_id = str(args.get("agent_instance_id", "") or "").strip()
        share_id = str(args.get("share_group_id", "") or "").strip()
        if agent_id and share_id:
            return {
                "content": [{
                    "type": "text",
                    "text": "error: conflicting_governance_scope; provide exactly one of agent_instance_id or share_group_id",
                }],
                "isError": True,
            }
        if not agent_id and not share_id:
            return {
                "content": [{
                    "type": "text",
                    "text": "error: missing_governance_scope; provide agent_instance_id or share_group_id",
                }],
                "isError": True,
            }
        mode = str(args.get("mode", "reconstructed") or "reconstructed")
        if share_id:
            proj = api.get_neuron_graph(mode=mode, share_group_id=share_id)
        else:
            proj = api.get_neuron_graph(mode=mode, agent_instance_id=agent_id)
        if proj.get("empty") or proj.get("error"):
            text = (
                f"Neuron graph: {proj.get('reason') or proj.get('error') or 'not_built'}\n"
                "Build via GUI/CLI with the same explicit scope."
            )
            return {"content": [{"type": "text", "text": text}]}
        nodes = proj.get("nodes", [])
        edges = proj.get("edges", [])
        scope = proj.get("scope") or {}
        text = (
            f"Neuron graph projection:\n"
            f"  scope: {scope}\n"
            f"  snapshot: {proj.get('snapshot_id', '')}\n"
            f"  built_at: {proj.get('built_at', '')}\n"
            f"  nodes: {len(nodes)}  edges: {len(edges)}\n"
            f"  nodes (first 10):"
        )
        for n in nodes[:10]:
            text += f"\n    [{n['node_kind']}] {n.get('label','')[:30]} provenance={n.get('provenance_count',0)}"
        return {"content": [{"type": "text", "text": text}]}

    if name == "memoryguard_import_preview":
        api = _get_governance_api(workspace)
        result = api.preview_import(args["path"])
        if "error" in result:
            return {"content": [{"type": "text", "text": f"error: {result['error']}"}], "isError": True}
        text = (
            f"Import preview:\n"
            f"  provider: {result.get('provider', 'unknown')}\n"
            f"  confidence: {result.get('confidence', 0)}\n"
            f"  notes: {result.get('notes', '')}\n"
            f"  inventory: {result.get('inventory', {})}"
        )
        return {"content": [{"type": "text", "text": text}]}

    # --- v3.2 memory backend tools ---
    if name == "memoryguard_memory_read":
        return _handle_memory_read(args)
    if name == "memoryguard_memory_search":
        return _handle_memory_search(args)
    if name == "memoryguard_memory_write":
        return _handle_memory_write(args)
    if name == "memoryguard_memory_update":
        return _handle_memory_update(args)
    if name == "memoryguard_memory_delete":
        return _handle_memory_delete(args)
    if name == "memoryguard_memory_status":
        return _handle_memory_status(args)
    if name == "memoryguard_context_bootstrap":
        return _handle_context_bootstrap(args)

    # --- v3.2 agent binding tools ---
    if name == "memoryguard_binding_create":
        # P0-A: binding_create 升为 admin capability,防止自助提权
        from .access_context import load_access_context
        ctx = load_access_context()
        ok, err = ctx.require_admin()
        if not ok:
            return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
        api = _get_governance_api(workspace)
        result = api.bind_agent(
            agent_instance_id=args["agent_instance_id"],
            share_group_id=args["share_group_id"],
            mcp_server_name=args.get("mcp_server_name", "memoryguard"),
            native_memory_mode=args.get("native_memory_mode", "observed"),
            redirect_paths=args.get("redirect_paths"),
        )
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

    if name == "memoryguard_binding_list":
        api = _get_governance_api(workspace)
        result = api.list_bindings(include_inactive=args.get("include_inactive", True))
        text = f"Bindings: {result['total']}\n"
        for b in result["bindings"]:
            text += (
                f"  - {b['binding_id'][:12]}  agent={b['agent_instance_id']}  "
                f"group={b['share_group_id']} kind={b.get('group_kind', 'shared')} "
                f"members={b.get('member_count', 0)} status={b['status']}  mode={b['native_memory_mode']}\n"
                f"      canonical_store: {b.get('canonical_store_path', '')}\n"
            )
        return {"content": [{"type": "text", "text": text}]}

    # --- v3.2 external MCP descriptor tools ---
    if name == "memoryguard_external_mcp_list":
        api = _get_governance_api(workspace)
        result = api.list_external_mcp_servers()
        text = f"External MCP servers: {result['total']}\n"
        for s in result["servers"]:
            text += (
                f"  - {s.get('server_id')}  level={s.get('level')}  "
                f"tools={s.get('tool_count', 0)}  resources={s.get('resource_count', 0)}\n"
                f"      display_name: {s.get('display_name', '')}\n"
            )
        return {"content": [{"type": "text", "text": text}]}

    if name == "memoryguard_external_mcp_import":
        descriptor_raw = args.get("descriptor_json", "")
        try:
            descriptor = (
                json.loads(descriptor_raw)
                if isinstance(descriptor_raw, str)
                else dict(descriptor_raw)
            )
        except (ValueError, TypeError) as e:
            return {"content": [{"type": "text", "text": f"error: invalid descriptor_json: {e}"}], "isError": True}
        server_id = (
            args.get("server_id")
            or descriptor.get("name")
            or descriptor.get("display_name")
            or descriptor.get("id")
            or "external-mcp"
        )
        api = _get_governance_api(workspace)
        result = api.detect_external_mcp(server_id, descriptor)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

    # --- v3.2 document extraction tool (§8.5 两步流程) ---
    if name == "memoryguard_extract_memories":
        source_path = Path(args["source_path"]).resolve()
        from .source_registry import SourceRegistry

        reg = SourceRegistry(workspace)
        root_id: str | None = None
        relative_path: str | None = None
        for root in reg.list_sources():
            root_path = Path(root.path).resolve()
            try:
                rel = source_path.relative_to(root_path)
            except ValueError:
                continue
            root_id = root.root_id
            relative_path = str(rel).replace("\\", "/")
            break
        if root_id is None:
            return {
                "content": [{"type": "text", "text": f"error: source_path not under any authorized source root: {source_path}"}],
                "isError": True,
            }
        api = _get_governance_api(workspace)
        result = api.extract_preview(root_id, relative_path)
        if "error" in result:
            return {"content": [{"type": "text", "text": f"error: {result['error']}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

    if name == "memoryguard_accept_candidates":
        api = _get_governance_api(workspace)
        result = api.accept_candidates(
            extract_id=args["extract_id"],
            candidate_ids=args["candidate_ids"],
            share_group_id=_get_share_group_id(args)[0],
        )
        if "error" in result:
            return {"content": [{"type": "text", "text": f"error: {result['error']}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

    # --- v3.2 semantic dedup tool ---
    if name == "memoryguard_semantic_check":
        from .semantic_dedup import SemanticDedup

        text_to_check = args["text"]
        kind = args.get("kind")
        threshold = args.get("threshold")
        group_id = _get_share_group_id(args)[0]
        sd = SemanticDedup(workspace, group_id)
        duplicates = sd.find_semantic_duplicates(text_to_check, threshold=threshold)
        conflicts = []
        if kind:
            conflicts = sd.find_semantic_conflicts(text_to_check, kind, threshold=threshold)
        result = {
            "duplicates": duplicates,
            "conflicts": conflicts,
            "checked_against": len(duplicates),
        }
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

    # --- v3.2 provider adapter tool ---
    if name == "memoryguard_provider_install":
        from .provider_adapters import ClaudeAdapter, CodexAdapter, CursorAdapter, TraeAdapter
        from .access_context import load_access_context
        from .agent_binding import AgentBindingStore

        provider = args.get("provider", "").lower()
        adapter_map = {
            "claude": ClaudeAdapter,
            "codex": CodexAdapter,
            "cursor": CursorAdapter,
            "trae": TraeAdapter,
        }
        adapter_cls = adapter_map.get(provider)
        if adapter_cls is None:
            return {"content": [{"type": "text", "text": f"error: unknown provider '{provider}'. Supported: claude|codex|cursor|trae"}], "isError": True}
        # 正式安装/接管时才创建个人组；已有共享绑定保持不变。
        ctx = load_access_context()
        ok_admin, admin_err = ctx.require_admin()
        if not ok_admin:
            return {"content": [{"type": "text", "text": f"error: {admin_err}"}], "isError": True}
        agent_id, identity_err = ctx.resolve_agent(str(args.get("agent_instance_id", "") or ""))
        if identity_err:
            return {"content": [{"type": "text", "text": f"error: {identity_err}"}], "isError": True}
        binding_store = AgentBindingStore(workspace)
        ensured = binding_store.ensure_personal_memory_group(agent_id)
        binding = ensured.get("binding") or {}
        group_id = str(binding.get("share_group_id", "") or ensured.get("group_id", ""))
        adapter = adapter_cls(str(workspace))
        result = adapter.install(
            str(workspace), share_group_id=group_id,
            agent_instance_id=agent_id, global_scope=True,
        )
        result["binding"] = ensured
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

    # --- v3.2 agent group resolution tool ---
    if name == "memoryguard_resolve_group":
        from .agent_binding import AgentBindingStore

        agent_id = args["agent_instance_id"]
        binding_store = AgentBindingStore(workspace)
        bindings = binding_store.find_by_agent(agent_id, include_inactive=False)
        if not bindings:
            result = {
                "share_group_id": None,
                "binding_id": None,
                "suggestion": f"Run 'memoryguard agent bind {agent_id}' first",
            }
        else:
            binding = bindings[0]
            result = {
                "share_group_id": binding.share_group_id,
                "binding_id": binding.binding_id,
                "native_memory_mode": binding.native_memory_mode.value,
            }
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

    # --- v3.3 host AI enrichment tools ---
    if name == "memoryguard_list_pending_enrichments":
        from .host_enrichment import list_pending
        limit = int(args.get("limit", 50))
        agent_id = args.get("agent_instance_id", "")
        share_group_id = args.get("share_group_id", "")
        tasks = list_pending(
            workspace, limit=limit,
            agent_instance_id=agent_id, share_group_id=share_group_id,
        )
        # 精简输出,给宿主 AI 的每条 task 包含分类和翻译所需的信息
        simplified = []
        for t in tasks:
            simplified.append({
                "task_id": t["task_id"],
                "memory_id": t["memory_id"],
                "ops": t["ops"],
                "input": t["input"],
                "hint": "classify kind + translate title/body to user's language; return task_id, kind, title_zh, body_zh, confidence",
            })
        result = {
            "pending_count": len(simplified),
            "tasks": simplified,
            "next_step": "classify/translate then call memoryguard_apply_enrichments",
        }
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}

    if name == "memoryguard_apply_enrichments":
        from .host_enrichment import apply_results
        results = args.get("results", [])
        if not results or not isinstance(results, list):
            return _mcp_error("results must be a non-empty list")
        agent_id = args.get("agent_instance_id", "")
        share_group_id = args.get("share_group_id", "")
        stats = apply_results(
            workspace, results,
            agent_instance_id=agent_id, share_group_id=share_group_id,
        )
        if stats.get("rebuild_suggested"):
            stats["next_step"] = "call build_projection / memoryguard_build_and_enrich to refresh graph"
        return {"content": [{"type": "text", "text": json.dumps(stats, ensure_ascii=False, indent=2)}]}

    if name == "memoryguard_enrichment_status":
        from .host_enrichment import get_status
        agent_id = args.get("agent_instance_id", "")
        share_group_id = args.get("share_group_id", "")
        status = get_status(workspace, agent_instance_id=agent_id, share_group_id=share_group_id)
        status["mode"] = "build_integrated"
        return {"content": [{"type": "text", "text": json.dumps(status, ensure_ascii=False, indent=2)}]}

    # --- v3.3 build projection (MCP 入口,含 LLM 整理) ---
    if name == "memoryguard_build_and_enrich":
        from .gui import GovernanceApi
        agent_id = args.get("agent_instance_id", "")
        mode = args.get("mode", "reconstructed")
        share_group_id = args.get("share_group_id", "")
        enrich_mode = str(args.get("enrich_mode", "host") or "host").strip().lower()
        llm_agent = str(args.get("llm_agent", "") or "")
        llm_cli = str(args.get("llm_cli", "") or "")
        if share_group_id:
            scope = {"mode": "share_group", "share_group_id": share_group_id}
        else:
            scope = {"mode": "agent", "agent_instance_id": agent_id} if agent_id else {"mode": "agent"}
        api = GovernanceApi(str(workspace))
        result = api.build_projection(
            confirmed=True, mode=mode, scope=scope,
            agent_instance_id=agent_id, share_group_id=share_group_id,
            enrich_mode=enrich_mode,
            llm_agent=llm_agent,
            llm_cli=llm_cli,
        )
        if "error" in result:
            return _mcp_error(result["error"])
        enr = result.get("enrichment", {}) or {}
        pending_tasks = enr.get("pending_tasks") or []
        host_needed = bool(enr.get("host_action_required") or pending_tasks)
        summary = {
            "projection_built": True,
            "node_count": len(result.get("nodes", [])),
            "scoped_record_count": result.get("scoped_record_count", 0),
            "enrichment": {
                k: v for k, v in enr.items() if k != "pending_tasks"
            },
            "pending_tasks": pending_tasks[:80],
            "host_action_required": host_needed,
            "host_protocol": {
                "you_are_the_llm": True,
                "steps": [
                    "If host_action_required: classify+translate each pending_task yourself (kind/title/body/confidence)",
                    "Call memoryguard_apply_enrichments with results",
                    "Call memoryguard_build_and_enrich again to refresh the neuron graph",
                ],
            } if host_needed else None,
            "next_step": (
                "HOST ACTION REQUIRED: you are the enricher. Apply enrichments then rebuild."
                if host_needed
                else f"构建完成；已整理 {enr.get('auto_applied', 0)} 条。"
            ),
        }
        return {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False, indent=2)}]}

    return {"content": [{"type": "text", "text": f"error: unknown tool {name}"}], "isError": True}


# ---------------------------------------------------------------------------
# v3.2 memory backend 工具处理
# ---------------------------------------------------------------------------


def _get_share_group_id(args: dict[str, Any], workspace: Path | None = None,
                        *, strict: bool | None = None) -> tuple[str, str | None]:
    """S1.2: 服务端解析 binding,禁止显式 group 覆盖。"""
    if strict is None:
        strict = os.environ.get("MEMORYGUARD_STRICT_BINDING", "") == "1"

    explicit_group = args.get("share_group_id", "default")
    # S1.2: 忽略请求中的显式 share_group_id,只从 binding 解析
    # (admin API 走独立路径,不经此函数)
    _ = explicit_group  # 保留变量但不使用

    agent_id = args.get("agent_instance_id", "")
    if not agent_id:
        if strict:
            return ("", "missing agent_instance_id; strict binding mode requires it")
        return ("default", None)

    # 查 AgentBinding，由 binding 派生 group_id
    ws = workspace or _resolve_memory_workspace(args)
    try:
        from .agent_binding import AgentBindingStore

        binding_store = AgentBindingStore(ws)
        bindings = binding_store.find_by_agent(agent_id, include_inactive=False)
        if len(bindings) > 1:
            return ("", f"multiple active bindings for agent_instance_id={agent_id!r}")
        if bindings:
            return (bindings[0].share_group_id, None)
    except Exception as e:
        if strict:
            return ("", f"failed to query binding for '{agent_id}': {e}")
        print(f"Warning: failed to query agent binding for '{agent_id}': {e}", file=sys.stderr)
        return ("default", None)

    # agent 存在但无 binding
    if strict:
        return ("", f"agent '{agent_id}' has no active binding; access denied in strict mode")
    warning_msg = (
        f"agent '{agent_id}' has no binding, writing to default group. "
        f"Run 'memoryguard agent bind {agent_id}' to bind."
    )
    print(f"Warning: {warning_msg}", file=sys.stderr)
    return ("default", warning_msg)


# ---------------------------------------------------------------------------
# P0-D: 统一 secret 脱敏入口(所有写入路径共用)
# ---------------------------------------------------------------------------


def _redact_secret(body: str) -> tuple[str, str]:
    """统一 secret 检测+脱敏。返回 (safe_body, secret_pattern)。

    secret_pattern 非空表示检测到 secret(已脱敏)。
    所有写入路径(write/update/edit/import/accept)必须调用此函数。
    """
    from .auto_organizer import SECRET_PATTERNS
    safe_body = body
    secret_hit = ""
    for pattern in SECRET_PATTERNS:
        if pattern.search(body):
            secret_hit = pattern.pattern[:50]
            break
    if secret_hit:
        for pattern in SECRET_PATTERNS:
            safe_body = pattern.sub("[REDACTED]", safe_body)
    return (safe_body, secret_hit)


def _resolve_access(
    args: dict[str, Any],
    workspace: Path,
) -> tuple[str | None, str | None, "AccessContext | None"]:
    """P0-A: 统一身份校验 + group 解析。

    返回 (group_id, error_message, access_ctx)。
    error_message 非空时调用方必须拒绝。
    Active trusted binding is the read/write capability boundary.  This API
    intentionally has no separate, unenforced ``require_write`` mode.
    """
    from .access_context import load_access_context
    ctx = load_access_context()

    # 可信 MCP 环境是身份事实源；请求参数缺省时自动注入，显式提供时仅做一致性校验。
    claimed_agent = str(args.get("agent_instance_id", "") or "")
    agent_id, err = ctx.resolve_agent(claimed_agent)
    if err:
        return (None, err, ctx)
    args["agent_instance_id"] = agent_id

    # P0-A: 默认 strict binding
    group_id, binding_err = _get_share_group_id(args, workspace, strict=ctx.strict_binding)
    if binding_err:
        return (None, binding_err, ctx)
    if not group_id:
        return (None, "no share_group_id resolved; access denied", ctx)
    maintenance_marker = (
        workspace / ".memoryguard" / "shared-memory" / group_id / ".maintenance"
    )
    if maintenance_marker.exists():
        return (None, f"memory group is in maintenance: {group_id}", ctx)

    return (group_id, None, ctx)


# ---------------------------------------------------------------------------
# Memory handlers(P0-A/B/D 全部加固)
# ---------------------------------------------------------------------------


def _handle_memory_read(args: dict[str, Any]) -> dict[str, Any]:
    """P0-B: 只读路径用 read_only=True,不存在的 group 不建库。"""
    from .shared_memory_store import SharedMemoryStore
    workspace = _resolve_memory_workspace(args)
    group_id, err, _ = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
    try:
        store = SharedMemoryStore(workspace, group_id, read_only=True)
    except FileNotFoundError:
        return {"content": [{"type": "text", "text": f"error: group not found: {group_id}"}], "isError": True}
    record = store.get_record(args["memory_id"])
    if record is None:
        return {"content": [{"type": "text", "text": f"error: memory not found: {args['memory_id']}"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(record.to_dict(), ensure_ascii=False, indent=2)}]}


def _handle_memory_search(args: dict[str, Any]) -> dict[str, Any]:
    """B1/B2: FTS5 全文搜索 + BM25 排序 + 可选语义召回 + 元数据。

    semantic 参数:off(默认)/heuristic/model
    """
    from .shared_memory_store import SharedMemoryStore
    workspace = _resolve_memory_workspace(args)
    group_id, err, _ = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
    try:
        store = SharedMemoryStore(workspace, group_id, read_only=True)
    except FileNotFoundError:
        return {"content": [{"type": "text", "text": f"error: group not found: {group_id}"}], "isError": True}
    query = args.get("query", "")
    kind = args.get("kind")
    # Conversation recall is fail-closed: historical/unsafe states require an
    # explicit governance query and must never enter prompts by omission.
    status = args.get("status") or "active"
    semantic = args.get("semantic", "off")
    limit = max(1, min(20, int(args.get("limit", 5))))

    # B1: FTS5 全文搜索(主路径)
    fts_results = store.search_fts(query, status=status, kind=kind, limit=limit)

    # B1: 可选语义召回(heuristic 用 HashBackend,model 用 provider embedding)
    semantic_results: list[dict] = []
    if semantic in ("heuristic", "model") and query:
        try:
            from .semantic_dedup import SemanticDedup
            dedup = SemanticDedup(workspace, group_id)
            # 用 semantic 查找相似记忆
            sem_dups = dedup.find_semantic_duplicates(query, threshold=0.60)
            fts_ids = {r["record"]["memory_id"] for r in fts_results}
            for dup in sem_dups:
                if dup.memory_id not in fts_ids:
                    rec = store.get_record(dup.memory_id)
                    if (
                        rec
                        and rec.status.value == status
                        and (not kind or rec.kind.value == kind)
                    ):
                        semantic_results.append({
                            "record": rec.to_dict(),
                            "bm25_score": 0.0,
                            "semantic_score": dup.similarity,
                            "share_group_id": group_id,
                            "agent_instance_id": rec.agent_instance_id,
                            "kind": rec.kind.value,
                            "provenance": rec.provenance,
                            "confidence": rec.confidence,
                        })
        except Exception:
            pass  # 语义召回失败不影响 FTS 结果

    # 合并结果
    all_results = fts_results + semantic_results[:limit - len(fts_results)]
    text = f"Found {len(fts_results)} FTS + {len(semantic_results)} semantic results:\n"
    for r in all_results[:limit]:
        rec = r["record"]
        score = r.get("bm25_score", 0.0)
        sem = r.get("semantic_score", "")
        sem_str = f" sem={sem:.2f}" if sem else ""
        text += (
            f"  [score={score:.3f}{sem_str}] "
            f"[{rec['status']}] [{rec['kind']}] {rec['memory_id'][:8]}  "
            f"agent={r.get('agent_instance_id', '?')[:12]}  {rec['body'][:60]}\n"
        )
    return {"content": [{"type": "text", "text": text}]}


def _handle_memory_write(args: dict[str, Any]) -> dict[str, Any]:
    """P0-A/D: 身份校验 + secret 脱敏。"""
    from .governance_engine import GovernanceEngine
    from .schema_v3 import MemoryEvent, stable_hash, _now_iso, MemoryKind, MemoryWritePolicy
    workspace = _resolve_memory_workspace(args)
    group_id, err, _ = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
    body = args["body"]
    agent_instance_id = args.get("agent_instance_id", "")
    metadata = args.get("metadata", {})
    kind_override = args.get("kind", "")
    write_policy = args.get("write_policy", "auto_accept")

    # 写入前校验枚举值
    _VALID_POLICIES = {p.value for p in MemoryWritePolicy}
    if write_policy not in _VALID_POLICIES:
        return {"content": [{"type": "text", "text": f"error: invalid write_policy '{write_policy}'. Valid: {sorted(_VALID_POLICIES)}"}], "isError": True}
    if kind_override:
        _VALID_KINDS = {k.value for k in MemoryKind}
        if kind_override not in _VALID_KINDS:
            return {"content": [{"type": "text", "text": f"error: invalid kind '{kind_override}'. Valid: {sorted(_VALID_KINDS)}"}], "isError": True}

    # P0-D: 统一 secret 脱敏
    safe_body, secret_hit = _redact_secret(body)
    if secret_hit:
        metadata = dict(metadata)
        metadata["_secret_detected"] = secret_hit

    # 创建事件(用脱敏后的 body)
    event = MemoryEvent(
        event_id=stable_hash("event", safe_body, _now_iso()),
        agent_instance_id=agent_instance_id,
        share_group_id=group_id,
        raw_content=safe_body,
        metadata=metadata,
        auto_actions=[],
        created_at=_now_iso(),
    )
    result = GovernanceEngine(workspace, group_id).auto_write(
        event,
        kind_override=kind_override,
        write_policy=write_policy,
        idempotency_key=str(args.get("idempotency_key", "") or ""),
    )
    if not result["ok"]:
        return _mcp_error(result["blocked_reason"])
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}


def _handle_memory_update(args: dict[str, Any]) -> dict[str, Any]:
    """Agent governance update; it is not a human/manual override."""
    from .governance_engine import GovernanceEngine
    from .schema_v3 import SharedMemoryStatus, MemoryKind
    workspace = _resolve_memory_workspace(args)
    group_id, err, _ = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
    memory_id = args["memory_id"]
    body = args.get("body")
    kind = args.get("kind")
    status = args.get("status")
    decision_actor = f"agent:{args.get('agent_instance_id', '') or 'unknown'}"

    # 写入前校验枚举值
    _VALID_STATUSES = {s.value for s in SharedMemoryStatus}
    _VALID_KINDS = {k.value for k in MemoryKind}
    if status is not None and status not in _VALID_STATUSES:
        return {"content": [{"type": "text", "text": f"error: invalid status '{status}'. Valid: {sorted(_VALID_STATUSES)}"}], "isError": True}
    if kind is not None and kind not in _VALID_KINDS:
        return {"content": [{"type": "text", "text": f"error: invalid kind '{kind}'. Valid: {sorted(_VALID_KINDS)}"}], "isError": True}

    # P0-D: body 更新前做 secret 脱敏
    if body is not None:
        safe_body, secret_hit = _redact_secret(body)
        body = safe_body
    result = GovernanceEngine(workspace, group_id).agent_update(
        memory_id,
        actor=decision_actor,
        body=body,
        kind=kind,
        status=status,
        idempotency_key=str(args.get("idempotency_key", "") or ""),
    )
    if not result["ok"]:
        return _mcp_error(result["blocked_reason"])
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}


def _handle_memory_delete(args: dict[str, Any]) -> dict[str, Any]:
    from .governance_engine import GovernanceEngine
    workspace = _resolve_memory_workspace(args)
    group_id, err, _ = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
    result = GovernanceEngine(workspace, group_id).agent_delete(
        args["memory_id"],
        actor=f"agent:{args.get('agent_instance_id', '') or 'unknown'}",
        idempotency_key=str(args.get("idempotency_key", "") or ""),
    )
    if not result["ok"]:
        return _mcp_error(result["blocked_reason"])
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}


def _handle_memory_status(args: dict[str, Any]) -> dict[str, Any]:
    """P0-B: 只读路径用 read_only=True。"""
    from .shared_memory_store import SharedMemoryStore
    workspace = _resolve_memory_workspace(args)
    group_id, err, _ = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
    try:
        store = SharedMemoryStore(workspace, group_id, read_only=True)
    except FileNotFoundError:
        return {"content": [{"type": "text", "text": f"error: group not found: {group_id}"}], "isError": True}
    status = store.status()
    return {"content": [{"type": "text", "text": json.dumps(status, ensure_ascii=False, indent=2)}]}


def _handle_context_bootstrap(args: dict[str, Any]) -> dict[str, Any]:
    """Return one trusted, bounded long-term-memory packet for a new task."""
    from .context_bootstrap import (
        DEFAULT_MAX_CHARS,
        DEFAULT_MAX_ITEMS,
        build_context_packet,
    )
    from .shared_memory_store import SharedMemoryStore

    task = str(args.get("task", "") or "").strip()
    if not task:
        return _mcp_error("task is required")
    workspace = _resolve_memory_workspace(args)
    group_id, err, _ = _resolve_access(args, workspace)
    if err:
        return _mcp_error(err)
    try:
        store = SharedMemoryStore(workspace, group_id, read_only=True)
    except FileNotFoundError:
        return _mcp_error(f"group not found: {group_id}")
    try:
        packet = build_context_packet(
            store,
            task=task,
            project_hint=str(args.get("project_hint", "") or ""),
            max_items=int(args.get("max_items", DEFAULT_MAX_ITEMS)),
            max_chars=int(args.get("max_chars", DEFAULT_MAX_CHARS)),
        )
    except (TypeError, ValueError) as exc:
        return _mcp_error(str(exc))
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(packet, ensure_ascii=False, indent=2),
        }],
    }


# ---------------------------------------------------------------------------
# GovernanceApi 缓存（同一 workspace 复用，避免每次 tool call 重建神经树）
# ---------------------------------------------------------------------------

_api_cache: dict[str, Any] = {}


def _get_governance_api(workspace: Path):
    """获取或创建 workspace 对应的 GovernanceApi 实例。"""
    key = str(workspace)
    if key not in _api_cache:
        from .gui import GovernanceApi

        _api_cache[key] = GovernanceApi(str(workspace))
    return _api_cache[key]


def _format_audit_text(report: Report, html_path: str) -> str:
    """格式化 audit 结果为文本摘要。"""
    s = report.summary()
    lines = [
        f"MemoryGuard audit complete in {report.duration_ms} ms",
        f"  workspace: {report.workspace}",
        f"  objects: {s['object_count']}",
        f"  findings: {len(report.findings)}",
        f"  invisible: {s['invisible_count']}",
        f"  health: {report.health_score}/100",
        f"  report: {html_path}",
    ]
    if report.findings:
        lines.append("  findings detail:")
        for f in report.findings[:20]:
            lines.append(f"    [{f.severity.value}] {f.rule_id}: {f.evidence[:80]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON-RPC 处理
# ---------------------------------------------------------------------------


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """处理单个 JSON-RPC 请求，返回响应 dict（通知返回 None）。"""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {}},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            result = execute_tool(name, args)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"tool execution failed: {e}"},
            }

    if method == "notifications/initialized":
        return None  # 通知，无响应

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve_stdio() -> int:
    """MCP stdio 主循环。从 stdin 读 JSON-RPC，向 stdout 写响应。"""
    # MCP stdio 协议固定使用 UTF-8。Windows 中文系统的管道默认可能是
    # GBK；工具描述或记忆正文含中文时会让宿主无法解码整条 JSON-RPC。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")
    # A3: 启动预检,打印身份与权限态
    from .access_context import preflight_check
    preflight_check()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}}
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue
        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve_stdio())
