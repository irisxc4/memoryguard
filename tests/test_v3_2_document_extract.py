"""v3.2 document extraction through the V2 native content/memory plane."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memoryguard.access_context import AccessContext
from memoryguard.content.store import ContentStore
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    msg = f"[{status}] {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg)
    return ok


def _activate(workspace: Path) -> None:
    initialize_all(WorkspaceV2Layout(workspace))
    ContentStore(workspace)
    MemoryAtomStore(workspace)
    EvidenceStore(workspace)
    GovernanceV2(workspace)
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="v3-2-document-extract")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="extract-source",
        target_digest="extract-target",
        manifest_digest="extract-manifest",
        digests={"validator_passed": True, "checkpoints": {"content": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="extract-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="doc-group",
        project_ref=str(workspace.resolve()),
        provider="codex",
        runtime_role="root",
        entrypoint="test",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 7},
    )


def _atoms(workspace: Path) -> list:
    return MemoryAtomStore(workspace).list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(workspace.resolve()),
            share_group_id="doc-group",
            admin=True,
        ),
        include_building=True,
    )


def main() -> int:
    all_pass = True
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _activate(workspace)
        ContentStore(workspace).upsert_source_connector(
            source_id="project-root",
            provider="memoryguard-test",
            source_type="selected_directory",
            external_root_key=str(workspace.resolve()),
            workspace_id=str(workspace.resolve()),
            enabled=True,
        )
        GroupControlService(workspace, write=True).record_selection(
            "agent-a", ["project-root"], "selection-project-root",
        )
        docs = workspace / "docs"
        docs.mkdir()
        document = docs / "team.md"
        document.write_text(
            "# Team rules\n\nMemoryGuard shared facts come from MCP.\n\n"
            "## Preference\n\nI prefer focused tests before a full suite.\n\n"
            "Noise should not become one whole-document memory.\n",
            encoding="utf-8",
        )
        context = _context(workspace)
        port = _port(workspace)

        print("\n=== 1. V2 extraction stages candidates without writing atoms ===")
        preview = port.dispatch_mcp(
            "memoryguard_extract_memories",
            {"source_path": str(document)},
            context=context,
            generation=7,
            state="V2_ACTIVE",
            mutation=True,
        )
        all_pass &= _check("extraction succeeds", preview.get("ok") is True, repr(preview))
        data = preview.get("data", {})
        candidates = data.get("candidates", [])
        extract_id = data.get("extract_id", "")
        all_pass &= _check("V2 content staging", data.get("staging") == "v2_content_plane")
        all_pass &= _check("candidates have IDs", bool(candidates) and all(c.get("candidate_id") for c in candidates))
        all_pass &= _check("candidates carry kind and risk", all(c.get("kind") and c.get("risk_level") for c in candidates))
        all_pass &= _check("preview did not write memory atoms", _atoms(workspace) == [])
        all_pass &= _check("no V1 shared-memory directory", not (workspace / ".memoryguard" / "shared-memory").exists())

        print("\n=== 2. acceptance writes V2 atoms and source mappings ===")
        accepted = port.dispatch_mcp(
            "memoryguard_accept_candidates",
            {"extract_id": extract_id, "candidate_ids": [item["candidate_id"] for item in candidates]},
            context=context,
            generation=7,
            state="V2_ACTIVE",
            mutation=True,
        )
        all_pass &= _check("accept succeeds", accepted.get("ok") is True, repr(accepted))
        accepted_data = accepted.get("data", {})
        atoms = _atoms(workspace)
        all_pass &= _check("accept uses V2 memory", accepted_data.get("storage") == "v2_memory")
        all_pass &= _check("accepted count matches", accepted_data.get("total") == len(candidates))
        all_pass &= _check("accepted candidates become atoms", len(atoms) == len(candidates), f"atoms={len(atoms)}")
        all_pass &= _check(
            "source metadata is retained",
            all("source_reference" in atom.metadata and "extract_id" in atom.metadata for atom in atoms),
            f"metadata={[dict(atom.metadata) for atom in atoms]}",
        )
        staging = workspace / ".memoryguard" / "staging"
        all_pass &= _check(
            "staging extract is consumed",
            not any(path.name == f"extract-{extract_id}.json" for path in staging.glob("extract-*.json")),
        )

        print("\n=== 3. enrichment remains a V2 governed flow ===")
        build = port.dispatch_mcp(
            "memoryguard_build_and_enrich", {}, context=context, generation=7,
            state="V2_ACTIVE", mutation=True,
        )
        all_pass &= _check("build/enrich succeeds", build.get("ok") is True, repr(build))
        pending = port.dispatch_mcp(
            "memoryguard_list_pending_enrichments", {"limit": 50},
            context=context, generation=7, state="V2_ACTIVE",
        )
        task = (pending.get("data", {}).get("tasks") or [None])[0]
        all_pass &= _check("enrichment task is pending", task is not None)
        if task is not None:
            applied = port.dispatch_mcp(
                "memoryguard_apply_enrichments",
                {"results": [{
                    "task_id": task["task_id"],
                    "kind": "preference",
                    "title": "Focused test preference",
                    "body": "Prefer focused tests before the full suite.",
                    "confidence": 0.95,
                    "rationale": "explicit preference",
                }]},
                context=context, generation=7, state="V2_ACTIVE", mutation=True,
            )
            all_pass &= _check("enrichment applies", applied.get("ok") is True, repr(applied))
            all_pass &= _check("enrichment storage is V2", applied.get("data", {}).get("storage") == "v2_memory")

        print("\n=== 4. path containment fails closed ===")
        outside = workspace.parent / f"outside-{workspace.name}.md"
        escaped = port.dispatch_mcp(
            "memoryguard_extract_memories", {"source_path": str(outside)},
            context=context, generation=7, state="V2_ACTIVE", mutation=True,
        )
        all_pass &= _check("outside source is rejected", escaped.get("ok") is False)
        all_pass &= _check("outside content is not exposed", "outside" not in repr(escaped))

    print("\n" + "=" * 50)
    if all_pass:
        print("All v3.2 Document Extract tests PASSED")
        return 0
    print("Some Document Extract tests FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
