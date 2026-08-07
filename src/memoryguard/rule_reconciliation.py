"""Canonical rule reconciliation: durable scope-bundle jobs + recoverable saga.

The legacy governance layer stores one ``always`` record per governed rule.
Multiple records frequently express *one* obligation for the same audience
(two caveman/rtk rules shared by the whole group, three Codex-specific rules,
a project rule).  Reconciliation folds those records into **canonical
bundles**::

    shared_baseline   one canonical record/definition for the group-wide rules
    agent_overlay     one canonical record/definition per provider/agent scope
    project_overlay   one canonical record/definition per project scope
    kept_separate     standalone sources that stay active with their own def

Every folded source keeps a durable Source Link to its canonical Definition
and is shadowed (recoverable, never deleted).  ``kept_separate`` sources stay
active.  A **recoverable saga** (``RuleReconciliationService.run``) builds the
canonical layer, activates the group-level canonical read path, then shadows
the old duplicates -- never the other way around -- and persists its phase in
``rule_reconciliation_jobs`` so any failure is retryable from the exact phase
that failed.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace as replace_dataclass
from pathlib import Path
from typing import Any, Iterable

from .rule_binding import AUTO_ALLOWED_TARGET_TYPES, build_binding
from .rule_definition import build_definition
from .rule_evidence import build_evidence
from .rule_merge_store import RuleMergeStore
from .rule_scope import canonical_project_ref
from .schema_v3 import (
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
    stable_hash,
)

# Persisted job lifecycle (Req2).  ``superseded`` marks a job whose source
# state was replaced by a newer reconciliation run; ``terminal_failed`` is
# never reachable automatically -- the saga only ever lands on
# ``retryable_failed`` so the retry button is never hidden.
JOB_STATUSES = frozenset({
    "pending_model", "model_running", "staged", "applying",
    "retryable_failed", "terminal_failed", "canonical_ready", "superseded",
})

# Saga phase order (Req5): legacy stays active until the canonical layer is
# built and parity-checked, then the group-level canonical read activates, then
# old duplicates are shadowed.  Never shadow before building.
SAGA_PHASES = (
    "model", "stage", "backfill_p3", "write_canonical",
    "verify_source_links", "drain_outbox", "build_projection",
    "activate_canonical", "verify_readiness", "shadow_legacy",
    "retire_previous", "canonical_ready",
)

BUNDLE_KINDS = ("shared_baseline", "agent_overlay", "project_overlay")

# Req6: the migration path for a group whose only enrichment history is legacy
# heuristic (classify/translate) and which has never been scope-bundled.
REASON_LEGACY_HEURISTIC = "legacy_heuristic_enrichment_missing_scope_bundle"
REASON_SCOPE_BUNDLE = "scope_bundle"

_JOB_FIELDS = frozenset({
    "status", "phase", "attempt_count", "model_mode", "result_json",
    "canonical_digest_before", "canonical_digest_after", "projection_version",
    "last_error", "reason",
})


class RuleReconciliationStore:
    """Durable job + group-activation state on top of the RuleMergeStore DB.

    Every read/write goes through the same transaction-aware connection
    helpers as backfill (``_write_conn`` / ``_read_conn``), so a read inside an
    active write transaction reuses that transaction's connection instead of
    opening a second one (which would self-lock against ``BEGIN IMMEDIATE``).
    """

    def __init__(self, store: RuleMergeStore):
        self.store = store

    # ------------------------------------------------------------------ jobs

    @staticmethod
    def job_id_for(share_group_id: str, source_digest: str) -> str:
        return stable_hash("reconcile-job", share_group_id, source_digest)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.store._read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM rule_reconciliation_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_job(self, share_group_id: str) -> dict[str, Any] | None:
        with self.store._read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM rule_reconciliation_jobs "
                "WHERE share_group_id=? "
                "ORDER BY created_at DESC, updated_at DESC LIMIT 1",
                (share_group_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_jobs(
        self, share_group_id: str | None = None, status: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rule_reconciliation_jobs WHERE 1=1"
        params: list[Any] = []
        if share_group_id:
            sql += " AND share_group_id=?"
            params.append(share_group_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at, updated_at"
        with self.store._read_conn() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def create_job(
        self,
        *,
        share_group_id: str,
        source_digest: str,
        reason: str = "",
        model_mode: str = "",
        result_json: str = "",
        status: str = "pending_model",
        phase: str = "model",
    ) -> dict[str, Any]:
        """Idempotently create (or return) the job for a source state."""
        job_id = self.job_id_for(share_group_id, source_digest or "")
        now = _now_iso()
        with self.store._write_conn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM rule_reconciliation_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO rule_reconciliation_jobs (
                        job_id, share_group_id, source_digest, status, phase,
                        attempt_count, model_mode, result_json,
                        canonical_digest_before, canonical_digest_after,
                        projection_version, last_error, reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (job_id, share_group_id, source_digest or "", status, phase,
                     0, model_mode or "", result_json or "", "", "", "", "",
                     reason or "", now, now),
                )
        job = self.get_job(job_id)
        if job is None:  # pragma: no cover - write above cannot silently miss
            raise RuntimeError("reconciliation job write failed")
        return job

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - _JOB_FIELDS
        if unknown:
            raise ValueError(
                f"unknown reconciliation job field: {sorted(unknown)[0]}"
            )
        if not fields:
            return self.get_job(job_id)  # type: ignore[return-value]
        assignments = ", ".join(f"{key}=?" for key in fields)
        params = list(fields.values()) + [_now_iso(), job_id]
        with self.store._write_conn() as conn:
            conn.execute(
                f"UPDATE rule_reconciliation_jobs SET {assignments}, "
                f"updated_at=? WHERE job_id=?",
                params,
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def transition(
        self,
        job_id: str,
        *,
        status: str | None = None,
        phase: str | None = None,
        attempt_count: int | None = None,
        last_error: str | None = None,
        result_json: str | None = None,
        canonical_digest_before: str | None = None,
        canonical_digest_after: str | None = None,
        projection_version: str | None = None,
        model_mode: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if status is not None:
            fields["status"] = status
        if phase is not None:
            fields["phase"] = phase
        if attempt_count is not None:
            fields["attempt_count"] = int(attempt_count)
        if last_error is not None:
            fields["last_error"] = last_error
        if result_json is not None:
            fields["result_json"] = result_json
        if canonical_digest_before is not None:
            fields["canonical_digest_before"] = canonical_digest_before
        if canonical_digest_after is not None:
            fields["canonical_digest_after"] = canonical_digest_after
        if projection_version is not None:
            fields["projection_version"] = projection_version
        if model_mode is not None:
            fields["model_mode"] = model_mode
        if reason is not None:
            fields["reason"] = reason
        return self.update_job(job_id, **fields)

    # ------------------------------------------------- group canonical state

    def set_canonical_activation(
        self,
        share_group_id: str,
        *,
        activation_status: str,
        canonical_digest: str,
        read_path: str,
        activated_at: str = "",
    ) -> dict[str, Any]:
        now = _now_iso()
        with self.store._write_conn() as conn:
            conn.execute(
                """
                INSERT INTO rule_canonical_state (
                    share_group_id, activation_status, canonical_digest,
                    read_path, activated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(share_group_id) DO UPDATE SET
                    activation_status=excluded.activation_status,
                    canonical_digest=excluded.canonical_digest,
                    read_path=excluded.read_path,
                    activated_at=CASE
                        WHEN excluded.activated_at = ''
                        THEN rule_canonical_state.activated_at
                        ELSE excluded.activated_at
                    END,
                    updated_at=excluded.updated_at
                """,
                (share_group_id, activation_status or "", canonical_digest or "",
                 read_path or "legacy", activated_at or "", now),
            )
        activation = self.canonical_activation(share_group_id)
        if activation is None:  # pragma: no cover
            raise RuntimeError("canonical activation write failed")
        return activation

    def canonical_activation(
        self, share_group_id: str,
    ) -> dict[str, Any] | None:
        with self.store._read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM rule_canonical_state WHERE share_group_id=?",
                (share_group_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def clear_canonical_activation(self, share_group_id: str) -> None:
        with self.store._write_conn() as conn:
            conn.execute(
                "DELETE FROM rule_canonical_state WHERE share_group_id=?",
                (share_group_id,),
            )


# ---------------------------------------------------------------------------
# Scope bundles (Req3 / Req4)
# ---------------------------------------------------------------------------


@dataclass
class ScopeBundle:
    """One canonical bundle: sources folded into a single canonical rule."""

    bundle_kind: str  # shared_baseline | agent_overlay | project_overlay
    source_memory_ids: list[str]
    priority: int
    body: str
    project_ref: str = ""
    provider: str = ""
    runtime_role: str = ""
    effect: str = "include"
    definition_id: str = ""
    canonical_memory_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_kind": self.bundle_kind,
            "source_memory_ids": sorted(self.source_memory_ids),
            "priority": self.priority,
            "body": self.body,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
            "effect": self.effect,
            "definition_id": self.definition_id,
            "canonical_memory_id": self.canonical_memory_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScopeBundle":
        return cls(
            bundle_kind=str(data.get("bundle_kind", "shared_baseline")),
            source_memory_ids=sorted(str(x) for x in data.get("source_memory_ids", [])),
            priority=int(data.get("priority", 0) or 0),
            body=str(data.get("body", "") or ""),
            project_ref=str(data.get("project_ref", "") or ""),
            provider=str(data.get("provider", "") or ""),
            runtime_role=str(data.get("runtime_role", "") or ""),
            effect=str(data.get("effect", "include") or "include"),
            definition_id=str(data.get("definition_id", "") or ""),
            canonical_memory_id=str(data.get("canonical_memory_id", "") or ""),
        )


def _assignments_to_scope(assignments: Iterable[Any]) -> dict[str, Any]:
    """Stable scope signature for a record's audience (Req3/Req4).

    ``priority`` is deliberately excluded (Req4): 100/20/10 on the same
    audience must collapse to one bundle carrying the max priority.
    """
    entries: list[tuple[Any, ...]] = []
    project_refs: set[str] = set()
    providers: set[str] = set()
    runtime_roles: set[str] = set()
    agent_ids: set[str] = set()
    target_types: set[str] = set()
    effects: set[str] = set()
    for assignment in assignments:
        target_type = str(getattr(assignment, "target_type", "agent") or "agent")
        target_id = str(getattr(assignment, "target_id", "") or "")
        project_ref = canonical_project_ref(
            getattr(assignment, "project_ref", "") or ""
        )
        provider = str(getattr(assignment, "provider", "") or "").casefold()
        if not provider and target_type == "provider":
            provider = target_id.casefold()
        runtime_role = str(
            getattr(assignment, "runtime_role", "") or ""
        ).casefold()
        if not runtime_role and target_type == "runtime_role":
            runtime_role = target_id.casefold()
        effect = str(getattr(assignment, "effect", "include") or "include")
        entries.append(
            (target_type, target_id, project_ref, provider, runtime_role, effect)
        )
        if project_ref:
            project_refs.add(project_ref)
        if provider:
            providers.add(provider)
        if runtime_role:
            runtime_roles.add(runtime_role)
        if target_type == "agent" and target_id:
            agent_ids.add(target_id)
        target_types.add(target_type)
        effects.add(effect)
    key = stable_hash(
        "reconcile-scope",
        json.dumps(sorted(set(entries)), ensure_ascii=False, sort_keys=True),
    )
    return {
        "key": key,
        "entries": entries,
        "project_refs": project_refs,
        "providers": providers,
        "runtime_roles": runtime_roles,
        "agent_ids": agent_ids,
        "target_types": target_types,
        "effects": effects,
    }


def _classify_scope(
    scope: dict[str, Any], group_agent_ids: set[str],
) -> str:
    """Classify a scope group: project | agent_overlay | shared_baseline."""
    if scope["project_refs"]:
        return "project"
    if scope["providers"] or scope["runtime_roles"]:
        return "agent_overlay"
    if scope["agent_ids"] and scope["agent_ids"] != group_agent_ids:
        return "agent_overlay"
    return "shared_baseline"


def _bundle_body(source_records: list[Any]) -> str:
    bodies = [
        str(record.body or "").strip() for record in source_records if record.body
    ]
    if not bodies:
        return ""
    if len(bodies) == 1:
        return bodies[0]
    return "\n".join(
        f"[{i}] {body}" for i, body in enumerate(bodies, 1)
    )


def build_bundles(
    store: RuleMergeStore,
    legacy: Any,
    share_group_id: str,
    source_records: list[Any],
    *,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Deterministic heuristic bundle plan for a group's active always records.

    This is the offline fallback the model task (``batch_bundle_via_cli``) can
    replace.  The real-snapshot acceptance drives the saga with an explicit
    scripted plan; this builder exists so the layer works without an LLM.
    """
    group_agent_ids = set()
    binding_workspace = workspace or (store.workspace if store is not None else None)
    if binding_workspace is not None:
        try:
            from .agent_binding import AgentBindingStore
            group_agent_ids = {
                str(binding.agent_instance_id or "")
                for binding in AgentBindingStore(binding_workspace).find_by_group(
                    share_group_id, include_inactive=False,
                )
            }
        except Exception:  # noqa: BLE001 - binding store must never break planning
            group_agent_ids = set()
    grouped: dict[str, dict[str, Any]] = {}
    for record in source_records:
        scope = _assignments_to_scope(legacy.list_rule_assignments(record.memory_id))
        grouped.setdefault(scope["key"], {"scope": scope, "sources": []})
        grouped[scope["key"]]["sources"].append(record)

    bundles: list[ScopeBundle] = []
    kept_separate: list[str] = []
    for group in grouped.values():
        scope = group["scope"]
        sources = sorted(group["sources"], key=lambda record: record.memory_id)
        classification = _classify_scope(scope, group_agent_ids)
        if classification == "project":
            if len(sources) == 1:
                # Standalone project rule with no sibling to merge with: it
                # keeps its own active definition (never folded into a shared
                # baseline, which would widen the project audience).
                kept_separate.append(sources[0].memory_id)
                continue
            kind = "project_overlay"
            project_ref = sorted(scope["project_refs"])[0]
            provider = sorted(scope["providers"])[0] if scope["providers"] else ""
            runtime_role = sorted(scope["runtime_roles"])[0] if scope["runtime_roles"] else ""
        elif classification == "agent_overlay":
            kind = "agent_overlay"
            project_ref = ""
            provider = sorted(scope["providers"])[0] if scope["providers"] else ""
            runtime_role = sorted(scope["runtime_roles"])[0] if scope["runtime_roles"] else ""
        else:
            kind = "shared_baseline"
            project_ref = ""
            provider = ""
            runtime_role = ""
        bundles.append(ScopeBundle(
            bundle_kind=kind,
            source_memory_ids=[record.memory_id for record in sources],
            priority=max(int(record.priority or 0) for record in sources),
            body=_bundle_body(sources),
            project_ref=project_ref,
            provider=provider,
            runtime_role=runtime_role,
        ))
    return {"bundles": bundles, "kept_separate": kept_separate}


def validate_bundles(
    bundle_plan: dict[str, Any],
    source_ids: Iterable[str],
) -> None:
    """Req3 validation: every source in exactly one place, no illegal merges."""
    bundles = [
        bundle if isinstance(bundle, ScopeBundle)
        else ScopeBundle.from_dict(bundle)
        for bundle in bundle_plan.get("bundles", [])
    ]
    kept_separate = [str(x) for x in bundle_plan.get("kept_separate", [])]
    known = {str(source_id) for source_id in source_ids}
    assigned: list[str] = []
    for bundle in bundles:
        assigned.extend(str(x) for x in bundle.source_memory_ids)
    assigned.extend(kept_separate)

    counts = Counter(assigned)
    duplicated = sorted(sid for sid, count in counts.items() if count != 1)
    if duplicated:
        raise ValueError(f"source_assigned_multiple_times: {duplicated}")
    missing = sorted(known - set(assigned))
    if missing:
        raise ValueError(f"source_not_assigned: {missing}")
    if assigned and set(assigned) != known:
        raise ValueError("source_assignment_set_mismatch")

    # No cross-project_ref merge, no cross-effect merge, and heuristic output
    # is never accepted as a scope_bundle plan (caller sets model_mode).
    for bundle in bundles:
        if bundle.bundle_kind not in BUNDLE_KINDS:
            raise ValueError(f"invalid_bundle_kind: {bundle.bundle_kind}")
        if not bundle.body.strip():
            raise ValueError(
                f"empty_bundle_body: {sorted(bundle.source_memory_ids)}"
            )
        if bundle.bundle_kind == "project_overlay" and not bundle.project_ref:
            raise ValueError("project_overlay_without_project_ref")


def _is_deterministic_safe_plan(
    bundle_plan: dict[str, Any], source_records: Iterable[Any],
) -> bool:
    """True only for structurally identical, risk-free deduplication.

    A heuristic plan may still be used when it only keeps sources separate or
    folds sources whose body and audience are already identical.  Any semantic
    merge that rewrites a combined body requires a real model bundle.
    """
    records = {str(record.memory_id): record for record in source_records}
    for raw in bundle_plan.get("bundles", []):
        bundle = raw if isinstance(raw, ScopeBundle) else ScopeBundle.from_dict(raw)
        source_ids = [str(x) for x in bundle.source_memory_ids]
        if len(source_ids) <= 1:
            continue
        bodies = [
            str(records[sid].body or "").strip()
            for sid in source_ids if sid in records
        ]
        if not bodies or any(body != bodies[0] for body in bodies):
            return False
    return True


def _normalize_bundle_plan(bundle_plan: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical dict form of a bundle plan."""
    bundles = [
        bundle if isinstance(bundle, ScopeBundle) else ScopeBundle.from_dict(bundle)
        for bundle in bundle_plan.get("bundles", [])
    ]
    return {
        "bundles": [bundle.to_dict() for bundle in bundles],
        "kept_separate": [
            str(x) for x in bundle_plan.get("kept_separate", [])
        ],
    }


# ---------------------------------------------------------------------------
# Reconciliation service (Req5 saga)
# ---------------------------------------------------------------------------


class RuleReconciliationService:
    """Recoverable canonical-reconciliation saga for one shared group.

    Every phase is persisted on the job row.  Any exception lands the job in
    ``retryable_failed`` with the real failed phase + attempt_count bumped;
    ``run()`` resumes from there.  ``canonical_ready`` is only reached through
    the final readiness gate.
    """

    def __init__(self, store: RuleMergeStore, workspace: str | Path | None = None):
        self.store = store
        self.workspace = (
            Path(workspace).resolve()
            if workspace is not None
            else store.workspace
        )
        self.jobs = RuleReconciliationStore(store)
        self._legacy_cache: dict[str, Any] = {}

    def _legacy(self, share_group_id: str) -> Any:
        from .shared_memory_store import SharedMemoryStore
        if share_group_id not in self._legacy_cache:
            self._legacy_cache[share_group_id] = SharedMemoryStore(
                self.workspace, share_group_id,
            )
        return self._legacy_cache[share_group_id]

    # ------------------------------------------------------------- digests

    def source_digest(self, share_group_id: str) -> str:
        legacy = self._legacy(share_group_id)
        records = [
            record for record in legacy.list_records()
            if str(record.injection_policy or "") == "always"
            and str(getattr(record.status, "value", record.status) or "") in {
                SharedMemoryStatus.ACTIVE.value, SharedMemoryStatus.SHADOWED.value,
            }
            and not str(getattr(record, "dedup_domain", "") or "").startswith(
                "canonical:"
            )
        ]
        payload: list[Any] = []
        for record in sorted(records, key=lambda record: record.memory_id):
            assignments = sorted(
                (
                    a.target_type, a.target_id, a.project_ref,
                    str(getattr(a, "provider", "") or "").casefold(),
                    str(getattr(a, "runtime_role", "") or "").casefold(),
                    a.effect,
                    int(
                        getattr(a, "priority_override", None)
                        if getattr(a, "priority_override", None) is not None
                        else (getattr(a, "priority", 0) or 0)
                    ),
                )
                for a in legacy.list_rule_assignments(record.memory_id)
            )
            payload.append({
                "memory_id": record.memory_id,
                "body": record.body or "",
                "kind": str(getattr(record.kind, "value", record.kind) or ""),
                "confidence": float(record.confidence or 0.0),
                "priority": int(record.priority or 0),
                "agent_instance_id": record.agent_instance_id or "",
                "assignments": assignments,
            })
        return stable_hash(
            "reconcile-source-digest",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )

    def canonical_digest(self, share_group_id: str) -> str:
        bindings = sorted(
            (binding.to_dict() for binding in self.store.list_bindings(
                share_group_id=share_group_id, status="active",
            )),
            key=lambda item: str(item.get("binding_id", "")),
        )
        group_definition_ids = {
            str(item.get("definition_id", "")) for item in bindings
        }
        for link in self.store.list_source_links(
            share_group_id=share_group_id, status="active",
        ):
            group_definition_ids.add(
                str(link.get("canonical_definition_id") or "") or ""
            )
            group_definition_ids.add(
                str(link.get("original_definition_id") or "") or ""
            )
        definitions = sorted(
            (
                definition.to_dict()
                for definition in self.store.list_definitions(status="active")
                if str(definition.definition_id or "") in group_definition_ids
            ),
            key=lambda item: str(item.get("definition_id", "")),
        )
        definition_ids = {
            str(item.get("definition_id", "")) for item in definitions
        }
        links = sorted(
            (
                link["memory_id"], link["source_revision"],
                link["original_definition_id"], link["canonical_definition_id"],
                link["status"], link["updated_at"],
            )
            for link in self.store.list_source_links(
                share_group_id=share_group_id, status="active",
            )
        )
        contributions: list[tuple[Any, ...]] = []
        with self.store._read_conn() as conn:
            rows = conn.execute(
                "SELECT share_group_id, source_memory_id, source_revision, "
                "legacy_assignment_hash, definition_id, binding_id, target_type, "
                "target_id, project_ref, provider, runtime_role, effect, priority, "
                "owner_agent_id, active, status, revision, updated_at "
                "FROM rule_binding_contributions "
                "WHERE share_group_id=? AND active=1 "
                "ORDER BY contribution_id",
                (share_group_id,),
            ).fetchall()
            contributions = [tuple(row) for row in rows]
        evidence = sorted(
            (
                item.to_dict() for item in self.store.list_evidence()
                if item.definition_id in definition_ids
            ),
            key=lambda item: str(item.get("evidence_id", "")),
        )
        payload = {
            "definitions": definitions,
            "bindings": bindings,
            "contributions": contributions,
            "source_links": links,
            "evidence": evidence,
        }
        return stable_hash(
            "canonical-digest",
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        )

    def _projection_version(self, share_group_id: str) -> str:
        from .governance_scope import (
            GovernanceScope,
            share_group_projection_path,
        )
        path = share_group_projection_path(
            self.workspace, GovernanceScope(
                mode="share_group", share_group_id=share_group_id,
            ),
        )
        if not path.exists():
            return ""
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

    # ------------------------------------------------------------- helpers

    def _start_or_resume_job(
        self,
        share_group_id: str,
        *,
        reason: str,
        model_mode: str,
    ) -> dict[str, Any]:
        latest = self.jobs.latest_job(share_group_id)
        digest = self.source_digest(share_group_id)

        # A ready generation short-circuits only when the persisted input
        # digest, canonical digest and projection all still match.  If any of
        # them moved, a new generation is created while the old canonical
        # layer continues to serve until the new one verifies.
        if latest is not None and latest["status"] == "canonical_ready":
            status = canonical_reconciliation_status(
                self.workspace, share_group_id, store=self.store,
            )
            if status["canonical_ready"]:
                return latest
            drift_failures = {
                "source_digest_changed",
                "canonical_digest_drift",
                "projection_version_drift",
                "binding_multiset_mismatch",
            }
            if not drift_failures.intersection(status.get("failures") or []):
                # Non-source readiness noise (outbox/projection lag) must not
                # mint a pointless new generation.
                return latest

        # Resume the same generation in-place.  The persisted source_digest is
        # the fixed input snapshot; partial canonical writes must not change it.
        job = self.jobs.get_job(
            RuleReconciliationStore.job_id_for(share_group_id, digest)
        )
        if job is not None and job["status"] not in {
            "superseded", "terminal_failed",
        }:
            return job

        # Source state genuinely changed (or the prior generation was retired):
        # retire stale unfinished jobs, then mint the new generation.
        for old in self.jobs.list_jobs(share_group_id=share_group_id):
            if old["status"] in {
                "pending_model", "model_running", "staged",
                "applying", "retryable_failed",
            }:
                self.jobs.transition(
                    old["job_id"], status="superseded",
                    last_error="superseded_by_new_generation",
                )
        return self.jobs.create_job(
            share_group_id=share_group_id,
            source_digest=digest,
            reason=reason or REASON_SCOPE_BUNDLE,
            model_mode=model_mode or "",
        )

    def _union_assignments(
        self, legacy: Any, source_ids: Iterable[str],
    ) -> list[Any]:
        seen: dict[tuple[Any, ...], Any] = {}
        out: list[Any] = []
        for source_id in source_ids:
            source_record = legacy.get_record(source_id)
            source_priority = int(source_record.priority or 0) \
                if source_record is not None else 0
            for assignment in legacy.list_rule_assignments(source_id):
                override = getattr(assignment, "priority_override", None)
                priority = int(
                    override if override is not None else source_priority
                )
                if override is None:
                    assignment = replace_dataclass(
                        assignment, priority_override=priority,
                    )
                target_type = str(getattr(assignment, "target_type", "agent") or "agent")
                target_id = str(getattr(assignment, "target_id", "") or "")
                provider = str(getattr(assignment, "provider", "") or "").casefold()
                if not provider and target_type == "provider":
                    provider = target_id.casefold()
                runtime_role = str(
                    getattr(assignment, "runtime_role", "") or ""
                ).casefold()
                if not runtime_role and target_type == "runtime_role":
                    runtime_role = target_id.casefold()
                key = (
                    target_type, target_id,
                    getattr(assignment, "project_ref", "") or "",
                    provider,
                    runtime_role,
                    getattr(assignment, "effect", "include") or "include",
                )
                if key in seen:
                    existing = seen[key]
                    existing_priority = int(
                        getattr(existing, "priority_override", None)
                        if getattr(existing, "priority_override", None) is not None
                        else source_priority
                    )
                    if priority <= existing_priority:
                        continue
                    out.remove(existing)
                seen[key] = assignment
                out.append(assignment)
        return out

    def _binding_from_assignment(
        self,
        definition_id: str,
        assignment: Any,
        *,
        share_group_id: str,
        owner_agent_id: str = "",
        record_priority: int = 0,
    ) -> Any:
        """Mirror of ``RuleMergeService._binding_from_assignment``.

        Wide legacy assignments (group/provider/project/system) are copied
        losslessly as ``migration``-sourced bindings, exactly as backfill does,
        so canonical definitions keep the audience the legacy layer granted.
        """
        from .rule_binding import RuleBinding
        target_type = str(getattr(assignment, "target_type", "agent") or "agent")
        created_by = "migration"
        authorization = json.dumps({
            "canonical_reconciliation": True,
            "legacy_assignment_hash": stable_hash(
                "legacy-assignment", share_group_id,
                str(getattr(assignment, "target_type", "") or ""),
                str(getattr(assignment, "target_id", "") or ""),
                canonical_project_ref(
                    str(getattr(assignment, "project_ref", "") or "")
                ),
                str(getattr(assignment, "provider", "") or "").casefold(),
                str(getattr(assignment, "runtime_role", "") or "").casefold(),
                str(getattr(assignment, "effect", "include") or "include"),
                str(int(
                    getattr(assignment, "priority_override", None)
                    if getattr(assignment, "priority_override", None) is not None
                    else record_priority
                )),
            ),
        }, ensure_ascii=False, sort_keys=True)
        target_id = str(getattr(assignment, "target_id", "") or "")
        provider = str(getattr(assignment, "provider", "") or "")
        runtime_role = str(getattr(assignment, "runtime_role", "") or "")
        if not provider and target_type == "provider":
            provider = target_id
        if not runtime_role and target_type == "runtime_role":
            runtime_role = target_id
        binding = build_binding(
            definition_id,
            share_group_id=share_group_id,
            target_type=target_type,
            target_id=target_id,
            project_ref=getattr(assignment, "project_ref", ""),
            provider=provider,
            runtime_role=runtime_role,
            effect=getattr(assignment, "effect", "include"),
            priority=int(
                getattr(assignment, "priority_override", None)
                if getattr(assignment, "priority_override", None) is not None
                else record_priority
            ),
            owner_agent_id=owner_agent_id or str(
                getattr(assignment, "agent_instance_id", "") or ""
            ),
            created_by=created_by,
            authorization=authorization,
        )
        if not isinstance(binding, RuleBinding):  # pragma: no cover
            raise TypeError("binding construction failed")
        return binding

    def _write_canonical(
        self, share_group_id: str, legacy: Any, bundle_plan: dict[str, Any],
    ) -> dict[str, Any]:
        bundles = [
            bundle if isinstance(bundle, ScopeBundle)
            else ScopeBundle.from_dict(bundle)
            for bundle in bundle_plan.get("bundles", [])
        ]
        kept_separate = bundle_plan.get("kept_separate", [])
        for bundle in bundles:
            definition = build_definition(
                bundle.body, kind=MemoryKind.PROCEDURE, confidence=1.0,
            )
            bundle.definition_id = definition.definition_id
            self.store.upsert_definition(definition)

            canonical_memory_id = stable_hash(
                "canonical-record", share_group_id, bundle.bundle_kind,
                bundle.project_ref or bundle.provider or bundle.runtime_role or "all",
                json.dumps(sorted(bundle.source_memory_ids), ensure_ascii=False),
            )
            bundle.canonical_memory_id = canonical_memory_id
            now = _now_iso()
            record = SharedMemoryRecord(
                memory_id=canonical_memory_id,
                body=bundle.body,
                kind=MemoryKind.PROCEDURE,
                status=SharedMemoryStatus.ACTIVE,
                injection_policy="always",
                priority=bundle.priority,
                agent_instance_id="",
                created_at=now,
                updated_at=now,
            )
            union_assignments = self._union_assignments(
                legacy, bundle.source_memory_ids,
            )
            legacy.append_record(
                record,
                assignments=[
                    a.to_dict() if hasattr(a, "to_dict") else dict(a)
                    for a in union_assignments
                ],
                dedup_domain=f"canonical:{share_group_id}:{canonical_memory_id}",
            )
            # P3 bindings for the canonical definition, source-owned via the
            # canonical record (the read path fail-closes when an active
            # source link resolves to a Definition with no group binding).
            bindings = [
                self._binding_from_assignment(
                    definition.definition_id, assignment,
                    share_group_id=share_group_id,
                    record_priority=int(bundle.priority or 0),
                )
                for assignment in union_assignments
            ]
            self.store.replace_source_contributions(
                share_group_id, canonical_memory_id, bindings,
                source_revision=record.updated_at or "", owner_agent_id="",
            )
            for source_id in bundle.source_memory_ids:
                source_record = legacy.get_record(source_id)
                self.store.upsert_source_link(
                    share_group_id=share_group_id,
                    memory_id=source_id,
                    source_revision=(
                        source_record.updated_at
                        if source_record is not None else ""
                    ) or "",
                    original_definition_id="",
                    canonical_definition_id=definition.definition_id,
                    status="active",
                )
                if source_record is not None:
                    self.store.deactivate_source_contributions(
                        share_group_id, source_id,
                        owner_agent_id=source_record.agent_instance_id or "",
                    )
            self.store.upsert_source_link(
                share_group_id=share_group_id,
                memory_id=canonical_memory_id,
                source_revision=record.updated_at or "",
                original_definition_id="",
                canonical_definition_id=definition.definition_id,
                status="active",
            )
            # Evidence bridge (Req8).  The canonical read's shadow permission
            # check resolves legacy sources through evidence on the canonical
            # Definition; without it a folded source (or the canonical record
            # itself) is reported ``missing`` and the read fail-closes to
            # legacy forever.  Carry each folded source -- and the canonical
            # record -- forward with a stable evidence_id per source, so the
            # read can prove it still exposes every legacy rule to the audience.
            for source_id in [*bundle.source_memory_ids, canonical_memory_id]:
                source = legacy.get_record(source_id)
                self.store.upsert_evidence(build_evidence(
                    definition_id=definition.definition_id,
                    source_rule_id=source_id,
                    agent_instance_id="",
                    project_ref=bundle.project_ref or "",
                    session_id=f"reconcile:{share_group_id}:{source_id}",
                    content=(
                        source.body if source is not None else bundle.body
                    ) or "",
                    observed_at=(
                        source.updated_at if source is not None else now
                    ) or "",
                    share_group_id=share_group_id,
                ))

        for source_id in kept_separate:
            source_record = legacy.get_record(source_id)
            if source_record is None:
                raise ValueError(f"kept_separate_source_missing: {source_id}")
            definition = build_definition(
                source_record.body, kind=source_record.kind,
                confidence=source_record.confidence,
            )
            self.store.upsert_definition(definition)
            union_assignments = self._union_assignments(
                legacy, [source_id],
            )
            bindings = [
                self._binding_from_assignment(
                    definition.definition_id, assignment,
                    share_group_id=share_group_id,
                    owner_agent_id=source_record.agent_instance_id or "",
                    record_priority=int(source_record.priority or 0),
                )
                for assignment in union_assignments
            ]
            self.store.replace_source_contributions(
                share_group_id, source_id, bindings,
                source_revision=source_record.updated_at or "",
                owner_agent_id=source_record.agent_instance_id or "",
            )
            self.store.upsert_source_link(
                share_group_id=share_group_id,
                memory_id=source_id,
                source_revision=source_record.updated_at or "",
                original_definition_id="",
                canonical_definition_id=definition.definition_id,
                status="active",
            )
            self.store.upsert_evidence(build_evidence(
                definition_id=definition.definition_id,
                source_rule_id=source_id,
                agent_instance_id="",
                project_ref=canonical_project_ref(
                    getattr(
                        next(iter(union_assignments), None),
                        "project_ref", "",
                    )
                ),
                session_id=f"reconcile:{share_group_id}:{source_id}",
                content=source_record.body or "",
                observed_at=source_record.updated_at or "",
                share_group_id=share_group_id,
            ))
        # Persist the generated definition/canonical IDs so a later phase can
        # resume with the exact current generation instead of treating all
        # canonical records as old output during retirement.
        bundle_plan["bundles"] = [bundle.to_dict() for bundle in bundles]
        return _normalize_bundle_plan(bundle_plan)

    def _verify_source_links(self, share_group_id: str) -> list[str]:
        legacy = self._legacy(share_group_id)
        links = {
            link["memory_id"] for link in self.store.list_source_links(
                share_group_id=share_group_id, status="active",
            )
        }
        plan_sources = _plan_mandatory(legacy, links)
        return [
            record.memory_id for record in plan_sources
            if record.memory_id not in links
        ]

    def _shadow_legacy(
        self, share_group_id: str, legacy: Any, bundle_plan: dict[str, Any],
    ) -> int:
        shadowed = 0
        for bundle in bundle_plan.get("bundles", []):
            bundle = bundle if isinstance(bundle, ScopeBundle) \
                else ScopeBundle.from_dict(bundle)
            for source_id in bundle.source_memory_ids:
                legacy.shadow_record(
                    source_id, reason="folded_into_canonical",
                )
                shadowed += 1
        return shadowed

    def _retire_previous_canonical(
        self, share_group_id: str, legacy: Any, bundle_plan: dict[str, Any],
        *, job_id: str = "",
    ) -> None:
        """Shadow/revoke canonical output from an older generation.

        Old canonical records stay recoverable, but leave the active service
        set once the new generation has passed readiness.  Their definitions
        are removed from canonical digest scope by revoking source links and
        contributions; records are shadowed rather than deleted.
        """
        current_ids: set[str] = set()
        for raw in bundle_plan.get("bundles", []):
            bundle = raw if isinstance(raw, ScopeBundle) \
                else ScopeBundle.from_dict(raw)
            if bundle.canonical_memory_id:
                current_ids.add(bundle.canonical_memory_id)
        for record in legacy.list_records():
            dedup_domain = str(getattr(record, "dedup_domain", "") or "")
            if not dedup_domain.startswith("canonical:"):
                continue
            parts = dedup_domain.split(":", 2)
            canonical_id = parts[2] if len(parts) >= 3 else ""
            if canonical_id in current_ids:
                continue
            legacy.shadow_record(record.memory_id, reason="superseded_canonical")
            link = self.store.get_source_link(share_group_id, record.memory_id)
            try:
                self.store.deactivate_source_contributions(
                    share_group_id, record.memory_id, owner_agent_id="",
                )
            except ValueError:
                # No canonical contribution rows are fine; the source link is
                # still retired below.
                pass
            self.store.upsert_source_link(
                share_group_id=share_group_id,
                memory_id=record.memory_id,
                source_revision=record.updated_at or "",
                original_definition_id="",
                canonical_definition_id=str(
                    (link or {}).get("canonical_definition_id", "") or ""
                ),
                status="revoked",
            )
        if job_id:
            for old in self.jobs.list_jobs(share_group_id=share_group_id):
                if (
                    old["job_id"] != job_id
                    and old["status"] == "canonical_ready"
                ):
                    self.jobs.transition(
                        old["job_id"], status="superseded",
                        last_error="superseded_by_new_generation",
                    )

    def _validate_authoritative_plan(
        self,
        share_group_id: str,
        bundle_plan: dict[str, Any],
        *,
        model_mode: str = "",
    ) -> None:
        """Validate a plan against persisted legacy records and bindings only.

        This is the canonical authority boundary.  Caller-supplied bodies,
        audience labels, priority and provider fields are never trusted until
        they match the actual ``SharedMemoryStore`` records/assignments and
        active ``AgentBindingStore`` group membership.
        """
        legacy = self._legacy(share_group_id)
        active = _plan_mandatory(
            legacy,
            {
                link["memory_id"]
                for link in self.store.list_source_links(
                    share_group_id=share_group_id, status="active",
                )
            },
        )
        validate_bundles(bundle_plan, [record.memory_id for record in active])
        records = {str(record.memory_id): record for record in active}
        group_agents: set[str] = set()
        try:
            from .agent_binding import AgentBindingStore
            group_agents = {
                str(binding.agent_instance_id or "")
                for binding in AgentBindingStore(self.workspace).find_by_group(
                    share_group_id, include_inactive=False,
                )
            }
        except Exception:  # noqa: BLE001 - membership read is best-effort below
            group_agents = set()

        model_plan = str(model_mode or "").casefold() in {
            "scope_bundle", "scripted",
        }
        for raw in bundle_plan.get("bundles", []):
            bundle = raw if isinstance(raw, ScopeBundle) \
                else ScopeBundle.from_dict(raw)
            source_ids = [str(x) for x in bundle.source_memory_ids]
            sources = [
                records[sid] for sid in source_ids
                if sid in records
            ]
            if len(sources) != len(source_ids):
                raise ValueError(
                    "bundle_contains_inactive_or_unknown_source: "
                    + ",".join(sorted(source_ids))
                )
            scopes = [
                _assignments_to_scope(
                    legacy.list_rule_assignments(source.memory_id),
                )
                for source in sources
            ]
            if len({scope["key"] for scope in scopes}) != 1:
                raise ValueError(f"bundle_mixed_scope: {source_ids}")
            scope = scopes[0]
            project_refs = scope["project_refs"]
            providers = scope["providers"]
            runtime_roles = scope["runtime_roles"]
            agent_ids = scope["agent_ids"]
            effects = scope["effects"]

            if len(effects) != 1 or str(bundle.effect or "include") not in effects:
                raise ValueError(f"bundle_effect_mismatch: {source_ids}")
            if bundle.bundle_kind == "project_overlay":
                if len(project_refs) != 1 or bundle.project_ref not in project_refs:
                    raise ValueError(f"project_overlay_scope_mismatch: {source_ids}")
                if providers:
                    if len(providers) != 1 or bundle.provider.casefold() not in providers:
                        raise ValueError(f"project_overlay_provider_mismatch: {source_ids}")
                elif bundle.provider:
                    raise ValueError(f"project_overlay_provider_mismatch: {source_ids}")
                if runtime_roles:
                    if len(runtime_roles) != 1 or bundle.runtime_role.casefold() not in runtime_roles:
                        raise ValueError(f"project_overlay_runtime_role_mismatch: {source_ids}")
                elif bundle.runtime_role:
                    raise ValueError(f"project_overlay_runtime_role_mismatch: {source_ids}")
            elif bundle.bundle_kind == "agent_overlay":
                if project_refs:
                    raise ValueError(f"agent_overlay_widens_to_project: {source_ids}")
                if providers:
                    if len(providers) != 1 or bundle.provider.casefold() not in providers:
                        raise ValueError(f"agent_overlay_provider_mismatch: {source_ids}")
                elif bundle.provider:
                    raise ValueError(f"agent_overlay_provider_mismatch: {source_ids}")
                if runtime_roles:
                    if len(runtime_roles) != 1 or bundle.runtime_role.casefold() not in runtime_roles:
                        raise ValueError(f"agent_overlay_runtime_role_mismatch: {source_ids}")
                elif bundle.runtime_role:
                    raise ValueError(f"agent_overlay_runtime_role_mismatch: {source_ids}")
                if not providers and not runtime_roles and not agent_ids:
                    raise ValueError(f"agent_overlay_widens_audience: {source_ids}")
            else:  # shared_baseline
                if project_refs or providers or runtime_roles:
                    raise ValueError(f"shared_baseline_widens_audience: {source_ids}")
                if bundle.provider or bundle.runtime_role:
                    raise ValueError(f"shared_baseline_widens_audience: {source_ids}")
                if agent_ids and not group_agents:
                    raise ValueError(f"shared_baseline_unknown_members: {source_ids}")
                if agent_ids and group_agents and agent_ids != group_agents:
                    raise ValueError(f"shared_baseline_member_mismatch: {source_ids}")

            max_priority = max(
                (int(source.priority or 0) for source in sources),
                default=0,
            )
            if int(bundle.priority or 0) != max_priority:
                raise ValueError(f"bundle_priority_not_source_max: {source_ids}")
            if not model_plan and str(bundle.body or "") != _bundle_body(sources):
                raise ValueError(f"bundle_body_not_source_exact: {source_ids}")
            if not str(bundle.body or "").strip():
                raise ValueError(f"empty_bundle_body: {source_ids}")

        for source_id in bundle_plan.get("kept_separate", []):
            source_id = str(source_id or "")
            if source_id not in records:
                raise ValueError(f"kept_separate_inactive_or_unknown: {source_id}")

    def _build_plan_from_legacy(
        self, share_group_id: str, model_mode: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Build a model or deterministic plan for an unplanned generation."""
        legacy = self._legacy(share_group_id)
        active = _plan_mandatory(
            legacy,
            {
                link["memory_id"]
                for link in self.store.list_source_links(
                    share_group_id=share_group_id, status="active",
                )
            },
        )
        if not active:
            raise ValueError("no_active_mandatory")
        assignments_by_memory_id = {
            record.memory_id: legacy.list_rule_assignments(record.memory_id)
            for record in active
        }
        plan: dict[str, Any] | None = None
        effective_mode = ""
        if model_mode == "scope_bundle":
            try:
                from .host_agent_backend import batch_bundle_via_cli
                result = batch_bundle_via_cli(
                    active,
                    assignments_by_memory_id=assignments_by_memory_id,
                    workspace=self.workspace,
                )
                if result.get("model_mode") == "scope_bundle":
                    plan = result
                    effective_mode = "scope_bundle"
                else:
                    plan = result
                    effective_mode = "heuristic"
            except Exception:  # noqa: BLE001 - fall through to deterministic gate
                plan = None
        if plan is None:
            plan = build_bundles(
                self.store, legacy, share_group_id, active,
            )
            effective_mode = "heuristic"
        if effective_mode == "heuristic" and not _is_deterministic_safe_plan(
            plan, active,
        ):
            self.jobs.transition(
                RuleReconciliationStore.job_id_for(
                    share_group_id, self.source_digest(share_group_id),
                ),
                status="retryable_failed",
                phase="model",
                last_error="model_bundle_required",
            )
            return None, "model_bundle_required"
        if effective_mode == "heuristic":
            effective_mode = "deterministic_safe_only"
        return plan, effective_mode

    def _build_projection(self, share_group_id: str) -> None:
        from .governance_scope import (
            GovernanceScope,
            build_shared_memory_graph,
            share_group_projection_path,
        )
        graph = build_shared_memory_graph(self.workspace, share_group_id)
        scope = GovernanceScope(mode="share_group", share_group_id=share_group_id)
        out_path = share_group_projection_path(self.workspace, scope)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tomb = out_path.with_suffix(out_path.suffix + ".deleted")
        if tomb.exists():
            tomb.unlink()
        out_path.write_text(
            json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        self.store.set_projection_state(
            share_group_id, projection_lag=0, projection_error="",
        )

    # ------------------------------------------------------------- the saga

    def run(
        self,
        share_group_id: str,
        *,
        bundle_plan: dict[str, Any] | None = None,
        model_mode: str = "scope_bundle",
        reason: str = "",
    ) -> dict[str, Any]:
        """Run (or resume) the reconciliation saga for one group.

        ``bundle_plan`` can be supplied by a trusted model caller and is always
        re-validated against persisted sources/assignments.  When omitted, the
        model path is attempted for ``scope_bundle`` mode; heuristic output is
        only allowed when it is deterministic-safe (identical bodies / no
        semantic merge) and is labelled as such.
        """
        job = self._start_or_resume_job(
            share_group_id,
            reason=reason or REASON_SCOPE_BUNDLE,
            model_mode=model_mode,
        )
        job_id = job["job_id"]
        if job["status"] == "canonical_ready":
            status = canonical_reconciliation_status(
                self.workspace, share_group_id, store=self.store,
            )
            if not status["canonical_ready"]:
                raise RuntimeError(
                    "canonical_ready_drift: " + json.dumps(
                        status["failures"], ensure_ascii=False,
                    )
                )
            return job
        with self.store.governance_lock():
            try:
                return self._run_locked(
                    job, share_group_id,
                    bundle_plan=bundle_plan, model_mode=model_mode,
                )
            except Exception as exc:  # noqa: BLE001 - persist any failure
                current = self.jobs.get_job(job_id) or job
                self.jobs.transition(
                    job_id,
                    status="retryable_failed",
                    phase=str(current.get("phase", "model") or "model"),
                    attempt_count=int(current.get("attempt_count", 0) or 0) + 1,
                    last_error=f"{type(exc).__name__}: {exc}",
                )
                raise

    def _run_locked(
        self,
        job: dict[str, Any],
        share_group_id: str,
        *,
        bundle_plan: dict[str, Any] | None,
        model_mode: str,
    ) -> dict[str, Any]:
        from .rule_merge import RuleMergeService
        job_id = job["job_id"]
        legacy = self._legacy(share_group_id)
        phase = str(job.get("phase", "model") or "model")
        if phase not in SAGA_PHASES:
            raise ValueError(f"unknown_saga_phase: {phase}")

        plan: dict[str, Any] | None = None
        effective_model_mode = str(job.get("model_mode", "") or "")
        if phase != "model":
            try:
                raw = json.loads(job.get("result_json") or "{}")
                if not isinstance(raw, dict):
                    raise ValueError("staged_plan_not_object")
                plan = _normalize_bundle_plan(raw)
            except (TypeError, ValueError) as exc:
                self.jobs.transition(
                    job_id, status="retryable_failed", phase=phase,
                    last_error=f"staged_plan_missing: {exc}",
                )
                raise
            if plan is None:
                raise ValueError("staged_plan_missing")

        # ---- model ---------------------------------------------------------
        if phase == "model":
            self.jobs.transition(
                job_id, status="model_running", phase="model", last_error="",
            )
            if bundle_plan is None:
                plan, effective_model_mode = self._build_plan_from_legacy(
                    share_group_id, model_mode,
                )
                if plan is None:
                    return self.jobs.get_job(job_id)  # type: ignore[return-value]
            else:
                plan = _normalize_bundle_plan(bundle_plan)
                effective_model_mode = model_mode or "scripted"
                if effective_model_mode == "heuristic" and not _is_deterministic_safe_plan(
                    plan, _plan_mandatory(
                        legacy,
                        {
                            link["memory_id"]
                            for link in self.store.list_source_links(
                                share_group_id=share_group_id, status="active",
                            )
                        },
                    ),
                ):
                    self.jobs.transition(
                        job_id, status="retryable_failed", phase="model",
                        last_error="model_bundle_required",
                    )
                    return self.jobs.get_job(job_id)  # type: ignore[return-value]
            self._validate_authoritative_plan(
                share_group_id, plan, model_mode=effective_model_mode,
            )
            self.jobs.transition(
                job_id,
                status="staged",
                phase="stage",
                model_mode=effective_model_mode,
                result_json=json.dumps(
                    plan, ensure_ascii=False, sort_keys=True,
                ),
                canonical_digest_before=self.canonical_digest(share_group_id),
            )
            phase = "backfill_p3"

        # ---- backfill_p3: legacy parity before any canonical write ---------
        if phase in {"stage", "backfill_p3"}:
            self.jobs.transition(job_id, status="applying", phase="backfill_p3")
            RuleMergeService(self.store).backfill_group(legacy, share_group_id)

        # ---- write_canonical ----------------------------------------------
        if phase in {"backfill_p3", "write_canonical"}:
            self.jobs.transition(job_id, phase="write_canonical")
            updated_plan = self._write_canonical(
                share_group_id, legacy, plan,
            ) or _normalize_bundle_plan(plan)
            plan = updated_plan
            self.jobs.transition(
                job_id,
                phase="write_canonical",
                result_json=json.dumps(
                    plan, ensure_ascii=False, sort_keys=True,
                ),
            )
            phase = "write_canonical"

        # ---- verify_source_links ------------------------------------------
        if phase in {"write_canonical", "verify_source_links"}:
            self.jobs.transition(job_id, phase="verify_source_links")
            unlinked = self._verify_source_links(share_group_id)
            phase = "verify_source_links"
            if unlinked:
                raise RuntimeError(
                    f"unlinked_sources={len(unlinked)}: {','.join(unlinked[:5])}"
                )

        # ---- drain_outbox --------------------------------------------------
        if phase in {"verify_source_links", "shadow_legacy", "drain_outbox"}:
            self.jobs.transition(job_id, phase="drain_outbox")
            phase = "drain_outbox"
            RuleMergeService(self.store).consume_outbox(
                self.workspace, only_group=share_group_id,
            )

        # ---- build_projection ----------------------------------------------
        if phase in {"drain_outbox", "build_projection"}:
            self.jobs.transition(job_id, phase="build_projection")
            self._build_projection(share_group_id)
            phase = "build_projection"

        # ---- activate_canonical --------------------------------------------
        if phase in {"build_projection", "activate_canonical"}:
            canonical_digest = self.canonical_digest(share_group_id)
            self.jobs.transition(
                job_id,
                phase="activate_canonical",
                canonical_digest_after=canonical_digest,
                projection_version=self._projection_version(share_group_id),
            )
            activation = self.jobs.canonical_activation(share_group_id)
            self.jobs.set_canonical_activation(
                share_group_id,
                activation_status="active",
                canonical_digest=canonical_digest,
                read_path="rule-intelligence",
                activated_at=(
                    str((activation or {}).get("activated_at") or "")
                    if activation else _now_iso()
                ),
            )
            phase = "activate_canonical"

        # ---- verify_readiness ----------------------------------------------
        if phase in {"activate_canonical", "verify_readiness"}:
            self.jobs.transition(job_id, phase="verify_readiness")
            status = canonical_reconciliation_status(
                self.workspace, share_group_id, store=self.store,
                exclude_job_id=job_id,
            )
            if not status["canonical_ready"]:
                raise RuntimeError(
                    "readiness_failed: " + json.dumps(
                        status["failures"], ensure_ascii=False,
                    )
                )
            phase = "verify_readiness"

        # ---- shadow_legacy: only after readiness ---------------------------
        if phase in {"verify_readiness", "shadow_legacy"}:
            self.jobs.transition(job_id, phase="shadow_legacy")
            self._shadow_legacy(share_group_id, legacy, plan)
            phase = "shadow_legacy"

        # ---- retire_previous: old canonical output leaves service -----------
        if phase in {"shadow_legacy", "retire_previous"}:
            self.jobs.transition(job_id, phase="retire_previous")
            self._retire_previous_canonical(
                share_group_id, legacy, plan, job_id=job_id,
            )
            phase = "retire_previous"

        # ---- canonical_ready -----------------------------------------------
        canonical_digest = self.canonical_digest(share_group_id)
        projection_version = self._projection_version(share_group_id)
        activation = self.jobs.canonical_activation(share_group_id)
        if (
            activation is None
            or str(activation.get("canonical_digest") or "")
            != canonical_digest
        ):
            self.jobs.set_canonical_activation(
                share_group_id,
                activation_status="active",
                canonical_digest=canonical_digest,
                read_path="rule-intelligence",
                activated_at=(
                    str((activation or {}).get("activated_at") or "")
                    if activation else _now_iso()
                ),
            )
        return self.jobs.transition(
            job_id,
            status="canonical_ready",
            phase="canonical_ready",
            canonical_digest_after=canonical_digest,
            projection_version=projection_version,
        )


# ---------------------------------------------------------------------------
# Req6: legacy-heuristic migration gate
# ---------------------------------------------------------------------------


def _active_mandatory(legacy: Any) -> list[Any]:
    return [
        record for record in legacy.list_records()
        if str(record.injection_policy or "") == "always"
        and str(
            getattr(record.status, "value", record.status)
            or ""
        ) == SharedMemoryStatus.ACTIVE.value
    ]


def _original_mandatory(legacy: Any) -> list[Any]:
    """Original mandatory sources, including folded/shadowed originals.

    Canonical records are generated output and must never be treated as input
    for the next reconciliation generation.
    """
    return [
        record for record in legacy.list_records()
        if str(record.injection_policy or "") == "always"
        and str(
            getattr(record.status, "value", record.status)
            or ""
        ) in {
            SharedMemoryStatus.ACTIVE.value,
            SharedMemoryStatus.SHADOWED.value,
        }
        and not str(getattr(record, "dedup_domain", "") or "").startswith(
            "canonical:"
        )
    ]


def _plan_mandatory(
    legacy: Any, linked_memory_ids: set[str] | frozenset[str],
) -> list[Any]:
    """Sources that the current reconciliation plan must cover.

    Shadowed originals are included once they have an active source link
    (already folded by a prior generation).  Pre-existing shadowed records
    without links are not active input and are intentionally left alone.
    """
    linked = set(linked_memory_ids or set())
    return [
        record for record in legacy.list_records()
        if str(record.injection_policy or "") == "always"
        and not str(getattr(record, "dedup_domain", "") or "").startswith(
            "canonical:"
        )
        and (
            str(getattr(record.status, "value", record.status) or "")
            == SharedMemoryStatus.ACTIVE.value
            or (
                str(getattr(record.status, "value", record.status) or "")
                == SharedMemoryStatus.SHADOWED.value
                and str(record.memory_id or "") in linked
            )
        )
    ]


def _applied_enrichment_is_scope_bundle(
    workspace: str | Path, share_group_id: str,
) -> bool:
    """True when enrichment history contains a scope_bundle result.

    Applied enrichment tasks live as JSONL lines in the pending file with
    ``status == "applied"``; a scope-bundle task is any applied task whose
    ``task_type``/``kind`` says so.  ``get_status`` only exposes counts, so the
    raw lines are scanned here.
    """
    from .host_enrichment import _pending_path
    ppath = _pending_path(workspace)
    if not ppath.exists():
        return False
    try:
        lines = ppath.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(task, dict):
            continue
        if task.get("status") != "applied":
            continue
        scope = task.get("scope") or {}
        if share_group_id and str(scope.get("share_group_id", "") or "") != share_group_id:
            continue
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        task_type = str(result.get("task_type") or task.get("task_type") or "")
        if task_type in {"scope_bundle", "rule_reconciliation"}:
            return True
        if str(result.get("kind", "") or "") == "scope_bundle":
            return True
    return False


def ensure_reconciliation_job(
    workspace: str | Path,
    share_group_id: str,
    *,
    store: RuleMergeStore | None = None,
) -> dict[str, Any]:
    """Req6: create a reconciliation job only when every gate passes.

    The migration path exists for groups whose only enrichment history is
    legacy heuristic record enrichment (classify/translate) and which have
    never been scope-bundled.  A matching canonical_ready job blocks a second
    job, but a changed source digest must create a new generation instead of
    silently returning the old one.
    """
    from .shared_memory_store import SharedMemoryStore
    workspace = Path(workspace).resolve()
    store = store or RuleMergeStore(workspace)
    jobs = RuleReconciliationStore(store)
    legacy = SharedMemoryStore(workspace, share_group_id)
    existing_links = store.list_source_links(
        share_group_id=share_group_id, status="active",
    )
    active = _plan_mandatory(
        legacy, {link["memory_id"] for link in existing_links},
    )

    if not active:
        return {"created": False, "reason": "no_active_mandatory"}
    service = RuleReconciliationService(store, workspace=workspace)
    source_digest = service.source_digest(share_group_id)
    latest = jobs.latest_job(share_group_id)

    # A ready generation is idempotent only when every readiness gate still
    # passes.  Source/canonical/projection drift falls through to a new job.
    if latest is not None and latest["status"] == "canonical_ready":
        status = canonical_reconciliation_status(
            workspace, share_group_id, store=store,
        )
        if status["canonical_ready"]:
            return {"created": False, "reason": "canonical_ready_job_exists"}
        drift_failures = {
            "source_digest_changed",
            "canonical_digest_drift",
            "projection_version_drift",
            "binding_multiset_mismatch",
        }
        if not drift_failures.intersection(status.get("failures") or []):
            return {
                "created": False,
                "reason": "canonical_ready_job_exists",
                "existing_job": latest,
            }

    # Same-generation pending legacy job: keep it.
    if (
        latest is not None
        and latest["reason"] == REASON_LEGACY_HEURISTIC
        and latest["status"] in {
            "pending_model", "model_running", "staged", "applying",
            "retryable_failed",
        }
        and str(latest.get("source_digest", "") or "") == source_digest
    ):
        return {
            "created": False,
            "existing_job": latest,
            "reason": "legacy_heuristic_job_already_pending",
        }

    links = existing_links
    canonical_defs = {
        link["canonical_definition_id"] for link in links
        if link.get("canonical_definition_id")
    }
    source_links_complete = (
        len({link["memory_id"] for link in links})
        >= len(active)
        and bool(canonical_defs)
    )
    if (
        source_links_complete
        and latest is not None
        and str(latest.get("source_digest", "") or "") == source_digest
    ):
        return {"created": False, "reason": "canonical_already_built"}

    # History/mode gates still apply to new generations.
    if _applied_enrichment_is_scope_bundle(workspace, share_group_id):
        return {"created": False, "reason": "scope_bundle_history_exists"}
    scope_bundle_jobs = [
        job for job in jobs.list_jobs(share_group_id=share_group_id)
        if job.get("reason") == REASON_SCOPE_BUNDLE
        or job.get("model_mode") == "scope_bundle"
    ]
    if scope_bundle_jobs:
        return {"created": False, "reason": "scope_bundle_job_exists"}

    if latest is not None and latest["status"] == "canonical_ready":
        return {
            "created": False,
            "existing_job": latest,
            "reason": "scope_bundle_job_exists",
        }

    # Source state genuinely changed: retire stale unfinished generations.
    for old in jobs.list_jobs(share_group_id=share_group_id):
        if old["status"] in {
            "pending_model", "model_running", "staged",
            "applying", "retryable_failed",
        }:
            jobs.transition(
                old["job_id"], status="superseded",
                last_error="superseded_by_new_generation",
            )
    job = jobs.create_job(
        share_group_id=share_group_id,
        source_digest=source_digest,
        reason=REASON_LEGACY_HEURISTIC,
        model_mode="heuristic",
    )
    return {
        "created": True,
        "job": job,
        "reason": REASON_LEGACY_HEURISTIC,
    }


# ---------------------------------------------------------------------------
# Req7: unified canonical readiness
# ---------------------------------------------------------------------------


def _assignment_audience_key(assignment: Any) -> tuple[Any, ...]:
    target_type = str(getattr(assignment, "target_type", "agent") or "agent")
    target_id = str(getattr(assignment, "target_id", "") or "")
    provider = str(getattr(assignment, "provider", "") or "").casefold()
    if not provider and target_type == "provider":
        provider = target_id.casefold()
    runtime_role = str(getattr(assignment, "runtime_role", "") or "").casefold()
    if not runtime_role and target_type == "runtime_role":
        runtime_role = target_id.casefold()
    return (
        target_type,
        target_id,
        canonical_project_ref(getattr(assignment, "project_ref", "") or ""),
        provider,
        runtime_role,
        str(getattr(assignment, "effect", "include") or "include"),
    )


def _expected_binding_multiset(
    store: RuleMergeStore, legacy: Any, share_group_id: str,
) -> dict[str, list[tuple[Any, ...]]]:
    """Expected canonical bindings per Definition from durable Source Links.

    Original source records may be shadowed after folding, so the readiness
    gate derives the audience from the links, not from ``_active_mandatory``.
    Canonical records themselves are skipped: they are the folded output, not
    the authoritative source.
    """
    records = {
        str(record.memory_id): record for record in legacy.list_records()
    }
    expected: dict[str, dict[tuple[Any, ...], int]] = {}
    for link in store.list_source_links(
        share_group_id=share_group_id, status="active",
    ):
        memory_id = str(link.get("memory_id") or "")
        record = records.get(memory_id)
        if record is None:
            continue
        # Canonical records are included while they have an active source link:
        # during a generation swap the old canonical layer still serves until
        # the new generation passes readiness and retires it.
        definition_id = str(
            link.get("canonical_definition_id")
            or link.get("original_definition_id") or ""
        )
        if not definition_id:
            continue
        by_key = expected.setdefault(definition_id, {})
        record_priority = int(record.priority or 0)
        for assignment in legacy.list_rule_assignments(memory_id):
            key = _assignment_audience_key(assignment)
            priority = int(
                getattr(assignment, "priority_override", None)
                if getattr(assignment, "priority_override", None) is not None
                else record_priority
            )
            by_key[key] = max(by_key.get(key, 0), priority)
    return {
        definition_id: sorted(
            (*key, priority) for key, priority in by_key.items()
        )
        for definition_id, by_key in sorted(expected.items())
    }


def _actual_binding_multiset(
    store: RuleMergeStore, share_group_id: str,
) -> dict[str, list[tuple[Any, ...]]]:
    """Active P3 binding multiset grouped by canonical Definition."""
    actual: dict[str, dict[tuple[Any, ...], int]] = {}
    for binding in store.list_bindings(
        share_group_id=share_group_id, status="active",
    ):
        data = binding.to_dict()
        audience_key = (
            str(data.get("target_type", "agent") or "agent"),
            str(data.get("target_id", "") or ""),
            canonical_project_ref(str(data.get("project_ref", "") or "")),
            str(data.get("provider", "") or "").casefold(),
            str(data.get("runtime_role", "") or "").casefold(),
            str(data.get("effect", "include") or "include"),
        )
        by_key = actual.setdefault(
            str(data.get("definition_id", "") or ""), {},
        )
        priority = int(data.get("priority", 0) or 0)
        by_key[audience_key] = max(by_key.get(audience_key, 0), priority)
    return {
        definition_id: sorted(
            (*key, priority) for key, priority in by_key.items()
        )
        for definition_id, by_key in sorted(actual.items())
    }


def canonical_reconciliation_status(
    workspace: str | Path,
    share_group_id: str,
    *,
    store: RuleMergeStore | None = None,
    exclude_job_id: str = "",
) -> dict[str, Any]:
    """Group-level canonical readiness; ``canonical_ready`` is never ``pending
    == 0`` -- every gate below must hold independently.

    ``exclude_job_id`` lets the saga's own ``verify_readiness`` phase check the
    group while *it* is the ``applying`` job: the in-flight gate must not flag
    the very job performing the verification.
    """
    from .governance_scope import (
        GovernanceScope,
        share_group_projection_path,
    )
    from .shared_memory_store import SharedMemoryStore
    workspace = Path(workspace).resolve()
    store = store or RuleMergeStore(workspace)
    recon = RuleReconciliationService(store, workspace=workspace)
    jobs = RuleReconciliationStore(store)
    legacy = SharedMemoryStore(workspace, share_group_id)

    failures: list[str] = []
    checks: dict[str, Any] = {}

    # 1) No reconciliation job in flight or retryable (other than the one
    #    running this very verification, if the saga asked us to exclude it).
    jobs_for_group = jobs.list_jobs(share_group_id=share_group_id)
    in_flight = [
        job for job in jobs_for_group
        if job["status"] in {"pending_model", "model_running", "staged",
                             "applying", "retryable_failed"}
        and (not exclude_job_id or job["job_id"] != exclude_job_id)
    ]
    checks["reconciliation_jobs"] = len(jobs_for_group)
    checks["reconciliation_in_flight"] = len(in_flight)
    if in_flight:
        failures.append("reconciliation_in_flight")

    # 2) Outbox fully drained.
    high_water = legacy.outbox_high_water()
    checks["outbox_pending"] = int(high_water["pending"])
    if int(high_water["pending"]):
        failures.append("outbox_pending")

    # 3) Projection settled.
    projection = store.projection_status(group_ids=[share_group_id])
    checks["projection_lag"] = int(projection["projection_lag"] or 0)
    checks["projection_error"] = str(projection["projection_error"] or "")
    if checks["projection_lag"]:
        failures.append("projection_lag")
    if checks["projection_error"]:
        failures.append("projection_error")

    # 4) Every active mandatory source has a durable source link.
    active = _active_mandatory(legacy)
    links = store.list_source_links(share_group_id=share_group_id, status="active")
    links_by_id = {link["memory_id"]: link for link in links}
    unlinked = [record.memory_id for record in active if record.memory_id not in links_by_id]
    checks["active_mandatory"] = len(active)
    checks["unlinked_sources"] = len(unlinked)
    if unlinked:
        failures.append("unlinked_sources")

    # 5) Canonical definitions exist when any active mandatory exists.
    canonical_defs = {
        link["canonical_definition_id"] for link in links
        if link.get("canonical_definition_id")
    }
    checks["canonical_definitions"] = len(canonical_defs)
    if active and not canonical_defs:
        failures.append("no_canonical_definitions")

    # 6) Shadow diff is empty (missing/extra/permission_diff all 0).
    shadow = _shadow_diff(store, legacy, share_group_id)
    checks["shadow"] = shadow
    for key in ("missing", "extra", "permission_diff"):
        if shadow.get(key, 0):
            failures.append(f"shadow_{key}")

    # 7) Projection graph built and persisted.
    graph_path = share_group_projection_path(
        workspace, GovernanceScope(mode="share_group", share_group_id=share_group_id),
    )
    graph_built = graph_path.exists() and graph_path.stat().st_size > 0
    checks["graph_built"] = graph_built
    if not graph_built:
        failures.append("graph_not_built")

    # 8) Group-level canonical activation written (Req8 gate).
    activation = jobs.canonical_activation(share_group_id)
    checks["canonical_activation"] = (
        activation or {}
    ).get("activation_status", "")
    if not activation or str(activation.get("activation_status") or "") != "active":
        failures.append("canonical_not_activated")

    # 9) A ready generation must still match the current source input, the
    #    activated canonical digest and the persisted projection version.
    latest_ready = next(
        (job for job in jobs_for_group if job["status"] == "canonical_ready"),
        None,
    )
    # While the saga itself verifies a partially-applied generation, compare
    # against that job's fixed input digest and freshly staged canonical digest
    # instead of an older ready generation.  The old generation is retired only
    # after this gate passes.
    digest_anchor = latest_ready
    if exclude_job_id:
        excluded = next(
            (job for job in jobs_for_group if job["job_id"] == exclude_job_id),
            None,
        )
        if (
            excluded is not None
            and excluded["status"] in {
                "pending_model", "model_running", "staged",
                "applying", "retryable_failed",
            }
            and str(excluded.get("canonical_digest_after") or "")
        ):
            digest_anchor = excluded
    current_source_digest = recon.source_digest(share_group_id)
    current_canonical_digest = recon.canonical_digest(share_group_id)
    current_projection_version = recon._projection_version(share_group_id)
    checks["source_digest"] = current_source_digest
    checks["canonical_digest"] = current_canonical_digest
    checks["digest_anchor"] = (
        str(digest_anchor.get("job_id", "")) if digest_anchor is not None else ""
    )
    if digest_anchor is not None:
        if (
            str(digest_anchor.get("source_digest", "") or "")
            != current_source_digest
        ):
            failures.append("source_digest_changed")
        if (
            activation
            and str(activation.get("canonical_digest", "") or "")
            != current_canonical_digest
        ):
            failures.append("canonical_digest_drift")
        if (
            str(digest_anchor.get("projection_version", "") or "")
            != current_projection_version
        ):
            failures.append("projection_version_drift")

    # 10) Readiness must compare the full audience multiset, not just "at least
    #     one binding exists".
    expected_bindings = _expected_binding_multiset(store, legacy, share_group_id)
    actual_bindings = _actual_binding_multiset(store, share_group_id)
    checks["expected_bindings"] = expected_bindings
    checks["actual_bindings"] = actual_bindings
    checks["binding_multiset_match"] = expected_bindings == actual_bindings
    if expected_bindings != actual_bindings:
        failures.append("binding_multiset_mismatch")

    ready = not failures
    return {
        "share_group_id": share_group_id,
        "canonical_ready": ready,
        "read_path": "rule-intelligence" if ready else "legacy",
        "failures": failures,
        "checks": checks,
    }


def _shadow_diff(
    store: RuleMergeStore, legacy: Any, share_group_id: str,
) -> dict[str, int]:
    """Group-level shadow summary.

    ``missing``: active always source without an active source link.
    ``extra``: active source link whose source record no longer exists at all
    (a deleted source should not keep a live link).
    ``permission_diff``: active source whose canonical Definition has no active
    binding in this group (the read path would fail closed on it).
    """
    active_links = store.list_source_links(
        share_group_id=share_group_id, status="active",
    )
    bound_definitions = {
        binding.definition_id for binding in store.list_bindings(
            share_group_id=share_group_id, status="active",
        )
    }
    plan_sources = _plan_mandatory(
        legacy, {link["memory_id"] for link in active_links},
    )
    active_ids = {record.memory_id for record in plan_sources}
    all_records = {record.memory_id for record in legacy.list_records()}
    missing = [
        record.memory_id for record in plan_sources
        if record.memory_id not in {link["memory_id"] for link in active_links}
    ]
    extra = [
        link["memory_id"] for link in active_links
        if link["memory_id"] not in all_records
    ]
    permission_diff: list[str] = []
    for link in active_links:
        if link["memory_id"] not in active_ids:
            continue  # folded (shadowed) sources keep their fold link
        definition_id = (
            link.get("canonical_definition_id")
            or link.get("original_definition_id") or ""
        )
        if definition_id not in bound_definitions:
            permission_diff.append(link["memory_id"])
    return {
        "missing": len(missing),
        "extra": len(extra),
        "permission_diff": len(permission_diff),
    }
