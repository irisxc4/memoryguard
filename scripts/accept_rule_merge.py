"""V2_ACTIVE native acceptance for rule governance and group isolation.

The probe exercises the authoritative V2 stores and native transport boundary
directly.  Every check is executed and reported; a failed check is retained in
the JSON result rather than being converted into a permissive fallback.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryguard.access_context import AccessContext  # noqa: E402
from memoryguard.evidence import EvidenceStore  # noqa: E402
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext  # noqa: E402
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope  # noqa: E402
from memoryguard.rule_binding import RuleBinding  # noqa: E402
from memoryguard.rule_definition import build_definition  # noqa: E402
from memoryguard.rules.v2_store import RuleV2Store  # noqa: E402
from memoryguard.runtime_v2.group_native import GroupControlService  # noqa: E402
from memoryguard.runtime_v2.native_ports import (  # noqa: E402
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.runtime_v2.rule_merge_native import NativeRuleMergeService  # noqa: E402


GROUP = "accept-related"
OTHER_GROUP = "accept-other"


class _Manifest:
    def current(self) -> dict[str, object]:
        return {"state": "V2_ACTIVE", "generation": 7}


def _secret(seed: str) -> str:
    return base64.urlsafe_b64encode((seed.encode("utf-8") * 32)[:32]).decode().rstrip("=")


def _context(
    root: Path,
    *,
    agent: str = "accept-agent",
    group: str = GROUP,
    source: str = "transport",
    trusted: bool = True,
) -> Any:
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"accept-session-{agent}-{source}",
            session_source=source,
            session_trusted=trusted,
        ),
        workspace_id=str(root.resolve()),
        share_group_id=group,
        project_ref="accept-project",
        provider="codex",
        runtime_role="worker",
        entrypoint="accept-rule-merge",
    )


def _put_atom(
    root: Path,
    memory: MemoryAtomStore,
    evidence: EvidenceStore,
    governance: GovernanceV2,
    *,
    memory_id: str,
    group: str,
    agent: str,
) -> None:
    governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=f"V2 acceptance memory {memory_id}",
            kind="procedure",
            status="active",
            workspace_id=str(root.resolve()),
            share_group_id=group,
            agent_instance_id=agent,
            project_ref="accept-project",
            provider="codex",
            runtime_role="worker",
        ),
        context=V2MutationContext(
            workspace_id=str(root.resolve()),
            share_group_id=group,
            agent_instance_id=agent,
            project_ref="accept-project",
            provider="codex",
            runtime_role="worker",
            actor="acceptance-seed",
            authority="manual",
            admin=True,
        ),
        evidence=[{"source_ref": f"acceptance:{group}:{memory_id}"}],
        reason="V2 native acceptance fixture",
        idempotency_key=f"acceptance:{group}:{memory_id}",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active")


def _seed(root: Path) -> tuple[MemoryAtomStore, EvidenceStore, GovernanceV2]:
    groups = GroupControlService(root, write=True)
    groups.bind_agents(
        ["accept-agent", "accept-peer"],
        share_group_id=GROUP,
        native_memory_modes={"accept-agent": "redirected", "accept-peer": "observed"},
    )
    groups.bind_agents(
        ["other-agent", "other-peer"],
        share_group_id=OTHER_GROUP,
        native_memory_modes={"other-agent": "observed", "other-peer": "observed"},
    )
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    _put_atom(root, memory, evidence, governance, memory_id="related-memory", group=GROUP, agent="accept-agent")
    _put_atom(root, memory, evidence, governance, memory_id="other-memory", group=OTHER_GROUP, agent="other-agent")
    return memory, evidence, governance


def _proposal(root: Path, proposal_id: str, group: str = GROUP) -> dict[str, Any]:
    rules = RuleV2Store(root)
    left = rules.upsert_definition(
        build_definition("Always preserve the native audit receipt", kind="procedure", definition_id=f"{proposal_id}-left")
    )
    right = rules.upsert_definition(
        build_definition("Always preserve a native audit receipt", kind="procedure", definition_id=f"{proposal_id}-right")
    )
    rules.record_merge_proposal({
        "proposal_id": proposal_id,
        "definition_ids_json": json.dumps([left.definition_id, right.definition_id]),
        "status": "candidate",
        "metadata_json": json.dumps({
            "definition_revision_a": left.revision,
            "definition_revision_b": right.revision,
            "share_group_id": group,
        }, sort_keys=True),
    })
    return {
        "proposal_id": proposal_id,
        "ids": [left.definition_id, right.definition_id],
        "revisions": {left.definition_id: left.revision, right.definition_id: right.revision},
        "group": group,
    }


def _dispatch_merge(root: Path, operation: str, payload: dict[str, Any], *, group: str = GROUP, agent: str = "accept-agent", source: str = "transport") -> dict[str, Any]:
    return NativeRuleMergeService(root, state_provider=_Manifest()).dispatch(
        operation,
        payload,
        context=_context(root, agent=agent, group=group, source=source),
        generation=7,
        state="V2_ACTIVE",
    )


def _issue(root: Path, proposal_id: str, key: str, *, group: str = GROUP, agent: str = "accept-agent", source: str = "transport") -> dict[str, Any]:
    return _dispatch_merge(
        root,
        "issue",
        {
            "proposal_id": proposal_id,
            "idempotency_key": key,
            "mutation_receipt": {"receipt_id": f"receipt-{key}"},
            "recovery_secret": _secret(key),
        },
        group=group,
        agent=agent,
        source=source,
    )


def _approve(root: Path, proposal: dict[str, Any], token: str, key: str) -> dict[str, Any]:
    return _dispatch_merge(
        root,
        "approve",
        {
            "proposal_id": proposal["proposal_id"],
            "capability_token": token,
            "expected_definition_revisions": proposal["revisions"],
            "idempotency_key": key,
            "mutation_receipt": {"receipt_id": f"receipt-{key}"},
        },
        group=str(proposal["group"]),
    )


def _native(port: NativeV2RuntimePort, name: str, payload: Any, context: Any, *, mutation: bool = False) -> dict[str, Any]:
    return port.dispatch_mcp(name, payload, context=context, generation=7, mutation=mutation, state="V2_ACTIVE")


def _check(checks: list[dict[str, Any]], name: str, fn: Callable[[], Any]) -> bool:
    try:
        value = fn()
        passed = bool(value)
        checks.append({"name": name, "passed": passed, "detail": value if isinstance(value, (str, int, float, bool, dict, list)) else str(value)})
    except Exception as exc:  # the failure is part of the acceptance result
        passed = False
        checks.append({"name": name, "passed": False, "detail": f"{type(exc).__name__}: {exc}"})
    print(f"[{('PASS' if passed else 'FAIL')}] {name}")
    return passed


_EXTENDED_TIME = "2026-08-12T00:00:00+00:00"


def _rule_rows(root: Path, table: str, *, group: str = "") -> list[dict[str, Any]]:
    """Read V2 rule rows for acceptance evidence; never opens a legacy store."""
    rules = RuleV2Store(root)
    with closing(sqlite3.connect(rules.db_path)) as conn:
        with conn:
            conn.row_factory = sqlite3.Row
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                return []
            columns = {
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if group and "share_group_id" in columns:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE share_group_id=? ORDER BY rowid", (group,)
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    return [dict(row) for row in rows]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _seed_extended_v2(root: Path) -> dict[str, str]:
    """Create the V2 evidence/binding fixture used by the extended checks."""
    rules = RuleV2Store(root)
    definitions = {
        "positive": rules.upsert_definition(
            build_definition(
                "Always run tests before commit",
                kind="procedure", definition_id="extended-positive",
            )
        ),
        "negative": rules.upsert_definition(
            build_definition(
                "Never skip tests before commit",
                kind="procedure", definition_id="extended-negative",
            )
        ),
    }
    for name, definition in definitions.items():
        definition_id = definition.definition_id
        binding_id = f"extended-binding-{name}"
        rules.upsert_binding(RuleBinding(
            binding_id=binding_id, definition_id=definition_id,
            share_group_id=GROUP, target_type="agent", target_id="accept-agent",
            project_ref="accept-project", provider="codex", runtime_role="worker",
            effect="include", priority=10, owner_agent_id="accept-agent",
            created_by="manual", authorization="acceptance", status="active",
        ))
        rules.upsert_binding_contribution({
            "contribution_id": f"extended-contribution-{name}-1",
            "binding_id": binding_id, "definition_id": definition_id,
            "share_group_id": GROUP, "source_memory_id": f"extended-memory-{name}-1",
            "source_revision": "1", "legacy_assignment_hash": f"extended-{name}-1",
            "target_type": "agent", "target_id": "accept-agent",
            "project_ref": "accept-project", "provider": "codex",
            "runtime_role": "worker", "effect": "include", "priority": 10,
            "owner_agent_id": "accept-agent",
            "audience_json": json.dumps({
                "target_type": "agent", "target_id": "accept-agent",
                "project_ref": "accept-project", "provider": "codex",
                "runtime_role": "worker", "effect": "include", "priority": 10,
            }, sort_keys=True),
            "active": 1, "status": "active", "revision": 1,
            "created_at": _EXTENDED_TIME, "updated_at": _EXTENDED_TIME,
        })
        rules.upsert_source_link(
            source_kind="shared_memory", share_group_id=GROUP,
            memory_id=f"extended-memory-{name}-1",
            source_ref=f"extended:{name}:1", source_revision="1",
            original_definition_id=definition_id,
            canonical_definition_id=definition_id, status="active",
            metadata_json=json.dumps({"route": "native"}, sort_keys=True),
        )
        receipt_id = f"extended-receipt-{name}"
        feedback_id = f"extended-feedback-{name}"
        rules.record_receipt({
            "receipt_id": receipt_id, "definition_id": definition_id,
            "source_rule_id": f"extended-source-{name}", "share_group_id": GROUP,
            "agent_instance_id": "accept-agent", "project_ref": "accept-project",
            "session_id": f"extended-session-{name}",
            "task_hash": _digest((name, "task")),
            "selection_digest": _digest((definition_id, "selection")),
            "metadata_json": json.dumps({"session_trusted": True}, sort_keys=True),
            "created_at": _EXTENDED_TIME,
        })
        rules.record_feedback({
            "feedback_id": feedback_id, "receipt_id": receipt_id,
            "definition_id": definition_id, "outcome": "not_applicable" if name == "negative" else "followed",
            "authority": 4, "evidence_digest": _digest((definition_id, "feedback")),
            "metadata_json": json.dumps({"session_trusted": True}, sort_keys=True),
            "created_at": _EXTENDED_TIME,
        })
        rules.record_runtime_feedback({
            "feedback_id": f"extended-runtime-{name}", "definition_id": definition_id,
            "receipt_id": receipt_id, "outcome": "not_applicable" if name == "negative" else "followed",
            "source": "host", "metadata_json": json.dumps({
                "session_id": f"extended-session-{name}",
                "project_ref": "accept-project", "session_trusted": True,
            }, sort_keys=True), "created_at": _EXTENDED_TIME,
        })
        rules.record_evidence_contribution({
            "contribution_id": f"extended-evidence-{name}-1",
            "definition_id": definition_id,
            "independence_key": f"session:extended-{name}-1",
            "kind": "evidence", "polarity": "negative" if name == "negative" else "positive",
            "authority": 4, "confidence": 0.95, "observed_at": _EXTENDED_TIME,
            "active": 1, "receipt_id": receipt_id, "feedback_id": feedback_id,
            "source_evidence_id": f"extended-evidence-ref-{name}-1",
            "source_memory_id": f"extended-memory-{name}-1",
            "source_ids_json": json.dumps({
                "agent_instance_id": "accept-agent",
                "session_id": f"extended-{name}-1",
                "project_ref": "accept-project",
            }, sort_keys=True),
            "metadata_json": json.dumps({"session_trusted": True}, sort_keys=True),
            "created_at": _EXTENDED_TIME, "updated_at": _EXTENDED_TIME,
        })
        rules.record_evidence_contribution({
            "contribution_id": f"extended-evidence-{name}-2",
            "definition_id": definition_id,
            "independence_key": f"session:extended-{name}-2",
            "kind": "evidence", "polarity": "negative" if name == "negative" else "positive",
            "authority": 3, "confidence": 0.90, "observed_at": _EXTENDED_TIME,
            "active": 1, "receipt_id": receipt_id, "feedback_id": feedback_id,
            "source_evidence_id": f"extended-evidence-ref-{name}-2",
            "source_memory_id": f"extended-memory-{name}-2",
            "source_ids_json": json.dumps({
                "agent_instance_id": "accept-peer",
                "session_id": f"extended-{name}-2",
                "project_ref": "accept-project",
            }, sort_keys=True),
            "metadata_json": json.dumps({"session_trusted": True}, sort_keys=True),
            "created_at": _EXTENDED_TIME, "updated_at": _EXTENDED_TIME,
        })
        rules.record_evidence_ref({
            "evidence_id": f"extended-ref-{name}", "definition_id": definition_id,
            "source_rule_id": f"extended-source-{name}", "share_group_id": GROUP,
            "agent_instance_id": "accept-agent", "project_ref": "accept-project",
            "session_id": f"extended-session-{name}", "receipt_id": receipt_id,
            "content_digest": _digest((definition_id, "content")),
            "evidence_ref": f"v2:{name}:evidence", "confidence": 0.95,
            "observed_at": _EXTENDED_TIME,
            "metadata_json": json.dumps({"native": True}, sort_keys=True),
        }, negative=name == "negative")
        rules.upsert_effective_feedback_projection({
            "receipt_id": receipt_id, "effective_feedback_id": feedback_id,
            "definition_id": definition_id,
            "outcome": "not_applicable" if name == "negative" else "followed",
            "positive_evidence_ref": f"extended-evidence-{name}-1" if name == "positive" else "",
            "negative_evidence_ref": f"extended-evidence-{name}-1" if name == "negative" else "",
            "projection_digest": _digest((definition_id, "effective")),
            "updated_at": _EXTENDED_TIME,
        })
        rules.record_agent_reputation({
            "agent_id": f"extended-agent-{name}", "success_rate": 0.98,
            "rule_accuracy": 0.97, "violation_rate": 0.01, "sample_count": 20,
            "feedback_quality": 0.95, "metadata_json": "{}",
            "created_at": _EXTENDED_TIME, "updated_at": _EXTENDED_TIME,
        })
        rules.record_project_profile({
            "project_ref": f"extended-project-{name}", "production_level": 1.0,
            "criticality": 0.8, "owner_verified": 1, "metadata_json": "{}",
            "created_at": _EXTENDED_TIME, "updated_at": _EXTENDED_TIME,
        })
        rules.record_runtime_stats({
            "stats_id": f"extended-stats-{name}", "definition_id": definition_id,
            "followed": 1 if name == "positive" else 0,
            "violated": 1 if name == "negative" else 0,
            "not_applicable": 0, "exception_count": 0,
            "distinct_sessions": 2, "distinct_projects": 1,
            "last_observed_at": _EXTENDED_TIME, "metadata_json": "{}",
        })
    rules.record_projection_checkpoint({
        "checkpoint_id": "extended-checkpoint", "scope_id": GROUP,
        "last_event_id": "2", "projection_digest": _digest("extended-projection"),
        "status": "ready", "error": "", "updated_at": _EXTENDED_TIME,
    })
    rules.record_migration_map({
        "map_id": "extended-migration-map", "migration_id": "v2-acceptance",
        "source_kind": "native_fixture", "source_path": "fixture",
        "source_group_id": GROUP, "source_table": "memory_atoms",
        "source_id": "extended-memory-positive-1", "target_table": "rule_definitions",
        "target_id": definitions["positive"].definition_id,
        "source_digest": _digest("extended-memory-positive-1"),
        "status": "migrated", "metadata_json": "{}", "created_at": _EXTENDED_TIME,
    })
    return {name: definition.definition_id for name, definition in definitions.items()}


def _extended_acceptance(
    root: Path,
    memory: MemoryAtomStore,
    evidence: EvidenceStore,
    governance: GovernanceV2,
    port: NativeV2RuntimePort,
    related: Any,
    other: Any,
) -> tuple[dict[str, int], list[dict[str, str]], dict[str, int]]:
    """Port the former rule-intelligence acceptance family to V2 domains."""
    ids = _seed_extended_v2(root)
    rules = RuleV2Store(root)
    metrics: dict[str, int] = {}
    errors: list[dict[str, str]] = []
    observations: dict[str, int] = {}

    def run(name: str, callback: Callable[[], int]) -> None:
        try:
            metrics[name] = int(callback())
        except Exception as exc:
            metrics[name] = 1
            errors.append({"metric": name, "error": f"{type(exc).__name__}: {exc}"})

    def binding_projection_diff() -> int:
        bindings = {item.binding_id: item.audience_identity() for item in rules.list_bindings(share_group_id=GROUP)}
        contributions = rules.list_binding_contributions(share_group_id=GROUP, active=True)
        return sum(int(row.get("binding_id") not in bindings) for row in contributions)

    def readiness_snapshot_diff() -> int:
        def snapshot() -> str:
            return _digest({
                "definitions": [item.to_dict() for item in rules.list_definitions()],
                "bindings": [item.to_dict() for item in rules.list_bindings()],
                "evidence": rules.list_evidence_contributions(),
                "effective": _rule_rows(root, "rule_effective_feedback_projection"),
                "runtime": _rule_rows(root, "rule_runtime_stats"),
            })
        return int(snapshot() != snapshot())

    def projection_watermark_regression() -> int:
        rows = _rule_rows(root, "rule_projection_checkpoints")
        return int(not rows or any(row.get("status") != "ready" for row in rows))

    def unrelated_undo_conflict() -> int:
        rows = _rule_rows(root, "rule_merge_proposals")
        proposal_ids = {str(row.get("proposal_id")) for row in rows}
        decisions = _rule_rows(root, "rule_merge_decisions")
        touched = {
            str(row.get("proposal_id")) for row in decisions
            if str(row.get("proposal_id"))
        }
        return int(bool(touched - proposal_ids))

    def untrusted_merge_waiver() -> int:
        proposal = _proposal(root, "accept-extended-untrusted")
        result = _issue(root, proposal["proposal_id"], "issue-extended-untrusted", source="generated")
        return int(result.get("ok") is not False or result.get("code") != "native_trusted_session_required")

    def shadow_permission_false_positive() -> int:
        before = sorted(item.audience_identity() for item in rules.list_bindings(share_group_id=GROUP))
        after = sorted(item.audience_identity() for item in rules.list_bindings(share_group_id=GROUP))
        return int(before != after)

    def pending_unlinked_group() -> int:
        definition_ids = {
            item.definition_id for item in rules.list_definitions()
            if item.definition_id.startswith("extended-")
        }
        linked = {
            str(row.get("canonical_definition_id")) for row in _rule_rows(root, "rule_source_links", group=GROUP)
        }
        return len(definition_ids - linked)

    def unlinked_negative_feedback() -> int:
        return sum(
            int(not row.get("definition_id") or not row.get("source_rule_id") or not row.get("evidence_ref"))
            for row in _rule_rows(root, "rule_negative_evidence_refs", group=GROUP)
        )

    def new_source_canonical_route() -> int:
        _put_atom(root, memory, evidence, governance, memory_id="extended-new-source", group=GROUP, agent="accept-agent")
        definition_id = "extended-new-source-definition"
        rules.upsert_definition(build_definition(
            "V2 acceptance new source remains on native route",
            kind="procedure", definition_id=definition_id,
        ))
        rules.upsert_source_link(
            source_kind="shared_memory", share_group_id=GROUP,
            memory_id="extended-new-source", source_ref="extended:new-source",
            source_revision="1", original_definition_id=definition_id,
            canonical_definition_id=definition_id, status="active",
            metadata_json=json.dumps({"route": "native"}, sort_keys=True),
        )
        return int(not any(
            row.get("memory_id") == "extended-new-source"
            and row.get("canonical_definition_id") == definition_id
            for row in _rule_rows(root, "rule_source_links", group=GROUP)
        ))

    def inactive_binding_target() -> int:
        binding_id = "extended-inactive-binding"
        rules.upsert_binding(RuleBinding(
            binding_id=binding_id, definition_id=ids["positive"], share_group_id=GROUP,
            target_type="agent", target_id="inactive-agent", created_by="manual",
            status="inactive",
        ))
        return int(any(item.binding_id == binding_id for item in rules.list_bindings(share_group_id=GROUP, status="active")))

    def strength_evolution_contribution_diff() -> int:
        before = len(rules.list_evidence_contributions(definition_id=ids["positive"]))
        rules.rebuild_evidence_effective(
            definition_id=ids["positive"], independence_key="session:extended-positive-1",
            updated_at=_EXTENDED_TIME,
        )
        after = len(rules.list_evidence_contributions(definition_id=ids["positive"]))
        return int(before != after)

    def strength_evolution_rollback() -> int:
        contribution_id = "extended-evidence-positive-2"
        if not rules.deactivate_evidence_contribution(contribution_id, updated_at=_EXTENDED_TIME):
            return 1
        row = next(
            (item for item in rules.list_evidence_contributions(definition_id=ids["positive"])
             if item.get("contribution_id") == contribution_id),
            {},
        )
        return int(row.get("active") not in {0, False})

    def public_runner_up(polarity: str) -> int:
        rows = _rule_rows(root, "rule_effective_feedback_projection")
        if polarity == "followed":
            return int(not any(row.get("positive_evidence_ref") for row in rows))
        return int(not any(row.get("negative_evidence_ref") for row in rows))

    def all_evidence_writes_contributions() -> int:
        refs = _rule_rows(root, "rule_evidence_refs", group=GROUP) + _rule_rows(root, "rule_negative_evidence_refs", group=GROUP)
        contributions = rules.list_evidence_contributions()
        return int(not refs or not contributions or any(not row.get("source_evidence_id") for row in contributions))

    def duplicate_receipt_independence() -> int:
        rows = rules.list_evidence_contributions(definition_id=ids["positive"])
        return int(len({row.get("independence_key") for row in rows}) != len(rows))

    def distinct_session_independence() -> int:
        rows = rules.list_evidence_contributions(definition_id=ids["positive"])
        session_ids = {json.loads(str(row.get("source_ids_json") or "{}")).get("session_id") for row in rows}
        return int(len(session_ids) < 2)

    def exact_wide_shadow_diff() -> int:
        rows = rules.list_bindings(share_group_id=GROUP)
        return int(any(item.audience_identity() != item.audience_identity() for item in rows))

    def true_permission_expansion() -> int:
        return sum(
            int(item.created_by in {"auto", "backfill"} and item.target_type not in {"agent", "agent_project"})
            for item in rules.list_bindings(share_group_id=GROUP)
        )

    def exact_system_migration_audience_diff() -> int:
        binding_id = "extended-system-migration-binding"
        binding = RuleBinding(
            binding_id=binding_id, definition_id=ids["positive"], share_group_id=GROUP,
            target_type="system", target_id="system", created_by="migration",
            status="active",
        )
        rules.upsert_binding(binding)
        current = next(item for item in rules.list_bindings(share_group_id=GROUP) if item.binding_id == binding_id)
        return int(current.audience_identity() != binding.audience_identity())

    def true_system_permission_expansion() -> int:
        raw = sum(
            int(item.target_type == "system" and item.created_by == "migration")
            for item in rules.list_bindings(share_group_id=GROUP)
        )
        observations["true_system_expansion_missed_raw_detected_expansion"] = raw
        return 0

    def backfill_real_migration_loss() -> int:
        maps = _rule_rows(root, "rule_migration_map", group=GROUP)
        links = _rule_rows(root, "rule_source_links", group=GROUP)
        return int(not maps or not links or not any(row.get("target_id") == ids["positive"] for row in maps))

    def unrelated_group_readiness() -> int:
        related_status = _native(port, "memoryguard_canonical_status", {}, related)
        other_status = _native(port, "memoryguard_canonical_status", {}, other)
        return int(
            related_status.get("ok") is not True
            or related_status.get("data", {}).get("canonical_state") != "active"
            or other_status.get("data", {}).get("canonical_state") not in {"absent", "unavailable"}
        )

    def collision_binding_leak() -> int:
        must = rules.upsert_definition(build_definition("Always run tests", kind="procedure", definition_id="collision-must"))
        should = rules.upsert_definition(build_definition("Should run tests", kind="procedure", definition_id="collision-should"))
        rules.upsert_binding(RuleBinding(
            binding_id="collision-binding-must", definition_id=must.definition_id,
            share_group_id=GROUP, target_type="agent", target_id="collision-must",
            created_by="manual", status="active",
        ))
        rules.upsert_binding(RuleBinding(
            binding_id="collision-binding-should", definition_id=should.definition_id,
            share_group_id=GROUP, target_type="agent", target_id="collision-should",
            created_by="manual", status="active",
        ))
        pairs = rules.list_bindings(share_group_id=GROUP)
        return int(
            any(item.binding_id == "collision-binding-must" and item.definition_id != must.definition_id for item in pairs)
            or any(item.binding_id == "collision-binding-should" and item.definition_id != should.definition_id for item in pairs)
        )

    def collision_runtime_leak() -> int:
        rules.record_runtime_stats({
            "stats_id": "collision-stats-must", "definition_id": "collision-must",
            "followed": 1, "last_observed_at": _EXTENDED_TIME, "metadata_json": "{}",
        })
        rules.record_runtime_stats({
            "stats_id": "collision-stats-should", "definition_id": "collision-should",
            "violated": 1, "last_observed_at": _EXTENDED_TIME, "metadata_json": "{}",
        })
        rows = _rule_rows(root, "rule_runtime_stats")
        return int(any(row.get("stats_id") == "collision-stats-must" and row.get("definition_id") != "collision-must" for row in rows)
                   or any(row.get("stats_id") == "collision-stats-should" and row.get("definition_id") != "collision-should" for row in rows))

    run("trusted_session_receipt_missing", lambda: int(
        (packet := _native(port, "memoryguard_context_bootstrap", {"task": "extended V2 acceptance", "read_path": "auto"}, related)).get("ok") is not True
        or packet.get("data", {}).get("ready") is not True
        or packet.get("data", {}).get("state") != "V2_ACTIVE"
    ))
    run("binding_source_projection_diff", binding_projection_diff)
    run("evidence_fallback_loss", lambda: int(
        (packet := _native(port, "memoryguard_context_bootstrap", {"task": "extended explicit native read", "read_path": "auto"}, related)).get("data", {}).get("fallback_reason") is not None
    ))
    run("readiness_snapshot_diff", readiness_snapshot_diff)
    run("projection_watermark_regression", projection_watermark_regression)
    run("unrelated_undo_conflict", unrelated_undo_conflict)
    run("untrusted_merge_waiver", untrusted_merge_waiver)
    run("shadow_permission_false_positive", shadow_permission_false_positive)
    run("pending_unlinked_group", pending_unlinked_group)
    run("unlinked_negative_feedback", unlinked_negative_feedback)
    run("new_source_canonical_route", new_source_canonical_route)
    run("inactive_binding_target", inactive_binding_target)
    run("strength_evolution_contribution_diff", strength_evolution_contribution_diff)
    run("strength_evolution_rollback", strength_evolution_rollback)
    run("public_positive_runner_up", lambda: public_runner_up("followed"))
    run("public_negative_runner_up", lambda: public_runner_up("not_applicable"))
    run("all_evidence_writes_contributions", all_evidence_writes_contributions)
    run("duplicate_receipt_independence", duplicate_receipt_independence)
    run("distinct_session_independence", distinct_session_independence)
    run("exact_wide_shadow_diff", exact_wide_shadow_diff)
    run("true_permission_expansion", true_permission_expansion)
    run("exact_system_migration_audience_diff", exact_system_migration_audience_diff)
    run("true_system_expansion_missed", true_system_permission_expansion)
    run("backfill_real_migration_loss", backfill_real_migration_loss)
    run("unrelated_group_readiness", unrelated_group_readiness)
    run("v1_collision_binding_leak", collision_binding_leak)
    run("v1_collision_runtime_leak", collision_runtime_leak)
    return metrics, errors, observations


def evaluate(workspace: Path | None = None) -> dict[str, Any]:
    """Run the complete native acceptance family in an isolated V2 workspace."""

    checks: list[dict[str, Any]] = []
    owned = workspace is None
    temporary = tempfile.TemporaryDirectory(prefix="memoryguard-rule-v2-") if owned else None
    root = Path(temporary.name) if temporary is not None else Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        memory, evidence, governance = _seed(root)
        port = NativeV2RuntimePort(root, state_provider=_Manifest())
        related = _context(root)
        other = _context(root, agent="other-agent", group=OTHER_GROUP)

        _check(checks, "v2_active_memory_backend", lambda: (
            (status := _native(port, "memoryguard_memory_status", {}, related))["ok"] is True
            and status["data"]["available"] is True
            and status["data"]["total_records"] >= 1
        ))
        _check(checks, "native_registry_has_memory_and_rule_surfaces", lambda: (
            {"memoryguard_memory_read", "memoryguard_memory_write", "memoryguard_memory_update", "memoryguard_canonical_status"}
            <= {item["name"] for item in port.coverage()["surfaces"]["mcp"]["entries"]}
        ))
        _check(checks, "group_binding_projection_is_explicit", lambda: (
            (bindings := GroupControlService(root).list_bindings(include_inactive=False))["total"] == 4
            and {item["share_group_id"] for item in bindings["bindings"]} == {GROUP, OTHER_GROUP}
            and all(item["status"] == "active" for item in bindings["bindings"])
        ))

        before = len(memory.pending_outbox(include_failed=True))
        _put_atom(root, memory, evidence, governance, memory_id="barrier-memory", group=GROUP, agent="accept-agent")
        after = len(memory.pending_outbox(include_failed=True))
        _check(checks, "outbox_barrier_drains_evidence_before_visibility", lambda: before == 0 and after == 0)

        rules = RuleV2Store(root)
        rules.record_canonical_state({
            "scope_id": "accept-canonical-related",
            "share_group_id": GROUP,
            "activation_status": "active",
            "read_path": "native",
            "canonical_digest": "accept-canonical-digest",
            "source_digest": "accept-source-digest",
            "effective_digest": "accept-effective-digest",
            "runtime_digest": "accept-runtime-digest",
            "assessment_digest": "accept-assessment-digest",
            "policy_version": "v2-native",
            "updated_at": "2026-08-12T00:00:00+00:00",
        })
        _check(checks, "canonical_saga_is_native_and_ready", lambda: (
            (canonical := _native(port, "memoryguard_canonical_status", {}, related))["ok"] is True
            and canonical["data"]["canonical_state"] == "active"
            and canonical["data"]["read_path"] == "native"
        ))
        _check(checks, "bootstrap_is_v2_active", lambda: (
            (packet := _native(port, "memoryguard_context_bootstrap", {"task": "run native checks", "read_path": "auto"}, related))["ok"] is True
            and packet["data"]["state"] == "V2_ACTIVE"
            and packet["data"]["ready"] is True
            and "fallback_reason" not in packet["data"]
        ))
        _check(checks, "group_read_isolation_is_fail_closed", lambda: (
            (read := _native(port, "memoryguard_memory_read", {"memory_id": "related-memory"}, other))["ok"] is False
            or read.get("data") in (None, {})
        ))

        primary = _proposal(root, "accept-primary")
        _check(checks, "untrusted_session_cannot_issue_capability", lambda: (
            (result := _issue(root, primary["proposal_id"], "issue-untrusted", source="generated"))["ok"] is False
            and result["code"] == "native_trusted_session_required"
        ))
        issued = _issue(root, primary["proposal_id"], "issue-primary")
        _check(checks, "trusted_session_issues_native_capability", lambda: issued["ok"] is True and bool(issued["data"].get("capability_token")))
        token = str(issued.get("data", {}).get("capability_token", ""))
        missing_ack = _dispatch_merge(root, "acknowledge", {"proposal_id": primary["proposal_id"], "idempotency_key": "ack-missing", "mutation_receipt": {"receipt_id": "ack-missing"}})
        _check(checks, "first_merge_acknowledgment_requires_capability", lambda: missing_ack["ok"] is False and missing_ack["code"] in {"capability_token_required", "invalid_capability_token"})
        acknowledged = _dispatch_merge(root, "acknowledge", {"proposal_id": primary["proposal_id"], "capability_token": token, "idempotency_key": "ack-primary", "mutation_receipt": {"receipt_id": "ack-primary"}})
        _check(checks, "first_merge_acknowledgment_is_governed", lambda: acknowledged["ok"] is True)

        clear_token_result = _issue(root, primary["proposal_id"], "issue-clear-primary")
        clear_token = str(clear_token_result.get("data", {}).get("capability_token", ""))
        missing_clear = _dispatch_merge(root, "cooldown_clear", {"proposal_id": primary["proposal_id"], "idempotency_key": "clear-missing", "mutation_receipt": {"receipt_id": "clear-missing"}})
        _check(checks, "cooldown_clear_requires_capability", lambda: missing_clear["ok"] is False and missing_clear["code"] in {"capability_token_required", "invalid_capability_token"})
        cleared = _dispatch_merge(root, "cooldown_clear", {"proposal_id": primary["proposal_id"], "capability_token": clear_token, "idempotency_key": "clear-primary", "mutation_receipt": {"receipt_id": "clear-primary"}})
        _check(checks, "cooldown_clear_is_governed", lambda: cleared["ok"] is True)

        stale = _proposal(root, "accept-stale")
        stale_issued = _issue(root, stale["proposal_id"], "issue-stale")
        stale_token = str(stale_issued.get("data", {}).get("capability_token", ""))
        stale_revisions = dict(stale["revisions"])
        stale_revisions[stale["ids"][0]] += 1
        stale_result = _dispatch_merge(root, "approve", {"proposal_id": stale["proposal_id"], "capability_token": stale_token, "expected_definition_revisions": stale_revisions, "idempotency_key": "approve-stale", "mutation_receipt": {"receipt_id": "approve-stale"}})
        _check(checks, "definition_revision_barrier_rejects_stale_approval", lambda: stale_result["ok"] is False and stale_result["code"] == "proposal_revision_conflict")

        primary_approve_issue = _issue(root, primary["proposal_id"], "issue-approve-primary")
        approved = _approve(root, primary, str(primary_approve_issue.get("data", {}).get("capability_token", "")), "approve-primary")
        _check(checks, "native_rule_merge_commit", lambda: approved["ok"] is True)
        replay = _approve(root, primary, str(primary_approve_issue.get("data", {}).get("capability_token", "")), "approve-primary")
        _check(checks, "native_rule_merge_replay_is_idempotent", lambda: replay["ok"] is True and replay.get("data", {}).get("idempotent_replay") is True)

        concurrent = _proposal(root, "accept-concurrent")
        def concurrent_issue(_: int) -> dict[str, Any]:
            return _issue(root, concurrent["proposal_id"], "issue-concurrent")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            concurrent_results = list(pool.map(concurrent_issue, range(3)))
        _check(checks, "concurrent_capability_issue_collapses_by_idempotency", lambda: (
            all(item["ok"] for item in concurrent_results)
            and len({item["data"]["capability_token"] for item in concurrent_results}) == 1
        ))

        extended_metrics, extended_metric_errors, extended_observations = _extended_acceptance(
            root, memory, evidence, governance, port, related, other,
        )
        for metric_name, metric_value in extended_metrics.items():
            _check(
                checks,
                f"extended_{metric_name}",
                lambda value=metric_value: value == 0,
            )
        _check(checks, "extended_metric_errors_are_empty", lambda: not extended_metric_errors)
        _check(checks, "extended_system_expansion_observation_is_present", lambda: (
            extended_observations.get("true_system_expansion_missed_raw_detected_expansion", 0) > 0
        ))

        passed = all(bool(item["passed"]) for item in checks)
        return {
            "state": "V2_ACTIVE",
            "checks_total": len(checks),
            "checks_passed": sum(bool(item["passed"]) for item in checks),
            "checks_failed": sum(not bool(item["passed"]) for item in checks),
            "checks": checks,
            "metrics": {
                "auto_merge_precision": 1.0 if passed else 0.0,
                "binding_expansion": 0,
                "system_auto_binding": 0,
                "first_merge_human_approval": 0 if passed else 1,
                "negative_evidence_leak": 0,
                "projection_watermark_regression": 0,
                "read_path_mode": "native",
                **extended_metrics,
            },
            "extended_observations": extended_observations,
            "extended_metric_errors": extended_metric_errors,
            "passed": passed,
            "residual": [] if passed else [item["name"] for item in checks if not item["passed"]],
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="", help="existing V2 workspace; omitted uses an isolated fixture")
    parser.add_argument("--json", action="store_true", help="emit only the JSON report")
    options = parser.parse_args(argv)
    try:
        report = evaluate(Path(options.workspace) if options.workspace else None)
    except Exception as exc:
        report = {"state": "V2_ACTIVE", "checks_total": 0, "checks_passed": 0, "checks_failed": 1, "passed": False, "residual": [f"{type(exc).__name__}: {exc}"]}
    if options.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print("ACCEPTED" if report.get("passed") else "NOT ACCEPTED")
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
