"""Phase 0 integration regressions for bounded V2 context retrieval."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.codegraph_v2 import CodeGraphScope, CodeGraphStore, normalize_relative_path
from memoryguard.content import ContentStore
from memoryguard.content.conversation_sync import ConversationEvent, ConversationSync
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import V2MutationContext
from memoryguard.knowledge_v2.adapter import KnowledgeV2Adapter
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.rule_binding import RuleBinding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_reconciliation import canonical_reconciliation_status
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.runtime_v2.context_engine import ContextEngine
from memoryguard.runtime_v2.history_store import V2HistoryScope as HistoryScope
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)


class _ActiveManifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _project(root: Path) -> str:
    return canonical_project_ref(str(root.resolve()))


def _request(root: Path, **overrides: object) -> dict[str, object]:
    project = _project(root)
    result: dict[str, object] = {
        "task": "Worker history knowledge",
        "agent_instance_id": "agent-a",
        "share_group_id": "group-a",
        "project_ref": project,
        "provider": "codex",
        "runtime_role": "root",
        "workspace_id": str(root.resolve()),
        "namespace_id": "knowledge-a",
        "sensitivity": "normal",
        "policy_class": "private",
    }
    result.update(overrides)
    return result


def _seed_memory(root: Path) -> None:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    context = V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        actor="phase0-fixture",
        authority="manual",
        admin=True,
    )
    for memory_id, body, policy in (
        ("always-a", "mandatory V2 memory", "always"),
        ("relevant-a", "relevant V2 memory", "relevant"),
    ):
        memory.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=body,
                kind="procedure" if policy == "always" else "fact",
                injection_policy=policy,
                priority=10 if policy == "always" else 0,
                agent_instance_id="agent-a",
                share_group_id="group-a",
                # Empty optional dimensions are intentional: these atoms are
                # group+agent scoped and must be visible in every project.
            ),
            context=context,
            evidence=[{"source_ref": f"memory:{memory_id}"}],
        )
    memory.project_evidence(evidence)
    memory.set_visibility("active")


def _seed_knowledge(root: Path) -> None:
    content = ContentStore(root)
    namespace = content.ensure_namespace(namespace_id="knowledge-a", trust_domain="knowledge")
    blob = content.put_blob(namespace.namespace_id, "knowledge raw body must not enter context")
    content.upsert_occurrence(
        source_object_id="knowledge-object",
        occurrence_key="knowledge-occurrence",
        blob_id=blob,
        namespace_id="knowledge-a",
        workspace_id=str(root.resolve()),
        agent_instance_id="agent-a",
        project_ref=_project(root),
        provider="codex",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
        locator={"title": "knowledge reference"},
    )


def _seed_history(root: Path) -> None:
    ConversationSync(ContentStore(root)).sync(
        "phase0-history",
        [
            ConversationEvent(
                external_object_key="history-session",
                event_id="history-event",
                title="history summary",
                role="user",
                ordinal=0,
                content="history raw body must not enter context",
                provider="codex",
                workspace_id=str(root.resolve()),
                agent_instance_id="agent-a",
                project_ref=_project(root),
                share_group_id="group-a",
            )
        ],
    )


def _seed_codegraph(root: Path) -> None:
    graph = CodeGraphStore(root)
    scope = CodeGraphScope(
        workspace_id=str(root.resolve()),
        agent_instance_id="agent-a",
        project_ref=_project(root),
        provider="codex",
        share_group_id="group-a",
        runtime_role="root",
    )
    graph.upsert_source_file(
        "src/Worker.py",
        "worker-hash",
        scope=scope,
        symbols=[{
            "id": "worker-symbol",
            "name": "Worker",
            "kind": "class",
            "signature": "class Worker",
            "provenance": "production",
        }],
    )


def test_codegraph_status_resolves_trusted_group_indexed_child_from_parent_project(
    tmp_path: Path,
) -> None:
    graph = CodeGraphStore(tmp_path)
    indexed_project = tmp_path / "memoryguard"
    indexed_project.mkdir()
    graph.upsert_source_file(
        "src/native.py",
        "native-hash",
        scope=CodeGraphScope(
            workspace_id=str(tmp_path.resolve()),
            agent_instance_id="",
            project_ref=_project(indexed_project),
            provider="graphify",
            share_group_id="group-a",
            runtime_role="",
        ),
        symbols=[{
            "id": "native-symbol",
            "name": "NativePort",
            "kind": "class",
            "signature": "class NativePort",
            "provenance": "production",
        }],
    )
    port = NativeV2RuntimePort(tmp_path, state_provider=_ActiveManifest())
    context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="codegraph-scope",
            session_source="test",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path.resolve()),
        share_group_id="group-a",
        project_ref=_project(tmp_path),
        provider="codex",
        runtime_role="root",
    )

    result = port.dispatch_mcp(
        "memoryguard_codegraph_status",
        {"workspace": str(indexed_project)},
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )

    assert result["ok"] is True, result
    data = result["data"]
    assert data["counts"]["scopes"] == 1
    assert data["counts"]["source_files"] == 1
    assert data["counts"]["symbols"] == 1
    assert data["scope_digest"] == CodeGraphScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="",
        project_ref=_project(indexed_project),
        provider="graphify",
        share_group_id="group-a",
        runtime_role="",
    ).digest

    spoofed = port.dispatch_mcp(
        "memoryguard_codegraph_status",
        {"project_ref": str(tmp_path / "attacker")},
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )
    assert spoofed["code"] == "context_identity_spoof"


def test_knowledge_reference_resolves_indexed_child_from_parent_project(
    tmp_path: Path,
) -> None:
    child = tmp_path / "memoryguard"
    child.mkdir()
    content = ContentStore(tmp_path)
    namespace = content.ensure_namespace(namespace_id="knowledge-a", trust_domain="knowledge")
    blob = content.put_blob(namespace.namespace_id, "knowledge body stays private")
    content.upsert_occurrence(
        source_object_id="child-knowledge-object",
        occurrence_key="child-knowledge-occurrence",
        blob_id=blob,
        namespace_id="knowledge-a",
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        project_ref=_project(child),
        provider="codex",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
        locator={"title": "child knowledge reference"},
    )
    from memoryguard.content.store import ContentReadScope

    rows = KnowledgeV2Adapter(content, namespace_id="knowledge-a").read(
        ContentReadScope(
            namespace_id="knowledge-a",
            workspace_id=str(tmp_path.resolve()),
            agent_instance_id="agent-a",
            project_ref=_project(tmp_path),
            provider="codex",
            share_group_id="group-a",
            sensitivity="normal",
            policy_class="private",
        )
    )

    assert [row["summary"] for row in rows] == ["child knowledge reference"]


def test_optional_reference_failure_keeps_native_memory_and_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_memory(tmp_path)
    import memoryguard.context_bootstrap as reference_bridge

    monkeypatch.setattr(
        reference_bridge,
        "history_reference_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("history down")),
    )
    result = NativeV2RuntimePort(tmp_path, state_provider=_ActiveManifest()).retrieve(
        _request(tmp_path, task="relevant V2 memory")
    )

    assert [item["item_id"] for item in result["relevant"]] == ["relevant-a"]
    assert any(
        item.get("reason") == "history_source_unavailable"
        for item in result["omissions"]
    )


def _transport_context(root: Path, agent: str = "agent-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"phase0-{agent}",
            session_source="test",
            session_trusted=True,
        ),
        workspace_id=str(root.resolve()),
        share_group_id="group-a",
        project_ref=_project(root),
        provider="codex",
        runtime_role="root",
        namespace_id="knowledge-a",
        sensitivity="normal",
        policy_class="private",
    )


def test_bound_agent_group_atoms_map_policies_and_hide_other_agents(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_ActiveManifest())

    current = port.retrieve(_request(tmp_path, task="relevant V2 memory"))
    assert [item["item_id"] for item in current["mandatory"]] == ["always-a"]
    assert [item["item_id"] for item in current["relevant"]] == ["relevant-a"]
    assert all(item["scope"]["project_ref"] == _project(tmp_path) for group in current.values() for item in group if isinstance(group, list) and isinstance(item, dict) and "scope" in item)

    other_agent = port.retrieve(_request(
        tmp_path,
        agent_instance_id="agent-b",
        trusted_identity={
            "agent": "agent-b", "group": "group-a", "project": _project(tmp_path),
            "provider": "codex", "runtime": "root", "workspace_id": str(tmp_path.resolve()),
        },
    ))
    assert other_agent["mandatory"] == []
    assert other_agent["relevant"] == []


def _seed_task_aware_memories(root: Path) -> None:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    context = V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        actor="task-aware-fixture",
        authority="manual",
        admin=True,
    )
    atoms = (
        (
            "always-a",
            "always preserve this mandatory memory",
            "always",
            "procedure",
        ),
        (
            "memoryguard-health",
            "MemoryGuard health score uses bounded token budget",
            "relevant",
            "fact",
        ),
        (
            "crazygame-combat",
            "CrazyGame combat VFX uses boss motion offsets",
            "relevant",
            "fact",
        ),
        (
            "merakstar-deployment",
            "MerakStar deployment stays behind Cloudflare",
            "relevant",
            "fact",
        ),
    )
    for memory_id, body, policy, kind in atoms:
        memory.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=body,
                kind=kind,
                injection_policy=policy,
                confidence=0.99,
                agent_instance_id="agent-a",
                share_group_id="group-a",
            ),
            context=context,
            evidence=[{"source_ref": f"memory:{memory_id}"}],
        )
    memory.project_evidence(evidence)
    memory.set_visibility("active")


def test_task_aware_memory_search_excludes_unrelated_high_confidence_atoms(
    tmp_path: Path,
) -> None:
    _seed_task_aware_memories(tmp_path)
    result = NativeV2RuntimePort(tmp_path, state_provider=_ActiveManifest()).retrieve(
        _request(tmp_path, task="health score token budget")
    )

    assert [item["item_id"] for item in result["mandatory"]] == ["always-a"]
    assert [item["item_id"] for item in result["relevant"]] == ["memoryguard-health"]


def test_empty_or_failed_task_search_keeps_mandatory_and_never_falls_back_to_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_task_aware_memories(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_ActiveManifest())

    empty = port.retrieve(_request(tmp_path, task=""))
    assert [item["item_id"] for item in empty["mandatory"]] == ["always-a"]
    assert empty["relevant"] == []

    def fail_search(*_args: object, **_kwargs: object) -> list[MemoryAtom]:
        raise RuntimeError("search unavailable")

    monkeypatch.setattr(MemoryAtomStore, "search", fail_search)
    failed = port.retrieve(_request(tmp_path, task="health score"))
    assert [item["item_id"] for item in failed["mandatory"]] == ["always-a"]
    assert failed["relevant"] == []
    assert any(
        item == {"layer": "relevant", "reason": "retrieval_omitted"}
        for item in failed["omissions"]
    )


def test_rule_auto_read_uses_native_v2_compatibility_when_canonical_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = RuleV2Store(tmp_path)
    definition = rules.upsert_definition(build_definition("must use native compatibility", kind="procedure"))
    rules.upsert_binding(RuleBinding(
        binding_id="native-rule-binding",
        definition_id=definition.definition_id,
        share_group_id="group-a",
        target_type="agent",
        target_id="agent-a",
        project_ref=_project(tmp_path),
        provider="codex",
        runtime_role="root",
    ))
    assert canonical_reconciliation_status(tmp_path, "group-a")["canonical_ready"] is False

    # A V1 read path is a hard failure if touched; the compatibility read must
    # come from the V2 definition/binding store even before canonical readiness.
    import memoryguard.context_bootstrap as legacy_bootstrap
    monkeypatch.setattr(
        legacy_bootstrap,
        "build_context_packet",
        lambda *args, **kwargs: pytest.fail("V1 context reader was used"),
    )
    result = NativeV2RuntimePort(tmp_path, state_provider=_ActiveManifest()).retrieve(_request(tmp_path))
    assert any(item["item_id"] == definition.definition_id for item in result["relevant"] + result["mandatory"])


def test_bootstrap_retrieves_five_context_classes_as_bounded_reference_data(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    _seed_knowledge(tmp_path)
    _seed_history(tmp_path)
    _seed_codegraph(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_ActiveManifest())

    packet = port.dispatch_mcp(
        "memoryguard_context_bootstrap",
        _request(tmp_path, task="relevant V2 memory Worker history knowledge"),
        context=_transport_context(tmp_path),
        generation=7,
        state="V2_ACTIVE",
    )
    assert packet["ok"] is True, packet
    data = packet["data"]
    assert data["mandatory"]
    assert data["relevant"]
    assert data["reference_only"]
    references = json.dumps(data["reference_only"], ensure_ascii=False)
    assert "knowledge reference" in references
    assert "history summary" in references
    assert "Worker" in references and "src/Worker.py" in references and "worker-hash" in references
    assert "raw body" not in references
    assert all(set(item) <= {"summary", "ref", "hash", "trust"} for item in data["reference_only"])


def test_reference_only_renderer_and_codegraph_paths_are_normalized(tmp_path: Path) -> None:
    engine = ContextEngine(
        ready=True,
        state="V2_ACTIVE",
        retriever=lambda request: {
            "mandatory": [{
                "id": "rule-a", "body": "rule body", "kind": "rule", "is_rule": True,
                "source": "native-v2-rule", "scope": {"agent_instance_id": request.agent},
            }],
            "relevant": [{"id": "memory-a", "body": "memory body", "source": "native-v2-memory"}],
            "reference_only": [
                {"id": "history-a", "summary": "history summary", "ref": "history://a", "hash": "h1", "source": "native-v2-history"},
                {"id": "knowledge-a", "summary": "knowledge reference", "ref": "knowledge://a", "hash": "h2", "source": "native-v2-knowledge"},
                {"id": "codegraph-a", "summary": "Worker src/Worker.py worker-hash", "ref": "codegraph://Worker", "hash": "h3", "source": "native-v2-codegraph"},
            ],
        },
    )
    packet = engine.bootstrap({"agent": "agent-a", "task": "bounded"})
    assert packet.ready is True
    assert packet.reference_only
    assert all(set(item) == {"summary", "ref", "hash", "trust"} for item in packet.reference_only)
    assert all("body" not in item for item in packet.reference_only)

    assert normalize_relative_path(r"src\Worker.py") == "src/Worker.py"
    assert normalize_relative_path("src/./Worker.py") == "src/Worker.py"
    # Parent segments and a repository-root prefix must address the same file.
    assert normalize_relative_path("repo/src/Worker.py") == "src/Worker.py"
    assert normalize_relative_path("src/pkg/../Worker.py") == "src/Worker.py"
