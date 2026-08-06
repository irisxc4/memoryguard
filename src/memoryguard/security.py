"""安全层：会话令牌、API 白名单、请求队列、沙箱检测。

核心原则：
- 任何 Agent 都能启动和使用 GUI
- 任何不可逆操作都必须跨越到独立桌面执行器
- localhost 模式下变更 API 只允许"提交请求"
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# API 方法分类
# ---------------------------------------------------------------------------

# 只读 API：扫描、查看、列表，不修改任何文件或状态
READONLY_API_METHODS: frozenset[str] = frozenset({
    # 审计
    "get_audit", "run_audit", "generate_plan",
    # Agent 发现 / 选择树
    "discover_agents", "get_selection_tree", "get_agent_data",
    # 神经元图（只读）
    "get_neuron_graph", "get_projection_source_map", "get_governance_scope",
    "get_governance_scope_state",
    "list_native_memory_releases", "list_publish_targets", "choose_publish_target_path",
    # 来源管理（只读）
    "list_sources", "preview_source", "scan_sources",
    "get_raw_memory", "get_source_file_content",
    # Agent 候选 / 清理（只读）
    "list_agent_candidates", "list_archived_agents",
    "list_cleanup_history", "list_agents", "get_residual_cleanup", "open_agent_folder",
    # 绑定（只读）
    "list_bindings", "check_binding_drift", "get_shared_group_preview",
    "get_host_hook_status",
    # 外部 MCP（只读）
    "list_external_mcp_servers", "preview_external_mcp_import",
    "detect_external_mcp",
    # 记忆治理（只读）
    "list_memory", "get_memory", "search_memory",
    "list_memory_versions", "get_recent_events", "get_auto_actions",
    "get_supersede_chain", "get_conflicts", "get_quarantine",
    "get_governance_snapshot", "get_memory_status", "get_supersede_decisions",
    "list_share_groups", "get_global_memory_status", "get_memory_source_map",
    # 萃取 / 导入（只读）
    "extract_preview", "extract_preview_by_path", "preview_import", "get_memory_ir",
    # 旧发布历史（只读兼容；不再暴露构建/写回）
    "list_releases", "list_history",
    # 存储治理（只读）
    "plan_memoryguard_gc", "get_storage_overview",
    # 安全层（只读）
    "get_sandbox_status", "get_request_status", "list_pending_requests",
    "pick_path",
    # Host AI 整理（只读）
    "list_pending_enrichments", "get_enrichment_status",
    "get_host_enrichment_guide",
    "get_build_progress", "list_host_llm_agents",
    # Conversation history is raw evidence in its own local store.  These
    # operations only browse/search/export the caller's scoped archive.
    "list_history_sessions", "search_history", "history_timeline",
    "history_read", "history_extract_preview", "export_history",
    # Installed-before-MemoryGuard local transcript inventory.  Discovery
    # touches metadata only and never creates an archive or state receipt.
    "discover_local_history_sources",
    "list_rules_habits",
    # Mandatory-rule audience governance.  Options and previews are read-only;
    # edits must take the normal mutation/confirmation path below.
    "get_rule_scope_options", "preview_effective_rules",
    # Rule lifecycle cockpit: reads are safe previews; mutations still pass
    # the normal localhost confirmation/admin capability path.
    "list_rule_cockpit", "list_rule_decisions", "read_rule_decision",
    "get_rule_auto_scope_metrics", "list_rule_match_receipts", "list_rule_exceptions",
    # Knowledge bookshelf reads.
    "knowledge_list", "knowledge_deleted_list", "knowledge_search",
    "knowledge_read", "knowledge_book", "knowledge_job_status",
    "knowledge_candidates_list", "knowledge_candidate_targets",
})

# 变更 API：修改文件、删除目录、归档、绑定、记忆治理等
# localhost 沙箱模式下这些方法不直接执行，而是创建 pending request
MUTATION_API_METHODS: frozenset[str] = frozenset({
    # Agent 选择 / 投影
    "commit_selection", "neuron_decide", "set_projection_source_enabled",
    "set_governance_scope", "build_projection", "start_build_projection",
    "cancel_build_projection", "delete_projection",
    # 来源管理（变更）
    "add_source", "remove_source",
    # Agent 候选 / 清理 / 归档（变更）
    "mark_agent_uninstalled", "unmark_agent_uninstalled",
    "archive_agent_dir", "restore_archived_agent",
    "delete_archived_agent",
    # 多 Agent 模式 / 绑定（变更）
    "enter_multi_agent_mode", "exit_multi_agent_mode",
    "bind_agent", "bind_agents_to_shared_group", "unbind_agent",
    "ensure_personal_memory_group", "leave_shared_group_to_personal",
    "dissolve_shared_group",
    "export_memory_group", "clear_memory_group", "archive_memory_group",
    "install_shared_group_mcp_redirects",
    "set_host_hook_mode", "uninstall_host_hook",
    "import_native_memories_to_group", "commit_shared_memory_governance",
    # 外部 MCP 导入（变更）
    "import_external_mcp_entries",
    # 记忆治理（变更）
    "edit_memory", "lock_memory", "unlock_memory",
    "set_memory_injection_policy",
    "restore_memory", "delete_memory", "rollback_memory",
    "resolve_conflict", "release_quarantine", "delete_quarantine",
    # 萃取 / 导入（变更）
    "accept_candidates", "create_import",
    # 存储治理（变更）
    "apply_memoryguard_gc",
    # 计划执行 / 回滚
    "apply_plan", "undo_change",
    # Host AI 整理（变更:写回 IR；主路径已并入 build）
    "apply_enrichments",
    # Raw-history deletion is deliberate and confirmation-gated by the API.
    # It never deletes the governed long-term-memory record.
    "delete_history",
    # Imports raw transcript evidence into history.sqlite and updates the
    # resumable receipt; this must use the existing confirmation path.
    "backfill_local_history",
    # Changes audience assignments or performs an atomic relevant<->always
    # transition.  Never expose this as a read-only browser call.
    "update_rule_audience",
    "create_rule_from_text", "undo_rule_decision", "create_child_exception",
    "create_rule_exception", "submit_rule_feedback", "revoke_rule_exception",
    # 知识书库（变更）
    "knowledge_add", "knowledge_reingest", "knowledge_rebuild_smart",
    "knowledge_remove", "knowledge_restore", "knowledge_purge_deleted",
    "knowledge_update_settings", "knowledge_candidate_review",
})

# 所有允许的 API 方法
ALL_ALLOWED_METHODS: frozenset[str] = READONLY_API_METHODS | MUTATION_API_METHODS

# 保留在 GovernanceApi 内仅供旧数据兼容/内部测试的原生写回实现。
# 产品入口（GUI bridge、MCP、CLI）必须明确拒绝这些方法。
BLOCKED_LEGACY_NATIVE_WRITEBACK_METHODS: frozenset[str] = frozenset({
    "create_build_plan", "apply_build", "verify_release", "rollback_release",
    "publish_reconstructed_memory", "rollback_native_memory_release",
})

# 安全层自身的方法（不属于只读或变更，但需要允许调用）
_SECURITY_API_METHODS: frozenset[str] = frozenset({
    "submit_request", "get_request_status", "list_pending_requests",
    "get_sandbox_status", "get_api_method_registry",
})

# Feedback authority is a transport fact, never inferred from a caller's
# actor/display string.  The MCP path is bound to ``agent``; only the trusted
# GUI bridge and host-hook internals may select the other producers.
FEEDBACK_AUTHORITY: dict[str, int] = {
    "user": 4,
    "agent": 3,
    "hook": 2,
    "legacy": 1,
    "unobserved": 1,
}
TRUSTED_FEEDBACK_ENTRYPOINTS: dict[str, str] = {
    "mcp": "agent",
    "gui": "user",
    "hook": "hook",
}


def trusted_feedback_producer(entrypoint: str) -> str:
    """Resolve a producer from a trusted in-process boundary."""
    key = str(entrypoint or "").strip().casefold()
    try:
        return TRUSTED_FEEDBACK_ENTRYPOINTS[key]
    except KeyError as exc:
        raise ValueError("unknown feedback entrypoint") from exc


def feedback_authority(producer: str) -> int:
    """Return fixed precedence for a server-selected producer."""
    key = str(producer or "").strip().casefold()
    try:
        return FEEDBACK_AUTHORITY[key]
    except KeyError as exc:
        raise ValueError("unknown feedback producer") from exc


def is_readonly_method(method: str) -> bool:
    """判断方法是否只读。"""
    return method in READONLY_API_METHODS


def is_mutation_method(method: str) -> bool:
    """判断方法是否为变更操作。"""
    return method in MUTATION_API_METHODS


def is_allowed_method(method: str) -> bool:
    """判断方法是否在白名单中。"""
    return method in ALL_ALLOWED_METHODS or method in _SECURITY_API_METHODS


# ---------------------------------------------------------------------------
# 会话令牌
# ---------------------------------------------------------------------------

def generate_session_token() -> str:
    """生成随机会话令牌（256 bit）。"""
    return secrets.token_urlsafe(32)


def generate_request_id() -> str:
    """生成随机请求 ID。"""
    return secrets.token_hex(16)


def generate_nonce() -> str:
    """生成一次性 nonce。"""
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# 沙箱检测
# ---------------------------------------------------------------------------

def detect_sandbox_mode() -> bool:
    """检测当前进程是否在沙箱中运行。

    启发式：
    - 环境变量 MEMORYGUARD_SANDBOX=1 / 0（显式 0 优先，覆盖 IDE 标记）
    - TRAE/Cursor/Claude Code 等 IDE 启动的子进程
    - 进程令牌受限（Windows）
    """
    explicit = os.environ.get("MEMORYGUARD_SANDBOX", "").strip().lower()
    if explicit in ("0", "false", "no", "off"):
        return False
    if explicit in ("1", "true", "yes", "on"):
        return True
    # IDE 启动的子进程通常带有特定环境变量
    ide_markers = [
        "TRAE_WORKSPACE", "CURSOR_TRACE_ID", "CLAUDE_CODE_ENTRYPOINT",
        "VSCODE_IPC_HOOK", "CODE_WORKSPACE_FOLDER",
    ]
    for marker in ide_markers:
        if os.environ.get(marker):
            return True
    return False


# ---------------------------------------------------------------------------
# 请求队列
# ---------------------------------------------------------------------------

@dataclass
class PendingRequest:
    """待执行请求（由 localhost GUI 提交，由桌面执行器确认后执行）。"""
    request_id: str
    method: str
    args: list[Any]
    nonce: str
    created_at: float
    expires_at: float
    status: str = "pending"  # pending | approved | executing | done | rejected | expired
    result: dict | None = None
    error: str = ""
    executed_by: str = ""  # 执行者标识

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "method": self.method,
            "args": self.args,
            "nonce": self.nonce,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "executed_by": self.executed_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> PendingRequest:
        return cls(
            request_id=d["request_id"],
            method=d["method"],
            args=d.get("args", []),
            nonce=d.get("nonce", ""),
            created_at=d.get("created_at", 0),
            expires_at=d.get("expires_at", 0),
            status=d.get("status", "pending"),
            result=d.get("result"),
            error=d.get("error", ""),
            executed_by=d.get("executed_by", ""),
        )


class RequestQueue:
    """请求队列：localhost GUI 提交 -> 桌面执行器消费。

    存储在 .memoryguard/request-queue.json，确保跨进程可见。
    """

    REQUEST_TTL_SECONDS = 300  # 5 分钟过期

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self.queue_path = self.workspace / ".memoryguard" / "request-queue.json"

    def _load(self) -> list[dict]:
        if not self.queue_path.exists():
            return []
        try:
            data = json.loads(self.queue_path.read_text(encoding="utf-8"))
            return data.get("requests", [])
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, requests: list[dict]) -> None:
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        # 先写入临时文件再原子替换
        tmp = self.queue_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"requests": requests}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.queue_path)

    def submit(self, method: str, args: list[Any]) -> PendingRequest:
        """提交一个待执行请求。提交后通知桌面执行器。"""
        now = time.time()
        req = PendingRequest(
            request_id=generate_request_id(),
            method=method,
            args=args,
            nonce=generate_nonce(),
            created_at=now,
            expires_at=now + self.REQUEST_TTL_SECONDS,
        )
        requests = self._load()
        # 清理过期请求
        requests = [r for r in requests if r.get("expires_at", 0) > now]
        requests.append(req.to_dict())
        self._save(requests)
        # 通知桌面执行器
        self._notify_desktop(req.request_id)
        return req

    def _notify_desktop(self, request_id: str) -> None:
        """通知桌面执行器有新请求。

        可靠唤醒逻辑：
        1. 命名管道 -> 已有执行器运行，直接通知
        2. 直接启动桌面执行器处理指定请求（主路径，不依赖 URI 注册）
        3. URI 协议唤醒（备用）
        4. 写通知文件（最终 fallback）
        """
        from .ipc import (
            notify_desktop, build_uri, launch_uri, write_notify_file,
            launch_desktop_request,
            is_uri_protocol_registered, register_uri_protocol,
            PIPE_DELIVERED, PIPE_UNAVAILABLE,
        )

        # 方式1：如果已有桌面执行器在运行，用命名管道通知
        try:
            result = notify_desktop(self.workspace, request_id)
            if result == PIPE_DELIVERED:
                return
        except Exception:
            result = PIPE_UNAVAILABLE

        if result == PIPE_UNAVAILABLE:
            # 方式2：直接启动桌面执行器处理本请求（可靠主路径）
            if launch_desktop_request(self.workspace, request_id):
                return

            # 方式3：URI 协议唤醒（备用）
            if sys.platform == "win32" and not is_uri_protocol_registered():
                register_uri_protocol()
            uri = build_uri(request_id)
            if launch_uri(uri):
                return

        # 方式4：写通知文件（最终 fallback）
        write_notify_file(self.workspace, request_id)

    def list_pending(self) -> list[PendingRequest]:
        """列出所有待执行请求。"""
        now = time.time()
        requests = self._load()
        return [
            PendingRequest.from_dict(r) for r in requests
            if r.get("status") == "pending" and r.get("expires_at", 0) > now
        ]

    def list_all(self) -> list[PendingRequest]:
        """列出所有请求（包括已完成和过期的）。"""
        requests = self._load()
        return [PendingRequest.from_dict(r) for r in requests]

    def get(self, request_id: str) -> PendingRequest | None:
        """按 ID 获取请求。"""
        for r in self._load():
            if r.get("request_id") == request_id:
                return PendingRequest.from_dict(r)
        return None

    def update(self, request_id: str, **updates) -> PendingRequest | None:
        """更新请求状态（非原子，用于一般状态更新）。"""
        requests = self._load()
        for r in requests:
            if r.get("request_id") == request_id:
                r.update(updates)
                self._save(requests)
                return PendingRequest.from_dict(r)
        return None

    def claim(self, request_id: str, executor_id: str) -> PendingRequest | None:
        """原子声明请求：将 pending -> executing。

        使用文件锁防止两个执行器同时执行同一请求。
        返回声明后的请求，或 None（已被声明/不存在/已过期）。
        """
        lock_path = self.queue_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        lock_fd = None
        try:
            lock_fd = open(lock_path, "w")
            # 文件锁（跨进程互斥）
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            requests = self._load()
            for r in requests:
                if r.get("request_id") == request_id:
                    if r.get("status") != "pending":
                        return None  # 已被声明
                    if r.get("expires_at", 0) < time.time():
                        r["status"] = "expired"
                        self._save(requests)
                        return None  # 已过期
                    r["status"] = "executing"
                    r["executed_by"] = executor_id
                    # 消费 nonce：标记为已使用
                    r["nonce_consumed"] = True
                    self._save(requests)
                    return PendingRequest.from_dict(r)
            return None
        except (OSError, IOError):
            # 锁竞争失败
            return None
        finally:
            if lock_fd:
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
                lock_fd.close()

    def validate_nonce(self, request_id: str, nonce: str) -> bool:
        """校验 nonce 是否匹配且未被消费。"""
        req = self.get(request_id)
        if not req:
            return False
        if req.nonce != nonce:
            return False
        # 检查是否已被消费
        requests = self._load()
        for r in requests:
            if r.get("request_id") == request_id:
                return not r.get("nonce_consumed", False)
        return False

    def cleanup_expired(self) -> int:
        """清理过期请求，返回清理数量。"""
        now = time.time()
        requests = self._load()
        before = len(requests)
        requests = [r for r in requests if r.get("expires_at", 0) > now]
        after = len(requests)
        if before != after:
            self._save(requests)
        return before - after
