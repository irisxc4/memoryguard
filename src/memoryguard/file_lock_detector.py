"""文件占用检测：使用 Windows Restart Manager 检测占用进程。

Windows API：
- RmStartSession: 启动 Restart Manager 会话
- RmRegisterResources: 注册要检测的文件/目录
- RmGetList: 获取占用进程列表
- RmEndSession: 结束会话

Microsoft RmGetList 文档：
https://learn.microsoft.com/en-us/windows/win32/api/restartmanager/nf-restartmanager-rmgetlist

安全原则：
- 只检测和显示，不自动杀进程
- 让用户选择关闭后重试
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Windows API 类型定义
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _rstrtmgr = ctypes.WinDLL("rstrtmgr.dll", use_last_error=True)

    # RM_UNIQUE_PROCESS
    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [
            ("dwProcessId", wintypes.DWORD),
            ("ProcessStartTime", wintypes.FILETIME),
        ]

    # RM_PROCESS_INFO
    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ("Process", RM_UNIQUE_PROCESS),
            ("strAppName", wintypes.WCHAR * 256),
            ("strServiceShortName", wintypes.WCHAR * 64),
            ("ApplicationType", wintypes.DWORD),
            ("AppStatus", wintypes.ULONG),
            ("TSSessionId", wintypes.DWORD),
            ("bRestartable", wintypes.BOOL),
        ]

    _RmStartSession = _rstrtmgr.RmStartSession
    _RmStartSession.restype = wintypes.DWORD
    _RmStartSession.argtypes = [
        ctypes.POINTER(wintypes.DWORD),  # pSessionHandle
        wintypes.DWORD,                   # dwSessionFlags
        wintypes.LPWSTR,                  # strSessionKey
    ]

    _RmRegisterResources = _rstrtmgr.RmRegisterResources
    _RmRegisterResources.restype = wintypes.DWORD
    _RmRegisterResources.argtypes = [
        wintypes.DWORD,                   # dwSessionHandle
        wintypes.UINT,                    # nFiles
        ctypes.POINTER(wintypes.LPCWSTR), # rgsFilenames
        wintypes.UINT,                    # nApplications
        ctypes.POINTER(RM_UNIQUE_PROCESS),
        wintypes.UINT,                    # nServices
        ctypes.POINTER(wintypes.LPCWSTR), # rgsServiceNames
    ]

    _RmGetList = _rstrtmgr.RmGetList
    _RmGetList.restype = wintypes.DWORD
    _RmGetList.argtypes = [
        wintypes.DWORD,                          # dwSessionHandle
        ctypes.POINTER(wintypes.UINT),           # pnProcInfoNeeded
        ctypes.POINTER(wintypes.UINT),           # pnProcInfo
        ctypes.POINTER(RM_PROCESS_INFO),         # rgAffectedApps
        ctypes.POINTER(wintypes.DWORD),          # lpdwRebootReasons
    ]

    _RmEndSession = _rstrtmgr.RmEndSession
    _RmEndSession.restype = wintypes.DWORD
    _RmEndSession.argtypes = [wintypes.DWORD]


@dataclass
class LockingProcess:
    """占用进程信息。"""
    pid: int
    app_name: str
    service_name: str
    session_id: int

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "app_name": self.app_name,
            "service_name": self.service_name,
            "session_id": self.session_id,
        }


def detect_locking_processes(path: str | Path) -> list[LockingProcess]:
    """检测占用指定文件/目录的进程列表。

    使用 Windows Restart Manager API。
    返回 LockingProcess 列表，空列表表示无占用或不支持。
    """
    if sys.platform != "win32":
        return []

    target = str(Path(path).resolve())
    if not Path(target).exists():
        return []

    try:
        session_key = ctypes.create_unicode_buffer(256)
        session_handle = wintypes.DWORD(0)

        result = _RmStartSession(
            ctypes.byref(session_handle), 0, session_key
        )
        if result != 0:
            return []

        try:
            # 注册资源
            file_path = wintypes.LPCWSTR(target)
            result = _RmRegisterResources(
                session_handle, 1, ctypes.byref(file_path),
                0, None, 0, None,
            )
            if result != 0:
                return []

            # 获取进程数
            proc_count = wintypes.UINT(0)
            proc_needed = wintypes.UINT(0)
            reboot_reasons = wintypes.DWORD(0)

            result = _RmGetList(
                session_handle,
                ctypes.byref(proc_needed),
                ctypes.byref(proc_count),
                None,
                ctypes.byref(reboot_reasons),
            )

            # ERROR_MORE_DATA = 234
            if result != 234 and result != 0:
                return []

            if proc_needed.value == 0:
                return []

            # 分配缓冲区并再次获取
            proc_count = wintypes.UINT(proc_needed.value)
            proc_array = (RM_PROCESS_INFO * proc_needed.value)()

            result = _RmGetList(
                session_handle,
                ctypes.byref(proc_needed),
                ctypes.byref(proc_count),
                proc_array,
                ctypes.byref(reboot_reasons),
            )

            if result != 0:
                return []

            processes = []
            for i in range(proc_count.value):
                info = proc_array[i]
                processes.append(LockingProcess(
                    pid=info.Process.dwProcessId,
                    app_name=info.strAppName,
                    service_name=info.strServiceShortName,
                    session_id=info.TSSessionId,
                ))
            return processes

        finally:
            _RmEndSession(session_handle)

    except Exception:
        return []


def format_locking_message(processes: list[LockingProcess]) -> str:
    """格式化占用进程信息为人类可读消息。"""
    if not processes:
        return ""
    lines = ["以下进程正在占用目标文件："]
    for p in processes:
        name = p.app_name or p.service_name or f"PID {p.pid}"
        lines.append(f"  - {name} (PID: {p.pid})")
    lines.append("")
    lines.append("请关闭以上程序后重试。MemoryGuard 不会自动关闭进程。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 失败分类（增强版）
# ---------------------------------------------------------------------------

def classify_operation_failure(error: str, path: str | Path = "") -> str:
    """分类操作失败原因。

    返回值：
    - sandbox_permission_denied: 沙箱权限不足，交给桌面执行器
    - resource_locked: 文件被占用，需关闭占用进程
    - policy_denied: ACL/杀毒/系统策略阻止
    - target_recreated: Agent 后台进程又创建了目录
    - not_found: 目标不存在
    - unknown: 未知错误
    """
    error_lower = error.lower()

    if "permission" in error_lower or "access denied" in error_lower or "access is denied" in error_lower:
        # 检查是否有进程占用
        if path:
            lockers = detect_locking_processes(path)
            if lockers:
                return "resource_locked"
        return "sandbox_permission_denied"

    if "being used" in error_lower or " is locked" in error_lower or "file locked" in error_lower or "process is busy" in error_lower:
        return "resource_locked"

    if "sharing violation" in error_lower or "in use" in error_lower:
        return "resource_locked"

    if "policy" in error_lower or "acl" in error_lower or "antivirus" in error_lower:
        return "policy_denied"

    if "not found" in error_lower or "no longer exists" in error_lower or "cannot find" in error_lower:
        return "not_found"

    if "already exists" in error_lower or "recreated" in error_lower:
        return "target_recreated"

    return "unknown"
