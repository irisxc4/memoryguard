"""MemoryGuard v3.1 acceptance over the formal V2 public contracts.

The acceptance path is intentionally the same path used by the V2 GUI:
GroupControlService/SourceControlService -> NativeExtractionEnrichmentService
-> MemoryAtomStore -> ProjectionBuildService/ProjectionStore.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryguard.access_context import AccessContext
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.projection_v2 import ProjectionReadScope, ProjectionStore
from memoryguard.runtime_v2.extraction_native import NativeExtractionEnrichmentService
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import bind_native_transport_context
from memoryguard.runtime_v2.projection_build import ProjectionBuildService
from memoryguard.runtime_v2.source_control import SourceControlService


AGENT = "accept-v3-1-agent"
PROVIDER = "accept-v3-1"
RUNTIME_ROLE = "acceptance"


def _native_context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=AGENT,
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="accept-v3-1-session",
            session_source="acceptance",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="",
        project_ref=str(workspace.resolve()),
        provider=PROVIDER,
        runtime_role=RUNTIME_ROLE,
    )


def _source_context() -> dict[str, object]:
    return {"admin": True, "agent_instance_id": AGENT}


def _projection_scope(workspace: Path, group: str) -> ProjectionReadScope:
    return ProjectionReadScope(
        workspace_id=str(workspace.resolve()),
        agent_instance_id=AGENT,
        project_ref=str(workspace.resolve()),
        provider=PROVIDER,
        share_group_id=group,
        sensitivity="normal",
        policy_class="private",
    )


def _check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        suffix = f" :: {detail}" if detail else ""
        print(f"[PASS] {label}{suffix}")
        return True
    suffix = f" :: {detail}" if detail else ""
    print(f"[ERROR] {label}{suffix}")
    return False


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="memoryguard-v3-1-v2-"))
    passed = True
    try:
        docs = workspace / "docs"
        docs.mkdir()
        (docs / "notes.md").write_text(
            "# Project preferences\n\nUse explicit V2 contracts.\n\n"
            "## Migration procedure\nRun focused tests before release.\n",
            encoding="utf-8",
        )
        (docs / "review.md").write_text(
            "# Review rules\n\nKeep scope checks and evidence links.\n",
            encoding="utf-8",
        )

        groups = GroupControlService(workspace, write=True)
        binding = groups.bind_agent(AGENT, "accept-v3-1-group")
        group = str(binding["share_group_id"])
        native_context = bind_native_transport_context(
            AccessContext(
                trusted_agent_id=AGENT,
                is_admin=False,
                strict_binding=True,
                allow_anon=False,
                session_id="accept-v3-1-session",
                session_source="acceptance",
                session_trusted=True,
            ),
            workspace_id=str(workspace.resolve()),
            share_group_id=group,
            project_ref=str(workspace.resolve()),
            provider=PROVIDER,
            runtime_role=RUNTIME_ROLE,
        )

        # 1. Group binding is explicit and the acceptance scope is stable.
        passed &= _check(
            "V2 group binding",
            binding["binding"]["status"] == "active" and group == "accept-v3-1-group",
            f"agent={AGENT}, group={group}",
        )

        # 2–3. Source inventory and authorization coverage are reference-only.
        sources = SourceControlService(workspace)
        added = sources.add(
            str(docs),
            "selected_directory",
            _source_context(),
            display_name="v3.1 V2 acceptance documents",
        )
        scan = sources.scan_summary(_source_context())
        raw = sources.raw_summary(_source_context())
        passed &= _check(
            "SourceControl coverage",
            scan["status"] == "READY"
            and scan["coverage"]["unaccounted_count"] == 0
            and raw["coverage"]["unaccounted_count"] == 0,
            f"source={added['source_id']}, candidates={scan['coverage']['candidate_count']}",
        )
        passed &= _check(
            "SourceControl selected objects",
            raw["coverage"]["candidate_count"] == 2
            and all(item.get("authorized") for group_item in raw["groups"] for item in group_item["files"]),
            f"objects={raw['coverage']['candidate_count']}",
        )

        # 4. Native extraction is staged and accepted through one formal batch.
        native = NativeExtractionEnrichmentService(workspace)
        batches: list[dict[str, object]] = []
        for source_group in raw["groups"]:
            for item in source_group["files"]:
                preview = native.extract(
                    {"source_id": source_group["root_id"], "relative_path": item["relative_path"]},
                    context=native_context,
                )
                if preview.get("candidates"):
                    batches.append({
                        "extract_id": preview["extract_id"],
                        "candidate_ids": [candidate["candidate_id"] for candidate in preview["candidates"]],
                    })
        accepted = native.accept_batch({"batches": batches}, context=native_context)
        memory = MemoryAtomStore(workspace, readonly=True)
        memory_scope = MemoryReadScope(
            workspace_id=str(workspace.resolve()),
            share_group_id=group,
            agent_instance_id=AGENT,
            project_ref=str(workspace.resolve()),
            provider=PROVIDER,
            runtime_role=RUNTIME_ROLE,
        )
        atoms = memory.list_atoms(scope=memory_scope, status="active")
        passed &= _check(
            "NativeExtraction batch acceptance",
            accepted["total"] == len(atoms) and len(atoms) > 0,
            f"batches={len(batches)}, accepted={accepted['total']}, atoms={len(atoms)}",
        )
        passed &= _check(
            "V2 atom metadata is body-free",
            all("body" not in atom.metadata and "content" not in atom.metadata for atom in atoms),
            f"metadata_atoms={len(atoms)}",
        )

        # 5–7. Build and reread the official reference projection.
        projection_scope = _projection_scope(workspace, group)
        projections = ProjectionBuildService(workspace)
        first = projections.build(mode="reconstructed", scope=projection_scope, runtime_role=RUNTIME_ROLE)
        key = str((first.get("projection") or {}).get("key") or "")
        record = ProjectionStore(workspace, initialize=False).get_projection(
            "scenario", key, scope=projection_scope,
        ) if key else None
        payload = record.payload if record is not None else {}
        metadata = payload.get("metadata") if isinstance(payload, dict) else {}
        graph = metadata.get("derived_graph") if isinstance(metadata, dict) else {}
        body_free = not any(
            token in str(payload).lower() for token in ("raw_content", "full_text", "source_body")
        )
        passed &= _check(
            "ProjectionBuildService commit",
            first.get("status") == "succeeded" and record is not None and bool(graph.get("nodes")),
            f"projection={record.projection_id if record else 'none'}, nodes={len(graph.get('nodes', [])) if isinstance(graph, dict) else 0}",
        )
        passed &= _check("ProjectionStore reference-only payload", body_free)
        second = projections.build(mode="reconstructed", scope=projection_scope, runtime_role=RUNTIME_ROLE)
        passed &= _check(
            "Projection idempotency",
            (second.get("projection") or {}).get("projection_id") == (first.get("projection") or {}).get("projection_id"),
        )

        # 8. Source map and group status retain the external-source boundary.
        source_map = projections.source_map(scope=projection_scope)
        group_status = groups.get_global_memory_status()
        source_summary = source_map["summary"]
        passed &= _check(
            "V2 source map and group status",
            source_summary["selected_source_connectors"] == 1
            and source_summary["selected_source_connector_total"] == 1
            and source_summary["governed_memory"] == len(atoms)
            and source_summary["buildable_atom_count"] == len(atoms)
            and source_summary["enabled"] == 1 + len(atoms)
            and any(item["share_group_id"] == group and item["record_count"] == len(atoms) for item in group_status["groups"]),
            (
                f"connectors={source_summary['selected_source_connectors']}, "
                f"governed={source_summary['governed_memory']}, "
                f"buildable={source_summary['buildable_atom_count']}, "
                f"groups={group_status['total_groups']}"
            ),
        )

        # 9. Projection deletion is precise and idempotent; group export remains
        # the explicit rollback artifact rather than an implicit filesystem copy.
        exported = groups.export_group(group)
        first_delete = projections.delete(mode="reconstructed", scope=projection_scope)
        second_delete = projections.delete(mode="reconstructed", scope=projection_scope)
        passed &= _check(
            "V2 export and precise projection delete",
            exported["records_written"] == len(atoms)
            and first_delete["deleted"] is True
            and second_delete["deleted"] is False,
            f"export_records={exported['records_written']}, tombstone={first_delete['tombstone_id']}",
        )
        return 0 if passed else 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
