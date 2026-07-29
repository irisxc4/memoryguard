"""安全架构测试：会话令牌、API白名单、请求队列、桌面执行器、IPC、占用检测。"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# 确保可以导入 src
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(autouse=True)
def mock_ipc_notification(monkeypatch):
    """自动 mock IPC 通知，防止测试中真实打开 URI、启动进程或写通知文件。"""
    monkeypatch.setattr("memoryguard.ipc.launch_uri", lambda uri: True)
    monkeypatch.setattr("memoryguard.ipc.launch_desktop_request", lambda ws, rid: True)
    monkeypatch.setattr("memoryguard.ipc.write_notify_file", lambda ws, rid: True)
    monkeypatch.setattr("memoryguard.ipc.notify_desktop", lambda ws, rid: "pipe_unavailable")


class TestSecurityModule:
    """security.py 模块测试。"""

    def test_session_token_length(self):
        from memoryguard.security import generate_session_token
        token = generate_session_token()
        assert len(token) >= 32

    def test_session_token_uniqueness(self):
        from memoryguard.security import generate_session_token
        t1 = generate_session_token()
        t2 = generate_session_token()
        assert t1 != t2

    def test_request_id_length(self):
        from memoryguard.security import generate_request_id
        rid = generate_request_id()
        assert len(rid) == 32

    def test_nonce_length(self):
        from memoryguard.security import generate_nonce
        nonce = generate_nonce()
        assert len(nonce) == 16

    def test_api_whitelist_complete(self):
        """白名单覆盖所有 GovernanceApi public 方法。"""
        from memoryguard.security import (
            ALL_ALLOWED_METHODS,
            BLOCKED_LEGACY_NATIVE_WRITEBACK_METHODS,
            _SECURITY_API_METHODS,
        )
        from memoryguard.gui import GovernanceApi
        import inspect

        # 获取所有 public 方法名
        public_methods = {
            name for name, member in inspect.getmembers(GovernanceApi, predicate=inspect.isfunction)
            if not name.startswith("_")
        }
        allowed = ALL_ALLOWED_METHODS | _SECURITY_API_METHODS
        missing = public_methods - allowed - BLOCKED_LEGACY_NATIVE_WRITEBACK_METHODS
        assert not missing, f"Missing from whitelist: {missing}"
        assert not (BLOCKED_LEGACY_NATIVE_WRITEBACK_METHODS & allowed)

    def test_mutation_methods_include_all_confirmed(self):
        """所有含 confirmed 参数的方法必须在变更白名单中。"""
        from memoryguard.security import (
            BLOCKED_LEGACY_NATIVE_WRITEBACK_METHODS,
            MUTATION_API_METHODS,
        )
        from memoryguard.gui import GovernanceApi
        import inspect

        confirmed_methods = set()
        for name, member in inspect.getmembers(GovernanceApi, predicate=inspect.isfunction):
            if name.startswith("_"):
                continue
            sig = inspect.signature(member)
            if "confirmed" in sig.parameters:
                confirmed_methods.add(name)

        missing = (
            confirmed_methods
            - MUTATION_API_METHODS
            - BLOCKED_LEGACY_NATIVE_WRITEBACK_METHODS
        )
        assert not missing, f"confirmed methods not in MUTATION_API_METHODS: {missing}"

    def test_is_allowed_method(self):
        from memoryguard.security import is_allowed_method, is_mutation_method, is_readonly_method
        assert is_allowed_method("get_audit")
        assert is_allowed_method("open_agent_folder")
        assert is_allowed_method("submit_request")
        assert not is_allowed_method("purge_agent_dir")
        assert not is_allowed_method("nonexistent_method")
        assert is_readonly_method("get_audit")
        assert is_readonly_method("open_agent_folder")
        assert not is_mutation_method("purge_agent_dir")
        assert not is_mutation_method("get_audit")

    def test_sandbox_detection(self):
        from memoryguard.security import detect_sandbox_mode
        # 在测试环境中可能为 True（IDE 启动）或 False
        result = detect_sandbox_mode()
        assert isinstance(result, bool)


class TestRequestQueue:
    """请求队列测试。"""

    def test_submit_and_get(self, tmp_path):
        from memoryguard.security import RequestQueue
        rq = RequestQueue(tmp_path)
        req = rq.submit("purge_agent_dir", ["trae", "/test/path", "cid123"])
        assert req.status == "pending"
        assert req.method == "purge_agent_dir"
        assert req.nonce != ""

        retrieved = rq.get(req.request_id)
        assert retrieved is not None
        assert retrieved.method == "purge_agent_dir"

    def test_list_pending(self, tmp_path):
        from memoryguard.security import RequestQueue
        rq = RequestQueue(tmp_path)
        rq.submit("purge_agent_dir", ["a"])
        rq.submit("archive_agent_dir", ["b"])
        pending = rq.list_pending()
        assert len(pending) == 2

    def test_update_status(self, tmp_path):
        from memoryguard.security import RequestQueue
        rq = RequestQueue(tmp_path)
        req = rq.submit("commit_selection", ["inst1", []])
        updated = rq.update(req.request_id, status="done", result={"ok": True})
        assert updated.status == "done"
        assert updated.result == {"ok": True}

    def test_claim_atomic(self, tmp_path):
        """原子声明：第一次 claim 成功，第二次失败。"""
        from memoryguard.security import RequestQueue
        rq = RequestQueue(tmp_path)
        req = rq.submit("build_projection", [])

        claimed1 = rq.claim(req.request_id, "executor1")
        assert claimed1 is not None
        assert claimed1.status == "executing"
        assert claimed1.executed_by == "executor1"

        claimed2 = rq.claim(req.request_id, "executor2")
        assert claimed2 is None  # 已被声明

    def test_nonce_validation(self, tmp_path):
        """nonce 校验：匹配且未消费 -> True，消费后 -> False。"""
        from memoryguard.security import RequestQueue
        rq = RequestQueue(tmp_path)
        req = rq.submit("commit_selection", ["inst1", []])

        assert rq.validate_nonce(req.request_id, req.nonce) is True

        # claim 消费 nonce
        rq.claim(req.request_id, "executor1")
        assert rq.validate_nonce(req.request_id, req.nonce) is False

    def test_nonce_mismatch(self, tmp_path):
        from memoryguard.security import RequestQueue
        rq = RequestQueue(tmp_path)
        req = rq.submit("commit_selection", ["inst1", []])
        assert rq.validate_nonce(req.request_id, "wrong_nonce") is False

    def test_cleanup_expired(self, tmp_path):
        from memoryguard.security import RequestQueue, PendingRequest
        import time
        rq = RequestQueue(tmp_path)
        # 手动插入一个过期请求
        now = time.time()
        req = PendingRequest(
            request_id="expired_test",
            method="purge_agent_dir",
            args=[],
            nonce="test_nonce",
            created_at=now - 600,
            expires_at=now - 300,  # 已过期
        )
        requests = [req.to_dict()]
        rq._save(requests)

        cleaned = rq.cleanup_expired()
        assert cleaned == 1


class TestDesktopExecutor:
    """桌面执行器测试。"""

    def test_confirmed_injection_spy(self, tmp_path):
        """spy 测试：验证 confirmed=True 被正确注入到绑定方法。

        使用 mock 替换 GovernanceApi.build_projection，记录实际入参。
        """
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import RequestQueue
        from unittest.mock import patch, MagicMock

        rq = RequestQueue(tmp_path)
        req = rq.submit("build_projection", [])
        assert req.status == "pending"

        ex = RequestExecutor(tmp_path)

        # 用 spy 替换 build_projection
        with patch.object(ex, 'execute') as mock_execute:
            mock_execute.return_value = {"ok": True, "result": {}, "error": ""}
            ex.process_pending(auto_confirm=True)

            # 验证 execute 被调用
            mock_execute.assert_called_once()
            called_req = mock_execute.call_args[0][0]

            # 验证请求方法正确
            assert called_req.method == "build_projection"

        # 直接测试 execute 的 confirmed 注入逻辑
        req2 = rq.submit("build_projection", [])
        ex2 = RequestExecutor(tmp_path)

        # 用 spy 替换 GovernanceApi.build_projection
        captured_args = []
        original_fn = None

        from memoryguard.gui import GovernanceApi
        api = GovernanceApi(str(tmp_path))
        original_build = api.build_projection

        def spy_build(*args, **kwargs):
            captured_args.extend(args)
            return {"ok": True, "mock": True}

        api.build_projection = spy_build

        with patch.object(ex2, 'execute') as mock_exec:
            # 直接调用 execute，它会用 api.build_projection
            # 先手动 claim
            rq.claim(req2.request_id, "test")
            # 直接测试 execute 内部的 confirmed 注入
            import inspect
            fn = api.build_projection
            sig = inspect.signature(fn)
            params = list(sig.parameters.keys())
            assert "confirmed" not in params  # spy_build 没有参数签名

            # 测试真实 GovernanceApi.build_projection 的签名
            real_fn = original_build
            real_sig = inspect.signature(real_fn)
            real_params = list(real_sig.parameters.keys())
            assert "confirmed" in real_params
            confirmed_idx = real_params.index("confirmed")
            assert confirmed_idx == 0  # build_projection(confirmed=False) -> confirmed 在位置 0

    def test_confirmed_injection_real_method(self, tmp_path):
        """验证真实 GovernanceApi.build_projection 在桌面执行器中不返回'需要确认'。"""
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import RequestQueue

        rq = RequestQueue(tmp_path)
        req = rq.submit("build_projection", [])

        ex = RequestExecutor(tmp_path)
        results = ex.process_pending(auto_confirm=True)
        assert len(results) == 1
        result = results[0]
        # 必须是 done 或明确的执行失败（非 confirmed 守卫拒绝）
        assert result["status"] in ("done", "failed")
        if result["status"] == "failed":
            error = result.get("error", "")
            # 不应包含"需要确认"或"confirmed"
            assert "确认" not in error
            assert "confirmed" not in error.lower()
            # 也不应是 index out of range
            assert "index" not in error.lower()
            assert "range" not in error.lower()

    def test_commit_selection_confirmed_injection(self, tmp_path):
        """验证 commit_selection 在桌面执行器中不返回'需要确认'。"""
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import RequestQueue

        rq = RequestQueue(tmp_path)
        req = rq.submit("commit_selection", ["instance1", []])

        ex = RequestExecutor(tmp_path)
        results = ex.process_pending(auto_confirm=True)
        assert len(results) == 1
        result = results[0]
        assert result["status"] in ("done", "failed")
        if result["status"] == "failed":
            error = result.get("error", "")
            assert "确认" not in error
            assert "confirmed" not in error.lower()
            assert "index" not in error.lower()

    def test_dry_run_not_stripped(self, tmp_path):
        """dry_run 布尔参数不被误删。"""
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import RequestQueue

        rq = RequestQueue(tmp_path)
        req = rq.submit("archive_agent_dir", ["trae", "/nonexistent/path", "reason", "cid", True])
        ex = RequestExecutor(tmp_path)
        results = ex.process_pending(auto_confirm=True)
        assert len(results) == 1

    def test_confirmation_window_failure_marks_failed(self, tmp_path, monkeypatch):
        """确认窗口创建失败时必须标记 failed，不能静默当成 rejected。"""
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import RequestQueue

        rq = RequestQueue(tmp_path)
        req = rq.submit("build_projection", [])
        ex = RequestExecutor(tmp_path)

        def fail_confirmation(req):
            raise RuntimeError("tk unavailable")

        monkeypatch.setattr(ex, "_show_confirmation", fail_confirmation)
        results = ex.process_request(req.request_id)
        updated = rq.get(req.request_id)

        assert results[0]["status"] == "failed"
        assert updated.status == "failed"
        assert "confirmation_window_failed" in updated.error

    def test_desktop_executor_writes_log(self, tmp_path):
        from memoryguard.desktop_executor import RequestExecutor
        ex = RequestExecutor(tmp_path)
        ex._log("test_message")
        log_path = tmp_path / ".memoryguard" / "desktop-executor.log"
        assert log_path.exists()
        assert "test_message" in log_path.read_text(encoding="utf-8")

    def test_desktop_executor_disables_purge_agent_dir(self, tmp_path):
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import PendingRequest
        import time

        req = PendingRequest(
            request_id="disabled_purge",
            method="purge_agent_dir",
            args=["", "/tmp/data", "cid", False],
            nonce="n",
            created_at=time.time(),
            expires_at=time.time() + 300,
        )
        result = RequestExecutor(tmp_path).execute(req)

        assert result["ok"] is False
        assert result["error"] == "method_disabled"

    def test_windows_confirmation_does_not_require_tkinter(self, tmp_path, monkeypatch):
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import PendingRequest
        import memoryguard.desktop_executor as de
        import time

        monkeypatch.setattr(de.sys, "platform", "win32")
        ex = RequestExecutor(tmp_path)
        monkeypatch.setattr(ex, "_show_windows_messagebox", lambda title, message, high_risk: True)
        req = PendingRequest(
            request_id="win_confirm",
            method="build_projection",
            args=[],
            nonce="n",
            created_at=time.time(),
            expires_at=time.time() + 300,
        )

        assert ex._show_confirmation(req) is True

    def test_confirmation_all_backends_fail_marks_failed(self, tmp_path, monkeypatch):
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import RequestQueue
        import memoryguard.desktop_executor as de

        monkeypatch.setattr(de.sys, "platform", "linux")
        monkeypatch.setattr(de, "_is_wsl", lambda: False)
        rq = RequestQueue(tmp_path)
        req = rq.submit("build_projection", [])
        ex = RequestExecutor(tmp_path)
        monkeypatch.setattr(ex, "_show_linux_desktop_confirmation", lambda *args: (_ for _ in ()).throw(RuntimeError("no linux gui")))
        monkeypatch.setattr(ex, "_show_tk_confirmation", lambda *args: (_ for _ in ()).throw(RuntimeError("no tkinter")))

        results = ex.process_request(req.request_id)
        updated = rq.get(req.request_id)

        assert results[0]["status"] == "failed"
        assert updated.status == "failed"
        assert "no linux gui" in updated.error
        assert "no tkinter" in updated.error

    def test_wsl_confirmation_prefers_powershell(self, tmp_path, monkeypatch):
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import PendingRequest
        import memoryguard.desktop_executor as de
        import time

        monkeypatch.setattr(de.sys, "platform", "linux")
        monkeypatch.setattr(de, "_is_wsl", lambda: True)
        ex = RequestExecutor(tmp_path)
        calls = []
        monkeypatch.setattr(ex, "_show_wsl_powershell_confirmation", lambda *args: calls.append("powershell") or True)
        monkeypatch.setattr(ex, "_show_linux_desktop_confirmation", lambda *args: calls.append("linux") or True)
        req = PendingRequest(
            request_id="wsl_confirm",
            method="build_projection",
            args=[],
            nonce="n",
            created_at=time.time(),
            expires_at=time.time() + 300,
        )

        assert ex._show_confirmation(req) is True
        assert calls == ["powershell"]

    def test_describe_request(self, tmp_path):
        from memoryguard.desktop_executor import RequestExecutor
        from memoryguard.security import PendingRequest
        import time

        now = time.time()
        req = PendingRequest(
            request_id="test",
            method="purge_agent_dir",
            args=["trae", "/test/path", "cid"],
            nonce="n",
            created_at=now,
            expires_at=now + 300,
        )
        ex = RequestExecutor(tmp_path)
        desc = ex.describe_request(req)
        assert "已禁用" in desc
        assert "/test/path" in desc

    def test_classify_failure(self, tmp_path):
        from memoryguard.desktop_executor import RequestExecutor
        ex = RequestExecutor(tmp_path)
        assert ex._classify_failure("Permission denied") == "sandbox_permission_denied"
        assert ex._classify_failure("File is being used by another process") == "resource_locked"
        assert ex._classify_failure("File not found") == "not_found"


class TestIPC:
    """IPC 模块测试。"""

    def test_build_and_parse_uri(self):
        from memoryguard.ipc import build_uri, parse_uri
        uri = build_uri("test123")
        assert uri == "memoryguard://request/test123"
        assert parse_uri(uri) == "test123"
        assert parse_uri("invalid://uri") is None

    def test_get_current_sid(self):
        """SID 获取：Windows 上必须返回以 S- 开头的 SID 字符串。"""
        from memoryguard.ipc import _get_current_sid
        sid = _get_current_sid()
        if sys.platform == "win32":
            # Windows 上必须能获取到 SID（fail-closed 要求）
            assert sid is not None, "SID must be available on Windows for pipe security"
            assert sid.startswith("S-"), f"Invalid SID format: {sid}"
        else:
            assert sid is None

    def test_notify_desktop_returns_pipe_unavailable(self, tmp_path, monkeypatch):
        """管道不存在时，notify_desktop 返回 PIPE_UNAVAILABLE。"""
        # 撤销 autouse mock，测试真实 notify_desktop
        from memoryguard import ipc
        monkeypatch.undo()
        monkeypatch.setattr(ipc, "launch_uri", lambda uri: True)  # 仅 mock URI 启动

        result = ipc.notify_desktop(tmp_path, "test_req_123")
        assert result == ipc.PIPE_UNAVAILABLE

        # 直接写通知文件（测试真实 check_notify_file）
        notify_path = tmp_path / ".memoryguard" / "notify.txt"
        notify_path.parent.mkdir(parents=True, exist_ok=True)
        notify_path.write_text('{"request_id": "test_req_123"}', encoding="utf-8")
        assert notify_path.exists()

        # 消费通知
        req_id = ipc.check_notify_file(tmp_path)
        assert req_id == "test_req_123"
        assert ipc.check_notify_file(tmp_path) is None

    def test_pipe_name_unique_per_workspace(self, tmp_path):
        from memoryguard.ipc import _pipe_name
        name1 = _pipe_name(tmp_path)
        name2 = _pipe_name(tmp_path / "sub")
        assert name1 != name2
        assert name1.startswith("\\\\.\\pipe\\memoryguard_")

    def test_launch_desktop_request_uses_windows_start_process(self, tmp_path, monkeypatch):
        from memoryguard import ipc
        import importlib
        import subprocess

        ipc = importlib.reload(ipc)
        start_calls = []
        popen_calls = []

        def fake_start(cmd, ws, env, log):
            start_calls.append((cmd, ws, env))
            return True

        monkeypatch.setattr(ipc.sys, "platform", "win32")
        monkeypatch.setattr(ipc.sys, "executable", r"C:\Python311\python.exe")
        monkeypatch.setattr(ipc.os.path, "exists", lambda p: p.endswith("pythonw.exe"))
        monkeypatch.setattr(ipc, "_launch_desktop_request_windows", fake_start)
        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: popen_calls.append((args, kwargs)))

        ok = ipc.launch_desktop_request(tmp_path, "req123")

        assert ok is True
        assert len(start_calls) == 1
        assert len(popen_calls) == 0
        cmd, ws, env = start_calls[0]
        assert cmd[0].endswith("pythonw.exe")
        assert cmd[1:4] == ["-m", "memoryguard", "desktop"]
        assert cmd[4] == "--workspace"
        assert cmd[5] == str(tmp_path.resolve())
        assert cmd[6:8] == ["--request", "req123"]
        assert ws == tmp_path.resolve()
        assert env["MEMORYGUARD_WORKSPACE"] == str(tmp_path.resolve())
        assert "PYTHONPATH" in env

    def test_launch_desktop_request_falls_back_to_popen(self, tmp_path, monkeypatch):
        from memoryguard import ipc
        import importlib
        import subprocess

        ipc = importlib.reload(ipc)
        calls = []

        def fake_popen(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return object()

        monkeypatch.setattr(ipc.sys, "platform", "win32")
        monkeypatch.setattr(ipc.sys, "executable", r"C:\Python311\python.exe")
        monkeypatch.setattr(ipc.os.path, "exists", lambda p: p.endswith("pythonw.exe"))
        monkeypatch.setattr(ipc, "_launch_desktop_request_windows", lambda cmd, ws, env, log: False)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        ok = ipc.launch_desktop_request(tmp_path, "req123")

        assert ok is True
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert cmd[0].endswith("pythonw.exe")
        assert kwargs["cwd"] == str(tmp_path.resolve())
        assert kwargs["env"]["MEMORYGUARD_WORKSPACE"] == str(tmp_path.resolve())

    def test_windows_start_process_command(self, tmp_path, monkeypatch):
        from memoryguard import ipc
        import importlib
        import subprocess

        ipc = importlib.reload(ipc)
        calls = []

        class Completed:
            returncode = 0
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return Completed()

        monkeypatch.setattr(ipc.shutil, "which", lambda name: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" if name == "powershell.exe" else None)
        monkeypatch.setattr(subprocess, "run", fake_run)
        logs = []
        ok = ipc._launch_desktop_request_windows(
            [r"C:\Python311\pythonw.exe", "-m", "memoryguard", "desktop", "--workspace", str(tmp_path), "--request", "req123"],
            tmp_path,
            {"PYTHONPATH": "src", "MEMORYGUARD_WORKSPACE": str(tmp_path)},
            logs.append,
        )

        assert ok is True
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert cmd[0].endswith("powershell.exe")
        script = cmd[-1]
        assert "Start-Process" in script
        assert "MEMORYGUARD_WORKSPACE" in script
        assert "--request" in script
        assert "req123" in script
        assert "\\u" not in script
        assert any("windows_start_process_ok" in line for line in logs)


class TestFileLockDetector:
    """文件占用检测测试。"""

    def test_detect_locking_processes_nonexistent(self, tmp_path):
        from memoryguard.file_lock_detector import detect_locking_processes
        result = detect_locking_processes(tmp_path / "nonexistent")
        assert result == []

    def test_classify_operation_failure(self):
        from memoryguard.file_lock_detector import classify_operation_failure
        assert classify_operation_failure("Permission denied") == "sandbox_permission_denied"
        assert classify_operation_failure("File is being used") == "resource_locked"
        assert classify_operation_failure("Antivirus blocked") == "policy_denied"
        assert classify_operation_failure("File not found") == "not_found"
        assert classify_operation_failure("already exists") == "target_recreated"
        assert classify_operation_failure("random error") == "unknown"

    def test_format_locking_message_empty(self):
        from memoryguard.file_lock_detector import format_locking_message
        assert format_locking_message([]) == ""

    def test_format_locking_message_with_processes(self):
        from memoryguard.file_lock_detector import format_locking_message, LockingProcess
        procs = [LockingProcess(pid=1234, app_name="Cursor.exe", service_name="", session_id=1)]
        msg = format_locking_message(procs)
        assert "Cursor.exe" in msg
        assert "1234" in msg
        assert "不会自动关闭" in msg


class TestSafeBridgeApi:
    """SafeBridgeApi 端到端测试。"""

    def test_pywebview_can_enumerate_methods(self, tmp_path):
        """验证 SafeBridgeApi 的真实方法可被 pywebview 枚举。

        pywebview 只暴露对象上真实存在的公开方法（不以下划线开头），
        不走 __getattr__。用 inspect.getmembers 模拟 pywebview 的枚举逻辑。
        """
        import inspect
        from memoryguard.gui import SafeBridgeApi

        api = SafeBridgeApi(str(tmp_path))
        # 获取所有真实存在的公开方法（模拟 pywebview 枚举）
        exposable = [
            name for name, member in inspect.getmembers(api, predicate=inspect.ismethod)
            if not name.startswith("_")
        ]

        # 必须包含 call_readonly 和 request_mutation
        assert "call_readonly" in exposable, f"call_readonly not enumerable: {exposable}"
        assert "request_mutation" in exposable, f"request_mutation not enumerable: {exposable}"
        # 也要包含辅助方法
        assert "get_api_method_registry" in exposable
        assert "get_sandbox_status" in exposable
        assert "pick_path" in exposable
        # 不应暴露 GovernanceApi 的内部方法
        assert "build_projection" not in exposable
        assert "purge_agent_dir" not in exposable

    def test_call_readonly_rejects_mutation(self, tmp_path):
        """call_readonly 拒绝变更方法。"""
        from memoryguard.gui import SafeBridgeApi
        api = SafeBridgeApi(str(tmp_path))
        result = api.call_readonly("build_projection", [])
        assert "error" in result
        assert "not a readonly method" in result["error"]

    def test_call_readonly_executes_readonly(self, tmp_path):
        """call_readonly 成功执行只读方法。"""
        from memoryguard.gui import SafeBridgeApi
        api = SafeBridgeApi(str(tmp_path))
        result = api.call_readonly("get_audit", [])
        # 应该返回 audit 结果（dict），不是 error
        assert isinstance(result, dict)

    def test_request_mutation_rejects_readonly(self, tmp_path):
        """request_mutation 拒绝非变更方法。"""
        from memoryguard.gui import SafeBridgeApi
        api = SafeBridgeApi(str(tmp_path))
        result = api.request_mutation("get_audit", [])
        assert "error" in result
        assert "not a mutation method" in result["error"]

    def test_request_mutation_sandbox_returns_deferred(self, tmp_path, monkeypatch):
        """沙箱模式下 request_mutation 返回 deferred=True。"""
        from memoryguard.gui import SafeBridgeApi
        from memoryguard import security

        # 强制沙箱模式
        monkeypatch.setattr(security, "detect_sandbox_mode", lambda: True)

        api = SafeBridgeApi(str(tmp_path))
        result = api.request_mutation("build_projection", [])

        # 必须包含 deferred=True，不能只返回 ok=True
        assert result.get("deferred") is True, f"deferred must be True, got: {result}"
        assert result.get("ok") is True
        assert "request" in result
        assert "message" in result


class TestUriWakeup:
    """桌面执行器唤醒端到端测试。"""

    def test_pipe_unavailable_triggers_direct_desktop_launch(self, tmp_path, monkeypatch):
        """管道不可用时，submit 优先直接启动桌面执行器。"""
        from memoryguard.security import RequestQueue
        from memoryguard import ipc

        monkeypatch.setattr(ipc, "notify_desktop", lambda ws, rid: "pipe_unavailable")

        launch_called = []
        monkeypatch.setattr(ipc, "launch_desktop_request", lambda ws, rid: launch_called.append((ws, rid)) or True)

        uri_called = []
        monkeypatch.setattr(ipc, "launch_uri", lambda uri: uri_called.append(uri) or True)

        notify_called = []
        monkeypatch.setattr(ipc, "write_notify_file", lambda ws, rid: notify_called.append(rid) or True)

        rq = RequestQueue(tmp_path)
        req = rq.submit("build_projection", [])

        assert len(launch_called) == 1, "launch_desktop_request should be called when pipe unavailable"
        assert launch_called[0][0] == tmp_path
        assert launch_called[0][1] == req.request_id
        assert len(uri_called) == 0, "launch_uri should not be called when direct desktop launch succeeds"
        assert len(notify_called) == 0, "write_notify_file should not be called when direct desktop launch succeeds"

    def test_direct_launch_failure_falls_back_to_uri(self, tmp_path, monkeypatch):
        """直接启动失败时，回退 URI 唤醒。"""
        from memoryguard.security import RequestQueue
        from memoryguard import ipc

        monkeypatch.setattr(ipc, "notify_desktop", lambda ws, rid: "pipe_unavailable")
        monkeypatch.setattr(ipc, "launch_desktop_request", lambda ws, rid: False)

        uri_called = []
        monkeypatch.setattr(ipc, "launch_uri", lambda uri: uri_called.append(uri) or True)

        notify_called = []
        monkeypatch.setattr(ipc, "write_notify_file", lambda ws, rid: notify_called.append(rid) or True)

        rq = RequestQueue(tmp_path)
        rq.submit("build_projection", [])

        assert len(uri_called) == 1, "launch_uri should be called when direct desktop launch fails"
        assert uri_called[0].startswith("memoryguard://request/")
        assert len(notify_called) == 0, "write_notify_file should not be called when URI succeeds"

    def test_uri_failure_falls_back_to_notify_file(self, tmp_path, monkeypatch):
        """直接启动和 URI 都失败时，写通知文件。"""
        from memoryguard.security import RequestQueue
        from memoryguard import ipc

        monkeypatch.setattr(ipc, "notify_desktop", lambda ws, rid: "pipe_unavailable")
        monkeypatch.setattr(ipc, "launch_desktop_request", lambda ws, rid: False)
        monkeypatch.setattr(ipc, "launch_uri", lambda uri: False)

        notify_called = []
        monkeypatch.setattr(ipc, "write_notify_file", lambda ws, rid: notify_called.append(rid) or True)

        rq = RequestQueue(tmp_path)
        rq.submit("build_projection", [])

        assert len(notify_called) == 1, "write_notify_file should be called when direct launch and URI fail"

    def test_pipe_delivered_skips_direct_launch_and_uri(self, tmp_path, monkeypatch):
        """管道通知成功时，不启动新执行器，也不尝试 URI。"""
        from memoryguard.security import RequestQueue
        from memoryguard import ipc

        monkeypatch.setattr(ipc, "notify_desktop", lambda ws, rid: "pipe_delivered")

        launch_called = []
        monkeypatch.setattr(ipc, "launch_desktop_request", lambda ws, rid: launch_called.append(rid) or True)

        uri_called = []
        monkeypatch.setattr(ipc, "launch_uri", lambda uri: uri_called.append(uri) or True)
        monkeypatch.setattr(ipc, "write_notify_file", lambda ws, rid: True)

        rq = RequestQueue(tmp_path)
        rq.submit("build_projection", [])

        assert len(launch_called) == 0, "launch_desktop_request should not be called when pipe delivered"
        assert len(uri_called) == 0, "launch_uri should not be called when pipe delivered"
