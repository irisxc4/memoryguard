from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from memoryguard.governance_v2 import V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort


def _context(root: Path) -> dict[str, str]:
    return {
        "workspace_id": str(root.resolve()),
        "agent_instance_id": "agent-a",
        "share_group_id": "group-a",
        "project_ref": "project-a",
        "provider": "codex",
        "runtime_role": "root",
    }


def _mutation_context(root: Path) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
        actor="canonical-projection-fixture",
        authority="manual",
        admin=True,
    )


def _bind(rules: RuleV2Store, definition_id: str, *, binding_id: str) -> None:
    rules.upsert_binding(build_binding(
        definition_id,
        binding_id=binding_id,
        share_group_id="group-a",
        target_type="agent",
        target_id="agent-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
        owner_agent_id="agent-a",
        created_by="admin",
        authorization="fixture:canonical-projection",
    ))


def test_rule_snapshot_collapses_alias_chain_but_keeps_conflict_definitions(tmp_path: Path) -> None:
    rules = RuleV2Store(tmp_path)
    canonical = rules.upsert_definition(build_definition(
        "Always preserve the canonical projection.",
        definition_id="rule-canonical",
        kind="procedure",
        rule_strength="must",
    ))
    # Malformed/partially migrated stores can expose an active predecessor
    # with a successor marker. Native read projection must still resolve it.
    predecessor = build_definition(
        "Always preserve the old projection.",
        definition_id="rule-predecessor",
        kind="procedure",
        rule_strength="must",
    )
    predecessor.superseded_by = canonical.definition_id
    rules.upsert_definition(predecessor)
    _bind(rules, canonical.definition_id, binding_id="binding-canonical")
    _bind(rules, predecessor.definition_id, binding_id="binding-predecessor")
    rules.upsert_source_link(
        source_kind="memory",
        share_group_id="group-a",
        memory_id="mirror-memory",
        source_ref="memory:mirror-memory",
        original_definition_id=predecessor.definition_id,
        canonical_definition_id=predecessor.definition_id,
        status="active",
    )

    # Opposite polarity is a real conflict branch, not an alias.
    conflict = rules.upsert_definition(build_definition(
        "Never preserve the canonical projection.",
        definition_id="rule-conflict",
        kind="procedure",
        rule_strength="must",
    ))
    _bind(rules, conflict.definition_id, binding_id="binding-conflict")

    snapshot = NativeV2RuntimePort(tmp_path)._gui_rule_snapshot({}, _context(tmp_path))
    visible = snapshot["rules"]
    assert {row["definition_id"] for row in visible} == {
        "rule-canonical",
        "rule-conflict",
    }
    assert snapshot["total"] == 2
    assert sum(len(items) for items in snapshot["buckets"].values()) == 2


def test_memory_list_uses_rule_as_representative_and_folds_memory_supersede_chain(tmp_path: Path) -> None:
    memory = MemoryAtomStore(tmp_path)
    context = _mutation_context(tmp_path)
    memory.put_atom(MemoryAtom(
        memory_id="mirror-memory",
        body="Always preserve the canonical projection.",
        kind="procedure",
        status="active",
        visibility="active",
        workspace_id=str(tmp_path.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    ), context=context)
    memory.put_atom(MemoryAtom(
        memory_id="old-memory",
        body="old text",
        status="active",
        visibility="active",
        workspace_id=str(tmp_path.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    ), context=context)
    memory.put_atom(MemoryAtom(
        memory_id="new-memory",
        body="new text",
        status="active",
        supersedes=["old-memory"],
        visibility="active",
        workspace_id=str(tmp_path.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    ), context=context)

    rules = RuleV2Store(tmp_path)
    definition = rules.upsert_definition(build_definition(
        "Always preserve the canonical projection.",
        definition_id="rule-canonical",
        kind="procedure",
        rule_strength="must",
    ))
    _bind(rules, definition.definition_id, binding_id="binding-canonical")
    rules.upsert_source_link(
        source_kind="memory",
        share_group_id="group-a",
        memory_id="mirror-memory",
        source_ref="memory:mirror-memory",
        original_definition_id=definition.definition_id,
        canonical_definition_id=definition.definition_id,
        status="active",
    )

    rows = NativeV2RuntimePort(tmp_path)._memory_list(
        {"status": "active"}, _context(tmp_path),
    )
    assert {row.memory_id for row in rows} == {"new-memory"}


@dataclass
class _AuditResult:
    blockers: tuple[SimpleNamespace, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": "BLOCKED",
            "blocked": True,
            "domains": ["rules"],
            "blockers": [
                {
                    "code": item.code,
                    "domain": item.domain,
                    "table": item.table,
                }
                for item in self.blockers
            ],
        }


def test_reference_audit_projection_adds_readable_finding_fields(monkeypatch, tmp_path: Path) -> None:
    blocker = SimpleNamespace(
        code="schema_unreadable",
        domain="rules",
        table="rule_definitions",
    )

    class FakeAudit:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def audit(self) -> _AuditResult:
            return _AuditResult((blocker,))

    monkeypatch.setattr(
        "memoryguard.maintenance_v2.reference_audit.ReferenceAudit",
        FakeAudit,
    )
    data = NativeV2RuntimePort(tmp_path)._reference_audit({}, {})
    finding = data["data"]["blockers"][0]

    assert finding["code"] == "schema_unreadable"
    assert finding["rule_id"] == "schema_unreadable"
    assert finding["title"] == "架构不可读"
    assert finding["type_label"] == "架构不可读"
    assert finding["severity"] == "high"
    assert finding["severity_label"] == "高风险"
    assert finding["summary"]
