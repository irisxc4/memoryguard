"""v3.2 end-to-end coverage through the native V2 memory surface."""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memoryguard.access_context import AccessContext
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtomStore
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
    message = f"[{status}] {label}"
    if detail:
        message += f" :: {detail}"
    print(message)
    return ok


def _activate_v2(root: Path) -> None:
    initialize_all(WorkspaceV2Layout(root))
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="v3-2-e2e")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="v3-2-e2e-source",
        target_digest="v3-2-e2e-target",
        manifest_digest="v3-2-e2e-manifest",
        digests={"validator_passed": True, "checkpoints": {"memory": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _context(root: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="e2e-agent",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="v3-2-e2e-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root.resolve()),
        share_group_id="e2e-group",
        project_ref=str(root.resolve()),
        provider="codex",
        runtime_role="root",
        entrypoint="mcp",
    )


def _data(result: dict) -> dict:
    assert result.get("ok") is True, result
    return result["data"]


def _write(
    port: NativeV2RuntimePort,
    context,
    memory_id: str,
    body: str,
    *,
    key: str,
    kind: str = "fact",
) -> dict:
    return _data(
        port.dispatch_mcp(
            "memoryguard_memory_write",
            {
                "memory_id": memory_id,
                "body": body,
                "kind": kind,
                "visibility": "ready",
                "evidence": [{
                    "source_ref": f"e2e:{memory_id}",
                    "authority": "test",
                }],
                "idempotency_key": key,
            },
            context=context,
            generation=1,
            state="V2_ACTIVE",
        )
    )


def main() -> int:
    all_pass = True
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _activate_v2(workspace)
        GroupControlService(workspace, write=True).bind_agent("e2e-agent", "e2e-group")
        context = _context(workspace)
        port = NativeV2RuntimePort(
            workspace,
            state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
        )

        print("\n=== 1. ordinary write -> active ===")
        ordinary = _write(
            port,
            context,
            "pref-1",
            "User preference: concise answers",
            kind="preference",
            key="e2e-pref-1",
        )
        all_pass &= _check("ordinary memory -> active", ordinary["atom"]["status"] == "active")
        all_pass &= _check("classification -> preference", ordinary["atom"]["kind"] == "preference")
        all_pass &= _check("write has receipt", bool(ordinary.get("receipt")))

        print("\n=== 2. secret write -> quarantine ===")
        secret = _write(
            port,
            context,
            "secret-1",
            "API_KEY=sk-abc123def456ghi789",
            key="e2e-secret-1",
        )
        all_pass &= _check(
            "secret memory -> quarantine",
            secret["atom"]["status"] == "quarantined",
            f"status={secret['atom']['status']}",
        )

        print("\n=== 3. correction -> supersede ===")
        _write(
            port,
            context,
            "fact-1",
            "Project uses Python 3.8",
            key="e2e-fact-1",
        )
        corrected = _write(
            port,
            context,
            "correction-1",
            "Correction: Project uses Python 3.8",
            kind="correction",
            key="e2e-correction-1",
        )
        old = _data(
            port.dispatch_mcp(
                "memoryguard_memory_read",
                {"memory_id": "fact-1"},
                context=context,
                generation=1,
                state="V2_ACTIVE",
            )
        )
        all_pass &= _check(
            "old memory -> superseded",
            old["status"] == "superseded",
            f"status={old['status']}",
        )
        all_pass &= _check(
            "new memory records supersedes edge",
            "fact-1" in corrected["atom"].get("supersedes", []),
            f"supersedes={corrected['atom'].get('supersedes', [])}",
        )

        print("\n=== 4. status ===")
        status = _data(
            port.dispatch_gui(
                "get_memory_status",
                {},
                context=context,
                generation=1,
                state="V2_ACTIVE",
            )
        )
        print(f"  status: {json.dumps(status, indent=2)}")
        all_pass &= _check("active >= 2", status["status_counts"].get("active", 0) >= 2)
        all_pass &= _check(
            "quarantined >= 1",
            status["status_counts"].get("quarantined", 0) >= 1,
        )
        all_pass &= _check(
            "superseded >= 1",
            status["status_counts"].get("superseded", 0) >= 1,
        )

        print("\n=== 5. single read ===")
        found = _data(
            port.dispatch_mcp(
                "memoryguard_memory_read",
                {"memory_id": "pref-1"},
                context=context,
                generation=1,
                state="V2_ACTIVE",
            )
        )
        all_pass &= _check("read memory", found["body"] == "User preference: concise answers")

        print("\n=== 6. search ===")
        search = _data(
            port.dispatch_mcp(
                "memoryguard_memory_search",
                {"query": "Python", "status": "active"},
                context=context,
                generation=1,
                state="V2_ACTIVE",
            )
        )
        all_pass &= _check("search returns Python memory", bool(search))

        print("\n=== 7. soft delete ===")
        deleted = _data(
            port.dispatch_mcp(
                "memoryguard_memory_delete",
                {"memory_id": "pref-1", "idempotency_key": "e2e-delete-pref-1"},
                context=context,
                generation=1,
                state="V2_ACTIVE",
            )
        )
        all_pass &= _check("soft delete -> deleted", deleted["atom"]["status"] == "deleted")

        print("\n=== 8. GUI governance actions ===")
        memory = MemoryAtomStore(workspace)
        scope = {
            "workspace_id": str(workspace.resolve()),
            "share_group_id": "e2e-group",
            "agent_instance_id": "e2e-agent",
            "project_ref": str(workspace.resolve()),
            "provider": "codex",
            "runtime_role": "root",
        }
        current = memory.get_atom("correction-1", scope=scope, include_building=True)
        assert current is not None
        locked_atom, locked_receipt = GovernanceV2(workspace).put_atom(
            replace(current, locked=True),
            context=V2MutationContext(
                workspace_id=str(workspace.resolve()),
                share_group_id="e2e-group",
                agent_instance_id="e2e-agent",
                project_ref=str(workspace.resolve()),
                provider="codex",
                runtime_role="root",
                actor="e2e-agent",
                admin=True,
                authority="manual",
            ),
            reason="v3.2 e2e lock",
            idempotency_key="e2e-lock-1",
        )
        unlocked_atom, unlocked_receipt = GovernanceV2(workspace).put_atom(
            replace(locked_atom, locked=False),
            context=V2MutationContext(
                workspace_id=str(workspace.resolve()),
                share_group_id="e2e-group",
                agent_instance_id="e2e-agent",
                project_ref=str(workspace.resolve()),
                provider="codex",
                runtime_role="root",
                actor="e2e-agent",
                admin=True,
                authority="manual",
            ),
            reason="v3.2 e2e unlock",
            idempotency_key="e2e-unlock-1",
        )
        all_pass &= _check(
            "lock is governed",
            locked_atom.locked and bool(locked_receipt),
            str(locked_receipt.to_dict()),
        )
        all_pass &= _check(
            "unlock is governed",
            not unlocked_atom.locked and bool(unlocked_receipt),
            str(unlocked_receipt.to_dict()),
        )

        print("\n=== 9. versions + rollback ===")
        edited = _data(
            port.dispatch_mcp(
                "memoryguard_memory_update",
                {
                    "memory_id": "correction-1",
                    "body": "edited correction",
                    "idempotency_key": "e2e-edit-1",
                },
                context=context,
                generation=1,
                state="V2_ACTIVE",
            )
        )
        versions = _data(
            port.dispatch_gui(
                "list_memory_versions",
                {"memory_id": "correction-1"},
                context=context,
                generation=1,
                state="V2_ACTIVE",
            )
        )["versions"]
        all_pass &= _check("version history exists", len(versions) >= 2)
        replayed = MemoryAtomStore(workspace).replay_revision(
            edited["atom"]["atom_id"],
            int(versions[0]["revision"]),
        )
        assert replayed is not None
        _, rollback_receipt = GovernanceV2(workspace).put_atom(
            replayed,
            context=V2MutationContext(
                workspace_id=str(workspace.resolve()),
                share_group_id="e2e-group",
                agent_instance_id="e2e-agent",
                project_ref=str(workspace.resolve()),
                provider="codex",
                runtime_role="root",
                actor="e2e-agent",
                admin=True,
                authority="manual",
            ),
            reason="v3.2 e2e revision rollback",
            idempotency_key="e2e-rollback-1",
        )
        all_pass &= _check(
            "rollback returns governed receipt",
            bool(rollback_receipt),
            str(rollback_receipt.to_dict()),
        )
        restored = _data(
            port.dispatch_mcp(
                "memoryguard_memory_read",
                {"memory_id": "correction-1"},
                context=context,
                generation=1,
                state="V2_ACTIVE",
            )
        )
        all_pass &= _check(
            "rollback restores original body",
            restored["body"] != edited["atom"]["body"],
        )

    print("\n" + "=" * 50)
    if all_pass:
        print("All v3.2 end-to-end tests PASSED")
        return 0
    print("Some tests FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
