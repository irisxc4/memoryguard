from __future__ import annotations

import json
from pathlib import Path

from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.runtime_v2.dedup import canonical_hash
from memoryguard.runtime_v2.organizer import V2MemoryOrganizer


GROUP = "runtime-claim-composition"


def _organizer(workspace: Path) -> V2MemoryOrganizer:
    store = MemoryAtomStore(workspace)
    return V2MemoryOrganizer(
        workspace,
        GROUP,
        memory_store=store,
        governance=GovernanceV2(workspace, memory_store=store),
        threshold=0.50,
    )


def _context(workspace: Path) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(workspace.resolve()),
        share_group_id=GROUP,
        agent_instance_id="agent-a",
        actor="runtime-composition-test",
        admin=True,
        authority="system",
    )


def _write(
    organizer: V2MemoryOrganizer,
    workspace: Path,
    body: str,
    *,
    kind: str,
    event_id: str,
    metadata: dict | None = None,
    memory_id: str = "",
) -> dict:
    payload = {
        "body": body,
        "kind": kind,
        "event_id": event_id,
        "agent_instance_id": "agent-a",
        "share_group_id": GROUP,
        "visibility": "active",
        "metadata": metadata or {},
    }
    if memory_id:
        payload["memory_id"] = memory_id
    return organizer.write(payload, context=_context(workspace))


def _atoms(organizer: V2MemoryOrganizer) -> list:
    return organizer.store.list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(organizer.workspace),
            share_group_id=GROUP,
            admin=True,
        ),
        include_building=True,
    )


def _composition_records(organizer: V2MemoryOrganizer, memory_id: str) -> list[dict]:
    atom = next(atom for atom in _atoms(organizer) if atom.memory_id == memory_id)
    return list(atom.metadata.get("composition", {}).get("claims", []))


def _events_for_claim(records: list[dict], body: str) -> set[str]:
    digest = canonical_hash(body)
    return {
        str(item.get("source_event_id") or "")
        for item in records
        if item.get("claim_digest") == digest
    }


def test_runtime_composes_cross_kind_claims_into_one_canonical_body(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first_body = "测试策略：无代码改动不要重复全量测试"
    second_body = "测试策略：优先运行与改动相关的定向测试"

    first = _write(
        organizer,
        tmp_path,
        first_body,
        kind="procedure",
        event_id="event-procedure",
    )
    second = _write(
        organizer,
        tmp_path,
        second_body,
        kind="preference",
        event_id="event-preference",
    )

    active = [atom for atom in _atoms(organizer) if atom.status == "active"]
    assert second["memory_id"] == first["memory_id"]
    assert len(active) == 1
    assert active[0].body.count(first_body) == 1
    assert active[0].body.count(second_body) == 1
    read_back = organizer.store.get_atom(
        first["memory_id"], scope=organizer.scope, include_building=True,
    )
    assert read_back is not None
    assert first_body in read_back.body and second_body in read_back.body
    assert active[0].canonical_hash
    metadata_json = json.dumps(active[0].metadata, ensure_ascii=False)
    assert first_body not in metadata_json
    assert second_body not in metadata_json
    assert active[0].metadata["composition"]["claims"]


def test_runtime_composition_is_idempotent_for_same_meaning(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    body = "测试策略：优先运行与改动相关的定向测试"

    first = _write(organizer, tmp_path, body, kind="procedure", event_id="event-one")
    second = _write(organizer, tmp_path, body, kind="preference", event_id="event-two")

    active = [atom for atom in _atoms(organizer) if atom.status == "active"]
    assert second["memory_id"] == first["memory_id"]
    assert len(active) == 1
    assert active[0].body.count(body) == 1
    assert len({item["claim_digest"] for item in active[0].metadata["composition"]["claims"]}) == 1


def test_runtime_composes_short_and_long_same_topic_across_kinds(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    short_body = "数据库：查询前检查索引"
    long_body = "数据库：查询前检查索引，并记录查询来源"

    first = _write(
        organizer,
        tmp_path,
        short_body,
        kind="procedure",
        event_id="event-short",
    )
    result = _write(
        organizer,
        tmp_path,
        long_body,
        kind="fact",
        event_id="event-long",
    )

    active = [atom for atom in _atoms(organizer) if atom.status == "active"]
    assert len(active) == 1
    assert result["mutation_kind"] in {"superseded", "deduplicated"}
    assert long_body in active[0].body
    composition = active[0].metadata.get("composition", {})
    assert composition
    assert _events_for_claim(
        list(composition.get("claims", [])), long_body,
    ) == {"event-short", "event-long"}
    assert result["memory_id"] != "" and first["memory_id"] != ""


def test_runtime_does_not_turn_one_negative_claim_into_whole_topic_conflict(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first_body = "安全约束：不要把密钥写入日志；安全约束：部署前轮换密钥"
    incoming_body = "安全约束：发布前验证配置签名"

    first = _write(
        organizer,
        tmp_path,
        first_body,
        kind="procedure",
        event_id="event-security-negative",
    )
    result = _write(
        organizer,
        tmp_path,
        incoming_body,
        kind="preference",
        event_id="event-security-positive",
    )

    atoms = _atoms(organizer)
    assert result["mutation_kind"] != "conflicted"
    assert len([atom for atom in atoms if atom.status == "active"]) == 1
    canonical = next(atom for atom in atoms if atom.status == "active")
    assert "不要把密钥写入日志" in canonical.body
    assert "部署前轮换密钥" in canonical.body
    assert incoming_body in canonical.body


def test_composition_claim_sources_stay_bound_to_claims_across_replays(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    claim_a = "Release policy: publish after approval"
    claim_b = "Release policy: notify reviewers after publishing"
    claim_c = "Release policy: archive release notes"

    first = _write(organizer, tmp_path, claim_a, kind="procedure", event_id="event-a")
    _write(organizer, tmp_path, claim_b, kind="preference", event_id="event-b")
    records_after_b = _composition_records(organizer, first["memory_id"])
    assert _events_for_claim(records_after_b, claim_a) == {"event-a"}
    assert _events_for_claim(records_after_b, claim_b) == {"event-b"}
    assert {
        item.get("source_role")
        for item in records_after_b
        if item.get("claim_digest") == canonical_hash(claim_a)
    } == {"candidate_body_provenance"}
    assert {
        item.get("source_role")
        for item in records_after_b
        if item.get("claim_digest") == canonical_hash(claim_b)
    } == {"incoming_body"}

    _write(organizer, tmp_path, claim_c, kind="preference", event_id="event-c")
    records_after_c = _composition_records(organizer, first["memory_id"])
    assert _events_for_claim(records_after_c, claim_a) == {"event-a"}
    assert _events_for_claim(records_after_c, claim_b) == {"event-b"}
    assert _events_for_claim(records_after_c, claim_c) == {"event-c"}


def test_updated_claim_inherits_old_and_new_sources(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    old_claim = "Release policy: publish after approval"
    new_claim = "Release policy: publish after manual approval"

    first = _write(organizer, tmp_path, old_claim, kind="procedure", event_id="event-old")
    result = _write(organizer, tmp_path, new_claim, kind="procedure", event_id="event-new")

    records = _composition_records(organizer, result["memory_id"])
    assert result["memory_id"] == first["memory_id"]
    assert _events_for_claim(records, new_claim) == {"event-old", "event-new"}
    assert {
        item.get("source_role")
        for item in records
        if item.get("claim_digest") == canonical_hash(new_claim)
    } == {"candidate_body_provenance", "incoming_body"}
    assert all(old_claim not in str(item) and new_claim not in str(item) for item in records)


def test_composition_rejected_conflict_stays_independent(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    canonical_body = "测试策略：无代码改动不要重复全量测试"
    conflict_body = "每轮必须运行完整测试"

    first = _write(
        organizer,
        tmp_path,
        canonical_body,
        kind="procedure",
        event_id="event-canonical",
    )
    result = _write(
        organizer,
        tmp_path,
        conflict_body,
        kind="procedure",
        event_id="event-conflict",
    )

    atoms = _atoms(organizer)
    assert result["memory_id"] != first["memory_id"]
    assert result["mutation_kind"] in {"created", "conflicted"}
    assert len(atoms) == 2
    canonical = next(atom for atom in atoms if atom.memory_id == first["memory_id"])
    assert conflict_body not in canonical.body
    if result["mutation_kind"] == "conflicted":
        assert canonical.status == "conflicted"


def test_non_test_heading_conflict_uses_shared_anchor_and_modality(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first = _write(
        organizer,
        tmp_path,
        "发布策略：不要自动发布",
        kind="procedure",
        event_id="event-publish-negative",
    )
    result = _write(
        organizer,
        tmp_path,
        "发布策略：每次必须自动发布",
        kind="preference",
        event_id="event-publish-positive",
    )

    atoms = _atoms(organizer)
    assert result["mutation_kind"] == "conflicted"
    assert len(atoms) == 2
    canonical = next(atom for atom in atoms if atom.memory_id == first["memory_id"])
    assert "每次必须自动发布" not in canonical.body


def test_unrelated_negative_and_positive_claims_stay_independent(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first = _write(
        organizer,
        tmp_path,
        "发布策略：不要自动发布",
        kind="procedure",
        event_id="event-unrelated-negative",
    )
    result = _write(
        organizer,
        tmp_path,
        "备份策略：每次必须加密备份",
        kind="preference",
        event_id="event-unrelated-positive",
    )

    atoms = _atoms(organizer)
    assert result["mutation_kind"] == "created"
    assert result["memory_id"] != first["memory_id"]
    assert len(atoms) == 2


def test_composer_unrelated_claim_is_not_dropped_or_folded_into_canonical(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first_body = "发布策略：自动发布"
    incoming_body = "发布策略：自动发布。备份策略：每次必须加密备份"
    first = _write(
        organizer,
        tmp_path,
        first_body,
        kind="procedure",
        event_id="event-composer-base",
    )
    result = _write(
        organizer,
        tmp_path,
        incoming_body,
        kind="procedure",
        event_id="event-composer-unrelated",
    )

    atoms = _atoms(organizer)
    assert result["mutation_kind"] == "created"
    assert len(atoms) == 2
    canonical = next(atom for atom in atoms if atom.memory_id == first["memory_id"])
    assert canonical.body == first_body
    incoming = next(atom for atom in atoms if atom.memory_id == result["memory_id"])
    assert "备份策略：每次必须加密备份" in incoming.body


def test_explicit_memory_update_replaces_body_without_composition(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first = _write(
        organizer,
        tmp_path,
        "Always preserve the original body.",
        kind="procedure",
        event_id="event-original",
    )
    replacement = "Replace the complete body during explicit update."
    result = _write(
        organizer,
        tmp_path,
        replacement,
        kind="procedure",
        event_id="event-update",
        memory_id=first["memory_id"],
    )

    atom = next(atom for atom in _atoms(organizer) if atom.memory_id == first["memory_id"])
    assert result["memory_id"] == first["memory_id"]
    assert atom.body == replacement
    assert "Always preserve the original body." not in atom.body
