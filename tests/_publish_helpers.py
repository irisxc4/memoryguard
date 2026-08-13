"""Shared V2 fixtures for projection and release tests.

These helpers deliberately seed the canonical MemoryAtom/Evidence planes and
exercise the same scope objects used by the native runtime.  No legacy IR or
file-backed publication adapter belongs in a V2 test fixture.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from memoryguard.access_context import AccessContext
from memoryguard.content import ContentStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.projection_v2 import ProjectionReadScope
from memoryguard.runtime_v2.native_ports import bind_native_transport_context
from memoryguard.runtime_v2.projection_build import (
    ProjectionBuildError,
    ProjectionBuildService,
    V2ReleaseService,
)


DEFAULT_AGENT = "agent-test"
DEFAULT_GROUP = "group-test"
DEFAULT_PROVIDER = "test"
DEFAULT_RUNTIME_ROLE = "test"


def native_context(
    workspace: Path,
    *,
    agent_id: str = DEFAULT_AGENT,
    share_group_id: str = DEFAULT_GROUP,
    provider: str = DEFAULT_PROVIDER,
    runtime_role: str = DEFAULT_RUNTIME_ROLE,
    admin: bool = True,
):
    """Return a process-issued context accepted by native V2 services."""

    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent_id,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"fixture-{agent_id}",
            session_source="test",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id=share_group_id,
        project_ref=str(workspace.resolve()),
        provider=provider,
        runtime_role=runtime_role,
        entrypoint="test",
        sensitivity="normal",
        policy_class="private",
    )


def mutation_context(
    workspace: Path,
    *,
    agent_id: str = DEFAULT_AGENT,
    share_group_id: str = DEFAULT_GROUP,
    provider: str = DEFAULT_PROVIDER,
    runtime_role: str = DEFAULT_RUNTIME_ROLE,
    admin: bool = True,
) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(workspace.resolve()),
        share_group_id=share_group_id,
        agent_instance_id=agent_id,
        project_ref=str(workspace.resolve()),
        provider=provider,
        runtime_role=runtime_role,
        actor=agent_id,
        admin=admin,
        authority="admin" if admin else "manual",
    )


def projection_scope(
    workspace: Path,
    *,
    agent_id: str = DEFAULT_AGENT,
    share_group_id: str = DEFAULT_GROUP,
    provider: str = DEFAULT_PROVIDER,
    sensitivity: str = "normal",
    policy_class: str = "private",
) -> ProjectionReadScope:
    return ProjectionReadScope(
        workspace_id=str(workspace.resolve()),
        agent_instance_id=agent_id,
        project_ref=str(workspace.resolve()),
        provider=provider,
        share_group_id=share_group_id,
        sensitivity=sensitivity,
        policy_class=policy_class,
    )


def seed_atom(
    workspace: Path,
    memory_id: str,
    body: str,
    *,
    kind: str = "fact",
    confidence: float = 0.8,
    agent_id: str = DEFAULT_AGENT,
    share_group_id: str = DEFAULT_GROUP,
    provider: str = DEFAULT_PROVIDER,
    runtime_role: str = DEFAULT_RUNTIME_ROLE,
    metadata: Mapping[str, Any] | None = None,
    provenance: Sequence[Mapping[str, Any]] | None = None,
    status: str = "active",
) -> MemoryAtom:
    """Write one governed atom and its evidence, then make it readable."""

    workspace.mkdir(parents=True, exist_ok=True)
    memory = MemoryAtomStore(workspace, readonly=False)
    governance = GovernanceV2(workspace, memory_store=memory)
    ctx = mutation_context(
        workspace,
        agent_id=agent_id,
        share_group_id=share_group_id,
        provider=provider,
        runtime_role=runtime_role,
    )
    digest = hashlib.sha256(f"fixture:{memory_id}:{body}".encode("utf-8")).hexdigest()
    evidence, _ = governance.put_evidence(
        context=ctx,
        reason="projection fixture evidence",
        source_ref=f"fixture:{memory_id}",
        digest=digest,
        authority="governance",
        evidence_type="reference",
    )
    atom = MemoryAtom(
        memory_id=memory_id,
        body=body,
        kind=kind,
        status=status,
        confidence=confidence,
        workspace_id=str(workspace.resolve()),
        agent_instance_id=agent_id,
        share_group_id=share_group_id,
        project_ref=str(workspace.resolve()),
        provider=provider,
        runtime_role=runtime_role,
        provenance=list(provenance or []),
        metadata=dict(metadata or {}),
    )
    mappings = [{
        "source_domain": "fixture",
        "source_ref": f"fixture:{memory_id}",
        "source_record_id": memory_id,
        "source_revision": "1",
        "digest": digest,
    }]
    persisted, _ = governance.put_atom(
        atom,
        context=ctx,
        evidence=[evidence.to_dict()],
        source_mappings=mappings,
        reason="projection fixture atom",
        idempotency_key=f"fixture:{memory_id}",
    )
    memory.project_evidence(governance.evidence)
    memory.set_visibility("active", atom_ids=[persisted.atom_id])
    return memory.get_atom(
        persisted.memory_id,
        scope={
            "workspace_id": str(workspace.resolve()),
            "share_group_id": share_group_id,
            "agent_instance_id": agent_id,
            "project_ref": str(workspace.resolve()),
            "provider": provider,
            "runtime_role": runtime_role,
        },
        include_building=True,
    ) or persisted


def seed_atoms(workspace: Path, atoms: Sequence[Mapping[str, Any]]) -> list[MemoryAtom]:
    return [
        seed_atom(
            workspace,
            str(item["memory_id"]),
            str(item.get("body") or ""),
            kind=str(item.get("kind") or "fact"),
            confidence=float(item.get("confidence", 0.8)),
            agent_id=str(item.get("agent_id") or DEFAULT_AGENT),
            share_group_id=str(item.get("share_group_id") or DEFAULT_GROUP),
            provider=str(item.get("provider") or DEFAULT_PROVIDER),
            runtime_role=str(item.get("runtime_role") or DEFAULT_RUNTIME_ROLE),
            metadata=item.get("metadata"),
            provenance=item.get("provenance"),
            status=str(item.get("status") or "active"),
        )
        for item in atoms
    ]


def register_publish_target(
    workspace: Path,
    target: Path,
    *,
    source_id: str = "publish-target",
    source_type: str | None = None,
) -> str:
    """Register a V2 content connector for source-map/list-target assertions."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if source_type is None:
        source_type = "selected_directory" if target.exists() and target.is_dir() else "selected_file"
    if source_type == "selected_directory":
        target.mkdir(parents=True, exist_ok=True)
    elif not target.exists():
        target.write_text("# Memory\n\n", encoding="utf-8")
    ContentStore(workspace).upsert_source_connector(
        source_id=source_id,
        provider="fixture",
        source_type=source_type,
        external_root_key=str(target.resolve()),
        workspace_id=str(workspace.resolve()),
        enabled=True,
    )
    return source_id


def build_projection(
    workspace: Path,
    *,
    mode: str = "reconstructed",
    scope: ProjectionReadScope | None = None,
    runtime_role: str = DEFAULT_RUNTIME_ROLE,
) -> dict[str, Any]:
    checked_scope = scope or projection_scope(workspace)
    return ProjectionBuildService(workspace).build(
        mode=mode,
        scope=checked_scope,
        runtime_role=runtime_role,
    )


def publish(
    workspace: Path,
    target: Path,
    *,
    scope: ProjectionReadScope | None = None,
    mode: str = "reconstructed",
    runtime_role: str = DEFAULT_RUNTIME_ROLE,
) -> dict[str, Any]:
    """Build and publish through the immutable V2 release service."""

    checked_scope = scope or projection_scope(workspace)
    projections = ProjectionBuildService(workspace)
    built = projections.current(mode=mode, scope=checked_scope)
    if built.get("projection") is None:
        try:
            built = projections.build(
                mode=mode,
                scope=checked_scope,
                runtime_role=runtime_role,
            )
        except ProjectionBuildError as exc:
            if exc.code == "no_projection_sources":
                return {"ok": False, "status": "NO_SOURCE", "error": "projection_required"}
            raise
    if built.get("projection") is None:
        return {"ok": False, "status": "NO_SOURCE", "error": "projection_required"}
    release = V2ReleaseService(workspace)
    plan = release.create_plan(
        str(target.resolve()),
        scope=checked_scope,
        mode=mode,
        runtime_role=runtime_role,
    )
    return release.apply(
        str(plan["plan_id"]),
        str(target.resolve()),
        scope=checked_scope,
        confirmed=True,
        runtime_role=runtime_role,
    )
