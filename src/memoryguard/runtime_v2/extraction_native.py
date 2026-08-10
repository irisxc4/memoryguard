"""Native V2 extraction and host-enrichment workflow.

The V1 implementation staged extraction/enrichment work in JSON files and
committed accepted results through SharedMemoryStore/Memory IR.  This module
keeps the same public two-step workflow but stores staging facts in the V2
Content Plane and commits memory changes through GovernanceV2 only.

Candidate/task bodies live in content_blobs.  knowledge_records stores only
scope/status/provenance metadata, so accepted or applied work remains auditable
without copying source bodies into metadata or a sidecar queue.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..content.store import ContentStore
from ..content_parsers import parse_file
from ..governance_v2 import GovernanceV2
from ..memory.store import MemoryAtom, MemoryAtomStore
from ..policies import _VALID_KINDS, classify_kind
from ..storage.database import open_database
from ..storage.transaction import transaction
from .native_ports import NativePortError, resolve_native_transport_context
from .safe_services import ImportPreviewService, PureSourceReadService


_CANDIDATE_SOURCE = "native_extraction_candidates"
_TASK_SOURCE = "native_enrichment_tasks"
_MAX_SOURCE_BYTES = 5 * 1024 * 1024
_MAX_EXTRACT_SEGMENTS = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(*values: Any) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _scope(context: Any) -> dict[str, Any]:
    try:
        authority = resolve_native_transport_context(context)
    except Exception as exc:
        raise NativePortError("trusted_context_capability_required") from exc
    if not authority.workspace_id or not authority.share_group_id or not authority.agent_instance_id:
        raise NativePortError("context_scope_required")
    return {
        "workspace_id": authority.workspace_id,
        "share_group_id": authority.share_group_id,
        "agent_instance_id": authority.agent_instance_id,
        "project_ref": authority.project_ref,
        "provider": authority.provider,
        "runtime_role": authority.runtime_role,
    }


def _scope_digest(scope: Mapping[str, Any]) -> str:
    return _digest("scope", {str(key): str(value or "") for key, value in scope.items()})


def _redact_secret(body: str) -> tuple[str, bool]:
    # Reuse the existing, centrally maintained pattern set, but keep MCP
    # transport code out of this V2 service.
    from ..auto_organizer import SECRET_PATTERNS

    hit = any(pattern.search(body) for pattern in SECRET_PATTERNS)
    if not hit:
        return body, False
    safe = body
    for pattern in SECRET_PATTERNS:
        safe = pattern.sub("[REDACTED]", safe)
    return safe, True


class NativeExtractionEnrichmentService:
    """V2-only extraction staging, acceptance and host enrichment."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        content_store: ContentStore | None = None,
        memory_store: MemoryAtomStore | None = None,
        governance: GovernanceV2 | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.content = content_store or ContentStore(self.workspace, initialize=False)
        # Fail closed on a partial/future Content Plane before any staging write.
        preflight = getattr(self.content, "_preflight_aux_schema", None)
        if not callable(preflight) or preflight() != "current":
            raise NativePortError("v2_content_schema_invalid")
        self.memory = memory_store or MemoryAtomStore(self.workspace, readonly=False)
        self.governance = governance or GovernanceV2(self.workspace, memory_store=self.memory)
        self.source_reader = PureSourceReadService(self.workspace)
        self.import_preview = ImportPreviewService(self.workspace, source_reader=self.source_reader)

    # ------------------------------------------------------------------
    # Content-plane staging helpers
    # ------------------------------------------------------------------
    def _put_record(
        self,
        *,
        source_table: str,
        source_pk: str,
        record_type: str,
        blob_id: str,
        status: str,
        metadata: Mapping[str, Any],
        preserve_terminal: bool = False,
    ) -> str:
        record_id = _digest(source_table, source_pk, record_type)[:32]
        with open_database(self.content.db_path) as conn:
            with transaction(conn):
                existing = conn.execute(
                    "SELECT status FROM knowledge_records WHERE source_table=? AND source_pk=? AND record_type=?",
                    (source_table, source_pk, record_type),
                ).fetchone()
                effective = status
                if preserve_terminal and existing is not None and str(existing[0] or "") in {"accepted", "applied"}:
                    effective = str(existing[0])
                conn.execute(
                    "INSERT INTO knowledge_records(record_id,source_table,source_pk,record_type,content_blob_id,status,derived_status,metadata_json) "
                    "VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_table,source_pk,record_type) DO UPDATE SET "
                    "content_blob_id=excluded.content_blob_id,status=excluded.status,derived_status=excluded.derived_status,metadata_json=excluded.metadata_json",
                    (record_id, source_table, source_pk, record_type, blob_id, effective, "CANONICAL", _json(metadata)),
                )
        return record_id

    def _records(self, source_table: str, scope: Mapping[str, Any]) -> list[dict[str, Any]]:
        scope_hash = _scope_digest(scope)
        with open_database(self.content.db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT r.record_id,r.source_pk,r.record_type,r.content_blob_id,r.status,r.metadata_json,b.text,b.canonical_hash "
                "FROM knowledge_records r LEFT JOIN content_blobs b ON b.blob_id=r.content_blob_id "
                "WHERE r.source_table=? ORDER BY r.source_pk",
                (source_table,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            meta = _metadata(json.loads(str(row[5] or "{}")))
            if str(meta.get("scope_digest") or "") != scope_hash:
                continue
            result.append({
                "record_id": str(row[0]),
                "source_pk": str(row[1]),
                "record_type": str(row[2]),
                "blob_id": str(row[3] or ""),
                "status": str(row[4] or ""),
                "metadata": meta,
                "body": str(row[6] or ""),
                "body_digest": str(row[7] or ""),
            })
        return result

    def _update_record_status(self, record_id: str, status: str, metadata: Mapping[str, Any]) -> None:
        with open_database(self.content.db_path) as conn:
            with transaction(conn):
                changed = conn.execute(
                    "UPDATE knowledge_records SET status=?,metadata_json=? WHERE record_id=?",
                    (status, _json(metadata), record_id),
                ).rowcount
                if changed != 1:
                    raise NativePortError("v2_staging_record_missing")

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def extract(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        scope = _scope(context)
        raw_path = payload.get("source_path") or payload.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise NativePortError("source_path_required")
        requested = Path(raw_path).expanduser()
        if not requested.is_absolute():
            requested = self.workspace / requested
        # ImportPreviewService owns the source-root/reparse/containment check.
        try:
            target = self.import_preview._target({"path": str(requested)})
            root, _root_path = self.import_preview._authorized_root(target, context)
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "path_out_of_scope")
            raise NativePortError(code) from exc
        if not target.is_file():
            raise NativePortError("source_file_required")
        try:
            size = int(target.stat().st_size)
        except OSError as exc:
            raise NativePortError("source_unavailable") from exc
        if size > _MAX_SOURCE_BYTES:
            raise NativePortError("source_too_large")
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise NativePortError("source_unavailable") from exc
        source_digest = hashlib.sha256(raw).hexdigest()
        source_reference = f"source:{_digest(str(getattr(root, 'root_id', '')), target.name, source_digest)[:24]}"
        segments = parse_file(target, content=raw)
        if not segments:
            return {"extract_id": _digest("empty", source_reference, source_digest)[:32], "candidates": [], "total": 0}
        extract_id = _digest("extract", _scope_digest(scope), source_reference, source_digest)[:32]
        candidates: list[dict[str, Any]] = []
        for index, segment in enumerate(segments[:_MAX_EXTRACT_SEGMENTS]):
            safe_body, secret_hit = _redact_secret(str(segment.body or ""))
            body = safe_body.strip()
            if not body:
                continue
            kind = str(segment.kind_hint or classify_kind(body))
            if kind not in _VALID_KINDS:
                kind = "fact"
            confidence = 0.9 if segment.kind_hint else (0.6 if segment.signal_level in {"meta", "low"} else 0.72)
            risk = "high" if secret_hit else ("medium" if segment.truncated or segment.signal_level in {"meta", "low"} else "low")
            candidate_id = _digest("candidate", extract_id, index, segment.locator, body)[:24]
            blob_id = self.content.put_blob(body)
            if not blob_id:
                continue
            meta = {
                "schema": "v2-extraction-candidate-1",
                "extract_id": extract_id,
                "candidate_id": candidate_id,
                "scope_digest": _scope_digest(scope),
                "source_reference": source_reference,
                "source_digest": source_digest,
                "source_root_id": str(getattr(root, "root_id", "") or ""),
                "locator": str(segment.locator or ""),
                "title": str(segment.title or "")[:160],
                "kind": kind,
                "confidence": confidence,
                "risk_level": risk,
                "secret_redacted": bool(secret_hit),
                "created_at": _now(),
            }
            self._put_record(
                source_table=_CANDIDATE_SOURCE,
                source_pk=f"{extract_id}:{candidate_id}",
                record_type="memory_candidate",
                blob_id=blob_id,
                status="staged",
                metadata=meta,
                preserve_terminal=True,
            )
            candidates.append({
                "candidate_id": candidate_id,
                "kind": kind,
                "confidence": confidence,
                "risk_level": risk,
                "preview": body[:200],
                "locator": str(segment.locator or ""),
                "secret_redacted": bool(secret_hit),
            })
        return {
            "ok": True,
            "extract_id": extract_id,
            "source_reference": source_reference,
            "candidates": candidates,
            "total": len(candidates),
            "staging": "v2_content_plane",
        }

    def accept(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        scope = _scope(context)
        extract_id = str(payload.get("extract_id") or "").strip()
        raw_ids = payload.get("candidate_ids")
        if not extract_id:
            raise NativePortError("extract_id_required")
        if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
            raise NativePortError("candidate_ids_required")
        candidate_ids = [str(item).strip() for item in raw_ids]
        if any(not item for item in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids):
            raise NativePortError("candidate_ids_invalid")
        rows = [
            row for row in self._records(_CANDIDATE_SOURCE, scope)
            if str(row["metadata"].get("extract_id") or "") == extract_id
        ]
        by_id = {str(row["metadata"].get("candidate_id") or ""): row for row in rows}
        missing = [item for item in candidate_ids if item not in by_id]
        if missing:
            raise NativePortError("candidate_not_found")
        # Validate the whole batch before the first memory write.  Each write is
        # independently idempotent, so an unexpected process failure is safely
        # retryable without duplicating already-accepted atoms.
        for candidate_id in candidate_ids:
            row = by_id[candidate_id]
            if row["status"] not in {"staged", "accepted"} or not row["body"]:
                raise NativePortError("candidate_not_acceptible")
        results: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            row = by_id[candidate_id]
            meta = dict(row["metadata"])
            memory_id = str(meta.get("memory_id") or f"extract-{candidate_id}")
            atom = MemoryAtom(
                memory_id=memory_id,
                body=row["body"],
                kind=str(meta.get("kind") or "fact"),
                status="active",
                confidence=float(meta.get("confidence", 0.7) or 0.7),
                injection_policy="relevant",
                workspace_id=str(scope["workspace_id"]),
                agent_instance_id=str(scope["agent_instance_id"]),
                share_group_id=str(scope["share_group_id"]),
                project_ref=str(scope["project_ref"]),
                provider=str(scope["provider"]),
                runtime_role=str(scope["runtime_role"]),
                metadata={
                    "origin": "native_v2_extract",
                    "extract_id": extract_id,
                    "candidate_id": candidate_id,
                    "source_reference": str(meta.get("source_reference") or ""),
                    "secret_redacted": bool(meta.get("secret_redacted")),
                },
                provenance=[{
                    "source": "content",
                    "source_ref": f"content:{row['blob_id']}",
                    "source_digest": str(meta.get("source_digest") or row["body_digest"]),
                }],
            )
            evidence = [{
                "source_ref": f"content:{row['blob_id']}",
                "revision": str(meta.get("source_digest") or ""),
                "digest": str(row["body_digest"] or hashlib.sha256(row["body"].encode("utf-8")).hexdigest()),
                "authority": "governance",
                "metadata": {
                    "extract_id": extract_id,
                    "candidate_id": candidate_id,
                    "source_reference": str(meta.get("source_reference") or ""),
                },
            }]
            mappings = [{
                "source_domain": "content",
                "source_ref": f"content:{row['blob_id']}",
                "source_record_id": row["record_id"],
                "source_revision": str(meta.get("source_digest") or ""),
                "digest": str(row["body_digest"] or ""),
                "metadata": {"extract_id": extract_id, "candidate_id": candidate_id},
            }]
            try:
                persisted, decision = self.governance.put_atom(
                    atom,
                    context={
                        **scope,
                        "actor": str(scope["agent_instance_id"]),
                        "authority": "manual",
                    },
                    evidence=evidence,
                    source_mappings=mappings,
                    reason="accepted extracted memory candidate",
                    confidence=1.0,
                    idempotency_key=f"accept_extract:{extract_id}:{candidate_id}",
                )
                # Runtime acceptance is a complete commit, not a shadow-stage
                # write. Project the reference-only evidence before exposing
                # the atom; set_visibility itself re-checks that proof.
                self.memory.project_evidence(self.governance.evidence)
                self.memory.set_visibility("active", atom_ids=[persisted.atom_id])
                persisted = self.memory.get_atom(
                    persisted.memory_id, scope=scope, include_building=True,
                ) or persisted
            except Exception as exc:
                raise NativePortError("v2_extract_accept_failed") from exc
            meta["memory_id"] = persisted.memory_id
            meta["accepted_at"] = meta.get("accepted_at") or _now()
            meta["decision_id"] = decision.decision_id
            self._update_record_status(row["record_id"], "accepted", meta)
            results.append({
                "candidate_id": candidate_id,
                "memory_id": persisted.memory_id,
                "kind": persisted.kind,
                "status": persisted.status,
                "decision_id": decision.decision_id,
            })
        return {
            "ok": True,
            "extract_id": extract_id,
            "accepted": results,
            "total": len(results),
            "storage": "v2_memory",
        }

    # ------------------------------------------------------------------
    # Host enrichment
    # ------------------------------------------------------------------
    @staticmethod
    def _needs_enrichment(atom: MemoryAtom) -> bool:
        try:
            from ..memory_ir import looks_english_text
            english = looks_english_text(str(atom.body or ""))
        except Exception:
            english = False
        confidence = float(atom.confidence or 0.5)
        return bool(english or confidence < 0.6 or (atom.kind == "fact" and confidence < 0.7))

    def _stage_enrichment_task(self, atom: MemoryAtom, scope: Mapping[str, Any]) -> str:
        body = str(atom.body or "").strip()
        title = body.splitlines()[0][:120] if body else atom.memory_id[:24]
        content_fp = _digest(atom.memory_id, atom.canonical_hash, body, atom.kind, atom.confidence)
        task_id = "enr-" + _digest("v2-enrichment", _scope_digest(scope), atom.memory_id, content_fp)[:20]
        blob_id = self.content.put_blob(body)
        if not blob_id:
            raise NativePortError("enrichment_body_required")
        meta = {
            "schema": "v2-enrichment-task-1",
            "task_id": task_id,
            "memory_id": atom.memory_id,
            "atom_id": atom.atom_id,
            "scope_digest": _scope_digest(scope),
            "content_fp": content_fp,
            "title": title,
            "kind_hint": atom.kind,
            "confidence": float(atom.confidence),
            "ops": ["classify", "translate"],
            "created_at": _now(),
        }
        self._put_record(
            source_table=_TASK_SOURCE,
            source_pk=task_id,
            record_type="enrichment_task",
            blob_id=blob_id,
            status="pending",
            metadata=meta,
            preserve_terminal=True,
        )
        return task_id

    def list_pending(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        scope = _scope(context)
        raw_limit = payload.get("limit", 50)
        if isinstance(raw_limit, bool):
            raise NativePortError("invalid_limit")
        try:
            limit = max(1, min(int(raw_limit), 100))
        except (TypeError, ValueError) as exc:
            raise NativePortError("invalid_limit") from exc
        tasks: list[dict[str, Any]] = []
        for row in self._records(_TASK_SOURCE, scope):
            if row["status"] != "pending":
                continue
            meta = row["metadata"]
            tasks.append({
                "task_id": str(meta.get("task_id") or row["source_pk"]),
                "memory_id": str(meta.get("memory_id") or ""),
                "ops": list(meta.get("ops") or ["classify", "translate"]),
                "input": {
                    "title": str(meta.get("title") or ""),
                    "body": row["body"][:500],
                    "kind_hint": str(meta.get("kind_hint") or "fact"),
                },
            })
            if len(tasks) >= limit:
                break
        return {
            "pending_count": len(tasks),
            "tasks": tasks,
            "next_step": "classify/translate then call memoryguard_apply_enrichments",
            "storage": "v2_content_plane",
        }

    def enrichment_status(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        del payload
        scope = _scope(context)
        rows = self._records(_TASK_SOURCE, scope)
        counts: dict[str, int] = {"pending": 0, "applied": 0, "other": 0}
        for row in rows:
            status = row["status"]
            if status in counts:
                counts[status] += 1
            else:
                counts["other"] += 1
        return {**counts, "total": len(rows), "mode": "v2_content_plane"}

    def build_and_enrich(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        del payload
        scope = _scope(context)
        atoms = self.memory.list_atoms(scope=scope, status="active")
        queued = 0
        for atom in atoms:
            if not self._needs_enrichment(atom):
                continue
            task_id = self._stage_enrichment_task(atom, scope)
            # Count as newly queued only if it is currently pending.  Idempotent
            # rebuilds keep an applied task terminal for the same content_fp.
            if any(row["source_pk"] == task_id and row["status"] == "pending" for row in self._records(_TASK_SOURCE, scope)):
                queued += 1
        pending = self.list_pending({"limit": 100}, context=context)
        return {
            "projection_built": True,
            "projection_mode": "v2_native_memory",
            "projection_separated_from_codegraph": True,
            "scoped_record_count": len(atoms),
            "queued_or_pending": queued,
            "pending_tasks": pending["tasks"],
            "host_action_required": bool(pending["tasks"]),
            "enrichment": {
                "pending_count": pending["pending_count"],
                "storage": "v2_content_plane",
            },
        }

    def apply_enrichments(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        scope = _scope(context)
        raw = payload.get("results")
        if not isinstance(raw, list) or not raw:
            raise NativePortError("enrichment_results_required")
        pending_rows = {row["source_pk"]: row for row in self._records(_TASK_SOURCE, scope) if row["status"] == "pending"}
        normalized: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for value in raw:
            if not isinstance(value, Mapping):
                raise NativePortError("invalid_enrichment_result")
            item = dict(value)
            task_id = str(item.get("task_id") or "").strip()
            row = pending_rows.get(task_id)
            if row is None:
                raise NativePortError("enrichment_task_not_found")
            kind = str(item.get("kind") or "").strip()
            title = str(item.get("title") or "").strip()
            body = str(item.get("body") or "").strip()
            if kind not in _VALID_KINDS or not title or not body:
                raise NativePortError("invalid_enrichment_result")
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError) as exc:
                raise NativePortError("invalid_enrichment_result") from exc
            if not 0.0 <= confidence <= 1.0:
                raise NativePortError("invalid_enrichment_result")
            item["confidence"] = confidence
            normalized.append((item, row))
        applied: list[dict[str, Any]] = []
        for item, row in normalized:
            meta = dict(row["metadata"])
            memory_id = str(meta.get("memory_id") or "")
            current = self.memory.get_atom(memory_id, scope=scope)
            if current is None:
                raise NativePortError("enrichment_memory_not_found")
            body = str(item["body"])
            title = str(item["title"])
            new_body = body if body.startswith(title) else f"{title}\n\n{body}".strip()
            updated = MemoryAtom.from_value(current)
            updated.body = new_body
            updated.kind = str(item["kind"])
            updated.confidence = float(item["confidence"])
            updated.metadata = {
                **dict(updated.metadata),
                "enrichment_mode": "host",
                "enrichment_task_id": str(item["task_id"]),
                "enrichment_rationale_digest": _digest(str(item.get("rationale") or "")),
            }
            try:
                persisted, decision = self.governance.put_atom(
                    updated,
                    context={**scope, "actor": str(scope["agent_instance_id"]), "authority": "manual"},
                    reason="host enrichment applied",
                    confidence=float(item["confidence"]),
                    idempotency_key=f"apply_enrichment:{item['task_id']}:{meta.get('content_fp','')}",
                )
            except Exception as exc:
                raise NativePortError("v2_enrichment_apply_failed") from exc
            meta["applied_at"] = _now()
            meta["decision_id"] = decision.decision_id
            meta["result"] = {
                "kind": persisted.kind,
                "title": title,
                "confidence": persisted.confidence,
                "rationale_digest": _digest(str(item.get("rationale") or "")),
            }
            self._update_record_status(row["record_id"], "applied", meta)
            applied.append({
                "task_id": str(item["task_id"]),
                "memory_id": persisted.memory_id,
                "decision_id": decision.decision_id,
                "kind": persisted.kind,
                "confidence": persisted.confidence,
            })
        return {
            "applied": len(applied),
            "rejected": 0,
            "results": applied,
            "rebuild_suggested": False,
            "storage": "v2_memory",
        }

    def dispatch(self, operation: str, payload: Any = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        data = dict(payload or {}) if isinstance(payload, Mapping) else {}
        handlers = {
            "extract": self.extract,
            "memoryguard_extract_memories": self.extract,
            "accept": self.accept,
            "memoryguard_accept_candidates": self.accept,
            "list_pending": self.list_pending,
            "memoryguard_list_pending_enrichments": self.list_pending,
            "status": self.enrichment_status,
            "memoryguard_enrichment_status": self.enrichment_status,
            "build": self.build_and_enrich,
            "memoryguard_build_and_enrich": self.build_and_enrich,
            "apply": self.apply_enrichments,
            "memoryguard_apply_enrichments": self.apply_enrichments,
        }
        handler = handlers.get(str(operation or ""))
        if handler is None:
            return {"ok": False, "status": "error", "code": "unknown_extraction_operation", "error": "unknown_extraction_operation"}
        try:
            result = handler(data, context=context)
            return {"ok": True, "status": "ok", "operation": operation, "data": result}
        except NativePortError as exc:
            return {"ok": False, "status": "error", "operation": operation, "code": exc.code, "error": exc.code}
        except Exception:
            return {"ok": False, "status": "error", "operation": operation, "code": "v2_extraction_service_failed", "error": "v2_extraction_service_failed"}


__all__ = ["NativeExtractionEnrichmentService"]
