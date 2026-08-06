"""v3.2 AgentBinding 落盘与共享组绑定后端。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema_v3 import (
    AgentBinding,
    BindingStatus,
    NativeMemoryMode,
    stable_hash,
    _now_iso,
)
from .shared_memory_store import SharedMemoryStore


# 个人记忆层与共享记忆层共用同一 SharedMemoryStore；个人组只是稳定的
# 单成员组 ID。原始 agent id 永远不拼入路径，避免不安全字符和跨平台漂移。
PERSONAL_GROUP_PREFIX = "personal-"


def personal_group_id(agent_instance_id: str) -> str:
    """返回跨进程稳定、路径安全的个人组 ID。"""
    agent_id = str(agent_instance_id or "").strip()
    if not agent_id:
        raise ValueError("agent_instance_id_required")
    return PERSONAL_GROUP_PREFIX + stable_hash("personal-memory-group", agent_id)


# 语义更明确的别名，供 GUI/MCP/测试统一引用。
personal_memory_group_id = personal_group_id


def is_personal_group_id(group_id: str) -> bool:
    return str(group_id or "").startswith(PERSONAL_GROUP_PREFIX)


def group_kind(group_id: str) -> str:
    return "personal" if is_personal_group_id(group_id) else "shared"


class AgentBindingStore:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / ".memoryguard" / "agent-bindings"
        self.ledger_path = self.root / "ledger.jsonl"

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _binding_path(self, binding_id: str) -> Path:
        return self.root / f"{binding_id}.json"

    def list_bindings(self, include_inactive: bool = True) -> list[AgentBinding]:
        if not self.root.exists():
            return []
        bindings: list[AgentBinding] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                binding = AgentBinding.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, KeyError):
                continue
            if not include_inactive and binding.status == BindingStatus.INACTIVE:
                continue
            bindings.append(binding)
        return bindings

    def get_binding(self, binding_id: str) -> AgentBinding | None:
        path = self._binding_path(binding_id)
        if not path.exists():
            return None
        try:
            return AgentBinding.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError):
            return None

    def find_by_agent(self, agent_instance_id: str, include_inactive: bool = False) -> list[AgentBinding]:
        return [
            b for b in self.list_bindings(include_inactive=include_inactive)
            if b.agent_instance_id == agent_instance_id
        ]

    def find_by_group(self, share_group_id: str, include_inactive: bool = True) -> list[AgentBinding]:
        return [
            b for b in self.list_bindings(include_inactive=include_inactive)
            if b.share_group_id == share_group_id
        ]

    def bind_agent(self, agent_instance_id: str, share_group_id: str,
                   mcp_server_name: str = "memoryguard",
                   native_memory_mode: str | NativeMemoryMode = NativeMemoryMode.OBSERVED,
                   redirect_paths: list[str] | None = None) -> AgentBinding:
        agent_id = str(agent_instance_id or "").strip()
        group_id = str(share_group_id or "").strip()
        if not agent_id or not group_id:
            raise ValueError("agent_instance_id_and_share_group_id_required")
        if is_personal_group_id(group_id):
            if group_id != personal_group_id(agent_id):
                raise ValueError("personal_group_owner_mismatch")
            if any(
                b.agent_instance_id != agent_id
                for b in self.find_by_group(group_id, include_inactive=False)
            ):
                raise ValueError("personal_group_must_have_one_member")
        # 先验证/准备目标 SharedMemoryStore；目标不可用时不得影响旧 binding。
        target_store = SharedMemoryStore(self.workspace, group_id)
        mode = self._native_mode(native_memory_mode)
        binding_id = stable_hash("binding", agent_id, group_id, mcp_server_name)
        now = _now_iso()
        # P2: 唯一性约束 - 新绑定时把该 Agent 的旧 active binding 标记为 INACTIVE
        # 防止静默写错 group(find_by_agent 取 [0] 的问题)
        existing = self.find_by_agent(agent_id, include_inactive=False)
        try:
            for old_binding in existing:
                if old_binding.share_group_id != group_id:
                    self._deactivate_binding(old_binding)
            binding = AgentBinding(
                binding_id=binding_id,
                agent_instance_id=agent_id,
                share_group_id=group_id,
                mcp_server_name=mcp_server_name,
                native_memory_mode=mode,
                status=BindingStatus.ACTIVE,
                redirect_paths=list(redirect_paths or []),
                bound_at=now,
                last_drift_check="",
            )
            self._write_binding(binding)
            self._append_ledger("bind_agent", binding, {"redirect_path_count": len(binding.redirect_paths)})
            target_store._ensure_dirs()
            return binding
        except Exception:
            # 恢复旧 active binding，避免切换半完成留下无绑定状态。
            partial = self.get_binding(binding_id)
            if partial is not None:
                partial.status = BindingStatus.INACTIVE
                self._write_binding(partial)
            for old_binding in existing:
                old_binding.status = BindingStatus.ACTIVE
                self._write_binding(old_binding)
            raise

    def ensure_personal_memory_group(
        self,
        agent_instance_id: str,
        *,
        mcp_server_name: str = "memoryguard",
        native_memory_mode: str | NativeMemoryMode = NativeMemoryMode.OBSERVED,
        redirect_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """确保 Agent 有稳定的单成员个人绑定。

        已有共享绑定时保持不变；切换必须调用显式的
        ``leave_shared_group_to_personal``，因此安装/ensure 不会把共享组拉回个人组。
        """
        agent_id = str(agent_instance_id or "").strip()
        if not agent_id:
            raise ValueError("agent_instance_id_required")
        active = self.find_by_agent(agent_id, include_inactive=False)
        if len(active) > 1:
            raise RuntimeError("multiple_active_bindings")
        if active:
            binding = active[0]
            return self._group_result(binding, created=False, changed=False)
        gid = personal_group_id(agent_id)
        binding = self.bind_agent(
            agent_id, gid, mcp_server_name=mcp_server_name,
            native_memory_mode=native_memory_mode, redirect_paths=redirect_paths,
        )
        return self._group_result(binding, created=True, changed=True)

    def leave_shared_group_to_personal(
        self,
        agent_instance_id: str,
        *,
        confirmed: bool = False,
        mcp_server_name: str = "memoryguard",
        native_memory_mode: str | NativeMemoryMode = NativeMemoryMode.OBSERVED,
        redirect_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """显式退出共享组并回到该 Agent 原来的个人组。

        两边数据库均保留，绝不复制、合并或删除记忆。
        """
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        agent_id = str(agent_instance_id or "").strip()
        if not agent_id:
            return {"ok": False, "error": "agent_instance_id_required"}
        active = self.find_by_agent(agent_id, include_inactive=False)
        if len(active) > 1:
            return {"ok": False, "error": "multiple_active_bindings"}
        if not active:
            return {"ok": False, "error": "agent_not_bound_to_shared_group"}
        if active and is_personal_group_id(active[0].share_group_id):
            return self._group_result(active[0], created=False, changed=False)
        # 显式切换必须绕过 ensure 的“已有绑定保持不变”语义；旧共享 binding
        # 由 bind_agent 的唯一性约束标记 inactive，个人库不合并共享记录。
        personal_binding = self.bind_agent(
            agent_id, personal_group_id(agent_id),
            mcp_server_name=mcp_server_name,
            native_memory_mode=native_memory_mode,
            redirect_paths=redirect_paths,
        )
        result = self._group_result(personal_binding, created=True, changed=True)
        result["changed"] = bool(active)
        result["previous_group_id"] = active[0].share_group_id if active else ""
        return result

    def _group_result(self, binding: AgentBinding, *, created: bool, changed: bool) -> dict[str, Any]:
        group_id = binding.share_group_id
        members = [b.agent_instance_id for b in self.find_by_group(group_id, include_inactive=False)]
        store_path = self.workspace / ".memoryguard" / "shared-memory" / group_id
        # 仅提示旧 ManagedStore 存在；不读取、不导入、不删除。
        legacy_path = self.workspace / ".memoryguard" / "managed-memory" / binding.agent_instance_id
        migration_required = legacy_path.exists() and any(p.is_file() for p in legacy_path.rglob("*"))
        return {
            "ok": True,
            "binding": binding.to_dict(),
            "binding_id": binding.binding_id,
            "agent_instance_id": binding.agent_instance_id,
            "group_id": group_id,
            "share_group_id": group_id,
            "group_kind": group_kind(group_id),
            "members": members,
            "member_count": len(members),
            "store_path": str(store_path),
            "created": created,
            "changed": changed,
            "preserved": True,
            "migration_required": migration_required,
            "migration": "待迁移/可显式导入" if migration_required else "",
        }

    def group_status(self, group_id: str, *, agent_instance_id: str = "") -> dict[str, Any]:
        """输出无歧义的 personal/shared 组状态与 canonical DB 路径。"""
        members = self.find_by_group(group_id, include_inactive=False)
        current = next((b for b in members if b.agent_instance_id == agent_instance_id), None)
        store_path = self.workspace / ".memoryguard" / "shared-memory" / group_id
        legacy_path = self.workspace / ".memoryguard" / "managed-memory" / (current.agent_instance_id if current else (agent_instance_id or ""))
        return {
            "group_id": group_id,
            "share_group_id": group_id,
            "group_kind": group_kind(group_id),
            "members": [b.agent_instance_id for b in members],
            "member_count": len(members),
            "agent_bound": current is not None,
            "binding_id": current.binding_id if current else "",
            "canonical_store_path": str(store_path / "memory.db"),
            "store_path": str(store_path),
            "migration_required": bool(legacy_path and legacy_path.exists() and any(p.is_file() for p in legacy_path.rglob("*"))),
        }

    def _deactivate_binding(self, binding: AgentBinding) -> None:
        """将旧 binding 标记为 INACTIVE(不删除,保留审计)。"""
        from .schema_v3 import BindingStatus
        binding.status = BindingStatus.INACTIVE
        self._write_binding(binding)
        self._append_ledger("deactivate_binding", binding,
                            {"reason": "superseded by new binding"})

    def bind_agents_to_group(self, agent_instance_ids: list[str], share_group_id: str = "",
                             mcp_server_name: str = "memoryguard",
                             native_memory_modes: dict[str, str] | None = None,
                             redirect_paths: dict[str, list[str]] | None = None,
                             allow_empty_group_creation: bool = False) -> dict[str, Any]:
        clean_agents = [str(a or "").strip() for a in dict.fromkeys(agent_instance_ids) if str(a or "").strip()]
        if len(clean_agents) < 2:
            raise ValueError("shared_group_requires_at_least_two_agents")
        explicit_group = str(share_group_id or "").strip()
        group_id = explicit_group or self.create_share_group_id(clean_agents)
        if is_personal_group_id(group_id):
            raise ValueError("personal_group_cannot_be_shared")
        if not explicit_group and not allow_empty_group_creation:
            # C3: 自动派生一个全新共享组、而 workspace 里已有非空遗留组时
            # fail-closed。否则旧记忆会再次被「静默新建空组」孤立（控制面搬家
            # 事故的复发路径）。显式 --share-group-id 视为操作者意图，不拦。
            from .group_migration import find_nonempty_shared_groups
            db_path = Path(self.workspace) / ".memoryguard" / "shared-memory" / group_id / "memory.db"
            if not db_path.exists() and find_nonempty_shared_groups(self.workspace):
                raise ValueError(
                    "legacy_shared_group_data_detected: non-empty shared-memory "
                    "groups exist but no shared binding is linked to them; "
                    "refusing to silently create a new empty group. Run "
                    "`memoryguard groups migrate` first or pass "
                    "allow_empty_group_creation=True."
                )
        # 预校验目标目录，避免第一个 Agent 已切换后第二个才发现 group 非法。
        SharedMemoryStore(self.workspace, group_id)._ensure_dirs()
        modes = native_memory_modes or {}
        paths = redirect_paths or {}
        previous_by_agent = {
            agent_id: self.find_by_agent(agent_id, include_inactive=False)
            for agent_id in clean_agents
        }
        bindings: list[AgentBinding] = []
        try:
            for agent_id in clean_agents:
                binding = self.bind_agent(
                    agent_instance_id=agent_id,
                    share_group_id=group_id,
                    mcp_server_name=mcp_server_name,
                    native_memory_mode=modes.get(agent_id, NativeMemoryMode.REDIRECTED.value),
                    redirect_paths=paths.get(agent_id, []),
                )
                bindings.append(binding)
        except Exception:
            # 批量加入必须全成或全退。前面已切换成功的 Agent 回到原绑定；
            # 原本未绑定的 Agent 则仅留下 inactive 审计记录。
            for new_binding in bindings:
                current = self.get_binding(new_binding.binding_id)
                if current is not None:
                    current.status = BindingStatus.INACTIVE
                    self._write_binding(current)
            for old_bindings in previous_by_agent.values():
                for old_binding in old_bindings:
                    old_binding.status = BindingStatus.ACTIVE
                    self._write_binding(old_binding)
            raise
        store = SharedMemoryStore(self.workspace, group_id)
        return {
            "ok": True,
            "share_group_id": group_id,
            "bindings": [b.to_dict() for b in bindings],
            "preview": self.shared_group_preview(group_id, store),
        }

    def unbind_agent(self, binding_id: str) -> AgentBinding | None:
        binding = self.get_binding(binding_id)
        if binding is None:
            return None
        binding.status = BindingStatus.INACTIVE
        self._write_binding(binding)
        self._append_ledger("unbind_agent", binding, {})
        return binding

    def dissolve_group(self, share_group_id: str) -> dict[str, Any]:
        """解散共享组：将该组全部 active binding 置为 INACTIVE。"""
        if not share_group_id:
            return {"error": "share_group_id_required", "ok": False}
        bindings = self.find_by_group(share_group_id, include_inactive=False)
        unbound: list[dict[str, Any]] = []
        for binding in bindings:
            done = self.unbind_agent(binding.binding_id)
            if done is not None:
                unbound.append(done.to_dict())
        self._ensure_dirs()
        event = {
            "event_id": stable_hash("binding_event", "dissolve_group", share_group_id, _now_iso()),
            "action": "dissolve_group",
            "binding_id": "",
            "agent_instance_id": "",
            "share_group_id": share_group_id,
            "status": BindingStatus.INACTIVE.value,
            "detail": {"unbound_count": len(unbound)},
            "created_at": _now_iso(),
        }
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {
            "ok": True,
            "share_group_id": share_group_id,
            "unbound_count": len(unbound),
            "bindings": unbound,
        }

    def check_drift(self, binding_id: str) -> dict[str, Any]:
        binding = self.get_binding(binding_id)
        if binding is None:
            return {"error": f"binding not found: {binding_id}"}
        missing_paths = [p for p in binding.redirect_paths if p and not Path(p).exists()]
        status = BindingStatus.DRIFTED if missing_paths else BindingStatus.ACTIVE
        binding.status = status
        binding.last_drift_check = _now_iso()
        self._write_binding(binding)
        result = {
            "binding_id": binding.binding_id,
            "agent_instance_id": binding.agent_instance_id,
            "share_group_id": binding.share_group_id,
            "status": binding.status.value,
            "missing_redirect_paths": missing_paths,
            "checked_at": binding.last_drift_check,
        }
        self._append_ledger("check_drift", binding, result)
        return result

    def shared_group_preview(self, share_group_id: str,
                             store: SharedMemoryStore | None = None) -> dict[str, Any]:
        memory_store = store or SharedMemoryStore(self.workspace, share_group_id)
        bindings = self.find_by_group(share_group_id, include_inactive=False)
        status = memory_store.status()
        backup_root = self.workspace / ".memoryguard" / "native-memory"
        backup_status = []
        for binding in bindings:
            baseline = backup_root / binding.agent_instance_id / "baseline-backup"
            backup_status.append({
                "agent_instance_id": binding.agent_instance_id,
                "native_memory_mode": binding.native_memory_mode.value,
                "binding_status": binding.status.value,
                "baseline_backup_exists": baseline.exists(),
                "baseline_backup_path": str(baseline),
            })
        return {
            "share_group_id": share_group_id,
            "group_id": share_group_id,
            "group_kind": group_kind(share_group_id),
            "agent_count": len(bindings),
            "members": [b.agent_instance_id for b in bindings],
            "canonical_store_path": str(memory_store.db_path),
            "bindings": [b.to_dict() for b in bindings],
            "native_memory_backups": backup_status,
            "memory_status": status,
            "auto_write_count": status.get("total_events", 0),
            "auto_decision_count": status.get("total_decisions", 0),
            "conflict_count": status.get("total_conflicts", 0),
            "quarantine_count": status.get("total_quarantine", 0),
        }

    def create_share_group_id(self, agent_instance_ids: list[str]) -> str:
        normalized = sorted(a for a in agent_instance_ids if a)
        seed = "|".join(normalized) or _now_iso()
        return "shared-" + stable_hash("share_group", seed)

    def _write_binding(self, binding: AgentBinding) -> None:
        self._ensure_dirs()
        self._binding_path(binding.binding_id).write_text(
            json.dumps(binding.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _append_ledger(self, action: str, binding: AgentBinding, detail: dict[str, Any]) -> None:
        self._ensure_dirs()
        event = {
            "event_id": stable_hash("binding_event", action, binding.binding_id, _now_iso()),
            "action": action,
            "binding_id": binding.binding_id,
            "agent_instance_id": binding.agent_instance_id,
            "share_group_id": binding.share_group_id,
            "status": binding.status.value,
            "detail": dict(detail),
            "created_at": _now_iso(),
        }
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _native_mode(self, value: str | NativeMemoryMode) -> NativeMemoryMode:
        if isinstance(value, NativeMemoryMode):
            return value
        try:
            return NativeMemoryMode(value)
        except ValueError:
            return NativeMemoryMode.UNSUPPORTED
