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
from .history_api import TOOL_DEFINITIONS as HISTORY_TOOL_DEFINITIONS
from .history_api import handle_history_tool
from .knowledge_mcp import KNOWLEDGE_TOOL_DEFINITIONS
from .knowledge_mcp import handle_knowledge_tool


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
    "memoryguard_history_delete",
    "memoryguard_rule_feedback",
    "memoryguard_rule_create_auto",
    "memoryguard_rule_undo",
    "memoryguard_rule_merge_capability_issue",
    "memoryguard_rule_merge_approve",
    "memoryguard_rule_merge_acknowledge",
    "memoryguard_rule_merge_cooldown_clear",
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
                "injection_policy": {"type": "string", "enum": ["relevant", "always"], "default": "relevant", "description": "relevant participates in task recall; always is a mandatory rule"},
                "priority": {"type": "integer", "minimum": -100, "maximum": 100, "default": 0, "description": "stable ordering within the mandatory rule package"},
                "audience": {"type": "array", "minItems": 1, "description": "explicit mandatory-rule assignments; required when injection_policy=always", "items": {"type": "object"}},
                "write_policy": {"type": "string", "description": "write policy: auto_accept (default) | auto_quarantine_on_risk | propose_only. propose_only creates a low_confidence candidate without modifying existing memories"},
                "metadata": {"type": "object", "description": "optional metadata from agent"},
                "idempotency_key": {"type": "string", "description": "optional retry key bound to content, metadata, kind and policy"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["body"],
            "allOf": [{
                "if": {
                    "properties": {"injection_policy": {"const": "always"}},
                    "required": ["injection_policy"],
                },
                "then": {"required": ["audience"]},
            }],
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
                "injection_policy": {"type": "string", "enum": ["relevant", "always"], "description": "new injection policy"},
                "priority": {"type": "integer", "minimum": -100, "maximum": 100, "description": "new priority"},
                "audience": {"type": "array", "minItems": 1, "description": "explicit mandatory-rule assignments; required when transitioning to injection_policy=always", "items": {"type": "object"}},
                "idempotency_key": {"type": "string", "description": "optional retry key bound to this target and payload"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["memory_id"],
            "allOf": [{
                "if": {
                    "properties": {"injection_policy": {"const": "always"}},
                    "required": ["injection_policy"],
                },
                "then": {"required": ["audience"]},
            }],
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
                "read_path": {
                    "type": "string",
                    "enum": ["auto", "legacy", "rule-intelligence"],
                    "default": "legacy",
                    "description": "Phase5 canonical read path (shadow by "
                    "default): legacy forces the old path; auto / "
                    "rule-intelligence prefer the rule-intelligence layer when "
                    "data exists, deduplicating merged duplicates only after "
                    "the active/audience/exclude match",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_feedback",
        "description": (
            "Record explicit evidence for a mandatory-rule bootstrap match. "
            "This closes the loop for follow/violate/not_applicable/corrected decisions. "
            "One feedback is bound to one receipt_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "agent_instance_id": {"type": "string", "description": "trusted identity check"},
                "receipt_id": {
                    "type": "string",
                    "description": "receipt_id returned by memoryguard_context_bootstrap",
                },
                "outcome": {
                    "type": "string",
                    "enum": [
                        "followed",
                        "violated",
                        "not_applicable",
                        "corrected",
                        "exception",
                        "ignored",
                    ],
                    "description": "observed outcome after bootstrap packet is shown",
                },
                "actor": {
                    "type": "string",
                    "description": (
                        "deprecated display actor id; source/authority are fixed by MCP "
                        "transport and never inferred from this value"
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": "optional evidence/notes",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "confidence score 0-1",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "optional retry key bound to content and actor",
                },
            },
            # The caller cannot select the producer.  Older clients may still
            # send a display actor; when omitted the handler derives one from
            # the trusted transport identity.
            "required": ["receipt_id", "outcome"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_create_auto",
        "description": (
            "Create one mandatory rule from text. Automatic scope inference is fail-closed: "
            "only the trusted current agent or that agent plus the trusted project cwd are allowed. "
            "Broader scope requires explicit manual=true, an explicit scope object, and admin capability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "description": "rule text"},
                "kind": {"type": "string", "description": "optional preference|fact|project|procedure|episode|correction"},
                "priority": {"type": "integer", "minimum": -100, "maximum": 100, "default": 0},
                "scope": {"type": "object", "description": "optional explicit audience assignment; auto mode still rejects broad targets"},
                "manual": {"type": "boolean", "default": False, "description": "explicit human/admin declaration for broad scope"},
                "idempotency_key": {"type": "string"},
                "workspace": {"type": "string", "description": "workspace path (default: configured MemoryGuard workspace)"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_decision_read",
        "description": "Read one explainable rule lifecycle decision by decision_id. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "decision_id": {"type": "string"},
                "workspace": {"type": "string"},
            },
            "required": ["decision_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_undo",
        "description": "Undo a rule lifecycle mutation using its persisted pre-rule undo_id. Requires the trusted actor or admin capability.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "undo_id": {"type": "string"},
                "decision_id": {"type": "string", "description": "optional decision id alias; resolved to its undo_id"},
                "workspace": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_scope_stats",
        "description": "Read rule audience statistics and the automatic scope policy. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"workspace": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_capability_issue",
        "description": (
            "Issue one opaque, single-use rule-merge capability for a candidate "
            "proposal. Requires the trusted admin AccessContext. The raw token "
            "is returned once to the caller; persistent storage keeps only its hash."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "ttl_seconds": {"type": "number", "exclusiveMinimum": 0, "default": 300},
                "workspace": {"type": "string"},
            },
            "required": ["proposal_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_approve",
        "description": (
            "Approve one candidate rule-merge proposal with a server-issued "
            "single-use capability and trusted admin AccessContext."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "capability_token": {"type": "string"},
                "expected_definition_revisions": {"type": "object"},
                "workspace": {"type": "string"},
            },
            "required": ["proposal_id", "capability_token"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_acknowledge",
        "description": (
            "Acknowledge first-merge risk with a server-issued single-use "
            "capability and trusted admin AccessContext."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "capability_token": {"type": "string"},
                "workspace": {"type": "string"},
            },
            "required": ["proposal_id", "capability_token"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_cooldown_clear",
        "description": (
            "Clear one rule-merge proposal cooldown with a server-issued "
            "single-use capability and trusted admin AccessContext."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "capability_token": {"type": "string"},
                "workspace": {"type": "string"},
            },
            "required": ["proposal_id", "capability_token"],
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
        "description": "List pending memory enrichment tasks. For mandatory rules, first group tasks by the exact assignments in input.assignments: produce one consolidated scope bundle, keep every independent constraint, remove inherited/semantic duplicates, and mark replaced tasks with supersedes_task_ids. True conflicts stay separate. Then call apply_enrichments — do not ask the user to pick a CLI.",
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
        "description": "Apply host-agent enrichment results to Memory IR / SharedMemoryStore. Ordinary result: task_id, kind, title, body, confidence. A consolidated mandatory scope bundle additionally supplies supersedes_task_ids and matching supersedes_memory_ids; application atomically replaces/shadows those old rules. After applying, call memoryguard_build_and_enrich again.",
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
                            "supersedes_task_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "pending tasks atomically replaced by this same-scope bundle",
                            },
                            "supersedes_memory_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "memory IDs corresponding exactly to supersedes_task_ids",
                            },
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
        "description": "Build memory projection. Default enrich_mode=host: YOU are the LLM. If pending_tasks / host_action_required, classify and organize ordinary memories; consolidate mandatory rules into one versioned bundle per exact scope, then call apply_enrichments and build again. Multi-agent GUI may pass enrich_mode=cli with a chosen Agent CLI.",
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

# Raw history uses a physically separate SQLite archive.  Keep its MCP
# surface out of the long-term-memory tool family so it cannot accidentally
# participate in bootstrap or a SharedMemoryRecord write path.
TOOLS.extend(HISTORY_TOOL_DEFINITIONS)
TOOLS.extend([
    {
        "name": "memoryguard_history_list_sessions",
        "description": "List the trusted Agent's local conversation-history sessions. Read-only; raw text is not returned.",
        "inputSchema": {"type": "object", "properties": {
            "scope": {"type": "object"}, "limit": {"type": "integer"},
            "offset": {"type": "integer"}, "extracted": {"type": "boolean"},
            "date_from": {"type": "string"}, "date_to": {"type": "string"},
        }},
    },
    {
        "name": "memoryguard_history_export",
        "description": "Export explicitly selected sessions owned by the trusted Agent. This is raw-history evidence, not long-term memory.",
        "inputSchema": {"type": "object", "properties": {
            "session_ids": {"type": "array", "items": {"type": "string"}},
            "scope": {"type": "object"},
        }, "required": ["session_ids"]},
    },
    {
        "name": "memoryguard_history_delete",
        "description": "Permanently delete explicitly selected raw-history sessions for the trusted Agent. Requires confirmed=true; never deletes long-term memories.",
        "inputSchema": {"type": "object", "properties": {
            "session_ids": {"type": "array", "items": {"type": "string"}},
            "scope": {"type": "object"}, "invalidate_evidence": {"type": "boolean"},
            "confirmed": {"type": "boolean"},
        }, "required": ["session_ids", "confirmed"]},
    },
])

# 知识书库工具（只读）
TOOLS.extend(KNOWLEDGE_TOOL_DEFINITIONS)


# ---------------------------------------------------------------------------
# 工具执行
# ---------------------------------------------------------------------------


def _mcp_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


def _preflight_mutating_tool(name: str, args: dict[str, Any], workspace: Path) -> dict[str, Any] | None:
    """Validate mutating requests before writing local state."""
    if name == "memoryguard_rule_create_auto":
        if not str(args.get("text", "") or "").strip():
            return _mcp_error("text is required")
        if args.get("scope") is not None and not isinstance(args.get("scope"), dict):
            return _mcp_error("scope must be an object")
        if args.get("manual") and args.get("scope") is None:
            return _mcp_error("manual rule creation requires an explicit scope")
        return None

    if name == "memoryguard_rule_undo":
        if not str(args.get("undo_id", "") or "").strip() and not str(args.get("decision_id", "") or "").strip():
            return _mcp_error("undo_id or decision_id is required")
        return None

    if name == "memoryguard_history_delete":
        session_ids = args.get("session_ids")
        if not isinstance(session_ids, list) or not any(str(item).strip() for item in session_ids):
            return _mcp_error("session_ids must be a non-empty list")
        if args.get("confirmed") is not True:
            return _mcp_error("history deletion requires confirmed=true")
        return None

    if name == "memoryguard_memory_write":
        if not str(args.get("body", "")).strip():
            return _mcp_error("body is required")
        return None

    if name in {"memoryguard_memory_update", "memoryguard_memory_delete"}:
        memory_id = str(args.get("memory_id", "")).strip()
        if not memory_id:
            return _mcp_error("memory_id is required")
        if name == "memoryguard_memory_update" and not any(
            key in args for key in ("body", "kind", "status", "injection_policy", "priority", "audience")
        ):
            return _mcp_error(
                "at least one update field is required: body, kind, status, injection_policy, priority, or audience"
            )
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

    if name == "memoryguard_rule_feedback":
        receipt_id = str(args.get("receipt_id", "") or "").strip()
        if not receipt_id:
            return _mcp_error("receipt_id is required")
        outcome = str(args.get("outcome", "") or "").strip()
        if outcome not in {
            "followed", "violated", "not_applicable", "corrected",
            "exception", "ignored",
        }:
            return _mcp_error(
                "outcome must be one of: "
                "followed|violated|not_applicable|corrected|exception|ignored"
            )
        actor = str(args.get("actor", "") or "").strip()
        if not actor:
            return _mcp_error("actor is required")
        confidence = args.get("confidence")
        if confidence is None:
            return None
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            return _mcp_error("confidence must be numeric between 0 and 1")
        if not 0 <= confidence_value <= 1:
            return _mcp_error("confidence must be between 0 and 1")
        return None

    if name == "memoryguard_rule_merge_capability_issue":
        if not str(args.get("proposal_id", "") or "").strip():
            return _mcp_error("proposal_id is required")
        if "ttl_seconds" in args:
            try:
                ttl_seconds = float(args["ttl_seconds"])
            except (TypeError, ValueError):
                return _mcp_error("ttl_seconds must be a positive number")
            if ttl_seconds <= 0:
                return _mcp_error("ttl_seconds must be a positive number")
        return None

    if name in {
        "memoryguard_rule_merge_approve",
        "memoryguard_rule_merge_acknowledge",
        "memoryguard_rule_merge_cooldown_clear",
    }:
        if not str(args.get("proposal_id", "") or "").strip():
            return _mcp_error("proposal_id is required")
        token = args.get("capability_token")
        if not isinstance(token, str) or not token.strip():
            return _mcp_error("capability_token is required")
        if name == "memoryguard_rule_merge_approve":
            revisions = args.get("expected_definition_revisions")
            if revisions is not None and not isinstance(revisions, dict):
                return _mcp_error("expected_definition_revisions must be an object")
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
            or name.startswith("memoryguard_history_")
            or name == "memoryguard_context_bootstrap"
            or name == "memoryguard_rule_feedback"
            or name.startswith("memoryguard_rule_")
        )
        else _resolve_workspace(args)
    )

    # 知识书库工具（只读，不参与写操作预检）
    if name.startswith("memoryguard_knowledge_"):
        return handle_knowledge_tool(name, args, workspace) or _mcp_error(f"unknown knowledge tool: {name}")

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
    if name == "memoryguard_rule_feedback":
        return _handle_rule_feedback(args)
    if name == "memoryguard_rule_create_auto":
        return _handle_rule_create_auto(args)
    if name == "memoryguard_rule_decision_read":
        return _handle_rule_decision_read(args)
    if name == "memoryguard_rule_undo":
        return _handle_rule_undo(args)
    if name == "memoryguard_rule_scope_stats":
        return _handle_rule_scope_stats(args)
    if name == "memoryguard_rule_merge_capability_issue":
        return _handle_rule_merge_capability_issue(args)
    if name == "memoryguard_rule_merge_approve":
        return _handle_rule_merge_approve(args)
    if name == "memoryguard_rule_merge_acknowledge":
        return _handle_rule_merge_acknowledge(args)
    if name == "memoryguard_rule_merge_cooldown_clear":
        return _handle_rule_merge_cooldown_clear(args)
    if name.startswith("memoryguard_history_"):
        return _handle_history(args, name)

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
                "hint": "ordinary memory: classify/translate. mandatory rules: group identical input.assignments into one versioned bundle; keep all independent constraints, remove duplicates, and return supersedes_task_ids + supersedes_memory_ids for replaced tasks; never auto-resolve a true conflict",
            })
        result = {
            "pending_count": len(simplified),
            "tasks": simplified,
            "next_step": "consolidate mandatory rules per exact scope, classify/translate ordinary memories, then call memoryguard_apply_enrichments",
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
        if stats.get("build_status") == "reconciliation_failed":
            stats["next_step"] = "统一收敛失败；保持候选状态，禁止宣称 canonical_ready"
        elif stats.get("rebuild_suggested"):
            stats["next_step"] = "call build_projection / memoryguard_build_and_enrich to refresh graph"
        response = {
            "content": [{
                "type": "text",
                "text": json.dumps(stats, ensure_ascii=False, indent=2),
            }],
        }
        if stats.get("build_status") == "reconciliation_failed":
            response["isError"] = True
        return response

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
            if result.get("build_status") == "reconciliation_failed":
                failure = {
                    "projection_built": False,
                    "build_status": result.get("build_status"),
                    "reconciliation": result.get("reconciliation"),
                    "enrichment": result.get("enrichment", {}),
                    "error": result.get("error", "rule reconciliation failed"),
                }
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(failure, ensure_ascii=False, indent=2),
                    }],
                    "isError": True,
                }
            return _mcp_error(result["error"])
        enr = result.get("enrichment", {}) or {}
        pending_tasks = enr.get("pending_tasks") or []
        host_needed = bool(enr.get("host_action_required") or pending_tasks)
        build_status = result.get("build_status") or enr.get("build_status", "")
        summary = {
            "projection_built": build_status != "reconciliation_failed",
            "build_status": build_status,
            "reconciliation": result.get("reconciliation") or enr.get("reconciliation"),
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
                    "If host_action_required: organize ordinary memories and consolidate mandatory rules per exact scope (one bundle + supersedes lists)",
                    "Call memoryguard_apply_enrichments with results",
                    "Call memoryguard_build_and_enrich again to refresh the neuron graph",
                ],
            } if host_needed else None,
            "next_step": (
                "HOST ACTION REQUIRED: you are the enricher. Apply enrichments then rebuild."
                if host_needed
                else "规则候选待统一收敛；不得显示为 canonical_ready。"
                if build_status == "rule_candidates_pending"
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


def _effective_agent_context(args: dict[str, Any], group_id: str):
    """Build scope only from trusted connection/runtime environment.

    Clients cannot claim a provider or a sub-agent role in a tool call.  Hosts
    set these fields when launching their MCP process; absent role stays empty
    and therefore cannot match role-scoped mandatory rules.
    """
    from .schema_v3 import EffectiveAgentContext
    from .rule_scope import canonical_project_ref
    from .access_context import load_access_context
    access_context = load_access_context()
    return EffectiveAgentContext(
        agent_instance_id=str(args.get("agent_instance_id", "") or ""),
        share_group_id=group_id,
        provider=os.environ.get("MEMORYGUARD_PROVIDER", "").strip().lower(),
        project_ref=canonical_project_ref(
            os.environ.get("MEMORYGUARD_PROJECT_CWD") or os.getcwd()
        ),
        runtime_role=os.environ.get("MEMORYGUARD_RUNTIME_ROLE", "").strip(),
        runtime_agent_id=os.environ.get("MEMORYGUARD_RUNTIME_AGENT_ID", "").strip(),
        parent_agent_id=os.environ.get("MEMORYGUARD_PARENT_AGENT_ID", "").strip(),
        # Session/context identity is a trusted host launch fact.  Never read
        # these from the MCP request body: feedback/narrowing must not be able
        # to manufacture a second session by changing ordinary tool args.
        session_id=access_context.session_id,
        context_hash=os.environ.get("MEMORYGUARD_CONTEXT_HASH", "").strip(),
        session_trusted=access_context.session_trusted,
        session_source=access_context.session_source,
    )


def _authorized_audience(raw: Any, *, memory_id: str, actor_agent_id: str, is_admin: bool) -> list[dict[str, Any]]:
    from .rule_scope import can_manage_assignment, normalize_assignment
    if not isinstance(raw, list):
        raise ValueError("audience must be an array")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("audience entries must be objects")
        assignment = normalize_assignment({**item, "memory_id": memory_id})
        if not can_manage_assignment(assignment, actor_agent_id=actor_agent_id, is_admin=is_admin):
            raise ValueError("admin capability required for non-self rule audience")
        result.append(assignment.to_dict())
    return result


def _authorize_rule_mutation(
    store: Any, memory_id: str, *, actor_agent_id: str, is_admin: bool,
) -> tuple[Any | None, str]:
    """Authorize mutation from persisted ownership/audience, never request claims."""
    from .rule_scope import can_manage_assignment

    record = store.get_record(memory_id)
    if record is None:
        return None, "memory_not_found"
    if is_admin:
        return record, ""
    assignments = store.list_rule_assignments(memory_id)
    if record.injection_policy == "always":
        includes = [item for item in assignments if item.effect == "include"]
        if (
            len(assignments) != 1
            or len(includes) != 1
            or not can_manage_assignment(
                includes[0],
                actor_agent_id=actor_agent_id,
                is_admin=False,
            )
        ):
            return record, "admin capability required for non-self rule mutation"
        return record, ""
    if record.agent_instance_id != actor_agent_id:
        return record, "memory mutation denied: record is owned by another agent"
    return record, ""


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
    payload = record.to_dict()
    payload["assignments"] = [item.to_dict() for item in store.list_rule_assignments(record.memory_id)]
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]}


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
    for item in fts_results:
        item["record"]["assignments"] = [
            assignment.to_dict()
            for assignment in store.list_rule_assignments(item["record"]["memory_id"])
        ]

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
    from .schema_v3 import MemoryEvent, stable_hash, _now_iso, MemoryKind, MemoryWritePolicy, validate_injection_settings
    workspace = _resolve_memory_workspace(args)
    group_id, err, access_ctx = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
    body = args["body"]
    agent_instance_id = args.get("agent_instance_id", "")
    metadata = args.get("metadata", {})
    kind_override = args.get("kind", "")
    write_policy = args.get("write_policy", "auto_accept")
    injection_policy = args.get("injection_policy", "relevant")
    priority = args.get("priority", 0)
    audience = args.get("audience")

    # 写入前校验枚举值
    _VALID_POLICIES = {p.value for p in MemoryWritePolicy}
    if write_policy not in _VALID_POLICIES:
        return {"content": [{"type": "text", "text": f"error: invalid write_policy '{write_policy}'. Valid: {sorted(_VALID_POLICIES)}"}], "isError": True}
    if kind_override:
        _VALID_KINDS = {k.value for k in MemoryKind}
        if kind_override not in _VALID_KINDS:
            return {"content": [{"type": "text", "text": f"error: invalid kind '{kind_override}'. Valid: {sorted(_VALID_KINDS)}"}], "isError": True}
    try:
        injection_policy, priority = validate_injection_settings(injection_policy, priority)
        if injection_policy == "always" and not audience:
            raise ValueError(
                "injection_policy=always requires a non-empty explicit audience"
            )
        if audience is not None and injection_policy != "always":
            raise ValueError("audience is only valid for injection_policy=always")
        requested_audience = (
            _authorized_audience(audience, memory_id="pending", actor_agent_id=args["agent_instance_id"], is_admin=bool(access_ctx and access_ctx.is_admin))
            if audience is not None else []
        )
        if injection_policy == "always" and not requested_audience:
            raise ValueError(
                "injection_policy=always requires a non-empty explicit audience"
            )
    except ValueError as exc:
        return _mcp_error(str(exc))

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
        injection_policy=injection_policy,
        priority=priority,
        rule_assignments=(
            requested_audience if injection_policy == "always" else []
        ),
        idempotency_key=str(args.get("idempotency_key", "") or ""),
    )
    if not result["ok"]:
        return _mcp_error(result["blocked_reason"])
    if injection_policy == "always":
        from .shared_memory_store import SharedMemoryStore
        memory_id = result.get("memory_id", "")
        assignments = SharedMemoryStore(
            workspace, group_id, read_only=True,
        ).list_rule_assignments(memory_id)
        result["assignments"] = [item.to_dict() for item in assignments]
    result["record"] = result.get("after")
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}


def _handle_memory_update(args: dict[str, Any]) -> dict[str, Any]:
    """Agent governance update; it is not a human/manual override."""
    from .governance_engine import GovernanceEngine
    from .schema_v3 import (
        DecisionEvent,
        MemoryKind,
        SharedMemoryStatus,
        _now_iso,
        stable_hash,
        validate_injection_settings,
    )
    workspace = _resolve_memory_workspace(args)
    group_id, err, access_ctx = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
    memory_id = args["memory_id"]
    body = args.get("body")
    kind = args.get("kind")
    status = args.get("status")
    injection_policy = args.get("injection_policy")
    injection_policy_explicit = injection_policy is not None
    priority = args.get("priority")
    audience = args.get("audience")
    decision_actor = f"agent:{args.get('agent_instance_id', '') or 'unknown'}"
    prepared_audience: list[dict[str, Any]] | None = None
    engine = GovernanceEngine(workspace, group_id)
    current, mutation_error = _authorize_rule_mutation(
        engine.store, memory_id,
        actor_agent_id=args["agent_instance_id"],
        is_admin=bool(access_ctx and access_ctx.is_admin),
    )
    if mutation_error:
        return _mcp_error(mutation_error)

    # 写入前校验枚举值
    _VALID_STATUSES = {s.value for s in SharedMemoryStatus}
    _VALID_KINDS = {k.value for k in MemoryKind}
    if status is not None and status not in _VALID_STATUSES:
        return {"content": [{"type": "text", "text": f"error: invalid status '{status}'. Valid: {sorted(_VALID_STATUSES)}"}], "isError": True}
    if kind is not None and kind not in _VALID_KINDS:
        return {"content": [{"type": "text", "text": f"error: invalid kind '{kind}'. Valid: {sorted(_VALID_KINDS)}"}], "isError": True}
    if injection_policy is not None or priority is not None:
        try:
            injection_policy, priority = validate_injection_settings(
                injection_policy if injection_policy is not None else current.injection_policy,
                priority if priority is not None else current.priority,
            )
        except ValueError as exc:
            return _mcp_error(str(exc))
    if audience is not None:
        intended_policy = injection_policy if injection_policy is not None else current.injection_policy
        if intended_policy != "always":
            return _mcp_error("audience is only valid for injection_policy=always")
        try:
            prepared_audience = _authorized_audience(
                audience, memory_id=memory_id,
                actor_agent_id=args["agent_instance_id"],
                is_admin=bool(access_ctx and access_ctx.is_admin),
            )
        except ValueError as exc:
            return _mcp_error(str(exc))
        if not prepared_audience:
            return _mcp_error(
                "injection_policy=always requires a non-empty explicit audience"
            )

    if any(value is not None for value in (injection_policy, priority, audience)):
        if any(value is not None for value in (body, kind, status)):
            return _mcp_error(
                "policy/audience transition cannot be combined with body, kind, or status"
            )
        target_policy = injection_policy or current.injection_policy
        target_priority = (
            priority if priority is not None else current.priority
        )
        transition_idempotency_key = str(
            args.get("idempotency_key", "") or ""
        ).strip()
        target_audience = prepared_audience
        if (
            target_policy == "always"
            and target_audience is None
            and not injection_policy_explicit
        ):
            # A priority-only update does not redefine policy or audience.
            # Preserve the already-authorized assignment set.  Explicitly
            # declaring ``injection_policy=always`` still requires the caller
            # to restate a non-empty audience and therefore fails closed.
            existing = engine.store.list_rule_assignments(memory_id)
            target_audience = [item.to_dict() for item in existing]
        if target_policy == "always" and not target_audience:
            return _mcp_error(
                "injection_policy=always requires a non-empty explicit audience"
            )
        transition_at = _now_iso()
        audience_summary = sorted(
            (
                str(item.get("target_type", "")),
                str(item.get("target_id", "")),
                str(item.get("effect", "include")),
                str(item.get("project_ref", "")),
            )
            for item in (target_audience or [])
        )
        transition_decision = DecisionEvent(
            event_id=stable_hash(
                "mcp-rule-transition", memory_id,
                args["agent_instance_id"], target_policy,
                str(target_priority),
                transition_idempotency_key or transition_at,
            ),
            actor=decision_actor,
            action="agent_rule_transition",
            target_ids=[memory_id],
            reason=json.dumps({
                "before_policy": current.injection_policy,
                "after_policy": target_policy,
                "before_priority": current.priority,
                "after_priority": target_priority,
                "audience": audience_summary,
            }, ensure_ascii=False, separators=(",", ":"))[:1200],
            created_at=transition_at,
        )
        try:
            transition_result = (
                engine.store.transition_injection_policy(
                    memory_id, target_policy, target_priority,
                    assignments=target_audience or [],
                    decision=transition_decision,
                    idempotency_key=transition_idempotency_key,
                    return_result=True,
                )
            )
            updated_record, updated_assignments, atomic = transition_result
        except (TypeError, ValueError, RuntimeError) as exc:
            return _mcp_error(str(exc))
        payload = {
            "ok": True, "record": updated_record.to_dict(),
            "assignments": [
                item.to_dict() for item in updated_assignments
            ],
            "decision_id": (
                atomic.get("decision", {}).get("decision_id", "")
                if isinstance(atomic, dict) else ""
            ),
            "event_id": atomic.get("event_id", "") if isinstance(atomic, dict) else "",
            "idempotency_key": transition_idempotency_key,
            "idempotent_replay": bool(
                atomic.get("idempotent_replay", False)
            ) if isinstance(atomic, dict) else False,
        }
        return {"content": [{"type": "text", "text": json.dumps(
            payload, ensure_ascii=False, indent=2,
        )}]}

    # P0-D: body 更新前做 secret 脱敏
    if body is not None:
        safe_body, secret_hit = _redact_secret(body)
        body = safe_body
    result = engine.agent_update(
        memory_id,
        actor=decision_actor,
        body=body,
        kind=kind,
        status=status,
        injection_policy=injection_policy,
        priority=priority,
        idempotency_key=str(args.get("idempotency_key", "") or ""),
    )
    if not result["ok"]:
        return _mcp_error(result["blocked_reason"])
    result["record"] = result.get("after")
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}


def _handle_memory_delete(args: dict[str, Any]) -> dict[str, Any]:
    from .governance_engine import GovernanceEngine
    workspace = _resolve_memory_workspace(args)
    group_id, err, access_ctx = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}
    engine = GovernanceEngine(workspace, group_id)
    _, mutation_error = _authorize_rule_mutation(
        engine.store, args["memory_id"],
        actor_agent_id=args["agent_instance_id"],
        is_admin=bool(access_ctx and access_ctx.is_admin),
    )
    if mutation_error:
        return _mcp_error(mutation_error)
    result = engine.agent_delete(
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
    from .schema_v3 import RuleMatchReceipt

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
            effective_context=_effective_agent_context(args, group_id),
            read_path=str(args.get("read_path", "legacy") or "legacy"),
        )
    except (TypeError, ValueError) as exc:
        return _mcp_error(str(exc))
    # A feedback receipt is only useful when it is durable.  The selector
    # intentionally runs against a read-only store; persist generated
    # mandatory receipts through a separate trusted writer *before* the
    # packet is returned. Any write failure is fail-closed so callers can
    # never receive a pseudo-receipt that cannot later be referenced.
    raw_receipts = packet.get("mandatory_match_receipts") or []
    try:
        if raw_receipts:
            writer = SharedMemoryStore(workspace, group_id)
            persisted_receipts: list[dict[str, Any]] = []
            for raw_receipt in raw_receipts:
                receipt = RuleMatchReceipt.from_dict(dict(raw_receipt))
                saved = writer.append_rule_match_receipt(receipt)
                persisted_receipts.append(
                    saved.to_dict() if hasattr(saved, "to_dict") else receipt.to_dict()
                )
            packet["mandatory_match_receipts"] = persisted_receipts
            packet["receipt_persistence"] = {
                "status": "persisted",
                "count": len(persisted_receipts),
            }
        else:
            packet["receipt_persistence"] = {"status": "none", "count": 0}
    except (TypeError, ValueError, RuntimeError, OSError) as exc:
        return _mcp_error(f"context bootstrap receipt persistence failed: {exc}")
    except Exception as exc:  # sqlite/driver errors must not leak fake receipts
        return _mcp_error(f"context bootstrap receipt persistence failed: {exc}")
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(packet, ensure_ascii=False, indent=2),
        }],
    }


def _handle_rule_feedback(args: dict[str, Any]) -> dict[str, Any]:
    """Record mandatory-rule bootstrap feedback for one match receipt."""
    from .rule_creation import RuleCreationService
    workspace = _resolve_memory_workspace(args)
    group_id, err, access_ctx = _resolve_access(args, workspace)
    if err:
        return {"content": [{"type": "text", "text": f"error: {err}"}], "isError": True}

    receipt_id = str(args.get("receipt_id", "") or "").strip()
    outcome = str(args.get("outcome", "") or "").strip()
    actor_supplied = "actor" in args
    actor = str(args.get("actor", "") or "").strip()
    evidence = str(args.get("evidence", "") or "")
    confidence = args.get("confidence")
    if confidence is None:
        confidence = 1.0
    if not actor_supplied:
        # MCP is an Agent producer.  Keep the actor as an audit/display ID,
        # but derive it from the trusted transport context rather than from
        # an optional client field.
        try:
            trusted_context = _effective_agent_context(args, group_id)
            if trusted_context.agent_instance_id:
                actor = f"agent:{trusted_context.agent_instance_id}"
        except (TypeError, ValueError):
            pass
    from .shared_memory_store import SharedMemoryStore
    try:
        store = SharedMemoryStore(workspace, group_id)
        from .rule_creation import RuleCreationService
        service = RuleCreationService(
            workspace, group_id, store=store,
            is_admin=bool(access_ctx and access_ctx.is_admin),
        )
        result = service.submit_feedback(
            receipt_id, outcome, actor,
            evidence=evidence, confidence=confidence,
            effective_context=_effective_agent_context(args, group_id),
            idempotency_key=str(args.get("idempotency_key", "") or ""),
            producer="agent",
            actor_id=actor,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return _mcp_error(str(exc))
    # Project the feedback into the rule-intelligence layer now.  The outbox
    # row was written atomically with the feedback, so a failure is safe to
    # defer to the next scan, but it must remain visible to the MCP caller.
    projection: dict[str, Any]
    try:
        from .rule_merge import RuleMergeService, RuleMergeStore
        projection_summary = RuleMergeService(
            RuleMergeStore(workspace)
        ).consume_outbox(workspace)
        projection = {
            "status": "projected",
            "error": "",
            "summary": projection_summary,
            "outbox_recovery": "available",
        }
    except Exception as exc:
        projection = {
            "status": "pending",
            "error": f"{type(exc).__name__}: {exc}",
            "summary": {},
            "outbox_recovery": "available",
        }
    payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    # Keep the original feedback fields at the top-level for existing MCP
    # clients while exposing the lifecycle decision/narrowing result.
    feedback_after = getattr(result, "after", {}) or {}
    payload.setdefault(
        "feedback_id",
        str(
            feedback_after.get("feedback_id", "")
            if isinstance(feedback_after, dict) else ""
        )
        or str(args.get("idempotency_key", "") or ""),
    )
    payload["receipt_id"] = receipt_id
    payload["outcome"] = outcome
    payload["actor"] = actor
    payload["evidence"] = evidence
    payload["projection"] = projection
    if projection["status"] == "pending":
        return {
            "content": [{
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }],
            "isError": True,
        }
    if getattr(result, "status", None) == "blocked" or (
        isinstance(result, dict) and result.get("status") == "blocked"
    ):
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}], "isError": True}
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(payload, ensure_ascii=False, indent=2),
        }],
    }


def _rule_lifecycle_response(result: Any) -> dict[str, Any]:
    """Serialize a RuleDecision while preserving hard-reject semantics."""
    payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    payload.setdefault("ok", payload.get("status") != "blocked")
    response: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
    }
    if payload.get("status") == "blocked":
        response["isError"] = True
    return response


def _governance_json_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(payload, ensure_ascii=False, indent=2),
        }],
    }


def _handle_rule_merge_capability_issue(args: dict[str, Any]) -> dict[str, Any]:
    """Issue one opaque merge capability through the trusted admin context."""
    from .access_context import load_access_context
    from .rule_merge_store import RuleMergeStore

    workspace = _resolve_memory_workspace(args)
    proposal_id = str(args.get("proposal_id", "") or "").strip()
    access_context = load_access_context()
    ok, error = access_context.require_capability_issue()
    if not ok:
        return _mcp_error(error)
    kwargs: dict[str, Any] = {}
    if "ttl_seconds" in args:
        kwargs["ttl_seconds"] = float(args["ttl_seconds"])
    try:
        token = RuleMergeStore(workspace).issue_merge_capability(
            proposal_id, access_context, **kwargs,
        )
    except (TypeError, ValueError, RuntimeError, OSError) as exc:
        return _mcp_error(str(exc))
    # Raw token exists only in this one response and is never written to the
    # Store.  All later governance actions consume it immediately.
    return _governance_json_response({
        "ok": True,
        "proposal_id": proposal_id,
        "capability_token": token,
        "token_persistence": "sha256_only",
    })


def _handle_rule_merge_approve(args: dict[str, Any]) -> dict[str, Any]:
    from .access_context import load_access_context
    from .rule_merge_store import RuleMergeStore

    workspace = _resolve_memory_workspace(args)
    access_context = load_access_context()
    ok, error = access_context.require_capability_issue()
    if not ok:
        return _mcp_error(error)
    try:
        result = RuleMergeStore(workspace).approve_proposal(
            str(args.get("proposal_id", "") or "").strip(),
            capability_token=str(args.get("capability_token", "")),
            expected_definition_revisions=args.get("expected_definition_revisions"),
            access_context=access_context,
        )
    except (TypeError, ValueError, RuntimeError, OSError) as exc:
        return _mcp_error(str(exc))
    return _governance_json_response({"ok": True, **result})


def _handle_rule_merge_acknowledge(args: dict[str, Any]) -> dict[str, Any]:
    from .access_context import load_access_context
    from .rule_merge_store import RuleMergeStore

    workspace = _resolve_memory_workspace(args)
    access_context = load_access_context()
    ok, error = access_context.require_capability_issue()
    if not ok:
        return _mcp_error(error)
    try:
        result = RuleMergeStore(workspace).acknowledge_first_merge(
            str(args.get("proposal_id", "") or "").strip(),
            actor=access_context.principal,
            capability_token=str(args.get("capability_token", "")),
            access_context=access_context,
        )
    except (TypeError, ValueError, RuntimeError, OSError) as exc:
        return _mcp_error(str(exc))
    return _governance_json_response({"ok": True, **(result or {})})


def _handle_rule_merge_cooldown_clear(args: dict[str, Any]) -> dict[str, Any]:
    from .access_context import load_access_context
    from .rule_merge_store import RuleMergeStore

    workspace = _resolve_memory_workspace(args)
    access_context = load_access_context()
    ok, error = access_context.require_capability_issue()
    if not ok:
        return _mcp_error(error)
    try:
        result = RuleMergeStore(workspace).clear_proposal_cooldown(
            str(args.get("proposal_id", "") or "").strip(),
            capability_token=str(args.get("capability_token", "")),
            access_context=access_context,
        )
    except (TypeError, ValueError, RuntimeError, OSError) as exc:
        return _mcp_error(str(exc))
    return _governance_json_response({"ok": True, **(result or {})})


def _rule_service_for_args(args: dict[str, Any], workspace: Path):
    from .rule_creation import RuleCreationService
    group_id, err, access_ctx = _resolve_access(args, workspace)
    if err:
        return None, None, None, _mcp_error(err)
    from .shared_memory_store import SharedMemoryStore
    try:
        store = SharedMemoryStore(workspace, group_id)
    except FileNotFoundError:
        return None, None, None, _mcp_error(f"group not found: {group_id}")
    service = RuleCreationService(
        workspace, group_id, store=store,
        is_admin=bool(access_ctx and access_ctx.is_admin),
    )
    return service, group_id, access_ctx, None


def _handle_rule_create_auto(args: dict[str, Any]) -> dict[str, Any]:
    workspace = _resolve_memory_workspace(args)
    service, group_id, _access_ctx, error = _rule_service_for_args(args, workspace)
    if error:
        return error
    context = _effective_agent_context(args, group_id)
    result = service.create_rule_from_text(
        str(args.get("text", "") or ""),
        context,
        requested_scope=args.get("scope"),
        manual=bool(args.get("manual", False)),
        kind=str(args.get("kind", "") or ""),
        priority=int(args.get("priority", 0) or 0),
        idempotency_key=str(args.get("idempotency_key", "") or ""),
    )
    return _rule_lifecycle_response(result)


def _handle_rule_decision_read(args: dict[str, Any]) -> dict[str, Any]:
    workspace = _resolve_memory_workspace(args)
    service, _group_id, _access_ctx, error = _rule_service_for_args(args, workspace)
    if error:
        return error
    result = service.read_decision(str(args.get("decision_id", "") or ""))
    if result is None:
        return _mcp_error("decision not found")
    return _rule_lifecycle_response(result)


def _handle_rule_undo(args: dict[str, Any]) -> dict[str, Any]:
    workspace = _resolve_memory_workspace(args)
    service, group_id, _access_ctx, error = _rule_service_for_args(args, workspace)
    if error:
        return error
    result = service.undo_rule(
        str(args.get("undo_id", "") or ""), _effective_agent_context(args, group_id),
    ) if str(args.get("undo_id", "") or "").strip() else service.undo_rule_decision(
        str(args.get("decision_id", "") or ""), _effective_agent_context(args, group_id),
    )
    return _rule_lifecycle_response(result)


def _handle_rule_scope_stats(args: dict[str, Any]) -> dict[str, Any]:
    workspace = _resolve_memory_workspace(args)
    service, group_id, _access_ctx, error = _rule_service_for_args(args, workspace)
    if error:
        return error
    result = service.scope_stats(_effective_agent_context(args, group_id))
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}


def _handle_history(args: dict[str, Any], name: str) -> dict[str, Any]:
    """Serve raw-history evidence through the same trusted binding boundary.

    History is a raw-evidence surface separate from shared long-term memory.
    The binding establishes the caller identity; an active shared binding
    dynamically authorizes current group members for reads, while mutations
    stay owner-only. ``scope.agent_instance_id`` can never impersonate another
    Agent, even for an administrator.
    """
    from .conversation_history import ConversationHistoryStore, HistoryAccessResolver

    workspace = _resolve_memory_workspace(args)
    _group_id, err, access_ctx = _resolve_access(args, workspace)
    if err:
        return _mcp_error(err)
    trusted_agent_id = str(args.get("agent_instance_id") or "")
    try:
        if name in {item["name"] for item in HISTORY_TOOL_DEFINITIONS}:
            result = handle_history_tool(
                name, args, workspace=str(workspace),
                trusted_agent_id=trusted_agent_id,
            )
        else:
            scope = HistoryAccessResolver(workspace).resolve(trusted_agent_id, args.get("scope"))
            history_store = ConversationHistoryStore(workspace)
            if name == "memoryguard_history_list_sessions":
                result = history_store.list_sessions(
                    scope, limit=args.get("limit", 50), offset=args.get("offset", 0),
                    extracted=args.get("extracted"),
                    date_from=str(args.get("date_from") or ""),
                    date_to=str(args.get("date_to") or ""),
                )
            elif name == "memoryguard_history_export":
                result = history_store.export(scope, session_ids=list(args.get("session_ids") or []))
            elif name == "memoryguard_history_delete":
                if args.get("confirmed") is not True:
                    return _mcp_error("history deletion requires confirmed=true")
                result = ConversationHistoryStore.delete(
                    history_store, scope, session_ids=list(args.get("session_ids") or []),
                    # A valid provenance link must never outlive its source.
                    # The history archive atomically tombstones it before removal.
                    invalidate_evidence=True,
                )
            else:
                return _mcp_error(f"unknown tool {name}")
    except (LookupError, PermissionError, TypeError, ValueError) as exc:
        return _mcp_error(str(exc))
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}


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
