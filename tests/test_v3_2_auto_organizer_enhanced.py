"""v3.2 AutoOrganizer checks over the canonical V2 memory plane."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memoryguard.auto_organizer import AutoOrganizer
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.schema_v3 import MemoryEvent, stable_hash, _now_iso
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
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="v3-2-organizer")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="organizer-source",
        target_digest="organizer-target",
        manifest_digest="organizer-manifest",
        digests={"validator_passed": True, "checkpoints": {"memory": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE
    GroupControlService(workspace, write=True).bind_agent(
        "agent", "group", idempotency_key="organizer-bind",
    )


def _event(body: str, event_id: str, *, metadata: dict | None = None) -> MemoryEvent:
    return MemoryEvent(
        event_id=event_id,
        agent_instance_id="agent",
        share_group_id="group",
        raw_content=body,
        metadata=dict(metadata or {}),
        auto_actions=[],
        created_at=_now_iso(),
    )


def _organize(
    workspace: Path,
    body: str,
    event_id: str,
    *,
    metadata: dict | None = None,
    write_policy: str = "auto_accept",
):
    store = MemoryAtomStore(workspace)
    organizer = AutoOrganizer(
        workspace,
        "group",
        store=store,
        engine=GovernanceV2(
            workspace,
            memory_store=store,
            evidence_store=EvidenceStore(workspace),
        ),
    )
    return organizer.organize(
        _event(body, event_id, metadata=metadata),
        write_policy=write_policy,
    )


def main() -> int:
    all_pass = True
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _activate(workspace)
        store = MemoryAtomStore(workspace)
        read_scope = MemoryReadScope(
            workspace_id=str(workspace.resolve()),
            share_group_id="group",
            admin=True,
        )

        print("\n=== 1. V2 low-confidence proposal stays out of active ===")
        low, actions_low = _organize(
            workspace, "maybe", "event-low", write_policy="propose_only",
        )
        all_pass &= _check(
            "proposal -> low_confidence",
            low.status == "low_confidence",
            f"status={low.status}, actions={actions_low}",
        )
        all_pass &= _check(
            "low-confidence action recorded",
            any(a.get("action") == "create_low_confidence" for a in actions_low),
        )

        print("\n=== 2. duplicate content merges provenance ===")
        first, _ = _organize(workspace, "User prefers dark mode", "event-one")
        second, actions_second = _organize(
            workspace, "User prefers dark mode", "event-two",
        )
        merged = store.get_atom(
            first.memory_id, scope=read_scope, include_building=True,
        )
        all_pass &= _check("duplicate returns same atom", second.memory_id == first.memory_id)
        all_pass &= _check(
            "provenance increases",
            merged is not None and len(merged.provenance) >= 2,
            f"provenance={len(merged.provenance) if merged else 0}",
        )
        all_pass &= _check(
            "merge_provenance action",
            any(a.get("action") == "merge_provenance" for a in actions_second),
        )

        print("\n=== 3. V2 keeps the canonical body intact ===")
        long_body = "Project fact: " + ("retain this governed fact. " * 80)
        preserved, actions_preserved = _organize(
            workspace, long_body, "event-long",
        )
        persisted = store.get_atom(
            preserved.memory_id, scope=read_scope, include_building=True,
        )
        all_pass &= _check(
            "body is not silently rewritten",
            persisted is not None and persisted.body == long_body.strip(),
        )
        all_pass &= _check(
            "created through V2 action",
            any(a.get("action") == "create_active" for a in actions_preserved),
        )

        print("\n=== 4. correction creates a superseding atom ===")
        original, _ = _organize(
            workspace, "The release uses Python 3.8", "event-fact",
        )
        corrected, actions_corrected = _organize(
            workspace,
            "The release uses Python 3.8",
            "event-correction",
            metadata={"type": "correction"},
        )
        all_pass &= _check(
            "correction points at old atom",
            original.memory_id in corrected.supersedes,
            f"supersedes={corrected.supersedes}",
        )
        all_pass &= _check(
            "supersede action recorded",
            any(a.get("action") == "supersede" for a in actions_corrected),
        )

    print("\n" + "=" * 50)
    if all_pass:
        print("All v3.2 AutoOrganizer enhanced tests PASSED")
        return 0
    print("Some AutoOrganizer enhanced tests FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
