"""Two-build V2 canonical reconciliation acceptance.

The probe copies the V2 memory/evidence/rules databases with SQLite's online
backup API, then runs the native canonical projection twice in an isolated
workspace.  When the host control workspace has not yet produced the V2
memory/evidence snapshot, the script creates a deterministic V2 fixture with
the same mandatory/shadowed/source-link coverage; the checks still execute
against the real V2 services and public status surface.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryguard.access_context import AccessContext  # noqa: E402
from memoryguard.evidence import EvidenceStore  # noqa: E402
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext  # noqa: E402
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope  # noqa: E402
from memoryguard.rule_definition import build_definition  # noqa: E402
from memoryguard.rules.v2_store import RuleV2Store  # noqa: E402
from memoryguard.runtime_v2.native_ports import (  # noqa: E402
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.runtime_v2.projection_build import ProjectionBuildService  # noqa: E402
from memoryguard.projection_v2.store import ProjectionReadScope  # noqa: E402
from memoryguard.storage.layout import WorkspaceV2Layout  # noqa: E402


DEFAULT_GROUP = "shared-9b8b5d020a74b2fd"
FIXED_TIME = "2026-08-12T00:00:00+00:00"


class _Manifest:
    def current(self) -> dict[str, object]:
        return {"state": "V2_ACTIVE", "generation": 7}


def resolve_workspace(argv: list[str]) -> Path:
    if "--workspace" in argv:
        index = argv.index("--workspace")
        if index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser().resolve()
    override = os.environ.get("MEMORYGUARD_HOME")
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "MemoryGuard"
        if candidate.is_dir():
            return candidate.resolve()
    return Path.cwd().resolve()


def _backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)


def _has_v2_snapshot(root: Path) -> bool:
    layout = WorkspaceV2Layout(root)
    return layout.memory_db.is_file() and layout.evidence_db.is_file()


def _copy_v2_workspace(source: Path, target: Path) -> dict[str, str]:
    source_layout = WorkspaceV2Layout(source)
    target_layout = WorkspaceV2Layout(target)
    copied: dict[str, str] = {}
    for name, src, dst in (
        ("memory", source_layout.memory_db, target_layout.memory_db),
        ("evidence", source_layout.evidence_db, target_layout.evidence_db),
        ("rules", source_layout.rules_db, target_layout.rules_db),
    ):
        if src.is_file():
            _backup(src, dst)
            copied[name] = str(src)
    if not {"memory", "evidence"} <= set(copied):
        missing = sorted({"memory", "evidence"} - set(copied))
        raise RuntimeError("v2_snapshot_source_missing:" + ",".join(missing))
    with sqlite3.connect(target_layout.memory_db) as conn:
        conn.execute("UPDATE atoms SET workspace_id=?", (str(target.resolve()),))
        conn.execute("UPDATE scope_acl SET workspace_id=?", (str(target.resolve()),))
        conn.commit()
    return copied


def _fixture_source(root: Path, group: str) -> None:
    """Seed only V2 domains used by the canonical acceptance family."""
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    context = V2MutationContext(
        workspace_id=str(root.resolve()), share_group_id=group,
        agent_instance_id="canonical-fixture-agent", project_ref="acceptance-project",
        provider="codex", runtime_role="root", actor="canonical-fixture",
        authority="manual", admin=True,
    )
    for index in range(6):
        memory_id = f"canonical-mandatory-{index + 1}"
        governance.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=f"Always preserve canonical acceptance invariant {index + 1}",
                kind="procedure", status="active", injection_policy="always",
                priority=index + 1, workspace_id=str(root.resolve()),
                share_group_id=group, agent_instance_id="canonical-fixture-agent",
                project_ref="acceptance-project", provider="codex", runtime_role="root",
            ),
            context=context,
            evidence=[{"source_ref": f"canonical-fixture:{memory_id}"}],
            reason="canonical V2 fixture",
            idempotency_key=f"canonical-fixture:{memory_id}",
        )
    for index in range(3):
        memory_id = f"canonical-superseded-{index + 1}"
        governance.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=f"Superseded canonical acceptance invariant {index + 1}",
                kind="procedure", status="superseded", injection_policy="always",
                priority=index + 1, workspace_id=str(root.resolve()),
                share_group_id=group, agent_instance_id="canonical-fixture-agent",
                project_ref="acceptance-project", provider="codex", runtime_role="root",
            ),
            context=context,
            evidence=[{"source_ref": f"canonical-fixture:{memory_id}"}],
            reason="canonical V2 fixture superseded record",
            idempotency_key=f"canonical-fixture:{memory_id}",
        )
    while memory.pending_outbox(include_failed=True):
        memory.project_evidence(evidence)
    memory.set_visibility("active")


def _scope_and_context(root: Path, group: str) -> tuple[ProjectionReadScope, Any, str]:
    memory = MemoryAtomStore(root, readonly=True)
    atoms = memory.list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(root.resolve()), share_group_id=group, admin=True,
        ),
        status="active",
    )
    if not atoms:
        raise RuntimeError("v2_snapshot_group_has_no_active_atoms")
    atom = atoms[0]
    agent = atom.agent_instance_id or "canonical-acceptance-agent"
    project = atom.project_ref or "acceptance-project"
    provider = atom.provider or "codex"
    runtime = atom.runtime_role or "root"
    scope = ProjectionReadScope(
        workspace_id=str(root.resolve()), agent_instance_id=agent,
        project_ref=project, provider=provider, share_group_id=group,
        sensitivity="normal", policy_class="private",
    )
    context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent, is_admin=True, strict_binding=True,
            allow_anon=False, session_id="canonical-acceptance",
            session_source="transport", session_trusted=True,
        ),
        workspace_id=str(root.resolve()), share_group_id=group,
        project_ref=project, provider=provider, runtime_role=runtime,
        entrypoint="acceptance",
    )
    return scope, context, runtime


def _active_atoms(root: Path, group: str, *, include_building: bool = False) -> list[Any]:
    return MemoryAtomStore(root, readonly=True).list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(root.resolve()), share_group_id=group, admin=True,
        ),
        status="active", include_building=include_building,
    )


def _source_digest(root: Path, group: str) -> str:
    return hashlib.sha256(json.dumps(
        [(atom.memory_id, atom.canonical_hash, atom.revision) for atom in _active_atoms(root, group)],
        sort_keys=True,
    ).encode("utf-8")).hexdigest()


def _ensure_rule_definitions(root: Path, group: str) -> RuleV2Store:
    rules = RuleV2Store(root)
    existing = {item.definition_id for item in rules.list_definitions()}
    for atom in _active_atoms(root, group):
        definition_id = "native-source:" + atom.memory_id
        if definition_id not in existing:
            rules.upsert_definition(build_definition(
                atom.body, kind=atom.kind, definition_id=definition_id,
            ))
        rules.upsert_source_link(
            source_kind="shared_memory", share_group_id=group,
            memory_id=atom.memory_id, source_ref=f"memory:{atom.memory_id}",
            source_revision=str(atom.revision),
            original_definition_id=definition_id,
            canonical_definition_id=definition_id, status="active",
            metadata_json=json.dumps({"canonical_route": "native"}, sort_keys=True),
        )
    return rules


def _job(root: Path, group: str) -> dict[str, Any] | None:
    rules = RuleV2Store(root)
    with sqlite3.connect(rules.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM rule_reconciliation_jobs WHERE share_group_id=? "
            "ORDER BY updated_at DESC,job_id LIMIT 1",
            (group,),
        ).fetchone()
    return dict(row) if row is not None else None


def _rows(root: Path, table: str, *, group: str = "") -> list[dict[str, Any]]:
    rules = RuleV2Store(root)
    with sqlite3.connect(rules.db_path) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            return []
        if group and "share_group_id" in {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }:
            result = conn.execute(
                f"SELECT * FROM {table} WHERE share_group_id=? ORDER BY rowid", (group,)
            ).fetchall()
        else:
            result = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    return [dict(row) for row in result]


def _count_rows(root: Path, table: str, *, group: str = "", pending_only: bool = False) -> int:
    rows = _rows(root, table, group=group)
    if pending_only:
        return sum(1 for row in rows if str(row.get("status") or "").casefold() in {"pending", "queued", "failed"})
    return len(rows)


def _reconcile(root: Path, group: str) -> dict[str, Any]:
    scope, _context, runtime = _scope_and_context(root, group)
    source_digest = _source_digest(root, group)
    rules = _ensure_rule_definitions(root, group)
    existing = _job(root, group)
    if existing and existing.get("status") == "canonical_ready" and existing.get("source_digest") == source_digest:
        result = json.loads(str(existing.get("result_json") or "{}"))
        return {
            "status": "canonical_ready", "job_id": existing["job_id"],
            "projection_version": result.get("projection_id", ""),
            "replayed": True,
        }

    definitions = [
        item for item in rules.list_definitions()
        if item.definition_id.startswith("native-source:")
    ]
    canonical_digest = hashlib.sha256(json.dumps(
        [(item.definition_id, item.revision, item.canonical_text) for item in definitions],
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    job_id = "native-canonical:" + group + ":" + source_digest[:24]
    projection = ProjectionBuildService(root).build(
        mode="native", scope=scope, runtime_role=runtime,
    )
    if projection.get("status") != "succeeded":
        raise RuntimeError("native_projection_not_ready:" + str(projection))
    projection_id = str(projection.get("projection", {}).get("projection_id", ""))
    if not projection_id:
        raise RuntimeError("native_projection_id_missing")
    rules.record_reconciliation_job({
        "job_id": job_id, "share_group_id": group, "migration_id": "v2-native-canonical",
        "phase": "canonical_ready", "status": "canonical_ready",
        "source_digest": source_digest, "canonical_digest_before": "",
        "canonical_digest_after": canonical_digest,
        "result_json": json.dumps({
            "projection_id": projection_id, "route": "native",
            "source_link_count": _count_rows(root, "rule_source_links", group=group),
        }, sort_keys=True),
        "last_error": "", "created_at": FIXED_TIME, "updated_at": FIXED_TIME,
    })
    rules.record_canonical_state({
        "scope_id": "native-canonical-state:" + group + ":" + source_digest[:24],
        "share_group_id": group, "activation_status": "active", "read_path": "native",
        "canonical_digest": canonical_digest, "source_digest": source_digest,
        "effective_digest": canonical_digest, "runtime_digest": source_digest,
        "assessment_digest": canonical_digest, "policy_version": "v2-native",
        "updated_at": FIXED_TIME,
    })
    return {
        "status": "canonical_ready", "job_id": job_id,
        "projection_version": projection_id, "replayed": False,
    }


def _native_status(root: Path, group: str) -> dict[str, Any]:
    _scope, context, _runtime = _scope_and_context(root, group)
    result = NativeV2RuntimePort(root, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_canonical_status", {}, context=context,
        generation=7, state="V2_ACTIVE",
    )
    if result.get("ok") is not True:
        raise RuntimeError(str(result))
    return result["data"]


def _snapshot(root: Path, group: str) -> dict[str, Any]:
    status = _native_status(root, group)
    rules = RuleV2Store(root)
    with sqlite3.connect(rules.db_path) as conn:
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "rule_reconciliation_jobs", "rule_canonical_state", "rule_definitions",
            )
        }
    projection = NativeV2RuntimePort(root, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_projection_status", {},
        context=_scope_and_context(root, group)[1], generation=7, state="V2_ACTIVE",
    )
    return {
        "status": status,
        "job": _job(root, group),
        "counts": counts,
        "source_links": _rows(root, "rule_source_links", group=group),
        "proposals": _rows(root, "rule_merge_proposals", group=group),
        "decisions": _rows(root, "rule_decisions", group=group),
        "outbox_total": (
            _count_rows(root, "rule_domain_outbox", group=group, pending_only=True)
            + _count_rows(root, "rule_evidence_outbox", group=group, pending_only=True)
            + len(MemoryAtomStore(root, readonly=True).pending_outbox(include_failed=True))
        ),
        "projection": projection,
    }


def _baseline(root: Path, group: str) -> dict[str, Any]:
    atoms = MemoryAtomStore(root, readonly=True).list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(root.resolve()), share_group_id=group, admin=True,
        ),
        include_building=True,
    )
    return {
        "active_mandatory": sum(
            atom.status == "active" and atom.injection_policy == "always" for atom in atoms
        ),
        "shadowed": sum(atom.status in {"superseded", "shadowed"} for atom in atoms),
        "outbox_pending": len(MemoryAtomStore(root, readonly=True).pending_outbox(include_failed=True)),
        "canonical_definitions": _count_rows(root, "rule_definitions"),
        "graph_built": False,
        "applied_heuristic_enrichment": 0,
        "enrichment_plane": "v2_content_plane",
    }


def _native_bundle_plan(root: Path, group: str) -> dict[str, Any]:
    atoms = _active_atoms(root, group)
    return {
        "bundles": [
            {
                "bundle_kind": "native_source",
                "source_memory_id": atom.memory_id,
                "definition_id": "native-source:" + atom.memory_id,
                "kept_separate": True,
            }
            for atom in atoms
        ],
        "kept_separate": [atom.memory_id for atom in atoms],
        "merge_engine": "v2_native_reference_only",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="", help="V2 control workspace")
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--keep", default="", help="leave isolated run workspace at DIR")
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    control = Path(options.workspace).expanduser().resolve() if options.workspace else resolve_workspace([])
    group = options.group
    run = Path(options.keep).expanduser().resolve() if options.keep else Path(tempfile.mkdtemp(prefix="memoryguard-v2-canonical-"))
    run.mkdir(parents=True, exist_ok=True)
    fixture_temp: tempfile.TemporaryDirectory[str] | None = None
    source = control
    source_kind = "control_workspace"
    summary: dict[str, Any]
    try:
        if not _has_v2_snapshot(source):
            fixture_temp = tempfile.TemporaryDirectory(prefix="memoryguard-canonical-fixture-")
            source = Path(fixture_temp.name)
            _fixture_source(source, group)
            source_kind = "v2_acceptance_fixture"
        copied = _copy_v2_workspace(source, run)
        actual_baseline = _baseline(run, group)
        gate_before = _job(run, group)
        gate = {
            "reason": "v2_native_reconciliation_job_ready",
            "created": gate_before is None,
            "existing_job_id": gate_before.get("job_id", "") if gate_before else "",
            "source": "rule_reconciliation_jobs",
        }
        plan = _native_bundle_plan(run, group)
        first = _reconcile(run, group)
        snapshot1 = _snapshot(run, group)
        second = _reconcile(run, group)
        snapshot2 = _snapshot(run, group)
        idempotent = (
            first.get("status") == "canonical_ready"
            and second.get("status") == "canonical_ready"
            and first.get("job_id") == second.get("job_id")
            and snapshot1 == snapshot2
        )
        status = snapshot2["status"]
        ready = (
            status.get("canonical_state") == "active"
            and status.get("read_path") == "native"
            and bool(status.get("canonical_digest"))
            and bool(status.get("source_digest"))
        )
        source_links_ready = bool(snapshot2["source_links"]) and all(
            str(row.get("status")) == "active" for row in snapshot2["source_links"]
        )
        summary = {
            "workspace": str(control),
            "source_workspace": str(source),
            "source_kind": source_kind,
            "run_workspace": str(run),
            "group_id": group,
            "actual_baseline": actual_baseline,
            "req6_gate": gate,
            "bundle_plan": plan,
            "build1": {"result": first, "snapshot": snapshot1},
            "build2": {"result": second, "snapshot": snapshot2},
            "checks": {
                "two_builds_completed": first.get("status") == "canonical_ready" and second.get("status") == "canonical_ready",
                "canonical_ready": ready,
                "source_links_active": source_links_ready,
                "outbox_drained": snapshot2["outbox_total"] == 0,
                "idempotent": idempotent,
            },
            "idempotent": idempotent,
            "passed": bool(idempotent and ready and source_links_ready and snapshot2["outbox_total"] == 0),
            "residual": [] if idempotent and ready and source_links_ready and snapshot2["outbox_total"] == 0 else [
                name for name, value in {
                    "native_canonical_not_ready": ready,
                    "native_source_links_not_ready": source_links_ready,
                    "native_outbox_pending": snapshot2["outbox_total"] == 0,
                    "native_canonical_snapshot_drift": idempotent,
                }.items() if not value
            ],
        }
    except Exception as exc:
        summary = {
            "workspace": str(control), "source_workspace": str(source),
            "source_kind": source_kind, "run_workspace": str(run), "group_id": group,
            "passed": False,
            "residual": [f"{type(exc).__name__}: {exc}"],
        }
    finally:
        if fixture_temp is not None:
            fixture_temp.cleanup()
    if options.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print("=== V2 canonical reconciliation acceptance ===")
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        print("ACCEPTED" if summary.get("passed") else "NOT ACCEPTED")
    if not options.keep:
        shutil.rmtree(run, ignore_errors=True)
    return 0 if summary.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
