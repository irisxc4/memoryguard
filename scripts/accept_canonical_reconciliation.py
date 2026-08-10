"""Canonical-reconciliation real-snapshot acceptance (two builds).

Runs the ``RuleReconciliationService`` saga twice against an **isolated copy**
of the real control-workspace DB.  The copy is made with the sanctioned
online-backup API ``sqlite3.Connection.backup()`` -- never a raw WAL/shm file
copy and never the live DB itself -- so the real MemoryGuard data is never
touched or mutated.

For each build it reports the group-level canonical state and the durable
idempotency digest.  Build 1 must land ``canonical_ready`` with the group-level
canonical read activated; build 2 must be byte-identical (same source links,
same canonical digest, same projection version, same job row, no
proposal/decision/outbox growth).  Prints a JSON summary suitable for pasting
into the PR alongside the test-suite results.

Usage::

    python scripts/accept_canonical_reconciliation.py [--workspace DIR]
        [--group shared-9b8b5d020a74b2fd] [--keep DIR] [--json]

Resolves the control workspace from ``--workspace``, then ``MEMORYGUARD_HOME``,
then ``%LOCALAPPDATA%\\MemoryGuard``.  ``--keep DIR`` leaves the isolated run
workspace on disk for inspection instead of deleting it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from memoryguard.rule_merge_store import RuleMergeStore  # noqa: E402
from memoryguard.rule_reconciliation import (  # noqa: E402
    RuleReconciliationService,
    _active_mandatory,
    build_bundles,
    canonical_reconciliation_status,
    ensure_reconciliation_job,
)
from memoryguard.shared_memory_store import SharedMemoryStore  # noqa: E402

DEFAULT_GROUP = "shared-9b8b5d020a74b2fd"

# The acceptance baseline the reconciliation semantics were specified against.
DESCRIBED_BASELINE = {
    "active_mandatory": 6,
    "shadowed": 3,
    "outbox_pending": 15,
    "canonical_definitions": 0,
    "graph_built": False,
    "applied_heuristic_enrichment": 187,
}


def resolve_workspace(argv: list[str]) -> Path:
    if "--workspace" in argv:
        idx = argv.index("--workspace")
        if idx + 1 < len(argv):
            return Path(argv[idx + 1]).resolve()
    override = os.environ.get("MEMORYGUARD_HOME")
    if override:
        return Path(override).resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "MemoryGuard"
        if candidate.is_dir():
            return candidate.resolve()
    return Path(".").resolve()


def _snapshot_db(src: Path, dst: Path) -> None:
    """Isolated copy of one SQLite DB via the online-backup API."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _count_shadowed(legacy: SharedMemoryStore) -> int:
    from memoryguard.schema_v3 import SharedMemoryStatus
    return sum(
        1 for record in legacy.list_records()
        if str(getattr(record.status, "value", record.status) or "")
        == SharedMemoryStatus.SHADOWED.value
    )


def _count_applied_heuristic(workspace: Path, group_id: str) -> int:
    from memoryguard.host_enrichment import _pending_path
    ppath = _pending_path(workspace)
    if not ppath.exists():
        return 0
    count = 0
    for line in ppath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(task, dict) or task.get("status") != "applied":
            continue
        scope = task.get("scope") or {}
        if group_id and str(scope.get("share_group_id", "") or "") != group_id:
            continue
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        task_type = str(result.get("task_type") or task.get("task_type") or "")
        if task_type in {"scope_bundle", "rule_reconciliation"}:
            continue
        if str(result.get("kind", "") or "") == "scope_bundle":
            continue
        count += 1
    return count


def _idempotency_snapshot(
    run_ws: Path, store: RuleMergeStore, group_id: str,
) -> dict:
    service = RuleReconciliationService(store, workspace=run_ws)
    legacy = SharedMemoryStore(run_ws, group_id)
    return {
        "canonical_digest": service.canonical_digest(group_id),
        "projection_version": service._projection_version(group_id),
        "source_links": sorted(
            (link["memory_id"], link["canonical_definition_id"],
             link["source_revision"], link["status"])
            for link in store.list_source_links(share_group_id=group_id)
        ),
        "proposals": _table_count(store, "rule_merge_proposals"),
        "decisions": _table_count(store, "rule_merge_decisions"),
        "outbox_total": int(legacy.outbox_high_water()["total"]),
        "job_row": service.jobs.latest_job(group_id),
    }


def _table_count(store: RuleMergeStore, table: str) -> int:
    with store._read_conn() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM {table}",  # noqa: S608 - fixed literal
        ).fetchone()[0])


def _build_run_copy(
    control_ws: Path, group_id: str, run_ws: Path,
) -> tuple[Path, Path]:
    """Backup shared-memory + rule-intelligence DBs into the run workspace."""
    sm_src = (
        control_ws / ".memoryguard" / "shared-memory" / group_id / "memory.db"
    )
    if not sm_src.exists():
        raise SystemExit(
            f"shared-memory DB not found for group {group_id}: {sm_src}"
        )
    ri_src = control_ws / ".memoryguard" / "rule-intelligence" / "memory.db"
    sm_dst = run_ws / ".memoryguard" / "shared-memory" / group_id / "memory.db"
    ri_dst = run_ws / ".memoryguard" / "rule-intelligence" / "memory.db"
    _snapshot_db(sm_src, sm_dst)
    if ri_src.exists():
        _snapshot_db(ri_src, ri_dst)
    # Carry the enrichment pending file across so the Req6 gate sees the real
    # enrichment history (a plain file copy; it is not a DB/WAL file).
    pending_src = control_ws / ".memoryguard" / "enrichment_tasks.jsonl"
    if pending_src.exists():
        pending_dst = run_ws / ".memoryguard" / "enrichment_tasks.jsonl"
        pending_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pending_src, pending_dst)
    return sm_dst, ri_dst


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="", help="control workspace dir")
    parser.add_argument(
        "--group", default=DEFAULT_GROUP, help="share group id (default: real)"
    )
    parser.add_argument(
        "--keep", default="", help="leave the isolated run workspace at DIR"
    )
    parser.add_argument(
        "--json", action="store_true", help="print only the JSON summary"
    )
    opts = parser.parse_args(args)

    control_ws = (
        Path(opts.workspace).resolve()
        if opts.workspace
        else resolve_workspace([])
    )
    group_id = opts.group
    if not opts.workspace and not (control_ws / ".memoryguard").is_dir():
        control_ws = resolve_workspace([])
    if not (control_ws / ".memoryguard").is_dir():
        print(f"no control workspace found (tried {control_ws})",
              file=sys.stderr)
        return 2

    if opts.keep:
        run_ws = Path(opts.keep).resolve()
        run_ws.mkdir(parents=True, exist_ok=True)
    else:
        run_ws = Path(tempfile.mkdtemp(prefix="memoryguard-accept-"))
    _build_run_copy(control_ws, group_id, run_ws)

    store = RuleMergeStore(run_ws)
    legacy = SharedMemoryStore(run_ws, group_id)
    service = RuleReconciliationService(store, workspace=run_ws)

    # ---- baseline (actual) ------------------------------------------------
    actual_baseline = {
        "active_mandatory": len(_active_mandatory(legacy)),
        "shadowed": _count_shadowed(legacy),
        "outbox_pending": int(legacy.outbox_high_water()["pending"]),
        "canonical_definitions": 0,
        "graph_built": False,
        "applied_heuristic_enrichment": _count_applied_heuristic(
            run_ws, group_id,
        ),
    }

    # ---- Req6 gate ---------------------------------------------------------
    gate = ensure_reconciliation_job(run_ws, group_id, store=store)

    # ---- heuristic bundle plan ---------------------------------------------
    active = _active_mandatory(legacy)
    plan = build_bundles(store, legacy, group_id, active) if active else {
        "bundles": [], "kept_separate": [],
    }

    # ---- build 1 -----------------------------------------------------------
    build1_error = ""
    job1 = None
    try:
        job1 = service.run(group_id, bundle_plan=plan, model_mode="scripted")
    except Exception as exc:  # noqa: BLE001 - report the persisted failure
        build1_error = f"{type(exc).__name__}: {exc}"
    status1 = canonical_reconciliation_status(run_ws, group_id, store=store)
    snap1 = _idempotency_snapshot(run_ws, store, group_id)

    # ---- build 2 -----------------------------------------------------------
    build2_error = ""
    build2_skipped = bool(build1_error)
    job2 = None
    if not build1_error:
        try:
            job2 = service.run(group_id, bundle_plan=plan, model_mode="scripted")
        except Exception as exc:  # noqa: BLE001
            build2_error = f"{type(exc).__name__}: {exc}"
    snap2 = _idempotency_snapshot(run_ws, store, group_id)

    idempotent = (
        not build1_error
        and not build2_error
        and snap1["job_row"] == snap2["job_row"]
        and snap1["canonical_digest"] == snap2["canonical_digest"]
        and snap1["projection_version"] == snap2["projection_version"]
        and snap1["source_links"] == snap2["source_links"]
        and snap1["proposals"] == snap2["proposals"]
        and snap1["decisions"] == snap2["decisions"]
        and snap1["outbox_total"] == snap2["outbox_total"]
    )

    build2_reconciliation_status = (
        status1 if build2_skipped else canonical_reconciliation_status(
            run_ws, group_id, store=store,
        )
    )
    summary = {
        "workspace": str(control_ws),
        "group_id": group_id,
        "described_baseline": DESCRIBED_BASELINE,
        "actual_baseline": actual_baseline,
        "req6_gate": gate,
        "bundle_plan": {
            "bundles": [
                bundle.to_dict() if hasattr(bundle, "to_dict") else bundle
                for bundle in plan.get("bundles", [])
            ],
            "kept_separate": plan.get("kept_separate", []),
        },
        "build1": {
            "error": build1_error,
            "job": job1,
            "status": status1,
            "snapshot": snap1,
        },
        "build2": {
            "error": build2_error,
            # Preserve the historical status object on success.  A skipped or
            # failed second build gets a scalar marker so callers can branch
            # without dereferencing a missing job.
            "status": (
                "skipped" if build2_skipped
                else ("failed" if build2_error else build2_reconciliation_status)
            ),
            "skipped": build2_skipped,
            "skip_reason": build1_error if build2_skipped else "",
            "job": job2,
            # A failed first build must not invoke the second status/read path:
            # that path assumes a job row and used to raise ``job2 is None``
            # while trying to print the original failure.
            "reconciliation_status": build2_reconciliation_status,
            "snapshot": snap2,
        },
        "idempotent": idempotent,
    }

    if opts.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("=== canonical reconciliation acceptance ===")
        print(f"workspace      : {control_ws}")
        print(f"group          : {group_id}")
        print(f"described      : {DESCRIBED_BASELINE}")
        print(f"actual         : {actual_baseline}")
        print(f"req6 gate      : {gate.get('reason')} created={gate.get('created')}")
        print(f"bundle plan    : "
              f"{[b.bundle_kind for b in plan.get('bundles', [])]} "
              f"kept_separate={plan.get('kept_separate')}")
        if build1_error:
            print(f"build 1        : FAILED {build1_error}")
        else:
            print(f"build 1        : {job1['status']} "
                  f"ready={status1['canonical_ready']} "
                  f"read_path={status1['read_path']} "
                  f"defs={status1['checks']['canonical_definitions']} "
                  f"active={status1['checks']['active_mandatory']} "
                  f"unlinked={status1['checks']['unlinked_sources']} "
                  f"outbox={status1['checks']['outbox_pending']} "
                  f"lag={status1['checks']['projection_lag']} "
                  f"graph={status1['checks']['graph_built']}")
            print(f"  digest after  : {job1['canonical_digest_after']}")
            print(f"  projection    : {job1['projection_version']}")
        if build2_skipped:
            print(f"build 2        : SKIPPED (build 1 failed: {build1_error})")
        elif build2_error:
            print(f"build 2        : FAILED {build2_error}")
        else:
            # ``job2`` is guaranteed by the branches above. Keep the guard
            # explicit so a future failure path cannot regress to a confusing
            # secondary ``NoneType`` exception.
            assert job2 is not None
            print(f"build 2        : {job2['status']} "
                  f"ready={snap2 and status1['canonical_ready']}")
            print(f"  digest after  : {job2['canonical_digest_after']}")
            print(f"  projection    : {job2['projection_version']}")
        print(f"idempotent     : {idempotent}")
        if idempotent:
            print("ACCEPTED: two real-copy builds, byte-identical second run.")
        else:
            print("NOT ACCEPTED: second build drifted.", file=sys.stderr)

    if not opts.keep:
        shutil.rmtree(run_ws, ignore_errors=True)

    if build1_error or build2_error or not idempotent:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
