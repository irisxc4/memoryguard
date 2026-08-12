"""V2 performance benchmark for the governed ingestion/projection path.

The benchmark intentionally measures the public V2 services together:
SourceControlService -> NativeExtractionEnrichmentService -> MemoryAtomStore
-> ProjectionBuildService/ProjectionStore.  It is executable as a script so
that the size targets remain useful outside pytest.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.access_context import AccessContext
from memoryguard.content import ContentStore
from memoryguard.evidence import EvidenceStore
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.projection_v2 import ProjectionReadScope, ProjectionStore
from memoryguard.runtime_v2.extraction_native import NativeExtractionEnrichmentService
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import bind_native_transport_context
from memoryguard.runtime_v2.projection_build import ProjectionBuildService
from memoryguard.runtime_v2.source_control import SourceControlService


AGENT = "agent-bench"
GROUP = "benchmark-group"
PROVIDER = "benchmark"
RUNTIME_ROLE = "benchmark"


def _native_context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=AGENT,
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="benchmark-session",
            session_source="pytest",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id=GROUP,
        project_ref=str(workspace.resolve()),
        provider=PROVIDER,
        runtime_role=RUNTIME_ROLE,
    )


def _source_context() -> dict[str, object]:
    return {"admin": True, "agent_instance_id": AGENT}


def _projection_scope(workspace: Path) -> ProjectionReadScope:
    return ProjectionReadScope(
        workspace_id=str(workspace.resolve()),
        agent_instance_id=AGENT,
        project_ref=str(workspace.resolve()),
        provider=PROVIDER,
        share_group_id=GROUP,
        sensitivity="normal",
        policy_class="private",
    )


def make_test_workspace(record_count: int = 50) -> Path:
    """Create a V2 workspace with exactly ``record_count`` extractable segments.

    Native extraction intentionally caps one file at 20 segments.  The fixture
    therefore spreads records across enough files instead of silently clipping
    200/500-record runs.  Repeated bodies are intentional: the benchmark checks
    the real canonical-hash duplicate signal rather than confusing it with data
    loss.
    """
    workspace = Path(tempfile.mkdtemp(prefix="memoryguard-v2-bench-"))
    docs = workspace / "docs"
    docs.mkdir()
    ContentStore(workspace)
    MemoryAtomStore(workspace)
    EvidenceStore(workspace)
    GroupControlService(workspace, write=True).bind_agent(AGENT, GROUP)

    bodies = [
        "Prefer Python dataclasses and explicit type annotations.",
        "Run tests before building the container and deploying it.",
        "Review errors, logs, tests, and untrusted input handling.",
        "Use stable resource URLs and standard status codes.",
        "Validate the migration locally before staging and production.",
        "Use Go channels only when they make ownership explicit.",
        "Keep critical paths fully covered by regression tests.",
        "Document setup, usage, and operational recovery steps.",
    ]
    file_count = max(1, (record_count + 19) // 20)
    next_record = 0
    for index in range(file_count):
        segment_count = min(20, record_count - next_record)
        chunks: list[str] = []
        for offset in range(segment_count):
            ordinal = next_record + offset
            family = ordinal % len(bodies)
            title = f"Benchmark memory family {family}"
            # Keep the body stable within a family so canonical-hash duplicate
            # groups are genuine.  Candidate/memory ids remain unique.
            body = bodies[family]
            if offset == 0:
                chunks.append(f"# {title}\n\n{body}\n")
            else:
                chunks.append(f"## {title}\n{body}\n")
        (docs / f"memory-{index}.md").write_text(
            "\n".join(chunks), encoding="utf-8",
        )
        next_record += segment_count
    SourceControlService(workspace).add(
        str(docs),
        "selected_directory",
        _source_context(),
        display_name="V2 benchmark documents",
    )
    return workspace


def bench_scan_extract(workspace: Path) -> dict[str, object]:
    """Measure source inventory plus staged extraction and explicit acceptance."""
    control = SourceControlService(workspace)
    native = NativeExtractionEnrichmentService(workspace)
    context = _native_context(workspace)
    # Constructor/schema work and one read-only status call are the single
    # explainable warmup.  The timed path reuses these V2 services for all files.
    native.enrichment_status({}, context=context)
    started = time.perf_counter()
    scan_started = time.perf_counter()
    scan = control.scan_summary(_source_context())
    scan_ms = (time.perf_counter() - scan_started) * 1000
    raw_started = time.perf_counter()
    raw = control.raw_summary(_source_context())
    raw_ms = (time.perf_counter() - raw_started) * 1000
    extraction_started = time.perf_counter()
    batches: list[dict[str, object]] = []
    for group in raw.get("groups", []):
        source_id = str(group.get("root_id") or "")
        for item in group.get("files", []):
            preview = native.extract(
                {"source_id": source_id, "relative_path": item["relative_path"]},
                context=context,
            )
            candidates = list(preview.get("candidates") or [])
            if not candidates:
                continue
            batches.append({
                "extract_id": preview["extract_id"],
                "candidate_ids": [item["candidate_id"] for item in candidates],
            })
    extraction_ms = (time.perf_counter() - extraction_started) * 1000
    accept_started = time.perf_counter()
    result = native.accept_batch(
        {"batches": batches},
        context=context,
    ) if batches else {"total": 0}
    accept_ms = (time.perf_counter() - accept_started) * 1000
    accepted = int(result.get("total", 0))
    elapsed = (time.perf_counter() - started) * 1000

    scope = MemoryReadScope(
        workspace_id=str(workspace.resolve()),
        share_group_id=GROUP,
        agent_instance_id=AGENT,
        project_ref=str(workspace.resolve()),
        provider=PROVIDER,
        runtime_role=RUNTIME_ROLE,
    )
    atoms = MemoryAtomStore(workspace, readonly=True).list_atoms(scope=scope)
    hashes = Counter(atom.canonical_hash for atom in atoms if atom.canonical_hash)
    return {
        "scan_extract_ms": round(elapsed, 2),
        "scan_ms": round(scan_ms, 2),
        "raw_ms": round(raw_ms, 2),
        "extract_ms": round(extraction_ms, 2),
        "accept_ms": round(accept_ms, 2),
        "accept_batches": len(batches),
        "source_objects": int(scan["coverage"]["candidate_count"]),
        "accepted": accepted,
        "atom_count": len(atoms),
        "duplicate_groups": sum(1 for count in hashes.values() if count > 1),
    }


def bench_projection_build(workspace: Path) -> dict[str, object]:
    """Measure deterministic reference-only projection creation and reread."""
    service = ProjectionBuildService(workspace)
    scope = _projection_scope(workspace)
    started = time.perf_counter()
    result = service.build(
        mode="reconstructed",
        scope=scope,
        runtime_role=RUNTIME_ROLE,
    )
    elapsed = (time.perf_counter() - started) * 1000
    summary = result.get("projection") or {}
    key = str(summary.get("key") or "")
    record = ProjectionStore(workspace, initialize=False).get_projection(
        "scenario", key, scope=scope,
    ) if key else None
    payload = record.payload if record is not None else {}
    metadata = payload.get("metadata") if isinstance(payload, dict) else {}
    graph = metadata.get("derived_graph") if isinstance(metadata, dict) else {}
    second = service.build(
        mode="reconstructed",
        scope=scope,
        runtime_role=RUNTIME_ROLE,
    )
    return {
        "projection_ms": round(elapsed, 2),
        "projection_ok": result.get("status") == "succeeded" and record is not None,
        "projection_id": record.projection_id if record is not None else "",
        "idempotent": (second.get("projection") or {}).get("projection_id") == summary.get("projection_id"),
        "graph_nodes": len(graph.get("nodes", [])) if isinstance(graph, dict) else 0,
        "body_free": not any(key in str(payload).lower() for key in ("body", "raw_content", "full_text")),
    }


def bench_native_enrichment(workspace: Path) -> dict[str, object]:
    """Measure V2 host-enrichment task discovery without provider calls."""
    service = NativeExtractionEnrichmentService(workspace)
    started = time.perf_counter()
    result = service.build_and_enrich({}, context=_native_context(workspace))
    return {
        "enrichment_ms": round((time.perf_counter() - started) * 1000, 2),
        "scoped_record_count": int(result.get("scoped_record_count", 0)),
        "pending_tasks": len(result.get("pending_tasks") or []),
    }


def bench_group_status(workspace: Path) -> dict[str, object]:
    started = time.perf_counter()
    result = GroupControlService(workspace, write=False).get_global_memory_status()
    return {
        "group_status_ms": round((time.perf_counter() - started) * 1000, 2),
        "total_records": int(result.get("total_records", 0)),
        "total_groups": int(result.get("total_groups", 0)),
    }


def _run_size(record_count: int) -> tuple[Path, dict[str, object]]:
    workspace = make_test_workspace(record_count)
    ingestion = bench_scan_extract(workspace)
    projection = bench_projection_build(workspace)
    enrichment = bench_native_enrichment(workspace)
    groups = bench_group_status(workspace)
    return workspace, {
        "ingestion": ingestion,
        "projection": projection,
        "enrichment": enrichment,
        "groups": groups,
    }


def run_benchmarks() -> bool:
    print("MemoryGuard V2 performance benchmark")
    runs: dict[int, dict[str, object]] = {}
    workspaces: list[Path] = []
    try:
        for size in (50, 200, 500):
            workspace, result = _run_size(size)
            workspaces.append(workspace)
            runs[size] = result
            ingestion = result["ingestion"]
            projection = result["projection"]
            print(
                f"{size:>3} records: ingest={ingestion['scan_extract_ms']}ms "
                f"atoms={ingestion['atom_count']} "
                f"projection={projection['projection_ms']}ms "
                f"nodes={projection['graph_nodes']} "
                f"stages(scan/raw/extract/accept)="
                f"{ingestion['scan_ms']}/{ingestion['raw_ms']}/"
                f"{ingestion['extract_ms']}/{ingestion['accept_ms']}ms "
                f"batches={ingestion['accept_batches']}"
            )

        limits = {50: (5000, 5000), 200: (15000, 10000), 500: (45000, 20000)}
        passed = True
        for size, (ingestion_limit, projection_limit) in limits.items():
            result = runs[size]
            ingestion = result["ingestion"]
            projection = result["projection"]
            enrichment = result["enrichment"]
            groups = result["groups"]
            checks = {
                "ingestion target": ingestion["scan_extract_ms"] < ingestion_limit,
                "projection target": projection["projection_ms"] < projection_limit,
                "accepted atoms": (
                    ingestion["accepted"] == size
                    and ingestion["atom_count"] == size
                ),
                "real duplicate groups": ingestion["duplicate_groups"] > 0,
                "projection committed": projection["projection_ok"],
                "projection idempotent": projection["idempotent"],
                "reference-only payload": projection["body_free"],
                "enrichment scoped": enrichment["scoped_record_count"] == ingestion["atom_count"],
                "group aggregate": groups["total_records"] == ingestion["atom_count"],
            }
            for label, ok in checks.items():
                print(f"  [{('PASS' if ok else 'FAIL')}] {size}: {label}")
                passed = passed and bool(ok)
        return passed
    finally:
        for workspace in workspaces:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(0 if run_benchmarks() else 1)
