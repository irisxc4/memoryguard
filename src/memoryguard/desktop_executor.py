"""MemoryGuard Desktop：可信执行端。

独立桌面程序，用户双击启动，不需要管理员权限。
不是 Windows 服务，不常驻后台。

职责：
- 接收 Agent GUI 的操作请求（从请求队列读取）
- 自己重新扫描并确定真实目标路径
- 显示原生确认窗口后再执行
- 更新请求状态

安全原则：
- 任何不可逆操作都必须跨越到桌面执行器
- 由用户确认，不是客户端传来的 confirmed=true
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    try:
        text = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
        return "microsoft" in text or "wsl" in text
    except Exception:
        return bool(os.environ.get("WSL_DISTRO_NAME"))


# ---------------------------------------------------------------------------
# 请求执行器
# ---------------------------------------------------------------------------

class RequestExecutor:
    """执行请求队列中的待执行请求。

    在桌面执行器进程中运行，拥有完整用户权限。
    """

    # 变更方法的人类可读描述
    METHOD_DESCRIPTIONS = {
        "commit_selection": "授权提交选中的来源文件",
        "neuron_decide": "执行神经元治理操作",
        "build_projection": "构建原生记忆投影",
        "delete_projection": "删除原生记忆投影",
        "mark_agent_uninstalled": "标记 Agent 为已卸载",
        "unmark_agent_uninstalled": "取消标记 Agent 为已卸载",
        "archive_agent_dir": "归档 Agent 数据目录（可恢复）",
        "restore_archived_agent": "恢复已归档的 Agent 数据",
        "delete_archived_agent": "永久删除已归档的 Agent 数据",
        "purge_agent_dir": "直接清除 Agent 数据目录（已禁用，请打开文件夹手动处理）",
        "enter_multi_agent_mode": "进入多 Agent 共享 MCP 模式",
        "exit_multi_agent_mode": "退出多 Agent 共享 MCP 模式",
        "bind_agent": "绑定 Agent 到共享组",
        "bind_agents_to_shared_group": "批量绑定 Agent 到共享组",
        "unbind_agent": "解除 Agent 绑定",
        "import_external_mcp_entries": "导入外部 MCP 条目",
        "knowledge_add": "添加知识书库文件夹并入库",
        "knowledge_reingest": "重新整理知识书库",
        "knowledge_remove": "删除知识书库",
        "knowledge_candidate_review": "审核记忆候选（批准/拒绝）",
    }

    # 高风险方法（需要额外警告）
    HIGH_RISK_METHODS = frozenset({
        "delete_archived_agent",
        "delete_projection",
    })

    DISABLED_METHODS = frozenset({"purge_agent_dir"})

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        from .security import RequestQueue
        self.queue = RequestQueue(self.workspace)

    def _log(self, message: str) -> None:
        try:
            from datetime import datetime
            log_path = self.workspace / ".memoryguard" / "desktop-executor.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            line = f"{datetime.now().isoformat()} pid={os.getpid()} {message}\n"
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def describe_request(self, req) -> str:
        """生成请求的人类可读描述。"""
        desc = self.METHOD_DESCRIPTIONS.get(req.method, req.method)
        # 根据方法类型添加参数详情
        if req.method == "purge_agent_dir":
            # args: [product, dir_path, candidate_id, dry_run]
            dir_path = req.args[1] if len(req.args) > 1 else "未知路径"
            return f"{desc}\n目标路径: {dir_path}\n\n警告：此操作不可恢复！"
        elif req.method == "archive_agent_dir":
            dir_path = req.args[1] if len(req.args) > 1 else "未知路径"
            return f"{desc}\n目标路径: {dir_path}\n\n归档后可恢复。"
        elif req.method == "delete_archived_agent":
            archive_id = req.args[0] if req.args else "未知"
            return f"{desc}\n归档 ID: {archive_id}\n\n警告：此操作不可恢复！"
        elif req.method == "commit_selection":
            count = len(req.args[1]) if len(req.args) > 1 else 0
            return f"{desc}\n选中文件数: {count}"
        elif req.method in ("bind_agent", "unbind_agent"):
            args_str = " | ".join(str(a) for a in req.args)
            return f"{desc}\n参数: {args_str}"
        else:
            args_str = " | ".join(str(a) for a in req.args[:3]) if req.args else "无"
            return f"{desc}\n参数: {args_str}"

    def execute(self, req) -> dict:
        """执行单个请求。

        返回 {"ok": bool, "result": dict, "error": str}
        """
        if req.method in self.DISABLED_METHODS:
            return {
                "ok": False,
                "result": {"error": "method_disabled", "reason": "该操作已禁用，请打开文件夹手动处理。"},
                "error": "method_disabled",
            }

        from .gui import GovernanceApi
        api = GovernanceApi(str(self.workspace))

        fn = getattr(api, req.method, None)
        if not callable(fn):
            return {"ok": False, "result": {}, "error": f"method not implemented: {req.method}"}

        try:
            # 桌面端已获用户确认，注入 confirmed=True
            # fn 是绑定方法，inspect.signature(fn) 不包含 self
            import inspect
            sig = inspect.signature(fn)
            params = list(sig.parameters.keys())
            args = list(req.args)

            # 如果方法有 confirmed 参数，将其设为 True
            if "confirmed" in params:
                confirmed_idx = params.index("confirmed")  # 绑定方法不含 self，不需要偏移
                # 用默认值填充到 confirmed 位置
                while len(args) <= confirmed_idx:
                    param_name = params[len(args)]
                    args.append(sig.parameters[param_name].default)
                args[confirmed_idx] = True  # 桌面端确认后注入

            # The request reached this executor only after a user-confirmed
            # desktop flow.  `_admin_override` remains an internal keyword,
            # never something stored in untrusted request args.
            kwargs = {}
            if "_admin_override" in sig.parameters:
                kwargs["_admin_override"] = True
            result = fn(*args, **kwargs) if args else fn(**kwargs)
            result = result if result is not None else {}
            if isinstance(result, dict) and result.get("error"):
                return {"ok": False, "result": result, "error": result["error"]}
            return {"ok": True, "result": result, "error": ""}
        except Exception as e:
            return {"ok": False, "result": {}, "error": str(e)}

    def process_request(self, request_id: str, auto_confirm: bool = False) -> list[dict]:
        """只处理指定请求。"""
        req = self.queue.get(request_id)
        if not req:
            return [{"request_id": request_id, "status": "not_found", "error": "request not found"}]
        if req.status != "pending":
            return [{"request_id": request_id, "status": req.status}]
        return self._process_requests([req], auto_confirm=auto_confirm)

    def process_pending(self, auto_confirm: bool = False) -> list[dict]:
        """处理所有待执行请求。

        auto_confirm=False（默认）：显示原生确认窗口
        auto_confirm=True：跳过确认（仅用于测试或 CI）
        """
        pending = self.queue.list_pending()
        if not pending:
            return []
        return self._process_requests(pending, auto_confirm=auto_confirm)

    def _process_requests(self, pending: list, auto_confirm: bool = False) -> list[dict]:
        """处理请求列表。"""
        results = []

        for req in pending:
            self._log(f"process_start request={req.request_id} method={req.method} auto_confirm={auto_confirm}")
            if req.is_expired():
                self.queue.update(req.request_id, status="expired")
                self._log(f"expired request={req.request_id}")
                results.append({"request_id": req.request_id, "status": "expired"})
                continue

            if not auto_confirm:
                try:
                    approved = self._show_confirmation(req)
                except Exception as e:
                    error = f"confirmation_window_failed: {e}"
                    self.queue.update(req.request_id, status="failed", error=error)
                    self._log(f"confirmation_failed request={req.request_id} error={e!r}")
                    results.append({"request_id": req.request_id, "status": "failed", "error": error})
                    continue
                if not approved:
                    self.queue.update(req.request_id, status="rejected")
                    self._log(f"rejected request={req.request_id}")
                    results.append({"request_id": req.request_id, "status": "rejected"})
                    continue

            # 原子声明：防止两个执行器同时执行同一请求
            claimed = self.queue.claim(req.request_id, "desktop_executor")
            if claimed is None:
                # 已被其他执行器声明或已过期
                results.append({"request_id": req.request_id, "status": "skipped"})
                continue

            exec_result = self.execute(req)
            if exec_result["ok"]:
                self.queue.update(
                    req.request_id,
                    status="done",
                    result=exec_result["result"],
                    executed_by="desktop_executor",
                )
                results.append({
                    "request_id": req.request_id,
                    "status": "done",
                    "result": exec_result["result"],
                })
            else:
                # 分类失败原因
                # 从请求参数中提取目标路径
                target_path = ""
                if req.method in ("purge_agent_dir", "archive_agent_dir") and len(req.args) > 1:
                    target_path = req.args[1]
                failure_type = self._classify_failure(exec_result["error"], target_path)

                # 如果是资源占用，检测占用进程
                locking_processes = []
                if failure_type == "resource_locked" and target_path:
                    try:
                        from .file_lock_detector import detect_locking_processes
                        locking_processes = [
                            p.to_dict() for p in detect_locking_processes(target_path)
                        ]
                    except Exception:
                        pass

                self.queue.update(
                    req.request_id,
                    status="failed",
                    error=exec_result["error"],
                    result={
                        "failure_type": failure_type,
                        "locking_processes": locking_processes,
                    },
                )
                results.append({
                    "request_id": req.request_id,
                    "status": "failed",
                    "error": exec_result["error"],
                    "failure_type": failure_type,
                })

        return results

    def _show_confirmation(self, req) -> bool:
        """显示原生确认窗口，返回用户是否批准。"""
        desc = self.describe_request(req)
        is_high_risk = req.method in self.HIGH_RISK_METHODS
        title = "MemoryGuard - 操作确认" if not is_high_risk else "MemoryGuard - 高风险操作确认"
        message = f"MemoryGuard 收到一个操作请求：\n\n{desc}\n\n确定要执行此操作吗？"
        if is_high_risk:
            message = f"高风险操作\n\n{desc}\n\n确定要执行此操作吗？"

        self._log(f"confirmation_show request={req.request_id} method={req.method}")
        backends = []
        if sys.platform == "win32":
            backends.append(("windows_messagebox", lambda: self._show_windows_messagebox(title, message, is_high_risk)))
        elif _is_wsl():
            backends.append(("wsl_powershell", lambda: self._show_wsl_powershell_confirmation(title, message, is_high_risk)))
            backends.append(("linux_desktop", lambda: self._show_linux_desktop_confirmation(title, message, is_high_risk)))
        elif sys.platform == "darwin":
            backends.append(("macos_osascript", lambda: self._show_macos_confirmation(title, message)))
        else:
            backends.append(("linux_desktop", lambda: self._show_linux_desktop_confirmation(title, message, is_high_risk)))
        backends.append(("tkinter", lambda: self._show_tk_confirmation(req, title, message, is_high_risk)))

        errors = []
        for name, backend in backends:
            try:
                approved = backend()
                if approved:
                    self._log(f"confirmation_approved request={req.request_id} backend={name}")
                else:
                    self._log(f"confirmation_rejected request={req.request_id} backend={name}")
                return approved
            except Exception as e:
                errors.append(f"{name}: {e}")
                self._log(f"confirmation_backend_failed request={req.request_id} backend={name} error={e!r}")
        raise RuntimeError("; ".join(errors) if errors else "no confirmation backend available")

    def _show_windows_messagebox(self, title: str, message: str, is_high_risk: bool) -> bool:
        import ctypes

        MB_YESNO = 0x00000004
        MB_ICONQUESTION = 0x00000020
        MB_ICONWARNING = 0x00000030
        MB_TOPMOST = 0x00040000
        MB_SETFOREGROUND = 0x00010000
        IDYES = 6
        icon = MB_ICONWARNING if is_high_risk else MB_ICONQUESTION
        flags = MB_YESNO | icon | MB_TOPMOST | MB_SETFOREGROUND
        result = ctypes.windll.user32.MessageBoxW(None, message, title, flags)
        return result == IDYES

    def _show_wsl_powershell_confirmation(self, title: str, message: str, is_high_risk: bool) -> bool:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            raise RuntimeError("powershell.exe not found")
        icon = "Warning" if is_high_risk else "Question"
        script = (
            "Add-Type -AssemblyName PresentationFramework; "
            "$r=[System.Windows.MessageBox]::Show($args[1], $args[0], 'YesNo', $args[2]); "
            "if ($r -eq 'Yes') { exit 0 } else { exit 1 }"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script, title, message, icon],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode in (0, 1):
            return completed.returncode == 0
        raise RuntimeError((completed.stderr or "powershell messagebox failed").strip())

    def _show_linux_desktop_confirmation(self, title: str, message: str, is_high_risk: bool) -> bool:
        if shutil.which("zenity"):
            kind = "warning" if is_high_risk else "question"
            completed = subprocess.run(["zenity", f"--{kind}", "--title", title, "--text", message, "--ok-label", "确认执行", "--cancel-label", "取消"])
            if completed.returncode in (0, 1):
                return completed.returncode == 0
            raise RuntimeError(f"zenity exited with {completed.returncode}")
        if shutil.which("kdialog"):
            completed = subprocess.run(["kdialog", "--title", title, "--yesno", message])
            if completed.returncode in (0, 1):
                return completed.returncode == 0
            raise RuntimeError(f"kdialog exited with {completed.returncode}")
        if shutil.which("yad"):
            completed = subprocess.run(["yad", "--center", "--on-top", "--title", title, "--text", message, "--button=确认执行:0", "--button=取消:1"])
            if completed.returncode in (0, 1):
                return completed.returncode == 0
            raise RuntimeError(f"yad exited with {completed.returncode}")
        raise RuntimeError("zenity/kdialog/yad not found")

    def _show_macos_confirmation(self, title: str, message: str) -> bool:
        if not shutil.which("osascript"):
            raise RuntimeError("osascript not found")
        script = "display dialog argv's item 2 with title argv's item 1 buttons {\"取消\", \"确认执行\"} default button \"确认执行\" cancel button \"取消\""
        completed = subprocess.run(["osascript", "-e", script, title, message], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if completed.returncode in (0, 1):
            return completed.returncode == 0
        raise RuntimeError((completed.stderr or "osascript dialog failed").strip())

    def _show_tk_confirmation(self, req, title: str, message: str, is_high_risk: bool) -> bool:
        import tkinter as tk
        from tkinter import scrolledtext

        root = tk.Tk()
        root.title(title)
        root.geometry("560x360")
        root.resizable(True, True)
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()

        try:
            root.eval("tk::PlaceWindow . center")
        except Exception:
            pass

        result = {"approved": False}
        header_text = "高风险操作确认" if is_high_risk else "操作确认"
        header = tk.Label(root, text=header_text, font=("Microsoft YaHei UI", 14, "bold"), anchor="w")
        header.pack(fill="x", padx=18, pady=(16, 8))

        text = scrolledtext.ScrolledText(root, wrap="word", height=12)
        text.insert("1.0", message)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        button_frame = tk.Frame(root)
        button_frame.pack(fill="x", padx=18, pady=(0, 16))

        def approve() -> None:
            result["approved"] = True
            self._log(f"confirmation_approved request={req.request_id}")
            root.destroy()

        def reject() -> None:
            result["approved"] = False
            self._log(f"confirmation_rejected request={req.request_id}")
            root.destroy()

        reject_button = tk.Button(button_frame, text="取消", width=12, command=reject)
        reject_button.pack(side="right", padx=(8, 0))
        approve_button = tk.Button(button_frame, text="确认执行", width=12, command=approve)
        approve_button.pack(side="right")

        root.protocol("WM_DELETE_WINDOW", reject)
        root.after(300, lambda: root.attributes("-topmost", False))
        root.mainloop()
        return result["approved"]

    @staticmethod
    def _classify_failure(error: str, path: str = "") -> str:
        """分类失败原因（使用增强版检测）。"""
        from .file_lock_detector import classify_operation_failure
        return classify_operation_failure(error, path)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _find_workspace_for_request(request_id: str) -> Path | None:
    """搜索所有可能的工作区，找到包含指定请求的 request-queue.json。

    搜索顺序：
    1. 环境变量 MEMORYGUARD_WORKSPACE
    2. 当前工作目录及其父目录（向上找最多3层）
    3. 用户主目录
    4. 常见项目路径下递归搜索两层
    """
    from .security import RequestQueue

    candidates: list[Path] = []

    # 环境变量
    env_ws = os.environ.get("MEMORYGUARD_WORKSPACE", "")
    if env_ws:
        candidates.append(Path(env_ws).resolve())

    # 当前目录及向上3层
    cwd = Path(".").resolve()
    candidates.append(cwd)
    for _ in range(3):
        cwd = cwd.parent
        candidates.append(cwd)

    # 用户主目录
    candidates.append(Path.home())

    # 常见项目路径：递归搜索两层
    search_roots = [
        Path.home() / "workspace", Path.home() / "projects",
        Path("H:/ai/workspace"), Path("C:/workspace"),
        Path("D:/workspace"), Path("D:/ai/workspace"),
    ]
    for base in search_roots:
        if not base.exists():
            continue
        try:
            for sub in base.iterdir():
                if not sub.is_dir():
                    continue
                # 第一层
                if (sub / ".memoryguard" / "request-queue.json").exists():
                    candidates.append(sub)
                # 第二层
                try:
                    for sub2 in sub.iterdir():
                        if sub2.is_dir() and (sub2 / ".memoryguard" / "request-queue.json").exists():
                            candidates.append(sub2)
                except (OSError, PermissionError):
                    pass
        except (OSError, PermissionError):
            pass

    # 去重
    seen = set()
    unique_candidates = []
    for c in candidates:
        key = str(c)
        if key not in seen:
            seen.add(key)
            unique_candidates.append(c)

    for ws in unique_candidates:
        try:
            rq = RequestQueue(ws)
            if rq.get(request_id):
                return ws
        except Exception:
            continue
    return None


def handle_uri(uri: str) -> int:
    """处理 memoryguard:// URI 唤醒。

    URI 格式：memoryguard://request/<request_id>
    解析后搜索请求所在的工作区并执行。
    使用 tkinter 显示执行结果窗口，不会闪退。
    """
    from .ipc import parse_uri
    request_id = parse_uri(uri)
    if not request_id:
        _show_result_window("MemoryGuard 错误", f"无效的 URI: {uri}")
        return 1

    ws = _find_workspace_for_request(request_id)
    if not ws:
        _show_result_window(
            "MemoryGuard 错误",
            f"未找到请求: {request_id}\n\n"
            "可能原因：\n"
            "1. 请求已过期\n"
            "2. 工作区路径不在搜索范围内\n"
            f"当前搜索路径包括当前目录和 {Path.home()}"
        )
        return 1

    executor = RequestExecutor(ws)
    results = executor.process_request(request_id)

    _show_results_window(results)
    return 0


def _show_results_window(results: list[dict]) -> None:
    """显示请求处理结果窗口。"""
    lines = []
    for r in results:
        status_text = r.get("status", "unknown")
        request_id = r.get("request_id", "")
        if status_text == "done":
            lines.append(f"[成功] {request_id[:16]}...")
        elif status_text == "failed":
            err = r.get("error", "未知错误")
            lines.append(f"[失败] {request_id[:16]}...: {err}")
        elif status_text == "rejected":
            lines.append(f"[已拒绝] {request_id[:16]}...")
        elif status_text == "expired":
            lines.append(f"[已过期] {request_id[:16]}...")
        elif status_text == "skipped":
            lines.append(f"[已跳过] {request_id[:16]}... (已被其他执行器处理)")
        elif status_text == "not_found":
            lines.append(f"[未找到] {request_id[:16]}...")
        else:
            lines.append(f"[{status_text}] {request_id[:16]}...")
    summary = "\n".join(lines) if lines else "没有待处理的请求"
    _show_result_window("MemoryGuard 执行完成", summary)


def _show_result_window(title: str, message: str) -> None:
    """显示结果窗口。"""
    for backend in _result_window_backends(title, message):
        try:
            backend()
            return
        except Exception:
            continue
    print(f"\n{'='*40}")
    print(f"{title}")
    print(f"{'='*40}")
    print(message)
    print(f"{'='*40}")
    try:
        input("\n按回车键关闭...")
    except (EOFError, KeyboardInterrupt):
        pass


def _result_window_backends(title: str, message: str):
    if sys.platform == "win32":
        yield lambda: _show_windows_info(title, message)
    elif _is_wsl():
        yield lambda: _show_wsl_powershell_info(title, message)
        yield lambda: _show_linux_desktop_info(title, message)
    elif sys.platform == "darwin":
        yield lambda: _show_macos_info(title, message)
    else:
        yield lambda: _show_linux_desktop_info(title, message)
    yield lambda: _show_tk_info(title, message)


def _show_windows_info(title: str, message: str) -> None:
    import ctypes
    MB_OK = 0x00000000
    MB_ICONINFORMATION = 0x00000040
    MB_TOPMOST = 0x00040000
    MB_SETFOREGROUND = 0x00010000
    ctypes.windll.user32.MessageBoxW(None, message, title, MB_OK | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND)


def _show_wsl_powershell_info(title: str, message: str) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        raise RuntimeError("powershell.exe not found")
    script = "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show($args[1], $args[0], 'OK', 'Information') | Out-Null"
    completed = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script, title, message])
    if completed.returncode != 0:
        raise RuntimeError(f"powershell exited with {completed.returncode}")


def _show_linux_desktop_info(title: str, message: str) -> None:
    if shutil.which("zenity"):
        completed = subprocess.run(["zenity", "--info", "--title", title, "--text", message])
        if completed.returncode == 0:
            return
        raise RuntimeError(f"zenity exited with {completed.returncode}")
    if shutil.which("kdialog"):
        completed = subprocess.run(["kdialog", "--title", title, "--msgbox", message])
        if completed.returncode == 0:
            return
        raise RuntimeError(f"kdialog exited with {completed.returncode}")
    if shutil.which("yad"):
        completed = subprocess.run(["yad", "--center", "--on-top", "--title", title, "--text", message, "--button=确定:0"])
        if completed.returncode == 0:
            return
        raise RuntimeError(f"yad exited with {completed.returncode}")
    raise RuntimeError("zenity/kdialog/yad not found")


def _show_macos_info(title: str, message: str) -> None:
    if not shutil.which("osascript"):
        raise RuntimeError("osascript not found")
    script = "display dialog argv's item 2 with title argv's item 1 buttons {\"确定\"} default button \"确定\""
    completed = subprocess.run(["osascript", "-e", script, title, message])
    if completed.returncode != 0:
        raise RuntimeError(f"osascript exited with {completed.returncode}")


def _show_tk_info(title: str, message: str) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    messagebox.showinfo(title, message, parent=root)
    root.destroy()


def main(argv: list[str] | None = None) -> int:
    """桌面执行器 CLI 入口。

    用法：
        python -m memoryguard.desktop_executor [workspace] [--auto-confirm] [--watch]
        python -m memoryguard.desktop_executor --request <id> [workspace]
        python -m memoryguard.desktop_executor --uri memoryguard://request/<id>

    --auto-confirm: 跳过确认（仅用于测试）
    --watch: 持续监听新请求（命名管道 + 文件轮询 fallback）
    --request <id>: 只处理指定 ID 的请求
    --uri <uri>: 从 memoryguard:// URI 启动
    """
    import argparse

    parser = argparse.ArgumentParser(description="MemoryGuard Desktop Executor")
    parser.add_argument("workspace", nargs="?", default=".", help="工作区路径")
    parser.add_argument("--auto-confirm", action="store_true", help="跳过确认窗口（仅用于测试）")
    parser.add_argument("--watch", action="store_true", help="持续监听新请求")
    parser.add_argument("--request", default="", help="只处理指定 ID 的请求")
    parser.add_argument("--uri", default="", help="从 memoryguard:// URI 启动")
    parser.add_argument("--register-uri", action="store_true", help="注册 memoryguard:// URI 协议")
    args = parser.parse_args(argv)

    # URI 协议注册
    if args.register_uri:
        from .ipc import register_uri_protocol, is_uri_protocol_registered
        if register_uri_protocol():
            print("URI protocol registered: memoryguard://")
        else:
            print("Failed to register URI protocol (Windows only)")
        return 0

    # URI 唤醒
    if args.uri:
        return handle_uri(args.uri)

    workspace = Path(args.workspace).resolve()
    executor = RequestExecutor(workspace)

    if args.request:
        # 处理单个请求
        results = executor.process_request(args.request, auto_confirm=args.auto_confirm)
        for r in results:
            print(f"  {r['request_id']}: {r['status']}")
        # 从 GUI 启动时给用户明确结果窗口
        if not args.auto_confirm:
            _show_results_window(results)
        return 0

    if args.watch:
        # 持续监听模式：命名管道 + 文件轮询
        from .ipc import create_named_pipe, read_pipe_message, check_notify_file
        print(f"MemoryGuard Desktop Executor watching {workspace}")

        # 尝试创建命名管道
        pipe_handle = create_named_pipe(workspace)
        use_pipe = pipe_handle is not None
        if use_pipe:
            print("Using named pipe for real-time notification.")
        else:
            print("Named pipe unavailable, using file polling (3s interval).")
        print("Press Ctrl+C to stop.")

        try:
            while True:
                # 命名管道模式：阻塞等待消息
                if use_pipe:
                    msg = read_pipe_message(pipe_handle, timeout_ms=3000)
                    if msg and msg.get("request_id"):
                        print(f"  Received notification: {msg['request_id'][:16]}...")

                # 文件轮询 fallback
                notify_id = check_notify_file(workspace)
                if notify_id:
                    print(f"  Received file notification: {notify_id[:16]}...")

                # 处理所有待执行请求
                results = executor.process_pending(auto_confirm=args.auto_confirm)
                for r in results:
                    print(f"  {r['request_id']}: {r['status']}")

                if not use_pipe:
                    time.sleep(3)
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    # 单次处理模式
    pending = executor.queue.list_pending()
    if not pending:
        print("No pending requests.")
        return 0

    print(f"Found {len(pending)} pending request(s).")
    results = executor.process_pending(auto_confirm=args.auto_confirm)
    for r in results:
        status = r["status"]
        detail = r.get("error", "") or r.get("result", "")
        print(f"  {r['request_id']}: {status} {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
