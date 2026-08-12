from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.cutover_v2.facade import V2RuntimeFacade
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import V2MutationContext
from memoryguard.gui import SafeBridgeApi
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.runtime_v2.group_native import GroupControlService, personal_group_id as v2_personal_group_id
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
    bind_native_test_services,
)


def _context(workspace: Path, *, admin: bool = False):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="phase9-agent",
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id="phase9-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id="phase9-group",
        project_ref="phase9-project",
        provider="codex",
        runtime_role="root",
        namespace_id="ns-native",
        sensitivity="normal",
        policy_class="private",
    )


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def test_phase9_registry_entries_are_real_builtins_and_precise_mutations(tmp_path: Path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    entries = {
        item["name"]: item
        for item in port.coverage()["surfaces"]["mcp"]["entries"]
    }
    expected = {
        "memoryguard_history_search": False,
        "memoryguard_history_timeline": False,
        "memoryguard_history_read": False,
        "memoryguard_history_extract_preview": False,
        "memoryguard_history_list_sessions": False,
        "memoryguard_history_export": False,
        "memoryguard_history_delete": True,
        "memoryguard_list_sources": True,
        "memoryguard_scan_summary": True,
        "memoryguard_import_preview": False,
        "memoryguard_runtime_processes": False,
    }
    for name, mutation in expected.items():
        assert entries[name]["status"] == "implemented"
        assert entries[name]["mutation"] is mutation


def test_knowledge_v2_native_book_and_candidates_are_scoped_and_fail_closed(tmp_path: Path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    entries = {
        item["name"]: item
        for item in port.coverage()["surfaces"]["mcp"]["entries"]
    }
    assert entries["memoryguard_knowledge_book"]["status"] == "implemented"
    assert entries["memoryguard_knowledge_book"]["mutation"] is False
    assert entries["memoryguard_knowledge_candidates"]["status"] == "implemented"
    assert entries["memoryguard_knowledge_candidates"]["mutation"] is False
    context = _context(tmp_path)
    before = list(tmp_path.rglob("*"))
    for name in ("memoryguard_knowledge_book", "memoryguard_knowledge_candidates"):
        for _ in range(2):
            result = port.dispatch_mcp(
                name,
                {
                    "namespace_id": "ns-native",
                    "sensitivity": "normal",
                    "policy_class": "private",
                },
                context=context,
                generation=7,
            )
            assert result["ok"] is False
            assert result["code"] == (
                "content_db_missing" if name.endswith("book") else "candidate_db_missing"
            )
    assert list(tmp_path.rglob("*")) == before


def test_knowledge_v2_native_book_uses_exact_trusted_scope_without_writes(tmp_path: Path):
    from memoryguard.content import ContentStore

    store = ContentStore(tmp_path)
    namespace = store.ensure_namespace(namespace_id="ns-native", trust_domain="knowledge")
    blob = store.put_blob(namespace.namespace_id, "native secret body")
    occurrence = store.upsert_occurrence(
        source_object_id="native-source",
        occurrence_key="book-1",
        blob_id=blob,
        namespace_id=namespace.namespace_id,
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="phase9-agent",
        project_ref="phase9-project",
        provider="codex",
        share_group_id="phase9-group",
        sensitivity="normal",
        policy_class="private",
        locator={"title": "Native title"},
    )
    before = store.db_path.read_bytes()
    wal_path = Path(str(store.db_path) + "-wal")
    wal_before = wal_path.read_bytes() if wal_path.exists() else None
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    payload = {
        "namespace_id": "ns-native",
        "sensitivity": "normal",
        "policy_class": "private",
        "book_id": occurrence,
        "service": "forged",
    }
    result = port.dispatch_mcp(
        "memoryguard_knowledge_book", payload, context=context, generation=7,
    )
    assert result["ok"] is True
    assert result["data"]["service"] == "knowledge_book"
    assert result["data"]["references"][0]["summary"] == "Native title"
    assert "native secret body" not in str(result)
    again = port.dispatch_mcp(
        "memoryguard_knowledge_book", payload, context=context, generation=7,
    )
    assert again["data"]["references"] == result["data"]["references"]
    assert store.db_path.read_bytes() == before
    wal_after = wal_path.read_bytes() if wal_path.exists() else None
    assert wal_after == wal_before


def test_knowledge_v2_native_scope_cannot_broaden_or_be_forged(tmp_path: Path):
    """Namespace and ACL selectors are bound by the native capability."""
    from memoryguard.content import ContentStore

    store = ContentStore(tmp_path)
    namespace = store.ensure_namespace(namespace_id="ns-native", trust_domain="knowledge")
    blob = store.put_blob(namespace.namespace_id, "scope-guarded body")
    store.upsert_occurrence(
        source_object_id="scope-source",
        occurrence_key="row",
        blob_id=blob,
        namespace_id=namespace.namespace_id,
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="phase9-agent",
        project_ref="phase9-project",
        provider="codex",
        share_group_id="phase9-group",
        sensitivity="normal",
        policy_class="private",
        locator={"title": "scope guarded"},
    )
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    exact = {"namespace_id": "ns-native", "sensitivity": "normal", "policy_class": "private"}
    ok = port.dispatch_mcp("memoryguard_knowledge_book", exact, context=context, generation=7)
    assert ok["ok"] is True

    for broadened in (
        {**exact, "namespace_id": "ns-other"},
        {**exact, "sensitivity": "sensitive"},
        {**exact, "policy_class": "shared"},
    ):
        result = port.dispatch_mcp(
            "memoryguard_knowledge_book", broadened, context=context, generation=7,
        )
        assert result["ok"] is False
        assert result["code"] == "knowledge_scope_mismatch"

    for key in exact:
        missing = dict(exact)
        missing.pop(key)
        result = port.dispatch_mcp(
            "memoryguard_knowledge_book", missing, context=context, generation=7,
        )
        assert result["ok"] is False
        assert result["code"] == "knowledge_scope_required"

    forged = dict(context)
    forged["namespace_id"] = "ns-other"
    result = port.dispatch_mcp(
        "memoryguard_knowledge_book", {**exact, "namespace_id": "ns-other"},
        context=forged, generation=7,
    )
    assert result["ok"] is False
    assert result["code"] == "knowledge_scope_mismatch"

    # A valid identity capability without the Knowledge tuple cannot fall back
    # to payload selectors or service defaults.
    unscoped = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="phase9-agent",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="phase9-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id="phase9-group",
        project_ref="phase9-project",
        provider="codex",
        runtime_role="root",
    )
    result = port.dispatch_mcp(
        "memoryguard_knowledge_book", exact, context=unscoped, generation=7,
    )
    assert result["ok"] is False
    assert result["code"] == "knowledge_scope_required"


def test_knowledge_v2_native_reads_require_process_issued_capability(tmp_path: Path):
    """Knowledge reads reject public context projections without writing state."""
    from memoryguard.content import ContentStore

    store = ContentStore(tmp_path)
    namespace = store.ensure_namespace(namespace_id="ns-native", trust_domain="knowledge")
    blob = store.put_blob(namespace.namespace_id, "capability guarded body")
    occurrence = store.upsert_occurrence(
        source_object_id="capability-source",
        occurrence_key="row",
        blob_id=blob,
        namespace_id=namespace.namespace_id,
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="phase9-agent",
        project_ref="phase9-project",
        provider="codex",
        share_group_id="phase9-group",
        sensitivity="normal",
        policy_class="private",
        locator={"title": "capability guarded"},
    )
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    capability = _context(tmp_path)
    exact = {
        "namespace_id": "ns-native",
        "sensitivity": "normal",
        "policy_class": "private",
        "book_id": occurrence,
    }
    before = store.db_path.read_bytes()
    wal_path = Path(str(store.db_path) + "-wal")
    wal_before = wal_path.read_bytes() if wal_path.exists() else None

    # Public projections are useful for logging, but are not authorization
    # capabilities.  All of these must fail before the Knowledge service is
    # reached, with one stable native error and no storage side effects.
    plain_projection = capability.bound_context.to_dict()

    class ToDictProjection:
        def to_dict(self):
            return dict(plain_projection)

    forged_bound_projection = dict(plain_projection)
    forged_bound_projection["namespace_id"] = "ns-other"
    for untrusted in (plain_projection, ToDictProjection(), forged_bound_projection):
        result = port.dispatch_mcp(
            "memoryguard_knowledge_book", exact,
            context=untrusted, generation=7,
        )
        assert result["ok"] is False
        assert result["code"] == "trusted_context_capability_required"

    # The process-issued envelope (and its exact authority object) remain
    # valid and readable; this is still a read-only operation.
    allowed = port.dispatch_mcp(
        "memoryguard_knowledge_book", exact,
        context=capability, generation=7,
    )
    assert allowed["ok"] is True
    assert allowed["data"]["references"][0]["summary"] == "capability guarded"
    direct_authority = port.dispatch_mcp(
        "memoryguard_knowledge_book", exact,
        context=capability.bound_context, generation=7,
    )
    assert direct_authority["ok"] is True
    assert direct_authority["data"]["references"] == allowed["data"]["references"]

    # Unrelated neutral reads keep their historical plain-mapping behavior.
    status = port.dispatch_mcp(
        "memoryguard_memory_status", {}, context=plain_projection, generation=7,
    )
    assert status["ok"] is True
    assert store.db_path.read_bytes() == before
    wal_after = wal_path.read_bytes() if wal_path.exists() else None
    assert wal_after == wal_before


def test_knowledge_v2_native_rejects_service_override_and_forged_context(tmp_path: Path):
    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=_Manifest(),
        services=bind_native_test_services({
            "memoryguard_knowledge_book": lambda payload, **kwargs: {"forged": True},
        }),
    )
    class ForgedContext:
        def to_dict(self):
            return {
                "workspace_id": str(tmp_path),
                "agent_instance_id": "phase9-agent",
                "share_group_id": "phase9-group",
            }

    result = port.dispatch_mcp(
        "memoryguard_knowledge_book",
        {"namespace_id": "ns-native"},
        context=ForgedContext(),
        generation=7,
    )
    assert result["ok"] is False
    assert result["code"] == "trusted_context_capability_required"


def test_goal_b_rule_mutations_are_native_and_ready_state_remains_write_blocked(tmp_path: Path):
    """Rule lifecycle is native V2, but V2_READY is still read-only."""
    manifest = _Manifest(state="V2_READY")
    port = NativeV2RuntimePort(tmp_path, state_provider=manifest)
    names = (
        "memoryguard_rule_create_auto",
        "memoryguard_rule_feedback",
        "memoryguard_rule_undo",
    )
    entries = {
        item["name"]: item
        for item in port.coverage()["surfaces"]["mcp"]["entries"]
    }
    for name in names:
        assert entries[name]["status"] == "implemented"
        assert entries[name]["mutation"] is True

    context = _context(tmp_path)
    before = list(tmp_path.rglob("*"))
    for name, payload in (
        ("memoryguard_rule_create_auto", {"text": "must remain scoped"}),
        ("memoryguard_rule_feedback", {"receipt_id": "receipt", "outcome": "followed"}),
        ("memoryguard_rule_undo", {"decision_id": "decision"}),
    ):
        result = port.dispatch_mcp(name, payload, context=context, generation=7)
        assert result["ok"] is False
        assert result["code"] == "v2_not_active"
    # State gate runs before lazy writable service construction.
    assert list(tmp_path.rglob("*")) == before


def test_goal_b_enrichment_mutations_are_native_and_missing_schema_fails_closed(tmp_path: Path):
    """Extraction/enrichment is V2-native and requires V2 schemas."""
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    names = (
        "memoryguard_accept_candidates",
        "memoryguard_apply_enrichments",
        "memoryguard_build_and_enrich",
        "memoryguard_extract_memories",
    )
    entries = {
        item["name"]: item
        for item in port.coverage()["surfaces"]["mcp"]["entries"]
    }
    for name in names:
        assert entries[name]["status"] == "implemented"
        assert entries[name]["mutation"] is True
        assert entries[name]["reason"] == ""

    context = _context(tmp_path)
    before = list(tmp_path.rglob("*"))
    payloads = {
        "memoryguard_accept_candidates": {"extract_id": "extract", "candidate_ids": ["candidate"]},
        "memoryguard_apply_enrichments": {"results": [{"task_id": "task", "kind": "fact", "title": "title", "body": "body"}]},
        "memoryguard_build_and_enrich": {},
        "memoryguard_extract_memories": {"source_path": str(tmp_path / "source.txt")},
    }
    for name in names:
        result = port.dispatch_mcp(name, payloads[name], context=context, generation=7)
        assert result["ok"] is False
        assert result["code"] != "v2_operation_not_implemented"
    assert list(tmp_path.rglob("*")) == before


def test_goal_cli_native_and_retired_surfaces_are_explicit(tmp_path: Path):
    """V2 CLI names either have native semantics or an explicit retired receipt."""
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    entries = {
        item["name"]: item
        for item in port.coverage()["surfaces"]["cli"]["entries"]
    }
    for name in ("audit", "explain", "groups", "hooks", "scan", "source"):
        assert entries[name]["status"] == "implemented"
        assert entries[name]["reason"] == ""
    for name in ("plan", "verify"):
        assert entries[name]["status"] == "retired"
        assert entries[name]["reason"]

    before = list(tmp_path.rglob("*"))
    for name, payload in (
        ("plan", {"finding_ids": ["finding"]}),
        ("verify", {}),
    ):
        result = port.dispatch_cli(name, payload, generation=7)
        assert result["ok"] is False
        assert result["status"] == "retired"
        assert result["code"] == "v2_operation_retired"
        assert result["reason"] == entries[name]["reason"]
    assert list(tmp_path.rglob("*")) == before


def test_goal_gui_read_surfaces_are_native(tmp_path: Path):
    """V2 GUI reads resolve through native handlers without a fallback."""
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    entries = {
        item["name"]: item
        for item in port.coverage()["surfaces"]["gui"]["entries"]
    }
    implemented = (
        "get_storage_overview", "get_audit", "list_history",
        "list_rule_exceptions", "list_rule_match_receipts",
        "get_rule_scope_options", "preview_effective_rules", "get_recent_events",
    )
    for name in implemented:
        assert entries[name]["status"] == "implemented"
        assert entries[name]["reason"] == ""
    recent = port.dispatch_gui(
        "get_recent_events", {}, context=_context(tmp_path), generation=7,
    )
    assert recent["ok"] is True, recent
    assert isinstance(recent["data"].get("events"), list)


def test_goal_gui_memory_history_reads_use_scoped_v2_revisions_and_edges(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    write_context = V2MutationContext(
        workspace_id=str(tmp_path), share_group_id="phase9-group", agent_instance_id="phase9-agent", actor="test",
        project_ref="phase9-project", provider="codex", runtime_role="root",
        authority="manual",
    )
    old = memory.put_atom(
        MemoryAtom(
            memory_id="old", body="old secret", share_group_id="phase9-group",
            agent_instance_id="phase9-agent",
            project_ref="phase9-project", provider="codex", runtime_role="root",
        ),
        evidence=[{"source_ref": "phase9/old"}], context=write_context,
    )
    new = memory.put_atom(
        MemoryAtom(
            memory_id="new", body="new secret", share_group_id="phase9-group",
            agent_instance_id="phase9-agent",
            project_ref="phase9-project", provider="codex", runtime_role="root",
        ),
        evidence=[{"source_ref": "phase9/new"}], context=write_context,
    )
    foreign = memory.put_atom(
        MemoryAtom(
            memory_id="foreign", body="foreign secret", share_group_id="other-group",
            agent_instance_id="phase9-agent",
            project_ref="phase9-project", provider="codex", runtime_role="root",
        ),
        evidence=[{"source_ref": "other/foreign"}],
        context=V2MutationContext(
            workspace_id=str(tmp_path), share_group_id="other-group", agent_instance_id="phase9-agent", actor="test",
            project_ref="phase9-project", provider="codex", runtime_role="root",
            authority="manual",
        ),
    )
    memory.project_evidence(evidence)
    memory.promote("ready")
    memory.supersede(old.atom_id, new.atom_id, context=write_context, reason="replace")

    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    before = memory.path.read_bytes()
    versions = port.dispatch_gui(
        "list_memory_versions", {"limit": 50}, context=context, generation=7,
    )
    assert versions["ok"] is True
    rows = versions["data"]["versions"]
    assert rows and {row["share_group_id"] for row in rows} == {"phase9-group"}
    assert any(row["memory_id"] == "old" for row in rows)
    assert all("body" not in row and "metadata" not in row for row in rows)
    positional_versions = port.dispatch_gui(
        "list_memory_versions", ["phase9-group", 50], context=context, generation=7,
    )
    assert positional_versions["ok"] is True
    assert positional_versions["data"]["versions"] == rows

    chain = port.dispatch_gui(
        "get_supersede_chain", {"memory_id": "new"}, context=context, generation=7,
    )
    assert chain["ok"] is True
    assert chain["data"] == {
        "memory_id": "new", "supersedes": ["old"], "superseded_by": [],
    }
    positional_chain = port.dispatch_gui(
        "get_supersede_chain", ["new", "phase9-group"], context=context, generation=7,
    )
    assert positional_chain["ok"] is True
    assert positional_chain["data"] == chain["data"]
    old_chain = port.dispatch_gui(
        "get_supersede_chain", {"memory_id": "old"}, context=context, generation=7,
    )
    assert old_chain["ok"] is True
    assert old_chain["data"]["superseded_by"] == ["new"]
    assert foreign.atom_id not in {row["atom_id"] for row in rows}
    assert memory.path.read_bytes() == before


def test_goal_gui_memory_history_reads_fail_closed_for_missing_and_future_schema(tmp_path: Path):
    empty = tmp_path / "empty"
    port = NativeV2RuntimePort(empty, state_provider=_Manifest())
    missing = port.dispatch_gui(
        "list_memory_versions", {}, context=_context(empty), generation=7,
    )
    assert missing["ok"] is False
    assert missing["code"] == "v2_schema_missing"
    assert not (empty / ".memoryguard" / "memory" / "memory.db").exists()

    memory = MemoryAtomStore(tmp_path)
    with memory._connection() as conn:
        conn.execute("PRAGMA user_version=99")
    future = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_gui(
        "list_memory_versions", {}, context=_context(tmp_path), generation=7,
    )
    assert future["ok"] is False
    assert future["code"] == "v2_schema_future"


def test_phase9_missing_sources_and_history_are_stable_without_database_creation(tmp_path: Path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)

    sources = port.dispatch_mcp(
        "memoryguard_list_sources", {}, context=context, generation=7,
    )
    assert sources["ok"] is True
    assert sources["data"]["status"] == "NO_SOURCE"

    scan = port.dispatch_mcp(
        "memoryguard_scan_summary", {}, context=context, generation=7,
    )
    assert scan["ok"] is True
    assert scan["data"]["status"] == "NO_SOURCE"

    history = port.dispatch_mcp(
        "memoryguard_history_search", {"query": "secret"},
        context=context, generation=7,
    )
    assert history["ok"] is True
    assert history["data"]["neutral"] is True

    assert not (tmp_path / ".memoryguard" / "history" / "history.sqlite").exists()


def test_phase9_import_preview_and_runtime_processes_are_builtin_and_redacted(tmp_path: Path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)

    preview = port.dispatch_mcp(
        "memoryguard_import_preview", {"path": str(tmp_path)},
        context=context, generation=7,
    )
    assert preview["ok"] is False
    assert preview["code"] == "no_source"

    runtime = port.dispatch_mcp(
        "memoryguard_runtime_processes", {"admin": True},
        context=context, generation=7,
    )
    assert runtime["ok"] is True
    assert "details" not in runtime["data"]
    assert str(tmp_path) not in str(runtime)

    forged = port.dispatch_mcp(
        "memoryguard_import_preview", {"path": str(tmp_path)},
        context={
            "workspace_id": str(tmp_path),
            "agent_instance_id": "phase9-agent",
            "share_group_id": "phase9-group",
        },
        generation=7,
    )
    assert forged["ok"] is False
    assert forged["code"] == "trusted_context_capability_required"


def test_phase9_services_cannot_be_replaced_by_generic_test_injection(tmp_path: Path):
    calls: list[object] = []
    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=_Manifest(),
        services=bind_native_test_services({
            "memoryguard_history_search": lambda payload, **kwargs: calls.append(payload) or {"forged": True},
        }),
    )
    context = _context(tmp_path)
    history = port.dispatch_mcp(
        "memoryguard_history_search", {"query": "x"}, context=context, generation=7,
    )
    sources = port.dispatch_mcp(
        "memoryguard_list_sources", {}, context=context, generation=7,
    )
    assert not calls
    assert history["data"]["neutral"] is True
    assert sources["data"]["status"] == "NO_SOURCE"


def test_phase9_list_scan_remain_mutation_gated_and_reject_ready_or_stale_generation(tmp_path: Path):
    ready = NativeV2RuntimePort(tmp_path, state_provider=_Manifest("V2_READY", 7))
    context = _context(tmp_path)
    result = ready.dispatch_mcp(
        "memoryguard_list_sources", {}, context=context, generation=7, state="V2_READY",
    )
    assert result["code"] == "v2_not_active"

    active = NativeV2RuntimePort(tmp_path, state_provider=_Manifest("V2_ACTIVE", 8))
    stale = active.dispatch_mcp(
        "memoryguard_scan_summary", {}, context=context, generation=7, state="V2_ACTIVE",
    )
    assert stale["code"] == "manifest_generation_mismatch"


def test_phase9_native_import_is_lazy_and_does_not_initialize_storage(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    code = (
        "from pathlib import Path; "
        "from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort; "
        f"workspace=Path({str(tmp_path)!r}); NativeV2RuntimePort(workspace); "
        "assert not (workspace / '.memoryguard').exists()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_phase9_safe_bridge_native_gui_aliases_use_bound_context_and_positional_shapes(tmp_path: Path):
    group_id = v2_personal_group_id("phase9-gui")
    GroupControlService(tmp_path, write=True).bind_agent("phase9-gui", group_id)

    manifest = _Manifest("V2_ACTIVE", 4)
    native = NativeV2RuntimePort(tmp_path, state_provider=manifest)
    facade = V2RuntimeFacade(manifest=manifest, v2=native, workspace=str(tmp_path))
    bridge = SafeBridgeApi(
        str(tmp_path),
        _v2_port=facade,
        _trusted_access_context=AccessContext(
            trusted_agent_id="phase9-gui",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="phase9-gui-session",
            session_source="transport",
            session_trusted=True,
        ),
    )

    listed = bridge.call_readonly("list_history_sessions", [{}, 50, 0, None, "", ""])
    assert listed["path"] == "v2"
    assert listed["data"]["neutral"] is True

    searched = bridge.call_readonly("search_history", ["secret", {}, 20, 0])
    assert searched["path"] == "v2"
    assert searched["data"]["neutral"] is True

    sources = bridge.call_readonly("list_sources", [])
    assert str(tmp_path) not in str(sources)

    preview = bridge.call_readonly("preview_import", [str(tmp_path)])
    assert preview["path"] == "v2"
    assert str(tmp_path) not in str(preview)
    assert not hasattr(bridge, "_inner_instance")


def test_phase9_safe_bridge_gui_spoof_is_rejected_and_delete_stays_mutation_gated(tmp_path: Path):
    GroupControlService(tmp_path, write=True).bind_agent("phase9-agent", v2_personal_group_id("phase9-agent"))
    manifest = _Manifest("V2_ACTIVE", 4)
    native = NativeV2RuntimePort(tmp_path, state_provider=manifest)
    facade = V2RuntimeFacade(manifest=manifest, v2=native, workspace=str(tmp_path))
    bridge = SafeBridgeApi(
        str(tmp_path), _v2_port=facade,
        _trusted_access_context=AccessContext(
            trusted_agent_id="phase9-agent",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="phase9-gui-session",
            session_source="transport",
            session_trusted=True,
        ),
    )
    spoof = native.dispatch_gui(
        "search_history",
        ["x", {"agent_instance_id": "attacker"}, 20, 0],
        context={
            "workspace_id": str(tmp_path),
            "agent_instance_id": "attacker",
            "share_group_id": "attacker-group",
        },
        generation=4,
        state="V2_ACTIVE",
    )
    assert spoof["code"] == "trusted_context_capability_required"

    # SafeBridge passes the mutation through the Native CAS gate; with no
    # active history DB it remains neutral and never repairs/creates storage.
    deleted = bridge.request_mutation(
        "delete_history", [["session-1"], {}, True, True, {"receipt_id": "r1"}, "k1"],
    )
    assert deleted["path"] == "v2"
    assert deleted.get("data", {}).get("neutral") is True
    assert not (tmp_path / ".memoryguard" / "history" / "history.sqlite").exists()
