"""V2 group-control persistence and multi-agent lifecycle checks."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.runtime_v2.group_native import GroupControlService
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


def _activate_v2(workspace: Path) -> None:
    initialize_all(WorkspaceV2Layout(workspace))
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="v3-2-agent-binding-core")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="agent-binding-source",
        target_digest="agent-binding-target",
        manifest_digest="agent-binding-manifest",
        digests={"validator_passed": True, "checkpoints": {"core": True}},
    )
    manager.transition(ManifestState.V2_ACTIVE)


def _write_atom(workspace: Path, group: str, agent: str) -> None:
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    governance = GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    context = V2MutationContext(
        workspace_id=str(workspace.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        actor=agent,
        admin=True,
        authority="manual",
    )
    governance.put_atom(
        MemoryAtom(
            memory_id="team-memory",
            body="shared V2 memory",
            kind="project",
            share_group_id=group,
            agent_instance_id=agent,
            workspace_id=str(workspace.resolve()),
        ),
        context=context,
        evidence_ids=["evidence-team-memory"],
        reason="agent binding fixture",
        idempotency_key="agent-binding-memory",
    )


def main() -> int:
    all_pass = True
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _activate_v2(workspace)
        service = GroupControlService(workspace, write=True)

        print("\n=== 1. 单 Agent binding 落盘 ===")
        redirect_file = workspace / "AGENTS.md"
        redirect_file.write_text("# test\n", encoding="utf-8")
        result = service.bind_agent(
            agent_instance_id="codex-1",
            share_group_id="solo-codex",
            native_memory_mode="redirected",
            redirect_paths=[str(redirect_file)],
            idempotency_key="bind-codex-1",
        )
        binding = result["binding"]
        all_pass &= _check("binding 已持久化", service.store.db_path.exists(), str(service.store.db_path))
        loaded = service.active_binding_for_agent("codex-1")
        all_pass &= _check(
            "binding 可读回",
            loaded is not None
            and all(loaded.get(key) == binding.get(key) for key in binding),
        )
        conn = sqlite3.connect(service.store.db_path)
        try:
            row = conn.execute(
                "SELECT status,native_memory_mode FROM agent_group_bindings WHERE binding_id=?",
                (binding["binding_id"],),
            ).fetchone()
            receipt = conn.execute(
                "SELECT operation FROM group_operation_receipts WHERE idempotency_key=?",
                ("bind-codex-1",),
            ).fetchone()
        finally:
            conn.close()
        all_pass &= _check("binding 行与原子 receipt 存在", row == ("active", "redirected") and receipt == ("bind_agent",))

        print("\n=== 2. 多 Agent 共享组绑定 ===")
        result = service.bind_agents(
            ["codex-1", "claude-code-1"],
            share_group_id="team-alpha",
            native_memory_modes={"codex-1": "redirected", "claude-code-1": "observed"},
            redirect_paths={"codex-1": [str(redirect_file)], "claude-code-1": []},
            idempotency_key="bind-team-alpha",
        )
        bindings = result.get("bindings", [])
        team_binding = next(item for item in bindings if item["agent_instance_id"] == "codex-1")
        all_pass &= _check("两个 Agent 绑定成功", len(bindings) == 2, f"count={len(bindings)}")
        all_pass &= _check("同一 share_group", all(b["share_group_id"] == "team-alpha" for b in bindings))
        _write_atom(workspace, "team-alpha", "codex-1")
        preview = service.group_preview("team-alpha")
        all_pass &= _check("共享预览含两个 Agent", preview.get("agent_count", preview.get("member_count")) == 2, f"preview={preview}")
        all_pass &= _check("预览含 V2 memory 统计", "memory_count" in preview)

        print("\n=== 3. 漂移检测 ===")
        missing = workspace / "missing.md"
        drift_binding = service.bind_agent(
            "windsurf-1", "team-alpha", native_memory_mode="observed", redirect_paths=[str(missing)],
            idempotency_key="bind-windsurf-1",
        )["binding"]
        drift = service.check_drift(drift_binding["binding_id"])
        all_pass &= _check("缺失 redirect path -> drifted", drift.get("binding_status") == "drifted", f"status={drift.get('binding_status')}")
        active_check = service.check_drift(team_binding["binding_id"])
        all_pass &= _check("存在 redirect path -> active", active_check.get("binding_status") == "active", f"status={active_check.get('binding_status')}")

        print("\n=== 4. 解绑 ===")
        unbound = service.unbind(drift_binding["binding_id"], idempotency_key="unbind-windsurf-1")
        all_pass &= _check("解绑成功", unbound.get("changed") is True)
        active_bindings = service.list_bindings(include_inactive=False).get("bindings", [])
        all_pass &= _check("active 列表不含已解绑", all(b["binding_id"] != drift_binding["binding_id"] for b in active_bindings))

        print("\n=== 5. 归档共享组 ===")
        archived = service.archive_group("team-alpha")
        all_pass &= _check("归档成功", archived.get("ok") is True, str(archived))
        all_pass &= _check("解绑数 >= 2", archived.get("unbound_count", 0) >= 2, f"n={archived.get('unbound_count')}")
        export_path = Path(archived.get("export_path") or "")
        all_pass &= _check("V2 导出文件存在", export_path.is_file(), str(export_path))
        all_pass &= _check("V2 数据保留且原生文件未移动", archived.get("data_preserved") is True and archived.get("native_files_changed") is False)
        active_after = [
            b for b in service.list_bindings(include_inactive=False).get("bindings", [])
            if b.get("share_group_id") == "team-alpha"
        ]
        all_pass &= _check("解散后无 active binding", len(active_after) == 0, f"n={len(active_after)}")

    print("\n" + "=" * 50)
    if all_pass:
        print("All v3.2 V2 AgentBinding tests PASSED")
        return 0
    print("Some V2 AgentBinding tests FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
