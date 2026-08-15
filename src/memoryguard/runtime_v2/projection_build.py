"""Native V2 projection build/query service for GUI operations.

The service reads governed Memory/Evidence references under an exact trusted
scope and writes immutable ProjectionStore generations.  It never imports the
legacy SourceRegistry/MemoryIR/ManagedStore/ProjectionBuilder stack and never
persists source or memory bodies in projection metadata.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from ..content.store import ContentReadScope, ContentStore
from ..evidence.store import EvidenceReadScope, EvidenceStore
from ..memory.store import MemoryAtomStore, MemoryReadScope
from ..projection_v2.projector import ProfileProjector, ScenarioProjector
from ..projection_v2.store import ProjectionReadScope, ProjectionRecord, ProjectionStore
from ..storage.layout import WorkspaceV2Layout
from .task_coordinator import TaskCancelled, TaskExecution


class ProjectionBuildError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "projection_build_failed")
        super().__init__(self.code)


def _digest(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(item) for item in parts).encode("utf-8")).hexdigest()


_DERIVED_SCOPE_LABELS = {
    "project": "项目来源",
    "user": "用户来源",
    "shared": "共享来源",
    "agent": "Agent来源",
}
_DERIVED_KIND_LABELS = {
    "fact": "事实",
    "preference": "偏好",
    "procedure": "流程",
    "project": "项目",
    "episode": "事件",
    "correction": "纠错",
}
_MAX_DERIVED_GRAPH_BYTES = 8 * 1024
_COMPACT_PROJECTION_PAYLOAD_BYTES = 60 * 1024


def _derived_atom_metadata(atom: Any) -> Mapping[str, Any]:
    metadata = getattr(atom, "metadata", {})
    return metadata if isinstance(metadata, Mapping) else {}


def _derived_source(atom: Any) -> tuple[str, str]:
    metadata = _derived_atom_metadata(atom)
    source_key = str(metadata.get("source_key") or "").strip()
    locator = str(metadata.get("source_locator") or "").strip()
    for item in getattr(atom, "provenance", ()) or ():
        if not isinstance(item, Mapping):
            continue
        source_key = source_key or str(
            item.get("source_object_id") or item.get("source_ref") or ""
        ).strip()
        locator = locator or str(item.get("locator") or "").strip()
        if source_key and locator:
            break
    return source_key, locator


def _derive_reference_graph(atoms: list[Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Derive a bounded, reference-only graph for the V2 projection payload.

    This keeps the old projection's useful topology (scope/topic/source hub/
    claim anchor and related edges) without carrying MemoryAtom bodies or ACL
    fields into ProjectionStore.
    """
    root_id = "main"
    nodes: list[dict[str, Any]] = [{
        "id": root_id,
        "node_kind": "root",
        "label": "记忆胞体",
        "parent_id": "",
        "derivation": "记忆胞体",
    }]
    edges: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[Any]] = {}
    atom_claims: dict[str, str] = {}
    atom_memory_ids: dict[str, str] = {}
    source_keys: dict[str, tuple[str, str]] = {}
    for atom in sorted(atoms, key=lambda item: str(getattr(item, "atom_id", ""))):
        metadata = _derived_atom_metadata(atom)
        scope_name = str(metadata.get("scope") or "project").strip().casefold() or "project"
        kind_name = str(getattr(atom, "kind", "fact") or "fact").strip().casefold() or "fact"
        groups.setdefault((scope_name, kind_name), []).append(atom)
        atom_id = str(getattr(atom, "atom_id", "") or "")
        memory_id = str(getattr(atom, "memory_id", "") or atom_id)
        claim_id = "claim-" + _digest(atom_id, memory_id)[:16]
        atom_claims[atom_id] = claim_id
        atom_memory_ids[memory_id] = claim_id
        source_keys[atom_id] = _derived_source(atom)

    source_hub_count = 0
    claim_anchor_count = 0
    for (scope_name, kind_name), members in sorted(groups.items()):
        scope_id = "scope-" + _digest(scope_name)[:16]
        scope_label = _DERIVED_SCOPE_LABELS.get(scope_name, f"{scope_name}来源")
        if not any(node["id"] == scope_id for node in nodes):
            nodes.append({
                "id": scope_id,
                "node_kind": "topic",
                "label": scope_label,
                "kind": scope_name,
                "parent_id": root_id,
                "derivation": f"记忆胞体 -> {scope_label}",
            })
            edges.append({"source": root_id, "target": scope_id, "edge_type": "derived_from"})
        kind_id = "kind-" + _digest(scope_name, kind_name)[:16]
        kind_label = _DERIVED_KIND_LABELS.get(kind_name, kind_name)
        nodes.append({
            "id": kind_id,
            "node_kind": "topic",
            "label": kind_label,
            "kind": kind_name,
            "parent_id": scope_id,
            "derivation": f"记忆胞体 -> {scope_label} -> {kind_label}",
        })
        edges.append({"source": scope_id, "target": kind_id, "edge_type": "derived_from"})

        by_source: dict[str, list[Any]] = {}
        for atom in members:
            source_key, _ = source_keys[str(getattr(atom, "atom_id", "") or "")]
            if source_key:
                by_source.setdefault(source_key, []).append(atom)
        hubs: dict[str, str] = {}
        for source_key, source_members in sorted(by_source.items()):
            if len(source_members) < 2:
                continue
            hub_id = "hub-" + _digest(scope_name, kind_name, source_key)[:16]
            hubs[source_key] = hub_id
            source_hub_count += 1
            member_ids = [
                atom_claims[str(getattr(atom, "atom_id", "") or "")]
                for atom in sorted(source_members, key=lambda item: str(getattr(item, "atom_id", "")))
            ]
            locator = source_keys[str(getattr(source_members[0], "atom_id", "") or "")][1]
            nodes.append({
                "id": hub_id,
                "node_kind": "source_hub",
                "label": source_key,
                "parent_id": kind_id,
                "source_key": source_key,
                "source_locator": locator,
                "member_ids": member_ids,
                "derivation": f"记忆胞体 -> {scope_label} -> {kind_label} -> 同源突触",
            })
            edges.append({"source": kind_id, "target": hub_id, "edge_type": "derived_from"})

        for atom in sorted(members, key=lambda item: str(getattr(item, "atom_id", ""))):
            atom_id = str(getattr(atom, "atom_id", "") or "")
            claim_id = atom_claims[atom_id]
            source_key, locator = source_keys[atom_id]
            parent_id = hubs.get(source_key, kind_id)
            metadata = _derived_atom_metadata(atom)
            title = str(metadata.get("title") or getattr(atom, "memory_id", "") or atom_id)
            nodes.append({
                "id": claim_id,
                "node_kind": "claim_anchor",
                "label": title[:256],
                "parent_id": parent_id,
                "atom_id": atom_id,
                "memory_id": str(getattr(atom, "memory_id", "") or ""),
                "kind": kind_name,
                "source_key": source_key,
                "source_locator": locator,
                "provenance_count": len(getattr(atom, "provenance", ()) or ()),
                "confidence": float(getattr(atom, "confidence", 0.0) or 0.0),
                "derivation": f"记忆胞体 -> {scope_label} -> {kind_label} -> 末梢",
            })
            claim_anchor_count += 1
            edges.append({"source": parent_id, "target": claim_id, "edge_type": "derived_from"})

    related_pairs: set[tuple[str, str]] = set()
    duplicate_groups: dict[str, list[str]] = {}
    keep_all_groups: set[str] = set()
    for atom in atoms:
        metadata = _derived_atom_metadata(atom)
        group_id = str(metadata.get("duplicate_group") or "").strip()
        if group_id:
            duplicate_groups.setdefault(group_id, []).append(str(getattr(atom, "memory_id", "") or ""))
            if str(metadata.get("duplicate_decision") or "").strip().casefold() == "keep_all":
                keep_all_groups.add(group_id)
        related_ids = metadata.get("related_memory_ids")
        if isinstance(related_ids, (list, tuple, set)):
            source_memory_id = str(getattr(atom, "memory_id", "") or "")
            for related_id in related_ids:
                target_memory_id = str(related_id or "")
                if target_memory_id and target_memory_id in atom_memory_ids:
                    related_pairs.add(tuple(sorted((source_memory_id, target_memory_id))))
    for group_id in keep_all_groups:
        members = sorted(set(duplicate_groups.get(group_id, ())))
        for index, source_memory_id in enumerate(members):
            for target_memory_id in members[index + 1:]:
                related_pairs.add((source_memory_id, target_memory_id))
    for source_memory_id, target_memory_id in sorted(related_pairs):
        source_claim = atom_memory_ids.get(source_memory_id)
        target_claim = atom_memory_ids.get(target_memory_id)
        if source_claim and target_claim:
            edges.append({
                "source": source_claim,
                "target": target_claim,
                "edge_type": "related",
                "reason": "keep_all_or_explicit_relation",
            })
    related_edge_count = sum(1 for edge in edges if edge.get("edge_type") == "related")
    graph = {
        "root_id": root_id,
        "nodes": nodes,
        "edges": edges,
    }
    graph_truncated = False
    omitted_claim_count = 0
    encoded_graph = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded_graph.encode("utf-8")) > _MAX_DERIVED_GRAPH_BYTES:
        # ProjectionStore has a hard 64 KiB payload limit.  Keep the complete
        # atom/evidence reference arrays in that payload, while making the UI
        # graph a deterministic bounded view for large memory sets.  Claim
        # labels and memory IDs remain sufficient for safe hydration; bodies,
        # ACLs, and source text never enter this compact form.
        structural = [
            node for node in nodes
            if node.get("node_kind") in {"root", "topic"}
        ]
        structural_ids = {str(node.get("id") or "") for node in structural}
        compact_edges = [
            edge for edge in edges
            if str(edge.get("source") or "") in structural_ids
            and str(edge.get("target") or "") in structural_ids
        ]
        compact_nodes = list(structural)
        selected_ids = set(structural_ids)
        claim_nodes = [node for node in nodes if node.get("node_kind") == "claim_anchor"]
        for node in claim_nodes:
            compact = {
                "id": str(node.get("id") or ""),
                "node_kind": "claim_anchor",
                "label": str(node.get("label") or "")[:256],
                "parent_id": str(node.get("parent_id") or ""),
                "atom_id": str(node.get("atom_id") or ""),
                "memory_id": str(node.get("memory_id") or ""),
            }
            candidate_ids = selected_ids | {compact["id"]}
            candidate_edges = compact_edges + [
                edge for edge in edges
                if str(edge.get("source") or "") in candidate_ids
                and str(edge.get("target") or "") in candidate_ids
                and edge not in compact_edges
            ]
            candidate_graph = {
                "root_id": root_id,
                "nodes": compact_nodes + [compact],
                "edges": candidate_edges,
                "truncated": True,
            }
            if len(json.dumps(candidate_graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) > _MAX_DERIVED_GRAPH_BYTES:
                break
            compact_nodes.append(compact)
            compact_edges = candidate_edges
            selected_ids.add(compact["id"])
        omitted_claim_count = max(0, len(claim_nodes) - sum(1 for node in compact_nodes if node.get("node_kind") == "claim_anchor"))
        graph = {
            "root_id": root_id,
            "nodes": compact_nodes,
            "edges": compact_edges,
            "truncated": True,
        }
        graph_truncated = True
    stats = {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "claim_anchor_count": claim_anchor_count,
        "source_hub_count": source_hub_count,
        "related_edge_count": related_edge_count,
        "graph_truncated": graph_truncated,
        "graph_node_count": len(graph["nodes"]),
        "graph_edge_count": len(graph["edges"]),
        "graph_claim_anchor_count": sum(1 for node in graph["nodes"] if node.get("node_kind") == "claim_anchor"),
        "graph_omitted_claim_count": omitted_claim_count,
    }
    return graph, stats


def projection_scope_from_context(
    workspace: str | Path,
    context: Mapping[str, Any],
) -> ProjectionReadScope:
    """Build an exact projection ACL from a process-issued context projection."""
    root = str(Path(workspace).expanduser().resolve())
    workspace_id = str(context.get("workspace_id") or "")
    if workspace_id and str(Path(workspace_id).expanduser().resolve()) != root:
        raise ProjectionBuildError("projection_scope_workspace_mismatch")
    values = {
        "workspace_id": root,
        "agent_instance_id": str(context.get("agent_instance_id") or ""),
        "project_ref": str(context.get("project_ref") or ""),
        "provider": str(context.get("provider") or ""),
        "share_group_id": str(context.get("share_group_id") or ""),
        "sensitivity": str(context.get("sensitivity") or ""),
        "policy_class": str(context.get("policy_class") or ""),
    }
    if not all(values.values()):
        raise ProjectionBuildError("projection_scope_required")
    try:
        return ProjectionReadScope(**values)
    except (TypeError, ValueError) as exc:
        raise ProjectionBuildError("projection_scope_invalid") from exc


class ProjectionBuildService:
    """V2-only projection orchestration with cancellation compensation."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.content = ContentStore(self.workspace, initialize=False)

    def _memory(self) -> MemoryAtomStore:
        try:
            return MemoryAtomStore(self.workspace, readonly=True)
        except FileNotFoundError as exc:
            raise ProjectionBuildError("memory_db_missing") from exc

    def _evidence(self) -> EvidenceStore:
        try:
            return EvidenceStore(self.workspace, readonly=True)
        except FileNotFoundError as exc:
            raise ProjectionBuildError("evidence_db_missing") from exc

    def _projection(self, *, write: bool) -> ProjectionStore:
        return ProjectionStore(self.workspace, initialize=bool(write))

    @staticmethod
    def _mode(value: Any) -> str:
        mode = str(value or "reconstructed").strip().casefold()
        if mode not in {"native", "reconstructed", "scenario", "profile"}:
            raise ProjectionBuildError("projection_mode_invalid")
        return mode

    @staticmethod
    def _kind(mode: str) -> str:
        # Keep the two existing GUI concepts while storing them in the V2
        # scenario/profile domains.  Reconstructed governance is scenario-like;
        # native memory is a profile of the currently governed memory set.
        return "profile" if mode in {"native", "profile"} else "scenario"

    @staticmethod
    def _scope_key(mode: str, scope: ProjectionReadScope) -> str:
        return "gui-" + _digest(mode, *scope.as_tuple())

    @staticmethod
    def _memory_scope(scope: ProjectionReadScope, *, runtime_role: str = "") -> MemoryReadScope:
        # A blank Agent in a validated projection scope is the explicit
        # server-admin/shared-group view.  MemoryAtomStore uses its separate
        # ``admin`` flag to represent that group-wide read; leaving it false
        # would apply the writer-agent fallback and hide every member atom.
        return MemoryReadScope(
            workspace_id=scope.workspace_id,
            share_group_id=scope.share_group_id,
            agent_instance_id=scope.agent_instance_id,
            project_ref=scope.project_ref,
            provider=scope.provider,
            runtime_role=str(runtime_role or ""),
            admin=not bool(str(scope.agent_instance_id or "").strip()),
        )

    def _filter_enabled_content_sources(
        self,
        memory: MemoryAtomStore,
        atoms: list[Any],
        scope: ProjectionReadScope,
    ) -> list[Any]:
        """Drop atoms whose content provenance is backed only by disabled sources.

        Manual/native atoms have no ``source_domain=content`` mapping and remain
        eligible. Content-backed atoms are resolved through reference-only
        occurrence metadata under the exact projection scope; source bodies are
        never read here. An unresolved content mapping fails closed.
        """
        layout = WorkspaceV2Layout(self.workspace)
        if not layout.content_db.is_file():
            return atoms
        connectors = self.content.list_source_connectors(workspace_id=str(self.workspace))
        if not connectors:
            return atoms
        enabled_ids = {
            str(row.get("source_id") or "")
            for row in connectors
            if bool(row.get("enabled")) and str(row.get("source_id") or "")
        }
        all_ids = {
            str(row.get("source_id") or "")
            for row in connectors
            if str(row.get("source_id") or "")
        }
        if enabled_ids == all_ids:
            return atoms

        filtered: list[Any] = []
        with self.content.connection() as conn:
            for atom in atoms:
                mappings = [
                    row for row in memory.list_source_mappings(atom_id=atom.atom_id)
                    if str(row.get("source_domain") or "").casefold() == "content"
                ]
                if not mappings:
                    filtered.append(atom)
                    continue
                source_ids: set[str] = set()
                for mapping in mappings:
                    occurrence_id = str(mapping.get("source_record_id") or "").strip()
                    params: list[Any]
                    if occurrence_id:
                        predicate = "o.occurrence_id=?"
                        params = [occurrence_id]
                    else:
                        source_ref = str(mapping.get("source_ref") or "").strip()
                        if not source_ref.startswith("content:"):
                            continue
                        blob_id = source_ref.split(":", 1)[1].strip()
                        if not blob_id:
                            continue
                        predicate = "o.blob_id=?"
                        params = [blob_id]
                    rows = conn.execute(
                        "SELECT DISTINCT so.source_id FROM content_occurrences o "
                        "JOIN source_objects so ON so.source_object_id=o.source_object_id "
                        f"WHERE {predicate} AND o.active=1 AND o.workspace_id=? "
                        "AND o.agent_instance_id=? AND o.project_ref=? AND o.provider=? "
                        "AND o.share_group_id=? AND o.sensitivity=? AND o.policy_class=?",
                        (*params, scope.workspace_id, scope.agent_instance_id, scope.project_ref,
                         scope.provider, scope.share_group_id, scope.sensitivity, scope.policy_class),
                    ).fetchall()
                    source_ids.update(str(row[0] or "") for row in rows if str(row[0] or ""))
                if source_ids & enabled_ids:
                    filtered.append(atom)
        return filtered

    def _scoped_atoms(
        self,
        memory: MemoryAtomStore,
        scope: ProjectionReadScope,
        *,
        runtime_role: str = "",
    ) -> list[Any]:
        atoms = memory.list_atoms(
            scope=self._memory_scope(scope, runtime_role=runtime_role),
            status="active",
        )
        return self._filter_enabled_content_sources(memory, list(atoms), scope)

    @staticmethod
    def _record(record: ProjectionRecord) -> dict[str, Any]:
        return {
            "projection_id": record.projection_id,
            "kind": record.kind,
            "key": record.key,
            "generation": record.generation,
            "source_digest": record.source_digest,
            "projection_digest": record.projection_digest,
            "status": record.status,
            "evidence_count": len(record.evidence_links),
        }

    def build(
        self,
        *,
        mode: str,
        scope: ProjectionReadScope,
        runtime_role: str = "",
        llm_provider: str = "",
        llm_used: bool = False,
        llm_engine: str = "",
        execution: TaskExecution | None = None,
    ) -> dict[str, Any]:
        checked_mode = self._mode(mode)
        kind = self._kind(checked_mode)
        key = self._scope_key(checked_mode, scope)
        if execution is not None:
            execution.progress(5, "scan")
            execution.check_cancelled()

        try:
            memory = self._memory()
        except ProjectionBuildError as exc:
            # A build with no V2 memory plane has no eligible input.  Keep the
            # public failure semantic about inputs rather than leaking a
            # storage-initialization detail or allowing a task to succeed with
            # an empty projection.
            if exc.code == "memory_db_missing":
                raise ProjectionBuildError("no_projection_sources") from exc
            raise
        atoms = self._scoped_atoms(memory, scope, runtime_role=runtime_role)
        if execution is not None:
            execution.progress(25, "scope", item_count=len(atoms))
            execution.check_cancelled()
        if not atoms:
            raise ProjectionBuildError("no_projection_sources")

        evidence_store = self._evidence()

        evidence_by_id: dict[str, Any] = {}
        item_evidence: dict[str, list[str]] = {}
        for index, atom in enumerate(atoms):
            if execution is not None and index % 32 == 0:
                execution.check_cancelled()
            rows = evidence_store.list_for_subject(
                "atom",
                atom.atom_id,
                scope=EvidenceReadScope(
                    workspace_id=str(self.workspace),
                    subject_type="atom",
                    subject_id=atom.atom_id,
                ),
            )
            valid = [row for row in rows if str(row.status) == "valid" and str(row.digest)]
            if not valid:
                raise ProjectionBuildError("projection_atom_without_valid_evidence")
            item_evidence[atom.atom_id] = [row.evidence_id for row in valid]
            for row in valid:
                evidence_by_id[row.evidence_id] = row

        if execution is not None:
            execution.progress(55, "evidence", item_count=len(evidence_by_id))
            execution.check_cancelled()

        full_atom_refs = [
            {"atom_id": atom.atom_id, "atom_hash": atom.canonical_hash}
            for atom in atoms
        ]
        atom_refs = list(full_atom_refs)
        evidence_refs = [
            {
                "evidence_id": row.evidence_id,
                "evidence_hash": row.digest,
                "relation": "supports",
            }
            for row in sorted(evidence_by_id.values(), key=lambda item: item.evidence_id)
        ]
        source_digest = _digest(
            checked_mode,
            *sorted(f"{item['atom_id']}:{item['atom_hash']}" for item in full_atom_refs),
            *sorted(f"{item['evidence_id']}:{item['evidence_hash']}" for item in evidence_refs),
        )
        metadata = {
            "mode": checked_mode,
            "atom_count": len(atom_refs),
            "evidence_count": len(evidence_refs),
            "llm_provider": str(llm_provider or "none")[:128],
            "llm_used": bool(llm_used),
            "llm_engine": str(llm_engine or "")[:64],
            "source_digest": source_digest,
        }
        derived_graph, derived_stats = _derive_reference_graph(list(atoms))
        metadata.update({
            "derivation_engine": "deterministic_v3",
            "derived_graph": derived_graph,
            "derived_stats": derived_stats,
        })
        # ProjectionStore is reference-only but still has a hard 64 KiB JSON
        # payload ceiling.  Large atom sets keep every stable atom id and a
        # digest of the full hash stream; individual canonical hashes remain in
        # MemoryAtomStore and are not duplicated into an over-sized GUI record.
        payload_probe = {
            "projection_kind": kind,
            "projection_key": key,
            "atom_refs": atom_refs,
            "evidence_refs": evidence_refs,
            "metadata": metadata,
        }
        if len(json.dumps(payload_probe, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) > _COMPACT_PROJECTION_PAYLOAD_BYTES:
            metadata.update({
                "atom_refs_compacted": True,
                "atom_hash_digest": _digest(
                    *sorted(f"{item['atom_id']}:{item['atom_hash']}" for item in full_atom_refs)
                ),
                # Full evidence refs are already persisted transactionally in
                # projection_evidence_links, and atom->evidence associations in
                # projection_items.  Keeping hundreds of the same refs again
                # in payload_json can exceed ProjectionStore's 64 KiB safety
                # ceiling without adding any authoritative information.
                "evidence_refs_compacted": True,
                "evidence_hash_digest": _digest(
                    *sorted(f"{item['evidence_id']}:{item['evidence_hash']}" for item in evidence_refs)
                ),
                "evidence_refs_storage": "projection_evidence_links",
            })
            atom_refs = [{"atom_id": item["atom_id"]} for item in full_atom_refs]
        # Do not initialize ProjectionStore until after the input gate.  An
        # empty build must not create a successful-looking projection head.
        store = self._projection(write=True)
        previous = store.get_projection(kind, key, scope=scope)
        projector_cls = ProfileProjector if kind == "profile" else ScenarioProjector
        projector = projector_cls(store)

        if execution is not None:
            execution.progress(75, "graph", item_count=len(atom_refs))
            execution.check_cancelled()
        record = projector.project(
            key,
            atom_refs=atom_refs,
            evidence_refs=evidence_refs,
            item_evidence_refs=item_evidence,
            scope=scope,
            metadata=metadata,
            source_digest=source_digest,
        )
        try:
            if execution is not None:
                execution.progress(92, "save", cancellable=True)
                execution.check_cancelled()
        except TaskCancelled:
            # A cancellation racing the immutable commit must leave the previous
            # head visible (or no head when this was the first build).
            if previous is not None:
                store.rollback(
                    kind,
                    key,
                    previous.projection_id,
                    reason="cancelled_after_projection_commit",
                    scope=scope,
                )
            else:
                store.tombstone(kind, key, reason="cancelled_after_projection_commit")
            raise
        return {
            "status": "succeeded",
            "mode": checked_mode,
            "projection": self._record(record),
            "atom_count": len(atom_refs),
            "evidence_count": len(evidence_refs),
        }

    def current(self, *, mode: str, scope: ProjectionReadScope) -> dict[str, Any]:
        checked_mode = self._mode(mode)
        kind = self._kind(checked_mode)
        key = self._scope_key(checked_mode, scope)
        layout = WorkspaceV2Layout(self.workspace)
        path = layout.profile_db if kind == "profile" else layout.scenario_db
        if not path.is_file():
            row = None
        else:
            row = self._projection(write=False).get_projection(kind, key, scope=scope)
        return {
            "ok": True,
            "status": "succeeded",
            "mode": checked_mode,
            "projection": self._record(row) if row is not None else None,
        }

    def graph(
        self,
        *,
        mode: str,
        scope: ProjectionReadScope,
    ) -> dict[str, Any]:
        """Read Memory Core graph exclusively from the current projection.

        ProjectionStore metadata contains bounded reference-only graph data;
        this read never opens MemoryAtomStore or CodeGraphStore and never
        hydrates bodies into the graph response.
        """
        checked_mode = self._mode(mode)
        kind = self._kind(checked_mode)
        key = self._scope_key(checked_mode, scope)
        layout = WorkspaceV2Layout(self.workspace)
        path = layout.profile_db if kind == "profile" else layout.scenario_db
        if not path.is_file():
            return {
                "status": "NO_SOURCE",
                "mode": checked_mode,
                "kind": kind,
                "key": key,
                "scope_digest": _digest(*scope.as_tuple()),
                "empty": True,
                "root_id": "main",
                "nodes": [],
                "edges": [],
                "stats": {},
            }
        record = self._projection(write=False).get_projection(kind, key, scope=scope)
        if record is None:
            return {
                "status": "NO_SOURCE",
                "mode": checked_mode,
                "kind": kind,
                "key": key,
                "scope_digest": _digest(*scope.as_tuple()),
                "empty": True,
                "root_id": "main",
                "nodes": [],
                "edges": [],
                "stats": {},
            }
        metadata = record.payload.get("metadata")
        graph = metadata.get("derived_graph") if isinstance(metadata, Mapping) else None
        if not isinstance(graph, Mapping):
            raise ProjectionBuildError("projection_graph_missing")
        nodes = [dict(node) for node in graph.get("nodes", ()) if isinstance(node, Mapping)]
        edges = [dict(edge) for edge in graph.get("edges", ()) if isinstance(edge, Mapping)]
        return {
            "status": "READY",
            "mode": checked_mode,
            "kind": kind,
            "key": key,
            "scope_digest": _digest(*scope.as_tuple()),
            "empty": not nodes,
            "projection_id": record.projection_id,
            "projection_digest": record.projection_digest,
            "generation": record.generation,
            "root_id": str(graph.get("root_id") or "main"),
            "nodes": nodes,
            "edges": edges,
            "truncated": bool(graph.get("truncated")),
            "stats": dict(metadata.get("derived_stats") or {}) if isinstance(metadata, Mapping) else {},
        }

    def delete(self, *, mode: str, scope: ProjectionReadScope) -> dict[str, Any]:
        checked_mode = self._mode(mode)
        kind = self._kind(checked_mode)
        key = self._scope_key(checked_mode, scope)
        layout = WorkspaceV2Layout(self.workspace)
        path = layout.profile_db if kind == "profile" else layout.scenario_db
        if not path.is_file():
            return {
                "ok": True,
                "status": "succeeded",
                "mode": checked_mode,
                "deleted": False,
                "tombstone_id": "",
            }
        store = self._projection(write=False)
        before_tombstones = store.counts(kind)["tombstones"]
        tombstone_id = store.tombstone(kind, key, reason="gui_delete_projection")
        after_tombstones = store.counts(kind)["tombstones"]
        return {
            "ok": True,
            "status": "succeeded",
            "mode": checked_mode,
            "deleted": after_tombstones > before_tombstones,
            "tombstone_id": tombstone_id,
        }

    def source_map(self, *, scope: ProjectionReadScope) -> dict[str, Any]:
        layout = WorkspaceV2Layout(self.workspace)
        rows = (
            self.content.list_source_connectors(workspace_id=str(self.workspace))
            if layout.content_db.is_file()
            else []
        )
        connector_entries = [
            {
                "entry_kind": "source_connector",
                "source_id": str(row.get("source_id") or ""),
                "root_id": str(row.get("source_id") or ""),
                "surface_id": str(row.get("source_id") or ""),
                "display_name": str(row.get("source_type") or row.get("provider") or "source connector"),
                "provider": str(row.get("provider") or ""),
                "source_type": str(row.get("source_type") or ""),
                "source_category": "content_source",
                "projection_mode": "logical_reconstruction_projection",
                "enabled": bool(row.get("enabled")),
                "participates": bool(row.get("enabled")),
                "logical_eligible": bool(row.get("enabled")),
                "native_eligible": False,
                "is_shared_memory_origin": False,
                "record_count": 0,
                # A connector row has no authoritative Agent owner in the
                # Content Plane.  Keep it blank rather than deriving/faking an
                # Agent from a connector id.
                "agent_instance_id": "",
                "project_ref": "",
                "ingestion_policy": "selected_source_connector",
                "path": "",
            }
            for row in rows
            if str(row.get("source_id") or "")
        ]

        all_atoms: list[Any] = []
        eligible_atoms: list[Any] = []
        if layout.memory_db.is_file():
            memory = self._memory()
            all_atoms = list(memory.list_atoms(
                scope=self._memory_scope(scope),
                status="active",
            ))
            eligible_atoms = self._filter_enabled_content_sources(memory, all_atoms, scope)
        eligible_ids = {str(getattr(atom, "atom_id", "")) for atom in eligible_atoms}
        shared_scope = not str(scope.agent_instance_id or "").strip()
        memory_entries = [
            {
                "entry_kind": "governed_memory",
                "source_id": "v2-memory:" + str(getattr(atom, "atom_id", "")),
                "root_id": "v2-memory:" + str(getattr(atom, "atom_id", "")),
                "surface_id": "v2-memory",
                "display_name": "V2 governed memory · " + str(getattr(atom, "memory_id", "")),
                "provider": str(getattr(atom, "provider", "") or ""),
                "source_type": "v2_governed_memory",
                "source_category": "shared_memory" if shared_scope else "native_memory",
                "projection_mode": "shared_memory_projection" if shared_scope else "logical_reconstruction_projection",
                "enabled": str(getattr(atom, "atom_id", "")) in eligible_ids,
                "participates": str(getattr(atom, "atom_id", "")) in eligible_ids,
                "logical_eligible": str(getattr(atom, "atom_id", "")) in eligible_ids,
                "native_eligible": True,
                "is_shared_memory_origin": shared_scope,
                "record_count": 1,
                "agent_instance_id": str(getattr(atom, "agent_instance_id", "") or ""),
                "project_ref": str(getattr(atom, "project_ref", "") or ""),
                "ingestion_policy": "governed_v2_memory",
                "path": "",
            }
            for atom in sorted(all_atoms, key=lambda item: str(getattr(item, "atom_id", "")))
            if str(getattr(atom, "atom_id", ""))
        ]
        entries = connector_entries + memory_entries
        connector_enabled = sum(1 for item in connector_entries if item["enabled"])
        memory_eligible = len(eligible_atoms)
        return {
            "ok": True,
            "status": "succeeded",
            "entries": entries,
            "projection_kind": "shared_memory_projection" if shared_scope else "logical_reconstruction_projection",
            "scope_semantics": "share_group_members" if shared_scope else "agent",
            "summary": {
                "total": len(entries),
                "enabled": sum(1 for item in entries if item["enabled"]),
                "selected_source_connectors": connector_enabled,
                "selected_source_connector_total": len(connector_entries),
                "governed_memory": len(all_atoms),
                "governed_memory_eligible": memory_eligible,
                "buildable_atom_count": memory_eligible,
                "native_memory": len(memory_entries) if not shared_scope else 0,
                "logical_reconstruction": connector_enabled,
                "evidence_only": 0,
                "shared_memory": len(memory_entries) if shared_scope else 0,
            },
        }

    def set_source_enabled(
        self,
        source_id: str,
        enabled: bool,
        *,
        scope: ProjectionReadScope,
    ) -> dict[str, Any]:
        changed = self.content.set_source_connector_enabled(
            str(source_id),
            bool(enabled),
            workspace_id=str(self.workspace),
        )
        if not changed:
            raise ProjectionBuildError("projection_source_not_found")
        return {
            "ok": True,
            "status": "succeeded",
            "source_id": str(source_id),
            "enabled": bool(enabled),
            "changed": True,
        }



class V2ReleaseService:
    """Immutable V2 release plans/receipts backed by the projection ledger."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.projections = ProjectionBuildService(self.workspace)
        self.content = ContentStore(self.workspace, initialize=False)

    def _store(self, *, write: bool) -> ProjectionStore:
        return ProjectionStore(self.workspace, initialize=bool(write))

    @staticmethod
    def _file_digest(path: Path) -> str:
        if not path.is_file():
            return ""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _scope_digest(scope: ProjectionReadScope) -> str:
        return _digest(*scope.as_tuple())

    @classmethod
    def _backup_namespace_id(cls, scope: ProjectionReadScope) -> str:
        return "release-backup-ns-" + cls._scope_digest(scope)[:32]

    @classmethod
    def _backup_source_id(cls, scope: ProjectionReadScope) -> str:
        return "release-backup-source-" + cls._scope_digest(scope)[:32]

    @classmethod
    def _backup_read_scope(cls, scope: ProjectionReadScope) -> ContentReadScope:
        return ContentReadScope(
            namespace_id=cls._backup_namespace_id(scope),
            workspace_id=scope.workspace_id,
            agent_instance_id=scope.agent_instance_id,
            project_ref=scope.project_ref,
            provider=scope.provider,
            share_group_id=scope.share_group_id,
            sensitivity=scope.sensitivity,
            policy_class=scope.policy_class,
        )

    def _backup_previous_target(
        self,
        target: Path,
        *,
        release_id: str,
        scope: ProjectionReadScope,
    ) -> dict[str, Any]:
        if not target.is_file():
            return {
                "existed_before": False,
                "previous_blob_id": "",
                "previous_occurrence_id": "",
                "previous_digest": "",
            }
        previous = target.read_bytes()
        previous_digest = hashlib.sha256(previous).hexdigest()
        content = ContentStore(
            self.workspace,
            workspace_id=scope.workspace_id,
            initialize=True,
        )
        namespace_id = self._backup_namespace_id(scope)
        source_id = self._backup_source_id(scope)
        content.ensure_namespace(
            namespace_id=namespace_id,
            workspace_id=scope.workspace_id,
            trust_domain="release-backup",
            sensitivity=scope.sensitivity,
            retention_authority="release-rollback",
        )
        content.upsert_source_connector(
            source_id=source_id,
            provider=scope.provider,
            source_type="release_backup",
            external_root_key="release-backup:" + self._scope_digest(scope),
            workspace_id=scope.workspace_id,
        )
        encoded = "base64:" + base64.b64encode(previous).decode("ascii")
        blob_id = content.put_blob(encoded, namespace_id=namespace_id)
        if not blob_id:
            raise ProjectionBuildError("release_backup_blob_failed")
        source_object_id = "release-backup-object-" + _digest(release_id, previous_digest)
        occurrence_id = content.upsert_occurrence(
            source_object_id=source_object_id,
            occurrence_key="previous-target",
            blob_id=blob_id,
            namespace_id=namespace_id,
            source_id=source_id,
            source_kind="release_backup",
            external_object_key=release_id,
            object_type="release_backup",
            source_revision=previous_digest,
            content_role="release_backup",
            sensitivity=scope.sensitivity,
            workspace_id=scope.workspace_id,
            agent_instance_id=scope.agent_instance_id,
            project_ref=scope.project_ref,
            share_group_id=scope.share_group_id,
            policy_class=scope.policy_class,
            provider=scope.provider,
            access_scope={"mode": "release_backup"},
        )
        content.hold_blob(
            blob_id,
            reason="release_rollback_backup",
            source_ref=release_id,
        )
        return {
            "existed_before": True,
            "previous_blob_id": blob_id,
            "previous_occurrence_id": occurrence_id,
            "previous_digest": previous_digest,
        }

    def _restore_previous_target(
        self,
        target: Path,
        receipt: Mapping[str, Any],
        *,
        scope: ProjectionReadScope,
    ) -> None:
        if not bool(receipt.get("existed_before")):
            if target.exists():
                target.unlink()
            return
        blob_id = str(receipt.get("previous_blob_id") or "").strip()
        occurrence_id = str(receipt.get("previous_occurrence_id") or "").strip()
        expected_digest = str(receipt.get("previous_digest") or "").strip()
        if not blob_id or not occurrence_id or not expected_digest:
            raise ProjectionBuildError("release_backup_missing")
        try:
            content = ContentStore(
                self.workspace,
                workspace_id=scope.workspace_id,
                initialize=False,
            )
            read_scope = self._backup_read_scope(scope)
            occurrence = content.get_occurrence(occurrence_id, read_scope)
            blob = content.get_blob(blob_id, read_scope)
        except FileNotFoundError as exc:
            raise ProjectionBuildError("release_backup_missing") from exc
        if occurrence is None or blob is None or occurrence.blob_id != blob_id:
            raise ProjectionBuildError("release_backup_missing")
        encoded = str(blob.text)
        if not encoded.startswith("base64:"):
            raise ProjectionBuildError("release_backup_encoding_invalid")
        try:
            previous = base64.b64decode(encoded[7:].encode("ascii"), validate=True)
        except (UnicodeError, ValueError) as exc:
            raise ProjectionBuildError("release_backup_encoding_invalid") from exc
        if hashlib.sha256(previous).hexdigest() != expected_digest:
            raise ProjectionBuildError("release_backup_digest_mismatch")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_name(target.name + ".memoryguard-v2.rollback.tmp")
        temp_path.write_bytes(previous)
        os.replace(temp_path, target)

    @staticmethod
    def _public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
        allowed = (
            "plan_id", "target_before_digest", "input_digest", "output_digest",
            "projection_id", "projection_digest", "mode", "llm_provider", "memory_count",
        )
        return {key: plan[key] for key in allowed if key in plan}

    @staticmethod
    def _public_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
        allowed = (
            "release_id", "plan_id", "target_digest", "target_before_digest",
            "previous_blob_id", "previous_occurrence_id", "previous_digest",
            "existed_before", "projection_id", "projection_digest", "input_digest", "created_at",
        )
        return {key: receipt[key] for key in allowed if key in receipt}

    def _memory_snapshot(
        self,
        scope: ProjectionReadScope,
        *,
        runtime_role: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        memory = self.projections._memory()
        atoms = self.projections._scoped_atoms(memory, scope, runtime_role=runtime_role)
        rows = [
            {
                "memory_id": atom.memory_id,
                "body": atom.body,
                "kind": atom.kind,
                "status": atom.status,
                "confidence": atom.confidence,
                "locked": bool(atom.locked),
                "injection_policy": atom.injection_policy,
                "priority": atom.priority,
                "canonical_hash": atom.canonical_hash,
                "revision": atom.revision,
            }
            for atom in atoms
        ]
        digest = hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return rows, digest

    @staticmethod
    def _release_bytes(rows: list[dict[str, Any]], *, scope_digest: str, projection: Mapping[str, Any]) -> bytes:
        document = {
            "schema": "memoryguard-v2-native-release-1",
            "scope_digest": scope_digest,
            "projection_id": str(projection.get("projection_id") or ""),
            "projection_digest": str(projection.get("projection_digest") or ""),
            "memories": rows,
        }
        return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    def _ledger_rows(self, code: str) -> list[dict[str, Any]]:
        if not WorkspaceV2Layout(self.workspace).scenario_db.is_file():
            return []
        with self._store(write=False).connection("scenario") as conn:
            rows = conn.execute(
                "SELECT ledger_id,source_ref,detail,created_at FROM projection_ledger WHERE code=? ORDER BY created_at,ledger_id",
                (str(code),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                detail = json.loads(str(row[2] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                detail = {}
            if isinstance(detail, Mapping):
                result.append({"ledger_id": str(row[0]), "source_ref": str(row[1]), "created_at": str(row[3]), **dict(detail)})
        return result

    def _record(self, source_ref: str, code: str, detail: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(detail), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ProjectionBuildError("release_receipt_too_large")
        return self._store(write=True).record_ledger(source_ref, code, encoded)

    def resolve_target(
        self,
        *,
        scope: ProjectionReadScope,
        target_path: str = "",
        target_root_id: str = "",
    ) -> Path:
        raw = str(target_path or "").strip()
        root_id = str(target_root_id or "").strip()
        if root_id:
            rows = self.content.list_source_connectors(workspace_id=str(self.workspace))
            row = next((item for item in rows if str(item.get("source_id") or "") == root_id), None)
            if row is None:
                raise ProjectionBuildError("release_target_root_not_found")
            root_value = str(row.get("external_root_key") or "").strip()
            if not root_value:
                raise ProjectionBuildError("release_target_root_invalid")
            root = Path(root_value).expanduser()
            if not root.is_absolute():
                root = self.workspace / root
            root = root.resolve()
            candidate = (root / raw).resolve() if raw and not Path(raw).is_absolute() else (Path(raw).expanduser().resolve() if raw else root)
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ProjectionBuildError("release_target_outside_authorized_root") from exc
            return candidate
        if not raw:
            raise ProjectionBuildError("release_target_required")
        candidate = Path(raw).expanduser()
        candidate = (self.workspace / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise ProjectionBuildError("release_target_root_required") from exc
        return candidate

    def list_targets(self, *, scope: ProjectionReadScope) -> dict[str, Any]:
        rows = self.content.list_source_connectors(workspace_id=str(self.workspace))
        return {
            "ok": True,
            "status": "succeeded",
            "targets": [
                {
                    "target_root_id": str(row.get("source_id") or ""),
                    "provider": str(row.get("provider") or ""),
                    "source_type": str(row.get("source_type") or ""),
                    "enabled": bool(row.get("enabled")),
                }
                for row in rows if bool(row.get("enabled"))
            ],
            "workspace_relative_allowed": True,
        }

    def create_plan(
        self,
        target_path: str,
        *,
        scope: ProjectionReadScope,
        llm_provider: str = "deterministic",
        mode: str = "reconstructed",
        runtime_role: str = "",
    ) -> dict[str, Any]:
        target = Path(target_path).expanduser().resolve()
        current = self.projections.current(mode=mode, scope=scope).get("projection")
        if not isinstance(current, Mapping):
            raise ProjectionBuildError("release_projection_required")
        rows, input_digest = self._memory_snapshot(scope, runtime_role=runtime_role)
        payload = self._release_bytes(rows, scope_digest=self._scope_digest(scope), projection=current)
        output_digest = hashlib.sha256(payload).hexdigest()
        target_before_digest = self._file_digest(target)
        plan_id = "release-plan-" + _digest(
            self._scope_digest(scope), target, input_digest, output_digest,
            current.get("projection_id"), current.get("projection_digest"), mode,
        )
        detail = {
            "plan_id": plan_id,
            "scope_digest": self._scope_digest(scope),
            "target_path": str(target),
            "target_before_digest": target_before_digest,
            "input_digest": input_digest,
            "output_digest": output_digest,
            "projection_id": str(current.get("projection_id") or ""),
            "projection_digest": str(current.get("projection_digest") or ""),
            "mode": str(mode),
            "llm_provider": str(llm_provider or "deterministic")[:128],
            "memory_count": len(rows),
        }
        self._record(f"release-plan:{plan_id}", "release_plan", detail)
        return {"ok": True, "status": "succeeded", **self._public_plan(detail)}

    def _plan(self, plan_id: str, *, scope: ProjectionReadScope) -> dict[str, Any]:
        rows = [row for row in self._ledger_rows("release_plan") if str(row.get("plan_id") or "") == str(plan_id)]
        if len(rows) != 1:
            raise ProjectionBuildError("release_plan_not_found")
        plan = rows[0]
        if str(plan.get("scope_digest") or "") != self._scope_digest(scope):
            raise ProjectionBuildError("release_plan_scope_mismatch")
        return plan

    def apply(
        self,
        plan_id: str,
        target_path: str,
        *,
        scope: ProjectionReadScope,
        execution: TaskExecution | None = None,
        confirmed: bool = False,
        runtime_role: str = "",
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise ProjectionBuildError("release_confirmation_required")
        plan = self._plan(plan_id, scope=scope)
        target = Path(target_path).expanduser().resolve()
        if str(target) != str(plan.get("target_path") or ""):
            raise ProjectionBuildError("release_target_mismatch")
        if execution is not None:
            execution.progress(15, "verify")
            execution.check_cancelled()
        current = self.projections.current(mode=str(plan.get("mode") or "reconstructed"), scope=scope).get("projection")
        if not isinstance(current, Mapping):
            raise ProjectionBuildError("release_projection_required")
        if str(current.get("projection_id") or "") != str(plan.get("projection_id") or "") or str(current.get("projection_digest") or "") != str(plan.get("projection_digest") or ""):
            raise ProjectionBuildError("release_plan_stale")
        rows, input_digest = self._memory_snapshot(scope, runtime_role=runtime_role)
        if input_digest != str(plan.get("input_digest") or ""):
            raise ProjectionBuildError("release_plan_stale")
        payload = self._release_bytes(rows, scope_digest=self._scope_digest(scope), projection=current)
        if hashlib.sha256(payload).hexdigest() != str(plan.get("output_digest") or ""):
            raise ProjectionBuildError("release_plan_output_changed")
        if self._file_digest(target) != str(plan.get("target_before_digest") or ""):
            raise ProjectionBuildError("release_target_drift")
        if execution is not None:
            execution.progress(55, "publish")
            execution.check_cancelled()

        release_id = "release-" + _digest(plan_id, plan.get("output_digest"), target)
        backup = self._backup_previous_target(target, release_id=release_id, scope=scope)
        temp_path = target.with_name(target.name + ".memoryguard-v2.tmp")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(payload)
        os.replace(temp_path, target)
        target_digest = self._file_digest(target)
        if target_digest != str(plan.get("output_digest") or ""):
            self._restore_previous_target(target, backup, scope=scope)
            raise ProjectionBuildError("release_verify_failed")
        try:
            if execution is not None:
                execution.progress(85, "receipt")
                execution.check_cancelled()
            receipt = {
                "release_id": release_id,
                "plan_id": str(plan_id),
                "scope_digest": self._scope_digest(scope),
                # The immutable ledger keeps the exact target for rollback, but
                # public/task receipts are projected through _public_receipt and
                # never expose absolute paths.
                "target_path": str(target),
                "target_digest": target_digest,
                "target_before_digest": str(plan.get("target_before_digest") or ""),
                **backup,
                "projection_id": str(plan.get("projection_id") or ""),
                "projection_digest": str(plan.get("projection_digest") or ""),
                "input_digest": input_digest,
            }
            self._record(f"release:{release_id}", "release_applied", receipt)
        except BaseException:
            self._restore_previous_target(target, backup, scope=scope)
            raise
        return {"ok": True, "status": "succeeded", **self._public_receipt(receipt)}

    def list_releases(self, *, scope: ProjectionReadScope, limit: int = 100) -> dict[str, Any]:
        scope_digest = self._scope_digest(scope)
        rows = [row for row in self._ledger_rows("release_applied") if str(row.get("scope_digest") or "") == scope_digest]
        rows = rows[-max(1, min(int(limit or 100), 500)):]
        public_rows = [self._public_receipt(row) for row in rows]
        return {"ok": True, "status": "succeeded", "releases": public_rows, "total": len(public_rows)}

    def _release(self, release_id: str, *, scope: ProjectionReadScope) -> dict[str, Any]:
        rows = [row for row in self._ledger_rows("release_applied") if str(row.get("release_id") or "") == str(release_id)]
        if len(rows) != 1:
            raise ProjectionBuildError("release_not_found")
        receipt = rows[0]
        if str(receipt.get("scope_digest") or "") != self._scope_digest(scope):
            raise ProjectionBuildError("release_scope_mismatch")
        return receipt

    def verify(self, release_id: str, target_path: str, *, scope: ProjectionReadScope) -> dict[str, Any]:
        receipt = self._release(release_id, scope=scope)
        target = Path(target_path).expanduser().resolve()
        if str(target) != str(receipt.get("target_path") or ""):
            raise ProjectionBuildError("release_target_mismatch")
        actual = self._file_digest(target)
        expected = str(receipt.get("target_digest") or "")
        matches = actual == expected and bool(expected)
        return {
            "ok": matches,
            "status": "succeeded" if matches else "failed",
            "release_id": str(release_id),
            "hashes_match": matches,
            "expected_digest": expected,
            "actual_digest": actual,
        }

    def rollback(
        self,
        release_id: str,
        target_path: str,
        *,
        scope: ProjectionReadScope,
        confirmed: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise ProjectionBuildError("release_confirmation_required")
        receipt = self._release(release_id, scope=scope)
        if str(target_path or "").strip():
            target = Path(target_path).expanduser().resolve()
            if str(target) != str(receipt.get("target_path") or ""):
                raise ProjectionBuildError("release_target_mismatch")
        else:
            target_value = str(receipt.get("target_path") or "").strip()
            if not target_value:
                raise ProjectionBuildError("release_target_missing_from_receipt")
            target = Path(target_value).expanduser().resolve()
        current_digest = self._file_digest(target)
        if not force and current_digest != str(receipt.get("target_digest") or ""):
            raise ProjectionBuildError("release_target_drift")
        self._restore_previous_target(target, receipt, scope=scope)
        restored_digest = self._file_digest(target)
        if bool(receipt.get("existed_before")) and restored_digest != str(receipt.get("previous_digest") or ""):
            raise ProjectionBuildError("release_rollback_verify_failed")
        rollback_id = "release-rollback-" + _digest(release_id, current_digest, restored_digest)
        detail = {
            "rollback_id": rollback_id,
            "release_id": str(release_id),
            "scope_digest": self._scope_digest(scope),
            "target_path": str(target),
            "restored_digest": restored_digest,
        }
        self._record(f"release-rollback:{rollback_id}", "release_rollback", detail)
        return {
            "ok": True,
            "status": "succeeded",
            "rolled_back": True,
            "rollback_id": rollback_id,
            "release_id": str(release_id),
            "restored_digest": restored_digest,
        }


__all__ = [
    "ProjectionBuildError", "ProjectionBuildService", "V2ReleaseService",
    "projection_scope_from_context",
]
