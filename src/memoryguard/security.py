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
# API 方法分类（兼容导出；唯一事实源在 cutover_v2.surfaces）
# ---------------------------------------------------------------------------

from .cutover_v2.surfaces import GUI_METHOD_NAMES, GUI_MUTATION_NAMES, GUI_OPERATION_SPECS

MUTATION_API_METHODS: frozenset[str] = GUI_MUTATION_NAMES
READONLY_API_METHODS: frozenset[str] = frozenset(
    name for name, spec in GUI_OPERATION_SPECS.items() if not spec.mutation
)
ALL_ALLOWED_METHODS: frozenset[str] = GUI_METHOD_NAMES

# Phase 6 keeps this registry as the single security classification source for
# GUI SafeBridge and CLI cutover adapters.  The version is metadata only; it
# does not alter the public method names or old request envelope.
API_METHOD_REGISTRY_VERSION = 3
V2_CUTOVER_STATES: frozenset[str] = frozenset({
    "V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE",
})

# 保留在 GovernanceApi 内仅供旧数据兼容/内部测试的原生写回实现。
# 产品入口（GUI bridge、MCP、CLI）必须明确拒绝这些方法。
# Compatibility export only.  V2 GUI operations are governed by the canonical
# registry and must not be blocked merely because their historical
# implementation used a legacy writeback path.
BLOCKED_LEGACY_NATIVE_WRITEBACK_METHODS: frozenset[str] = frozenset()

# 安全层自身的方法（不属于只读或变更，但需要允许调用）
_SECURITY_API_METHODS: frozenset[str] = frozenset({
    "submit_request", "get_request_status", "list_pending_requests",
    "get_sandbox_status", "get_api_method_registry", "dispatch_api",
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


def get_api_method_registry() -> dict[str, Any]:
    """Return the canonical GUI registry plus compatibility classifications."""
    return {
        "version": API_METHOD_REGISTRY_VERSION,
        "readonly": sorted(READONLY_API_METHODS),
        "mutation": sorted(MUTATION_API_METHODS),
        "security": sorted(_SECURITY_API_METHODS),
        "operations": {
            name: spec.to_dict() for name, spec in GUI_OPERATION_SPECS.items()
        },
    }


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
