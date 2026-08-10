from __future__ import annotations

import sqlite3
import json
import subprocess
import sys
from pathlib import Path

import pytest

from memoryguard.assets_v2.store import AssetStore
from memoryguard.codegraph_v2.store import CodeGraphStore
from memoryguard.content.store import ContentStore
from memoryguard.evidence.store import EvidenceStore
from memoryguard.maintenance_v2.blob_sweep import (
    BlobSweepBlocked,
    BlobSweepExecutor,
    BlobSweepRecoveryRequired,
    SweepSafetyEvidence,
)
from memoryguard.maintenance_v2.api import MaintenanceV2Api
from memoryguard.maintenance_v2.runtime_port import bind_maintenance_transport_context
from memoryguard.maintenance_v2.models import CandidateState, MaintenanceContext, MaintenanceJobState, MaintenanceScope
from memoryguard.maintenance_v2.store import MaintenanceStore
from memoryguard.memory.store import MemoryAtomStore
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.skills_v2.store import SkillStore
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_database
from memoryguard.system.manifest import ManifestManager, ManifestState
from memoryguard.cutover_v2.facade import get_v2_runtime_facade


class _SafetyPort:
    def __init__(self, *proofs: str) -> None:
        self.proofs = proofs or ("stable-proof",)
        self.calls = 0

    def verify(self, *, workspace_id: str, scope_digest: str, lease_id: str, generation: int) -> SweepSafetyEvidence:
        proof = self.proofs[min(self.calls, len(self.proofs) - 1)]
        self.calls += 1
        return SweepSafetyEvidence(workspace_id, scope_digest, lease_id, generation, True, True, proof)


def _active_workspace(root: Path, *, held: bool = False):
    layout = WorkspaceV2Layout(root)
    layout.ensure_dirs()
    for domain, paths in layout.databases.items():
        for path in paths:
            try:
                initialize_database(path, domain if domain != "projection" else "projection", layout=layout)
            except Exception:
                pass
    for store_cls in (RuntimeStore, MemoryAtomStore, RuleV2Store, EvidenceStore, ContentStore, CodeGraphStore, AssetStore, SkillStore):
        store_cls(root)
    ProjectionStore(root)

    content = ContentStore(root)
    namespace = content.ensure_namespace(workspace_id=str(layout.workspace), trust_domain="p7-sweep")
    blob_id = content.put_blob(namespace.namespace_id, "orphan content blob")
    assert blob_id
    if held:
        content.hold_blob(blob_id, reason="legal-hold", source_ref="test")

    manager = ManifestManager(layout)
    manager.transition(ManifestState.V2_BUILDING, migration_id="p7-sweep-tests")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="source",
        target_digest="target",
        manifest_digest="manifest",
        digests={"validator_passed": True, "checkpoints": {"p7": True}},
    )
    active = manager.transition(ManifestState.V2_ACTIVE)
    scope = MaintenanceScope(workspace_id=str(layout.workspace), runtime_role="maintenance", trusted_context=True)
    base = MaintenanceContext.trusted(scope, actor_id="p7-sweep", expected_generation=active.generation)
    store = MaintenanceStore(root)
    lease = store.acquire_lease(base, ttl_seconds=300)
    context = MaintenanceContext.trusted(
        scope,
        actor_id="p7-sweep",
        maintenance_lease_id=lease.lease_id,
        expected_generation=active.generation,
    )
    return layout, content, blob_id, manager, store, context


def _blob_exists(content: ContentStore, blob_id: str) -> bool:
    with content.connection() as conn:
        return conn.execute("SELECT 1 FROM content_blobs WHERE blob_id=?", (blob_id,)).fetchone() is not None


def test_two_epoch_dry_run_is_zero_write_and_idempotent(tmp_path: Path) -> None:
    _layout, content, blob_id, manager, store, context = _active_workspace(tmp_path)
    executor = BlobSweepExecutor(tmp_path, maintenance_store=store, manifest=manager)
    first = executor.execute("dry-run", context)
    replay = executor.execute("dry-run", context)
    assert first == replay
    assert first.status == "PASS" and first.dry_run and first.candidate_count == 1 and first.swept_count == 0
    assert _blob_exists(content, blob_id)
    assert store.get_job(first.job_id).state is MaintenanceJobState.SUCCEEDED


def test_public_api_receipts_do_not_expose_reference_or_blob_ids(tmp_path: Path) -> None:
    _layout, _content, blob_id, manager, store, _context = _active_workspace(tmp_path)
    api = MaintenanceV2Api(tmp_path, maintenance_store=store, manifest=manager)
    audit = api.audit_references()
    encoded = json.dumps(audit, ensure_ascii=False, sort_keys=True)
    assert audit["candidate_count"] == 1 and blob_id not in encoded
    assert "references" not in audit and "candidates" not in audit
    report = api.storage_report("content")
    assert report["path"] == ".memoryguard/content/content.db"
    assert str(tmp_path) not in json.dumps(report, ensure_ascii=False)


def test_apply_deletes_only_stable_orphan_and_replay_is_identical(tmp_path: Path) -> None:
    _layout, content, blob_id, manager, store, context = _active_workspace(tmp_path)
    safety = _SafetyPort()
    executor = BlobSweepExecutor(tmp_path, maintenance_store=store, manifest=manager, safety_port=safety)
    first = executor.execute("apply", context, apply=True)
    replay = executor.execute("apply", context, apply=True)
    assert first == replay
    assert first.applied and first.swept_count == 1 and not _blob_exists(content, blob_id)
    candidates = store.list_job_candidates(first.job_id, context, epoch_number=2)
    assert len(candidates) == 1 and candidates[0].state is CandidateState.SWEPT
    assert safety.calls == 2


def test_active_hold_prevents_candidate_and_physical_delete(tmp_path: Path) -> None:
    _layout, content, blob_id, manager, store, context = _active_workspace(tmp_path, held=True)
    result = BlobSweepExecutor(tmp_path, maintenance_store=store, manifest=manager, safety_port=_SafetyPort()).execute("held", context, apply=True)
    assert result.candidate_count == result.swept_count == 0
    assert _blob_exists(content, blob_id)


def test_changed_safety_proof_blocks_before_delete(tmp_path: Path) -> None:
    _layout, content, blob_id, manager, store, context = _active_workspace(tmp_path)
    executor = BlobSweepExecutor(tmp_path, maintenance_store=store, manifest=manager, safety_port=_SafetyPort("proof-one", "proof-two"))
    with pytest.raises(BlobSweepBlocked, match="proof changed"):
        executor.execute("proof-change", context, apply=True)
    assert _blob_exists(content, blob_id)


def test_new_hold_between_final_audit_and_delete_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _layout, content, blob_id, manager, store, context = _active_workspace(tmp_path)
    executor = BlobSweepExecutor(
        tmp_path, maintenance_store=store, manifest=manager, safety_port=_SafetyPort()
    )
    original = executor._delete

    def add_hold_then_delete(*args, **kwargs):
        content.hold_blob(blob_id, reason="concurrent-hold", source_ref="race")
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "_delete", add_hold_then_delete)
    with pytest.raises(BlobSweepBlocked, match="gained a reference"):
        executor.execute("concurrent-hold", context, apply=True)
    assert _blob_exists(content, blob_id)
    with content.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM content_holds WHERE blob_id=? AND active=1", (blob_id,)
        ).fetchone() is not None


def test_content_rollback_compensates_deletion_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _layout, content, blob_id, manager, store, context = _active_workspace(tmp_path)
    executor = BlobSweepExecutor(tmp_path, maintenance_store=store, manifest=manager, safety_port=_SafetyPort())
    monkeypatch.setattr(BlobSweepExecutor, "_has_current_reference", staticmethod(lambda _conn, _blob_id: True))
    with pytest.raises(BlobSweepBlocked, match="gained a reference"):
        executor.execute("rollback", context, apply=True)
    assert _blob_exists(content, blob_id)
    with sqlite3.connect(store.db_path) as conn:
        state = conn.execute("SELECT state FROM candidates ORDER BY updated_at DESC LIMIT 1").fetchone()[0]
        job_state = conn.execute("SELECT state FROM jobs WHERE request_key='rollback'").fetchone()[0]
    assert state == CandidateState.CONFIRMED.value
    assert job_state == MaintenanceJobState.FAILED.value


def test_committed_delete_is_reconciled_by_identical_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _layout, content, blob_id, manager, store, context = _active_workspace(tmp_path)
    executor = BlobSweepExecutor(tmp_path, maintenance_store=store, manifest=manager, safety_port=_SafetyPort())
    original = store.mark_candidate_swept
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("receipt fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "mark_candidate_swept", fail_once)
    with pytest.raises(BlobSweepRecoveryRequired):
        executor.execute("recover", context, apply=True)
    assert not _blob_exists(content, blob_id)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT state FROM jobs WHERE request_key='recover'").fetchone()[0] == MaintenanceJobState.ACTIVE.value
        assert conn.execute("SELECT state FROM candidates ORDER BY updated_at DESC LIMIT 1").fetchone()[0] == CandidateState.DELETING.value

    monkeypatch.setattr(store, "mark_candidate_swept", original)
    recovered = executor.execute("recover", context, apply=True)
    replay = executor.execute("recover", context, apply=True)
    assert recovered == replay
    assert recovered.swept_count == 1 and recovered.applied


def test_apply_requires_active_manifest_lease_and_trusted_safety_port(tmp_path: Path) -> None:
    _layout, content, blob_id, manager, store, context = _active_workspace(tmp_path)
    with pytest.raises(BlobSweepBlocked, match="safety verifier"):
        BlobSweepExecutor(tmp_path, maintenance_store=store, manifest=manager).execute("no-safety", context, apply=True)
    assert _blob_exists(content, blob_id)

    record = manager.current()
    manager.transition(ManifestState.V1_ACTIVE, expected_generation=record.generation, error="test rollback")
    with pytest.raises(BlobSweepBlocked, match="V2_ACTIVE"):
        BlobSweepExecutor(tmp_path, maintenance_store=store, manifest=manager, safety_port=_SafetyPort()).execute("wrong-state", context, apply=True)
    assert _blob_exists(content, blob_id)


def test_production_facade_routes_storage_cli_and_binds_transport_context(tmp_path: Path) -> None:
    _layout, _content, blob_id, _manager, _store, _context = _active_workspace(tmp_path)
    facade = get_v2_runtime_facade(str(tmp_path))
    audit = facade.dispatch_cli("storage", {"action": "audit"}, mutation=False)
    assert audit["path"] == "v2" and audit["data"]["candidate_count"] == 1
    assert blob_id not in json.dumps(audit, ensure_ascii=False)

    untrusted = facade.dispatch_cli("storage", {"action": "lease-acquire", "ttl_seconds": 300}, mutation=True, context={"trusted_agent_id": "agent"})
    assert not untrusted["ok"]
    trusted = bind_maintenance_transport_context({"entrypoint": "cli", "trusted_agent_id": "agent", "session_id": "session"})
    acquired = facade.dispatch_cli("storage", {"action": "lease-acquire", "ttl_seconds": 300}, mutation=True, context=trusted)
    assert acquired["ok"] and acquired["data"]["lease_id"], acquired
    lease_id = acquired["data"]["lease_id"]
    dry = facade.dispatch_cli("storage", {"action": "sweep", "request_key": "port-dry", "lease_id": lease_id, "apply": False}, mutation=True, context=trusted)
    assert dry["ok"] and dry["data"]["dry_run"]
    denied = facade.dispatch_cli("storage", {"action": "sweep", "request_key": "port-apply", "lease_id": lease_id, "apply": True}, mutation=True, context=trusted)
    assert denied["code"] == "sweep_safety_verifier_unavailable"
    compact_facade = get_v2_runtime_facade(str(tmp_path), quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
    compact_args = {"action": "compact", "domain": "runtime", "request_key": "port-compact", "lease_id": lease_id, "apply": True}
    compact = compact_facade.dispatch_cli("storage", compact_args, mutation=True, context=trusted)
    compact_replay = compact_facade.dispatch_cli("storage", compact_args, mutation=True, context=trusted)
    assert compact["ok"] and compact["data"] == compact_replay["data"], (compact, compact_replay)
    assert str(tmp_path) not in json.dumps(compact, ensure_ascii=False)


def test_compact_receipt_failure_is_finalized_by_identical_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _active_workspace(tmp_path)
    facade = get_v2_runtime_facade(
        str(tmp_path),
        quiescence_verifier=lambda **_: True,
        outbox_verifier=lambda **_: True,
    )
    trusted = bind_maintenance_transport_context(
        {"entrypoint": "cli", "trusted_agent_id": "agent", "session_id": "session"}
    )
    acquired = facade.dispatch_cli(
        "storage", {"action": "lease-acquire", "ttl_seconds": 300}, mutation=True, context=trusted
    )
    lease_id = acquired["data"]["lease_id"]
    port = facade.ports.v2
    store = port._store()
    original = store.transition_job
    failed = False

    def fail_success_once(job_id, state, context, **kwargs):
        nonlocal failed
        if state is MaintenanceJobState.SUCCEEDED and not failed:
            failed = True
            raise RuntimeError("receipt transition fault")
        return original(job_id, state, context, **kwargs)

    monkeypatch.setattr(store, "transition_job", fail_success_once)
    args = {
        "action": "compact",
        "domain": "runtime",
        "request_key": "compact-recovery",
        "lease_id": lease_id,
        "apply": True,
    }
    first = facade.dispatch_cli("storage", args, mutation=True, context=trusted)
    assert first["code"] == "compact_receipt_recovery_required"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT state FROM jobs WHERE request_key='compact-recovery'").fetchone()[0] == MaintenanceJobState.ACTIVE.value
        assert conn.execute("SELECT COUNT(*) FROM reports WHERE status='PASS'").fetchone()[0] == 1

    monkeypatch.setattr(store, "transition_job", original)
    recovered = facade.dispatch_cli("storage", args, mutation=True, context=trusted)
    replay = facade.dispatch_cli("storage", args, mutation=True, context=trusted)
    assert recovered["ok"] and recovered["data"] == replay["data"]
    assert str(tmp_path) not in json.dumps(recovered, ensure_ascii=False)


def test_phase7_acceptance_default_is_read_only_and_machine_json(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "accept_v2_phase7.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--workspace", str(tmp_path), "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 2 and payload["status"] == "BLOCKED"
    assert payload["dry_run"] and payload["checks"]["workspace_unchanged"]
    assert not (tmp_path / ".memoryguard").exists()
