from __future__ import annotations

from pathlib import Path
import hashlib

import pytest

from memoryguard.content import ContentStore
from memoryguard.rule_binding import RuleBinding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_reconciliation import canonical_reconciliation_status
from memoryguard.rules.v2_store import RuleV2Store
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


def test_native_rules_are_not_injected_before_canonical_readiness(tmp_path: Path) -> None:
    rules = RuleV2Store(tmp_path)
    definition = rules.upsert_definition(build_definition("must keep evidence", kind="procedure"))
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
    assert result["relevant"] == []


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
    assert packet["reference_only"] == list(references)
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
