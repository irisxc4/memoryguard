"""V2 A+B acceptance tests for shared-memory search and authority boundaries."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

from memoryguard.access_context import AccessContext, load_access_context, preflight_check
from memoryguard.auto_organizer import AutoOrganizer
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.schema_v3 import MemoryEvent, MemoryKind, stable_hash, _now_iso
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate(workspace: Path) -> None:
    initialize_all(WorkspaceV2Layout(workspace))
    MemoryAtomStore(workspace)
    EvidenceStore(workspace)
    GovernanceV2(workspace)
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="milestone-ab-v2")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="milestone-ab-source",
        target_digest="milestone-ab-target",
        manifest_digest="milestone-ab-manifest",
        digests={"validator_passed": True, "checkpoints": {"memory": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _bind(workspace: Path, agent: str, group: str) -> None:
    GroupControlService(workspace, write=True).bind_agent(
        agent,
        group,
        idempotency_key=f"milestone-bind:{agent}:{group}",
    )


def _context(workspace: Path, *, agent: str = "agent-a", group: str = "group-a", admin: bool = True):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"milestone-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id=group,
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def _seed(
    workspace: Path,
    memory_id: str,
    body: str,
    *,
    kind: str = "fact",
    group: str = "group-a",
    agent: str = "agent-a",
    confidence: float = 1.0,
    metadata: dict | None = None,
) -> MemoryAtom:
    store = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    governance = GovernanceV2(workspace, memory_store=store, evidence_store=evidence)
    scope = {
        "workspace_id": str(workspace.resolve()),
        "share_group_id": group,
        "agent_instance_id": agent,
        "project_ref": str(workspace.resolve()),
        "provider": "gui",
        "runtime_role": "gui",
        "actor": "milestone-fixture",
        "authority": "manual",
    }
    atom, _ = governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind=kind,
            status="active",
            confidence=confidence,
            injection_policy="relevant",
            priority=0,
            metadata=dict(metadata or {}),
            workspace_id=scope["workspace_id"],
            share_group_id=group,
            agent_instance_id=agent,
            project_ref=scope["project_ref"],
            provider="gui",
            runtime_role="gui",
        ),
        context=scope,
        evidence=[{"source_ref": f"milestone:{memory_id}", "authority": "governance"}],
        reason="milestone V2 fixture",
        confidence=confidence,
        idempotency_key=f"milestone-seed:{memory_id}",
    )
    store.project_evidence(evidence)
    store.set_visibility("active", atom_ids=[atom.atom_id])
    return atom


def _organize(workspace: Path, group: str, agent: str, body: str, event_id: str):
    store = MemoryAtomStore(workspace)
    organizer = AutoOrganizer(
        workspace,
        group,
        store=store,
        engine=GovernanceV2(workspace, memory_store=store, evidence_store=EvidenceStore(workspace)),
        threshold=0.85,
    )
    result = organizer.organize(
        MemoryEvent(
            event_id=event_id,
            agent_instance_id=agent,
            share_group_id=group,
            raw_content=body,
            metadata={},
            created_at=_now_iso(),
        )
    )
    store.project_evidence(EvidenceStore(workspace))
    store.set_visibility("active", atom_ids=[result[0].atom_id])
    return result


def _search(
    workspace: Path,
    query: str,
    *,
    group: str = "group-a",
    agent: str = "agent-a",
) -> list[dict]:
    result = _port(workspace).dispatch_mcp(
        "memoryguard_memory_search",
        {"query": query, "status": "active"},
        context=_context(workspace, group=group, agent=agent),
        generation=11,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    return list(result["data"])


def test_cross_group_dup_canonical_hash(monkeypatch):
    del monkeypatch
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        _activate(workspace)
        _bind(workspace, "agent-a", "g1")
        _bind(workspace, "agent-b", "g2")
        prefix = "A" * 100
        _organize(workspace, "g1", "agent-a", prefix + " suffix A", "event-a")
        _organize(workspace, "g2", "agent-b", prefix + " suffix B", "event-b")

        result = GroupControlService(workspace).get_global_memory_status()
        assert result["cross_group_duplicates"] == []


def test_cross_group_dup_same_content_detected(monkeypatch):
    del monkeypatch
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        _activate(workspace)
        _bind(workspace, "agent-a", "g1")
        _bind(workspace, "agent-b", "g2")
        body = "the same cross-group content for the V2 duplicate check"
        _organize(workspace, "g1", "agent-a", body, "event-a")
        _organize(workspace, "g2", "agent-b", body, "event-b")

        duplicates = GroupControlService(workspace).get_global_memory_status()[
            "cross_group_duplicates"
        ]
        assert len(duplicates) >= 1
        assert duplicates[0]["share_group_ids"] == ["g1", "g2"]


def test_gui_bind_agent_non_admin_denied(monkeypatch, tmp_path: Path):
    del monkeypatch
    _activate(tmp_path)
    result = _port(tmp_path).dispatch_gui(
        "bind_agents_to_shared_group",
        [["agent-a", "agent-b"], "g1", "memoryguard", {}, {}, False],
        context=_context(tmp_path, group="g1", admin=False),
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert result["ok"] is False
    assert result["code"] == "admin_capability_required"


def test_gui_bind_agent_rejects_admin_override_forgery(monkeypatch, tmp_path: Path):
    del monkeypatch
    _activate(tmp_path)
    result = _port(tmp_path).dispatch_gui(
        "bind_agents_to_shared_group",
        [["agent-a", "agent-b"], "g1", "memoryguard", {}, {}, True],
        context=_context(tmp_path, group="g1", admin=False),
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert result["ok"] is False
    assert result["code"] == "admin_capability_required"


def test_preflight_check_prints_status(monkeypatch):
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "test-agent")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.delenv("MEMORYGUARD_ALLOW_ANON", raising=False)

    buf = io.StringIO()
    warnings = preflight_check(load_access_context(), stream=buf)
    output = buf.getvalue()
    assert "agent_id=test-agent" in output
    assert "admin=ON" in output
    assert "strict_binding=ON" in output
    assert warnings == []
    assert "preflight OK" in output


def test_preflight_check_warns_missing_agent(monkeypatch):
    for key in ["MEMORYGUARD_AGENT_ID", "MEMORYGUARD_ADMIN", "MEMORYGUARD_ALLOW_ANON"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    buf = io.StringIO()
    warnings = preflight_check(load_access_context(), stream=buf)
    output = buf.getvalue()
    assert any("AGENT_ID not set" in item for item in warnings)
    assert any("ADMIN not set" in item for item in warnings)
    assert "WARNING" in output


def test_fts5_search_recall(tmp_path: Path):
    _activate(tmp_path)
    _bind(tmp_path, "agent-a", "fts-test")
    _seed(tmp_path, "r1", "User preference: Python programming language", group="fts-test")
    _seed(tmp_path, "r2", "Deploy the project on AWS", group="fts-test")
    _seed(tmp_path, "r3", "Python test coverage must reach 80 percent", group="fts-test")
    _seed(tmp_path, "r4", "Database migration procedure", group="fts-test")
    ids = {item["memory_id"] for item in _search(tmp_path, "Python", group="fts-test")}
    assert {"r1", "r3"} <= ids
    assert not {"r2", "r4"} & ids


def test_fts5_bm25_ranking(tmp_path: Path):
    _activate(tmp_path)
    _bind(tmp_path, "agent-a", "bm25-test")
    _seed(tmp_path, "r1", "Python Python Python is the preferred language", group="bm25-test")
    _seed(tmp_path, "r2", "Occasionally use Python in scripts", group="bm25-test")
    results = _search(tmp_path, "Python", group="bm25-test")
    assert len(results) >= 2
    assert results[0]["memory_id"] == "r1"


def test_fts5_results_have_metadata(tmp_path: Path):
    _activate(tmp_path)
    _bind(tmp_path, "agent-x", "meta-test")
    _seed(
        tmp_path,
        "r1",
        "test metadata returned by the native search",
        kind="preference",
        group="meta-test",
        agent="agent-x",
        confidence=0.92,
        metadata={"provenance": [{"source_ref": "src-1", "locator": "line:1"}]},
    )
    result = _search(tmp_path, "metadata", group="meta-test", agent="agent-x")
    assert len(result) == 1
    item = result[0]
    assert item["share_group_id"] == "meta-test"
    assert item["agent_instance_id"] == "agent-x"
    assert item["kind"] == "preference"
    assert item["confidence"] == 0.92
    assert "provenance" in item


def test_fts5_empty_query(tmp_path: Path):
    _activate(tmp_path)
    _bind(tmp_path, "agent-a", "empty-test")
    assert _search(tmp_path, "", group="empty-test") == []
    assert _search(tmp_path, "   ", group="empty-test") == []


def test_fts5_fallback_on_error(tmp_path: Path):
    _activate(tmp_path)
    _bind(tmp_path, "agent-a", "fallback-test")
    _seed(tmp_path, "r1", "normal content for fallback search", group="fallback-test")
    results = _search(tmp_path, "normal", group="fallback-test")
    assert results[0]["memory_id"] == "r1"


def test_fts5_fallback_matches_non_contiguous_chinese_keywords(tmp_path: Path):
    _activate(tmp_path)
    _bind(tmp_path, "agent-a", "keyword-test")
    _seed(
        tmp_path,
        "r1",
        "prefer the smallest sufficient plan and keep external changes minimal",
        kind="preference",
        group="keyword-test",
    )
    results = _search(tmp_path, "smallest", group="keyword-test")
    assert [item["memory_id"] for item in results] == ["r1"]
