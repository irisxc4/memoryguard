from __future__ import annotations

from pathlib import Path
import hashlib

import pytest

from memoryguard.content import ContentStore
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.rule_binding import RuleBinding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_reconciliation import canonical_reconciliation_status
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.context_engine import ContextEngine
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort


def _request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "task": "repair native retrieval",
        "agent_instance_id": "agent-a",
        "share_group_id": "group-a",
        "project_ref": "project-a",
        "provider": "codex",
        "runtime_role": "root",
        "trusted_identity": {
            "agent": "agent-a",
            "group": "group-a",
            "project": "project-a",
            "provider": "codex",
            "runtime": "root",
        },
    }
    value.update(overrides)
    return value


def test_native_retrieval_exception_is_not_converted_to_an_empty_packet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = NativeV2RuntimePort(tmp_path)

    class BrokenMemory:
        def list_atoms(self, **_: object) -> list[object]:
            raise RuntimeError("memory schema unavailable")

    monkeypatch.setattr(port, "_domain_store", lambda domain, **_: BrokenMemory())
    port.layout.memory_db.parent.mkdir(parents=True, exist_ok=True)
    port.layout.memory_db.touch()

    with pytest.raises(RuntimeError, match="native_v2_retrieval_failed"):
        port.retrieve(_request())


def test_native_rule_retrieval_exception_is_not_converted_to_an_empty_packet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = NativeV2RuntimePort(tmp_path)
    RuleV2Store(tmp_path)

    class BrokenRules:
        def list_definitions(self, **_: object) -> list[object]:
            raise RuntimeError("rules schema unavailable")

    import memoryguard.rule_reconciliation as reconciliation
    monkeypatch.setattr(
        reconciliation,
        "canonical_reconciliation_status",
        lambda *_args, **_kwargs: {"canonical_ready": True, "failures": []},
    )
    monkeypatch.setattr(port, "_domain_store", lambda domain, **_: BrokenRules())
    with pytest.raises(RuntimeError, match="native_v2_retrieval_failed"):
        port.retrieve(_request())


def test_native_rules_use_v2_compatibility_before_canonical_readiness(tmp_path: Path) -> None:
    rules = RuleV2Store(tmp_path)
    definition = rules.upsert_definition(build_definition(
        "keep evidence available",
        kind="procedure",
        rule_strength="observation",
    ))
    rules.upsert_binding(RuleBinding(
        binding_id="binding-a",
        definition_id=definition.definition_id,
        share_group_id="group-a",
        target_type="agent",
        target_id="agent-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    ))

    status = canonical_reconciliation_status(tmp_path, "group-a")
    assert status["canonical_ready"] is False
    assert "canonical_not_activated" in status["failures"]
    port = NativeV2RuntimePort(tmp_path)
    result = port.retrieve(_request())
    assert result["mandatory"] == []
    assert [item["item_id"] for item in result["relevant"]] == [definition.definition_id]


def test_native_rule_v2_compatibility_keeps_excludes(tmp_path: Path) -> None:
    rules = RuleV2Store(tmp_path)
    definition = rules.upsert_definition(build_definition("do not inject this rule", kind="procedure"))
    rules.upsert_binding(RuleBinding(
        binding_id="include-rule",
        definition_id=definition.definition_id,
        share_group_id="group-a",
        target_type="agent",
        target_id="agent-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
        effect="include",
    ))
    rules.upsert_binding(RuleBinding(
        binding_id="exclude-rule",
        definition_id=definition.definition_id,
        share_group_id="group-a",
        target_type="agent",
        target_id="agent-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
        effect="exclude",
    ))

    result = NativeV2RuntimePort(tmp_path).retrieve(_request())
    assert result["mandatory"] == []
    assert result["relevant"] == []


def _seed_rule_source_pair(
    root: Path,
    *,
    index: int,
    matched_agent: str = "agent-a",
    width: int = 96,
    rule_strength: str = "must",
) -> tuple[str, str]:
    source_memory_id = f"legacy-rule-{index}"
    memory = MemoryAtomStore(root)
    atom = memory.put_atom(
        MemoryAtom(
            memory_id=f"memory-{index}",
            body=f"legacy source {index} " + ("x" * width),
            kind="procedure",
            injection_policy="always",
            priority=10,
            agent_instance_id="agent-a",
            share_group_id="group-a",
        ),
        context=V2MutationContext(
            workspace_id=str(root.resolve()),
            share_group_id="group-a",
            agent_instance_id="agent-a",
            actor="native-retrieval-test",
            authority="manual",
            admin=True,
        ),
        evidence=[{"source_ref": f"legacy:{source_memory_id}"}],
        source_mappings=[{
            "source_domain": "shared_memory",
            "source_ref": "group-a/memory.db",
            "source_record_id": source_memory_id,
            "source_revision": "1",
        }],
    )
    memory.project_evidence(EvidenceStore(root))
    memory.set_visibility("active", atom_ids=[atom.atom_id])

    rules = RuleV2Store(root)
    definition = rules.upsert_definition(build_definition(
        f"canonical rule {index} " + ("y" * width),
        kind="procedure",
        rule_strength=rule_strength,
    ))
    rules.upsert_binding(RuleBinding(
        binding_id=f"binding-{index}",
        definition_id=definition.definition_id,
        share_group_id="group-a",
        target_type="agent",
        target_id=matched_agent,
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    ))
    rules.upsert_source_link(
        source_kind="shared_memory",
        share_group_id="group-a",
        memory_id=source_memory_id,
        source_ref="group-a/memory.db",
        source_revision="1",
        original_definition_id=definition.definition_id,
        canonical_definition_id=definition.definition_id,
        status="active",
    )
    return atom.memory_id, definition.definition_id


def test_matched_canonical_rule_suppresses_its_source_memory_shadow(tmp_path: Path) -> None:
    atom_id, definition_id = _seed_rule_source_pair(tmp_path, index=1)

    result = NativeV2RuntimePort(tmp_path).retrieve(_request())

    mandatory_ids = [item["item_id"] for item in result["mandatory"]]
    assert mandatory_ids == [definition_id]
    assert atom_id not in mandatory_ids


def test_unmatched_canonical_rule_does_not_hide_source_memory(tmp_path: Path) -> None:
    atom_id, definition_id = _seed_rule_source_pair(
        tmp_path,
        index=1,
        matched_agent="agent-b",
    )

    result = NativeV2RuntimePort(tmp_path).retrieve(_request())

    mandatory_ids = [item["item_id"] for item in result["mandatory"]]
    assert mandatory_ids == [atom_id]
    assert definition_id not in mandatory_ids


def test_relevant_canonical_rule_cannot_downgrade_always_source_memory(tmp_path: Path) -> None:
    atom_id, definition_id = _seed_rule_source_pair(
        tmp_path,
        index=1,
        rule_strength="observation",
    )

    result = NativeV2RuntimePort(tmp_path).retrieve(_request())

    assert [item["item_id"] for item in result["mandatory"]] == [atom_id]
    assert definition_id in [item["item_id"] for item in result["relevant"]]


def test_canonical_source_shadow_does_not_double_charge_mandatory_budget(tmp_path: Path) -> None:
    for index in range(6):
        _seed_rule_source_pair(tmp_path, index=index, width=96)

    port = NativeV2RuntimePort(tmp_path)
    request = _request(
        workspace_id=str(tmp_path.resolve()),
        trusted_identity={
            "agent": "agent-a",
            "group": "group-a",
            "project": "project-a",
            "provider": "codex",
            "runtime": "root",
            "workspace_id": str(tmp_path.resolve()),
        },
    )
    packet = ContextEngine(
        retriever=port,
        ready=True,
        state="V2_ACTIVE",
    ).bootstrap(request).to_dict()

    assert packet["status"] == "ok", packet
    assert packet.get("error") in {None, ""}
    assert len(packet["mandatory"]) == 6
    assert packet["budget"]["mandatory"]["tokens"] < 1000


def test_knowledge_references_use_exact_v2_acl_and_reference_only_shape(tmp_path: Path) -> None:
    content = ContentStore(tmp_path)
    namespace = content.ensure_namespace(namespace_id="ns-a", trust_domain="knowledge")
    blob = content.put_blob(namespace.namespace_id, "must not be returned")
    occurrence = content.upsert_occurrence(
        source_object_id="object-a",
        occurrence_key="occ-a",
        blob_id=blob,
        namespace_id="ns-a",
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="codex",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
        locator={"title": "Safe title"},
    )
    from memoryguard.context_bootstrap import knowledge_reference_candidates

    references = knowledge_reference_candidates(
        tmp_path,
        namespace_id="ns-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="codex",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
        query="Safe",
    )
    assert references == ({
        "summary": "Safe title",
        "ref": occurrence,
        "hash": hashlib.sha256("must not be returned".encode()).hexdigest(),
        "trust": "reference_only",
    },)
    assert "must not be returned" not in str(references)

    denied = knowledge_reference_candidates(
        tmp_path,
        namespace_id="ns-a",
        agent_instance_id="agent-b",
        project_ref="project-a",
        provider="codex",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
        query="Safe",
    )
    assert denied == ()

    packet = NativeV2RuntimePort(tmp_path).retrieve(_request(
        namespace_id="ns-a",
        sensitivity="normal",
        policy_class="private",
        task="Safe",
    ))
    assert len(packet["reference_only"]) == 1
    projected = packet["reference_only"][0]
    assert {key: projected[key] for key in ("summary", "ref", "hash", "trust")} == references[0]
    assert projected["source"] == "native-v2-knowledge"
    assert "must not be returned" not in str(packet)

    denied_packet = NativeV2RuntimePort(tmp_path).retrieve(_request(
        namespace_id="ns-a",
        sensitivity="normal",
        policy_class="private",
        task="Safe",
        agent_instance_id="agent-b",
        trusted_identity={
            "agent": "agent-b",
            "group": "group-a",
            "project": "project-a",
            "provider": "codex",
            "runtime": "root",
        },
    ))
    assert denied_packet["reference_only"] == []
