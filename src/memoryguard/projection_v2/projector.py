"""Deterministic scenario/profile projectors.

Projectors accept MemoryAtom/Evidence objects or reference mappings, then keep
only IDs and hashes.  They never write to memory.db/evidence.db and never copy
source text into projection payloads.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping, Sequence

from .store import (
    ProjectionError,
    ProjectionReadScope,
    ProjectionRecord,
    ProjectionStore,
    _digest,
)


def _value(item: Any, key: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _ref_atom(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        return {"atom_id": item, "atom_hash": ""}
    atom_id = str(_value(item, "atom_id") or _value(item, "memory_id") or _value(item, "id") or "")
    atom_hash = str(_value(item, "atom_hash") or _value(item, "canonical_hash") or _value(item, "hash") or "")
    if not atom_id:
        raise ProjectionError("projection atom reference requires atom_id")
    return {"atom_id": atom_id, "atom_hash": atom_hash}


def _ref_evidence(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        return {"evidence_id": item, "evidence_hash": "", "relation": "supports"}
    evidence_id = str(_value(item, "evidence_id") or _value(item, "id") or "")
    evidence_hash = str(_value(item, "evidence_hash") or _value(item, "digest") or _value(item, "hash") or "")
    relation = str(_value(item, "relation") or "supports")
    if not evidence_id:
        raise ProjectionError("projection evidence reference requires evidence_id")
    return {"evidence_id": evidence_id, "evidence_hash": evidence_hash, "relation": relation}


def _scope_match(item: Any, scope: ProjectionReadScope) -> None:
    for field in (
        "workspace_id", "agent_instance_id", "project_ref", "provider",
        "share_group_id", "sensitivity", "policy_class",
    ):
        value = _value(item, field, None)
        if value is not None and str(value) != getattr(scope, field):
            raise ProjectionError(f"projection scope mismatch: {field}")


class BaseProjector:
    kind = "scenario"

    def __init__(
        self,
        store: ProjectionStore,
        *,
        memory_store: Any | None = None,
        evidence_store: Any | None = None,
    ) -> None:
        self.store = store
        self.memory_store = memory_store  # read-only dependency, never mutated
        self.evidence_store = evidence_store  # read-only dependency, never mutated

    def project(
        self,
        key: str,
        atoms: Sequence[Any] | None = None,
        evidence: Sequence[Any] | None = None,
        *,
        scope: ProjectionReadScope,
        metadata: Mapping[str, Any] | None = None,
        source_digest: str = "",
        atom_refs: Sequence[Any] | None = None,
        evidence_refs: Sequence[Any] | None = None,
        item_evidence_refs: Mapping[str, Sequence[str]] | None = None,
        fail_at: str | None = None,
    ) -> ProjectionRecord:
        if atoms is None:
            atoms = atom_refs or ()
        if evidence is None:
            evidence = evidence_refs or ()
        if not isinstance(scope, ProjectionReadScope):
            raise ProjectionError("explicit ProjectionReadScope is required")
        atom_list = [_ref_atom(item) for item in atoms]
        evidence_list = [_ref_evidence(item) for item in evidence]
        if not evidence_list:
            raise ProjectionError("every projection requires at least one evidence link")
        for item in list(atoms) + list(evidence):
            _scope_match(item, scope)
        safe_metadata = dict(metadata or {})
        payload = {
            "projection_kind": self.kind,
            "projection_key": str(key),
            "atom_refs": atom_list,
            "evidence_refs": evidence_list,
            "metadata": safe_metadata,
        }
        if not source_digest:
            source_digest = _digest({"atoms": atom_list, "evidence": evidence_list, "scope": scope.as_tuple(), "metadata": safe_metadata})
        evidence_by_id = {item["evidence_id"]: item for item in evidence_list}
        refs: list[dict[str, str]] = []
        if item_evidence_refs is None:
            # Compatibility path for existing callers that supplied one
            # evidence stream.  Native GUI build supplies an explicit mapping
            # below so no cross-atom association is inferred from list order.
            refs = [
                {
                    "atom_id": atom["atom_id"],
                    "atom_hash": atom["atom_hash"],
                    "evidence_id": evidence_list[index % len(evidence_list)]["evidence_id"],
                    "evidence_hash": evidence_list[index % len(evidence_list)]["evidence_hash"],
                }
                for index, atom in enumerate(atom_list)
            ]
        else:
            for atom in atom_list:
                linked_ids = tuple(str(item) for item in item_evidence_refs.get(atom["atom_id"], ()))
                if not linked_ids:
                    raise ProjectionError("projection atom has no explicit evidence mapping")
                for evidence_id in linked_ids:
                    linked = evidence_by_id.get(evidence_id)
                    if linked is None:
                        raise ProjectionError("projection atom references evidence outside projection")
                    refs.append({
                        "atom_id": atom["atom_id"],
                        "atom_hash": atom["atom_hash"],
                        "evidence_id": evidence_id,
                        "evidence_hash": linked["evidence_hash"],
                    })
        return self.store.put_projection(
            self.kind,
            str(key),
            source_digest=source_digest,
            payload=payload,
            scope=scope,
            evidence_links=evidence_list,
            item_refs=refs,
            fail_at=fail_at,
        )

    build = project


class ScenarioProjector(BaseProjector):
    kind = "scenario"


class ProfileProjector(BaseProjector):
    kind = "profile"


ScenarioProjectionProjector = ScenarioProjector
ProfileProjectionProjector = ProfileProjector


__all__ = [
    "BaseProjector",
    "ScenarioProjector",
    "ProfileProjector",
    "ScenarioProjectionProjector",
    "ProfileProjectionProjector",
]
