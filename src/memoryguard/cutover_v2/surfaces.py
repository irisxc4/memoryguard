"""Pure public surface contract shared by cutover adapters and native ports.

This module intentionally imports no runtime implementation.  Keeping the
name snapshots here prevents a V2 native port from importing ``compat_v2``
while preserving one canonical registry for adapters and tests.
"""

from __future__ import annotations


MCP_TOOL_NAMES = frozenset({
    "memoryguard_audit", "memoryguard_explain", "memoryguard_list_sources",
    "memoryguard_scan_summary", "memoryguard_neuron_graph", "memoryguard_import_preview",
    "memoryguard_memory_read", "memoryguard_memory_search", "memoryguard_memory_write",
    "memoryguard_memory_update", "memoryguard_memory_delete", "memoryguard_memory_status",
    "memoryguard_context_bootstrap", "memoryguard_rule_feedback", "memoryguard_rule_create_auto",
    "memoryguard_rule_decision_read", "memoryguard_rule_undo", "memoryguard_rule_scope_stats",
    "memoryguard_rule_merge_capability_issue", "memoryguard_rule_merge_approve",
    "memoryguard_rule_merge_acknowledge", "memoryguard_rule_merge_cooldown_clear",
    "memoryguard_binding_create", "memoryguard_binding_list", "memoryguard_external_mcp_list",
    "memoryguard_external_mcp_import", "memoryguard_extract_memories", "memoryguard_accept_candidates",
    "memoryguard_semantic_check", "memoryguard_provider_install", "memoryguard_resolve_group",
    "memoryguard_list_pending_enrichments", "memoryguard_apply_enrichments", "memoryguard_enrichment_status",
    "memoryguard_build_and_enrich", "memoryguard_canonical_status", "memoryguard_diagnostics_snapshot",
    "memoryguard_projection_status", "memoryguard_runtime_processes",
    "memoryguard_history_search", "memoryguard_history_timeline", "memoryguard_history_read",
    "memoryguard_history_extract_preview", "memoryguard_history_list_sessions",
    "memoryguard_history_export", "memoryguard_history_delete",
    "memoryguard_knowledge_list", "memoryguard_knowledge_search", "memoryguard_knowledge_read",
    "memoryguard_knowledge_book", "memoryguard_knowledge_candidates",
})

SAFE_BRIDGE_METHOD_NAMES = frozenset({
    "call_readonly", "request_mutation", "get_api_method_registry",
    "get_sandbox_status", "pick_path",
})
GUI_METHOD_NAMES = frozenset({
    *SAFE_BRIDGE_METHOD_NAMES,
    "get_audit", "run_audit", "generate_plan", "discover_agents", "get_selection_tree", "get_agent_data",
    "get_neuron_graph", "get_projection_source_map", "get_governance_scope", "get_governance_scope_state",
    "list_native_memory_releases", "list_publish_targets", "choose_publish_target_path", "list_sources",
    "preview_source", "scan_sources", "get_raw_memory", "get_source_file_content", "list_agent_candidates",
    "list_archived_agents", "list_cleanup_history", "list_agents", "get_residual_cleanup", "open_agent_folder",
    "list_bindings", "check_binding_drift", "get_shared_group_preview", "get_host_hook_status",
    "list_external_mcp_servers", "preview_external_mcp_import", "detect_external_mcp", "list_memory",
    "get_memory", "search_memory", "list_memory_versions", "get_recent_events", "get_auto_actions",
    "get_supersede_chain", "get_conflicts", "get_quarantine", "get_governance_snapshot", "get_memory_status",
    "get_supersede_decisions", "list_share_groups", "get_global_memory_status", "get_memory_source_map",
    "extract_preview", "extract_preview_by_path", "preview_import", "get_memory_ir", "list_releases",
    "list_history", "plan_memoryguard_gc", "get_storage_overview", "get_request_status", "list_pending_requests",
    "list_pending_enrichments", "get_enrichment_status", "get_host_enrichment_guide", "get_build_progress",
    "list_host_llm_agents", "list_history_sessions", "search_history", "history_timeline", "history_read",
    "history_extract_preview", "export_history", "discover_local_history_sources", "list_rules_habits",
    "get_rule_scope_options", "preview_effective_rules", "list_rule_cockpit", "list_rule_decisions",
    "read_rule_decision", "get_rule_auto_scope_metrics", "list_rule_match_receipts", "list_rule_exceptions",
    "knowledge_list", "knowledge_deleted_list", "knowledge_search", "knowledge_read", "knowledge_book",
    "knowledge_job_status", "knowledge_candidates_list", "knowledge_candidate_targets", "get_sandbox_status",
    "submit_request", "knowledge_add", "knowledge_reingest", "knowledge_rebuild_smart", "knowledge_remove",
    "knowledge_restore", "knowledge_purge_deleted", "knowledge_update_settings", "knowledge_candidate_review",
    "set_governance_scope", "commit_selection", "neuron_decide", "set_projection_source_enabled",
    "build_projection", "start_build_projection", "cancel_build_projection", "delete_projection", "add_source",
    "remove_source", "mark_agent_uninstalled", "unmark_agent_uninstalled", "archive_agent_dir",
    "restore_archived_agent", "delete_archived_agent", "enter_multi_agent_mode", "exit_multi_agent_mode",
    "bind_agent", "bind_agents_to_shared_group", "unbind_agent", "ensure_personal_memory_group",
    "leave_shared_group_to_personal", "dissolve_shared_group", "export_memory_group", "clear_memory_group",
    "archive_memory_group", "install_shared_group_mcp_redirects", "set_host_hook_mode", "uninstall_host_hook",
    "import_native_memories_to_group", "commit_shared_memory_governance", "import_external_mcp_entries",
    "edit_memory", "lock_memory", "unlock_memory", "set_memory_injection_policy", "restore_memory",
    "delete_memory", "rollback_memory", "resolve_conflict", "release_quarantine", "delete_quarantine",
    "accept_candidates", "create_import", "apply_memoryguard_gc", "apply_plan", "undo_change",
    "apply_enrichments", "delete_history", "backfill_local_history", "update_rule_audience",
    "create_rule_from_text", "undo_rule_decision", "create_child_exception", "create_rule_exception",
    "submit_rule_feedback", "revoke_rule_exception",
    "publish_reconstructed_memory", "rollback_native_memory_release", "create_build_plan",
    "apply_build", "verify_release", "rollback_release",
})

CLI_COMMAND_NAMES = frozenset({
    "audit", "open", "explain", "plan", "apply", "verify", "undo", "source", "scan",
    "import", "provider", "gc", "storage", "gui", "desktop", "hooks", "mcp-status", "doctor", "groups",
})

RULE_MUTATION_MCP_NAMES = frozenset({
    "memoryguard_rule_feedback", "memoryguard_rule_create_auto", "memoryguard_rule_undo",
    "memoryguard_rule_merge_capability_issue", "memoryguard_rule_merge_approve",
    "memoryguard_rule_merge_acknowledge", "memoryguard_rule_merge_cooldown_clear",
    "memoryguard_binding_create",
})
MCP_MUTATION_NAMES = frozenset({
    # These MCP entrypoints persist reports, extraction state, snapshots, or
    # projections even though their public descriptions sound read-oriented.
    # Keep them in the canonical write gate until a genuinely pure service
    # replaces the current implementations.
    "memoryguard_audit", "memoryguard_list_sources", "memoryguard_scan_summary",
    "memoryguard_extract_memories", "memoryguard_build_and_enrich",
    "memoryguard_memory_write", "memoryguard_memory_update", "memoryguard_memory_delete",
    "memoryguard_binding_create", "memoryguard_external_mcp_import", "memoryguard_accept_candidates",
    "memoryguard_provider_install", "memoryguard_apply_enrichments", "memoryguard_history_delete",
    *RULE_MUTATION_MCP_NAMES,
})
MUTATING_MCP_TOOL_NAMES = MCP_MUTATION_NAMES
RULE_MUTATION_GUI_NAMES = frozenset({
    "update_rule_audience", "create_rule_from_text", "undo_rule_decision", "create_child_exception",
    "create_rule_exception", "submit_rule_feedback", "revoke_rule_exception", "neuron_decide",
})

# ``security.MUTATION_API_METHODS`` predates the cutover registry and omits
# the release/build bridge plus the request-queue transport method.  Keep the
# complete 162-method GUI surface and its 72 real writes explicit here so a
# caller cannot downgrade a known mutator by passing ``mutation=False``.
GUI_MUTATION_NAMES = frozenset({
    "accept_candidates", "add_source", "apply_enrichments", "apply_memoryguard_gc",
    "apply_plan", "archive_agent_dir", "archive_memory_group", "backfill_local_history",
    "bind_agent", "bind_agents_to_shared_group", "build_projection", "cancel_build_projection",
    "clear_memory_group", "commit_selection", "commit_shared_memory_governance",
    "create_child_exception", "create_import", "create_rule_exception", "create_rule_from_text",
    "delete_archived_agent", "delete_history", "delete_memory", "delete_projection",
    "delete_quarantine", "dissolve_shared_group", "edit_memory", "ensure_personal_memory_group",
    "enter_multi_agent_mode", "exit_multi_agent_mode", "export_memory_group",
    "import_external_mcp_entries", "import_native_memories_to_group", "install_shared_group_mcp_redirects",
    "knowledge_add", "knowledge_candidate_review", "knowledge_purge_deleted", "knowledge_rebuild_smart",
    "knowledge_reingest", "knowledge_remove", "knowledge_restore", "knowledge_update_settings",
    "leave_shared_group_to_personal", "lock_memory", "mark_agent_uninstalled", "neuron_decide",
    "release_quarantine", "remove_source", "resolve_conflict", "restore_archived_agent", "restore_memory",
    "revoke_rule_exception", "rollback_memory", "set_governance_scope", "set_host_hook_mode",
    "set_memory_injection_policy", "set_projection_source_enabled", "start_build_projection",
    "submit_rule_feedback", "unbind_agent", "undo_change", "undo_rule_decision", "uninstall_host_hook",
    "unlock_memory", "unmark_agent_uninstalled", "update_rule_audience",
    "request_mutation", "submit_request", "publish_reconstructed_memory", "rollback_native_memory_release",
    "create_build_plan", "apply_build", "rollback_release",
})

# Compatibility spellings used by older native-coverage and host code.
MUTATING_GUI_METHOD_NAMES = GUI_MUTATION_NAMES
GUI_MUTATION_METHOD_NAMES = GUI_MUTATION_NAMES

LEGACY_MCP_TOOL_NAMES = MCP_TOOL_NAMES
LEGACY_GUI_METHOD_NAMES = GUI_METHOD_NAMES
LEGACY_CLI_COMMAND_NAMES = CLI_COMMAND_NAMES


__all__ = [
    "MCP_TOOL_NAMES", "LEGACY_MCP_TOOL_NAMES", "SAFE_BRIDGE_METHOD_NAMES",
    "GUI_METHOD_NAMES", "LEGACY_GUI_METHOD_NAMES", "CLI_COMMAND_NAMES",
    "LEGACY_CLI_COMMAND_NAMES", "RULE_MUTATION_MCP_NAMES", "MCP_MUTATION_NAMES",
    "MUTATING_MCP_TOOL_NAMES", "RULE_MUTATION_GUI_NAMES",
    "GUI_MUTATION_NAMES", "MUTATING_GUI_METHOD_NAMES", "GUI_MUTATION_METHOD_NAMES",
]
