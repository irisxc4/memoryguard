"""V2 group-control and personal-memory contract tests.

These tests exercise the one V2 memory plane with explicit group/read scopes.
Personal and shared groups are control-plane bindings, not separate legacy
store files; switching a binding must never move or expose another group's
atoms.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

import pytest

from memoryguard.content import ContentStore
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext, V2ScopeError
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.projection_v2 import ProjectionReadScope
from memoryguard.runtime_v2.group_native import (
    GroupControlError,
    GroupControlService,
    personal_group_id,
)
from memoryguard.runtime_v2.projection_build import ProjectionBuildService


AGENT = "agent-a"
PROVIDER = "test-provider"
RUNTIME_ROLE = "test"


def _context(tmp_path: Path, agent: str, group: str) -> V2MutationContext:
    root = str(tmp_path.resolve())
    return V2MutationContext(
        workspace_id=root,
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=root,
        provider=PROVIDER,
        runtime_role=RUNTIME_ROLE,
        actor=agent,
        authority="manual",
    )


def _admin_context(tmp_path: Path, group: str) -> V2MutationContext:
    root = str(tmp_path.resolve())
    return V2MutationContext(
        workspace_id=root,
        share_group_id=group,
        project_ref=root,
        provider=PROVIDER,
        runtime_role=RUNTIME_ROLE,
        actor="group-admin",
        admin=True,
        authority="admin",
    )


def _read_scope(tmp_path: Path, agent: str, group: str, *, admin: bool = False) -> MemoryReadScope:
    root = str(tmp_path.resolve())
    return MemoryReadScope(
        workspace_id=root,
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=root,
        provider=PROVIDER,
        runtime_role=RUNTIME_ROLE,
        admin=admin,
    )


def _seed_atoms(
    tmp_path: Path,
    agent: str,
    group: str,
    values: list[tuple[str, str]],
) -> list[MemoryAtom]:
    """Write governed V2 atoms and drain the evidence gate once."""
    memory = MemoryAtomStore(tmp_path)
    evidence_store = EvidenceStore(tmp_path)
    governance = GovernanceV2(
        tmp_path,
        memory_store=memory,
        evidence_store=evidence_store,
    )
    context = _context(tmp_path, agent, group)
    persisted: list[MemoryAtom] = []
    for memory_id, body in values:
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        atom = MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind="fact",
            status="active",
            confidence=0.9,
            workspace_id=str(tmp_path.resolve()),
            agent_instance_id=agent,
            share_group_id=group,
            project_ref=str(tmp_path.resolve()),
            provider=PROVIDER,
            runtime_role=RUNTIME_ROLE,
            metadata={"origin": "v2-group-test", "title": memory_id},
            provenance=[{
                "source": "test",
                "source_ref": f"test:{group}:{memory_id}",
                "source_digest": digest,
            }],
        )
        item, _decision = governance.put_atom(
            atom,
            context=context,
            evidence=[{
                "source_ref": f"test:{group}:{memory_id}",
                "revision": "1",
                "digest": digest,
                "authority": "governance",
                "metadata": {},
            }],
            source_mappings=[{
                "source_domain": "test",
                "source_ref": f"test:{group}:{memory_id}",
                "source_record_id": memory_id,
                "source_revision": "1",
                "digest": digest,
            }],
            reason="seed V2 personal-group contract test",
            confidence=1.0,
            idempotency_key=f"seed:{group}:{memory_id}",
        )
        persisted.append(item)
    if persisted:
        memory.project_evidence(evidence_store)
        memory.set_visibility("active", atom_ids=[item.atom_id for item in persisted])
    return persisted


def test_personal_group_id_is_stable_and_safe(tmp_path: Path):
    del tmp_path
    gid = personal_group_id(r"unsafe/agent\\id")
    assert gid == personal_group_id(r"unsafe/agent\\id")
    assert gid.startswith("personal-")
    assert "/" not in gid and "\\" not in gid and len(gid) <= 128


def test_ensure_personal_is_idempotent_and_preserves_shared_binding(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    first = control.ensure_personal(AGENT)
    again = control.ensure_personal(AGENT)
    assert first["created"] is True
    assert again["created"] is False and again["changed"] is False

    control.bind_agent("agent-b", "shared-team")
    control.bind_agent(AGENT, "shared-team")
    preserved = control.ensure_personal(AGENT)
    assert preserved["share_group_id"] == "shared-team"
    assert control.active_binding_for_agent(AGENT)["share_group_id"] == "shared-team"


def test_leave_unbound_agent_creates_its_personal_group(tmp_path: Path):
    result = GroupControlService(tmp_path, write=True).leave_to_personal(AGENT)
    assert result["share_group_id"] == personal_group_id(AGENT)
    assert result["previous_group_id"] == ""
    assert result["changed"] is True


def test_leave_shared_returns_to_same_personal_group_without_merge(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    personal = control.ensure_personal(AGENT)["share_group_id"]
    control.bind_agent("agent-b", "shared-team")
    control.bind_agent(AGENT, "shared-team")
    result = control.leave_to_personal(AGENT)
    assert result["share_group_id"] == personal
    assert control.active_binding_for_agent(AGENT)["share_group_id"] == personal
    assert control.group_preview("shared-team")["member_count"] == 1


def test_personal_and_shared_groups_use_one_v2_store_with_scope_isolation(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    personal = control.ensure_personal("agent-personal")["share_group_id"]
    control.bind_agents([AGENT, "agent-b"], share_group_id="shared-team")
    _seed_atoms(tmp_path, "agent-personal", personal, [("personal-marker", "personal-only marker")])
    _seed_atoms(tmp_path, AGENT, "shared-team", [("shared-marker", "shared-only marker")])

    personal_memory = MemoryAtomStore(tmp_path, readonly=True)
    assert personal_memory.layout.memory_db == MemoryAtomStore(tmp_path, readonly=True).layout.memory_db
    personal_bodies = {item.body for item in personal_memory.list_atoms(scope=_read_scope(tmp_path, "agent-personal", personal))}
    shared_bodies = {item.body for item in personal_memory.list_atoms(scope=_read_scope(tmp_path, AGENT, "shared-team"))}
    assert personal_bodies == {"personal-only marker"}
    assert shared_bodies == {"shared-only marker"}
    assert not personal_bodies & shared_bodies

    control.leave_to_personal(AGENT)
    # Binding changes do not rewrite the shared group's V2 atoms; the former
    # shared scope remains an explicit, auditable scope for administrators.
    assert {item.body for item in personal_memory.list_atoms(scope=_read_scope(tmp_path, AGENT, "shared-team"))} == {"shared-only marker"}
    assert {item.body for item in personal_memory.list_atoms(scope=_read_scope(tmp_path, AGENT, personal))} == set()
    assert {item.body for item in personal_memory.list_atoms(scope=_read_scope(tmp_path, "agent-personal", personal))} == personal_bodies


def test_v2_read_snapshot_survives_concurrent_governed_writes(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    control.bind_agent(AGENT, "wal-group")
    control.bind_agent("agent-b", "wal-group")
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=memory, evidence_store=evidence)
    context = _context(tmp_path, AGENT, "wal-group")
    failures: list[BaseException] = []
    started = threading.Event()

    def writer() -> None:
        try:
            for index in range(20):
                body = f"concurrent record {index}"
                digest = hashlib.sha256(body.encode()).hexdigest()
                item, _ = governance.put_atom(
                    MemoryAtom(
                        memory_id=f"concurrent-{index}",
                        body=body,
                        status="active",
                        workspace_id=str(tmp_path.resolve()),
                        agent_instance_id=AGENT,
                        share_group_id="wal-group",
                        project_ref=str(tmp_path.resolve()),
                        provider=PROVIDER,
                        runtime_role=RUNTIME_ROLE,
                    ),
                    context=context,
                    evidence=[{"source_ref": f"concurrent:{index}", "revision": "1", "digest": digest, "authority": "governance", "metadata": {}}],
                    reason="concurrent V2 write",
                    idempotency_key=f"concurrent:{index}",
                )
                if index == 0:
                    started.set()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    started.wait(timeout=5)
    scope = _read_scope(tmp_path, AGENT, "wal-group")
    while thread.is_alive():
        MemoryAtomStore(tmp_path, readonly=True).list_atoms(scope=scope, include_building=True)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures
    memory.project_evidence(evidence)
    atoms = memory.list_atoms(scope=scope, include_building=True)
    memory.set_visibility("active", atom_ids=[item.atom_id for item in atoms])
    assert len(memory.list_atoms(scope=scope)) == 20


def test_group_listing_exposes_personal_or_shared_kind(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    personal = control.ensure_personal(AGENT)["share_group_id"]
    control.bind_agents(["agent-b", "agent-c"], share_group_id="shared-team")
    groups = {item["share_group_id"]: item for item in control.list_share_groups()["groups"]}
    assert groups[personal]["group_kind"] == "personal"
    assert groups["shared-team"]["group_kind"] == "shared"


def test_projection_source_map_is_reference_only_and_uses_v2_connector(tmp_path: Path):
    ContentStore(tmp_path).upsert_source_connector(
        source_id="source-test",
        provider=PROVIDER,
        source_type="selected_directory",
        external_root_key="docs",
        workspace_id=str(tmp_path.resolve()),
    )
    scope = ProjectionReadScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id=AGENT,
        project_ref=str(tmp_path.resolve()),
        provider=PROVIDER,
        share_group_id="source-group",
        sensitivity="normal",
        policy_class="private",
    )
    result = ProjectionBuildService(tmp_path).source_map(scope=scope)
    assert result["ok"] is True
    assert result["summary"] == {"total": 1, "enabled": 1}
    assert result["entries"][0]["source_id"] == "source-test"
    assert "body" not in json.dumps(result, ensure_ascii=False).lower()


def test_missing_group_lifecycle_is_empty_but_deterministic(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    dissolved = control.dissolve("missing-group")
    preview = control.group_preview("missing-group")
    exported = control.export_group("missing-group")
    assert dissolved["changed"] is False and dissolved["unbound_count"] == 0
    assert preview["memory_count"] == 0
    assert exported["records_written"] == 0
    assert Path(exported["export_path"]).is_file()
    assert not (tmp_path / ".memoryguard" / "memory" / "memory.db").exists()


def test_export_contains_governed_v2_records_and_provenance_refs(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    control.bind_agents([AGENT, "agent-b"], share_group_id="export-group")
    _seed_atoms(tmp_path, AGENT, "export-group", [("exported", "exported body")])
    result = control.export_group("export-group")
    payload = json.loads(Path(result["export_path"]).read_text(encoding="utf-8"))
    assert payload["schema"] == "memoryguard-v2-group-export-1"
    assert payload["share_group_id"] == "export-group"
    assert payload["records"][0]["body"] == "exported body"
    assert payload["records"][0]["provenance"][0]["source_ref"].startswith("test:")


def test_clear_exports_then_empties_only_target_group(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    personal = control.ensure_personal(AGENT)["share_group_id"]
    control.bind_agents(["agent-b", "agent-c"], share_group_id="shared-team")
    _seed_atoms(tmp_path, AGENT, personal, [("remove", "remove from personal")])
    _seed_atoms(tmp_path, "agent-b", "shared-team", [("keep", "keep in shared")])
    result = control.clear_group(
        personal,
        trusted={"agent_instance_id": AGENT, "project_ref": str(tmp_path.resolve()), "provider": PROVIDER, "runtime_role": RUNTIME_ROLE},
    )
    memory = MemoryAtomStore(tmp_path, readonly=True)
    assert result["before"] == 1 and result["after"] == 0
    assert result["binding_preserved"] is True
    assert len(memory.list_atoms(scope=_read_scope(tmp_path, AGENT, personal), status="active", include_building=True)) == 0
    assert {item.body for item in memory.list_atoms(scope=_read_scope(tmp_path, "agent-b", "shared-team"))} == {"keep in shared"}


def test_archive_exports_deactivates_only_group_bindings_and_preserves_v2_memory(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    control.bind_agents([AGENT, "agent-b"], share_group_id="archive-group")
    _seed_atoms(tmp_path, AGENT, "archive-group", [("archive", "preserve on archive")])
    result = control.archive_group("archive-group")
    bindings = control.list_bindings(include_inactive=False)["bindings"]
    memory = MemoryAtomStore(tmp_path, readonly=True)
    assert result["data_preserved"] is True
    assert not any(item["share_group_id"] == "archive-group" for item in bindings)
    assert len(memory.list_atoms(scope=_read_scope(tmp_path, AGENT, "archive-group"))) == 1


def test_bind_failure_rolls_back_every_agent_in_one_v2_control_transaction(tmp_path: Path, monkeypatch):
    control = GroupControlService(tmp_path, write=True)
    original = control._bind_tx

    def fail_second(conn, **kwargs):
        if kwargs.get("agent_id") == "agent-b":
            raise OSError("injected second-agent failure")
        return original(conn, **kwargs)

    monkeypatch.setattr(control, "_bind_tx", fail_second)
    with pytest.raises(OSError):
        control.bind_agents([AGENT, "agent-b"], share_group_id="new-group")
    assert control.list_bindings(include_inactive=False)["bindings"] == []


def test_personal_namespace_owner_and_multi_member_rejected(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    personal = personal_group_id(AGENT)
    with pytest.raises(GroupControlError, match="personal_group_owner_mismatch"):
        control.bind_agent("agent-b", personal)
    with pytest.raises(GroupControlError, match="personal_group_cannot_be_shared"):
        control.bind_agents([AGENT, "agent-b"], share_group_id=personal)
    assert control.list_bindings(include_inactive=False)["bindings"] == []


def test_unbound_scope_is_fail_closed_before_personal_binding(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    personal = personal_group_id(AGENT)
    assert control.active_binding_for_agent(AGENT) is None
    assert control.scope_state(AGENT)["empty"] is True
    assert not (tmp_path / ".memoryguard" / "memory" / "memory.db").exists()


def test_v2_search_can_filter_active_records_without_deleted_history(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    control.bind_agent(AGENT, "search-group")
    _seed_atoms(tmp_path, AGENT, "search-group", [("active", "active recall"), ("deleted", "deleted recall")])
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=memory, evidence_store=evidence)
    governance.tombstone(
        "deleted",
        context=_context(tmp_path, AGENT, "search-group"),
        reason="V2 active-only search test",
        idempotency_key="delete:deleted",
    )
    scope = _read_scope(tmp_path, AGENT, "search-group")
    assert {item.body for item in memory.list_atoms(scope=scope, status="active")} == {"active recall"}
    assert {item.body for item in memory.list_atoms(scope=scope, status="deleted", include_building=True)} == {"deleted recall"}


def test_mutation_context_cannot_write_another_group(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=memory, evidence_store=evidence)
    wrong_context = _context(tmp_path, AGENT, "bound-group")
    atom = MemoryAtom(
        memory_id="outside",
        body="must not cross group scope",
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id=AGENT,
        share_group_id="other-group",
        project_ref=str(tmp_path.resolve()),
        provider=PROVIDER,
        runtime_role=RUNTIME_ROLE,
    )
    with pytest.raises(V2ScopeError, match="outside context"):
        governance.put_atom(
            atom,
            context=wrong_context,
            evidence=[{"source_ref": "scope:test", "revision": "1", "digest": "scope", "authority": "governance", "metadata": {}}],
            reason="scope isolation test",
        )


def test_multiple_active_bindings_are_rejected_fail_closed(tmp_path: Path, monkeypatch):
    control = GroupControlService(tmp_path, write=True)
    rows = [
        {"binding_id": "one", "agent_instance_id": AGENT, "share_group_id": "one", "status": "active"},
        {"binding_id": "two", "agent_instance_id": AGENT, "share_group_id": "two", "status": "active"},
    ]
    monkeypatch.setattr(control, "_read_bindings", lambda **_: rows)
    with pytest.raises(GroupControlError, match="multiple_active_bindings"):
        control.active_binding_for_agent(AGENT)


def test_selection_and_scope_state_are_persisted_by_group_control(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    control.bind_agents([AGENT, "agent-b"], share_group_id="selection-group")
    scope = control.set_scope(AGENT, {"mode": "share_group", "share_group_id": "selection-group"})
    selected = control.record_selection(AGENT, ["source-b", "source-a"], "digest-1")
    assert scope["scope"]["share_group_id"] == "selection-group"
    assert control.scope_state(AGENT)["scope"]["mode"] == "share_group"
    assert selected["source_ids"] == ["source-a", "source-b"]
    assert control.selected_source_ids(AGENT) == ["source-a", "source-b"]


def test_global_status_reports_real_cross_group_duplicate_candidates(tmp_path: Path):
    control = GroupControlService(tmp_path, write=True)
    control.bind_agents([AGENT, "agent-b"], share_group_id="group-a")
    control.bind_agents(["agent-c", "agent-d"], share_group_id="group-b")
    _seed_atoms(tmp_path, AGENT, "group-a", [("same-a", "same governed body")])
    _seed_atoms(tmp_path, "agent-c", "group-b", [("same-b", "same governed body")])
    result = control.get_global_memory_status()
    exact = [item for item in result["cross_group_duplicates"] if item["match_type"] == "exact"]
    assert exact
    assert exact[0]["share_group_ids"] == ["group-a", "group-b"]
    assert exact[0]["record_count"] == 2


def test_redirect_install_requires_an_active_v2_group_binding(tmp_path: Path):
    with pytest.raises(GroupControlError, match="group_has_no_active_bindings"):
        GroupControlService(tmp_path, write=True).install_redirects("empty-group")


def test_native_file_unchanged_across_binding_switch(tmp_path: Path):
    native = tmp_path / "user_profile.md"
    native.write_bytes(b"native bytes\r\n")
    before = native.read_bytes()
    control = GroupControlService(tmp_path, write=True)
    control.bind_agent(AGENT, "shared-team")
    control.bind_agent("agent-b", "shared-team")
    control.leave_to_personal(AGENT)
    assert native.read_bytes() == before
