"""本地 IPC：命名管道 + URI 协议唤醒。

Windows 优先使用命名管道（DACL 仅允许当前登录 SID）。
桌面执行器不运行时，使用 memoryguard://request/<id> 唤醒。

安全：
- 命名管道 DACL 限制到当前登录会话 SID
- URI 里只放不透明的请求编号，不能直接放路径或权限
- 短时 request_id、过期时间、一次性 nonce
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 命名管道（Windows）
# ---------------------------------------------------------------------------

PIPE_PREFIX = r"\\.\pipe\memoryguard"

def _pipe_name(workspace: str | Path) -> str:
    """生成每工作区的命名管道名称。"""
    import hashlib
    ws_hash = hashlib.md5(str(Path(workspace).resolve()).encode()).hexdigest()[:8]
    return f"{PIPE_PREFIX}_{ws_hash}"


def _get_current_sid() -> str | None:
    """获取当前登录用户的 SID（用于 DACL）。

    优先使用 pywin32（win32api.GetCurrentProcess + win32security），
    fallback 到 ctypes。
    """
    if sys.platform != "win32":
        return None

    # 方式1：pywin32（已验证可用）
    try:
        import win32api
        import win32security

        process_handle = win32api.GetCurrentProcess()
        token = win32security.OpenProcessToken(process_handle, win32security.TOKEN_QUERY)
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid)
    except ImportError:
        pass
    except Exception:
        pass

    # 方式2：ctypes fallback（不依赖 pywin32）
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # kernel32.GetCurrentProcess() 返回伪句柄
        process_handle = kernel32.GetCurrentProcess()

        # OpenProcessToken
        token_handle = wintypes.HANDLE()
        TOKEN_QUERY = 0x0008
        # 需要设置 restype/argtypes 以正确调用
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        ]
        if not advapi32.OpenProcessToken(
            process_handle, TOKEN_QUERY, ctypes.byref(token_handle)
        ):
            return None

        # GetTokenInformation - 先查询所需大小
        TokenUser = 1
        ret_len = wintypes.DWORD(0)
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
        ]
        advapi32.GetTokenInformation(
            token_handle, TokenUser, None, 0, ctypes.byref(ret_len)
        )

        buf = (ctypes.c_byte * ret_len.value)()
        if not advapi32.GetTokenInformation(
            token_handle, TokenUser, buf, ret_len.value, ctypes.byref(ret_len)
        ):
            kernel32.CloseHandle(token_handle)
            return None

        # TOKEN_USER 结构: SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }
        # 第一个字段是指针
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]

        # ConvertSidToStringSidW
        sid_str = wintypes.LPWSTR()
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
        ]
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str)):
            kernel32.CloseHandle(token_handle)
            return None

        result = sid_str.value
        kernel32.LocalFree(sid_str)
        kernel32.CloseHandle(token_handle)
        return result
    except Exception:
        return None


def create_named_pipe(workspace: str | Path) -> Any | None:
    """创建命名管道服务端（仅 Windows）。

    DACL 仅允许当前登录 SID。
    如果无法获取 SID，拒绝创建管道（不静默降级到不安全的默认描述符）。
    返回管道句柄，或 None（不支持/安全检查失败）。
    """
    if sys.platform != "win32":
        return None

    pipe_name = _pipe_name(workspace)
    sid_str = _get_current_sid()

    # 安全检查：SID 获取失败时拒绝创建管道
    if not sid_str:
        return None

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        # 构建安全描述符：仅允许当前用户
        # 使用 ctypes 构建 SECURITY_ATTRIBUTES + DACL
        sid = ctypes.c_void_p()
        advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        if not advapi32.ConvertStringSidToSidW(sid_str, ctypes.byref(sid)):
            return None

        # 创建 DACL
        ACL_REVISION = 2
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        dacl_size = 256
        dacl_buf = (ctypes.c_byte * dacl_size)()
        advapi32.InitializeAcl.restype = wintypes.BOOL
        advapi32.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
        if not advapi32.InitializeAcl(dacl_buf, dacl_size, ACL_REVISION):
            return None

        advapi32.AddAccessAllowedAce.restype = wintypes.BOOL
        advapi32.AddAccessAllowedAce.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p
        ]
        if not advapi32.AddAccessAllowedAce(dacl_buf, ACL_REVISION,
                                             GENERIC_READ | GENERIC_WRITE, sid):
            return None

        # SECURITY_DESCRIPTOR
        sd_size = 1024
        sd_buf = (ctypes.c_byte * sd_size)()
        advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL
        advapi32.InitializeSecurityDescriptor.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        if not advapi32.InitializeSecurityDescriptor(sd_buf, 1):
            return None

        advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi32.SetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, ctypes.c_void_p, wintypes.BOOL
        ]
        if not advapi32.SetSecurityDescriptorDacl(sd_buf, True, dacl_buf, False):
            return None

        # SECURITY_ATTRIBUTES
        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL),
            ]

        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = ctypes.cast(sd_buf, ctypes.c_void_p)
        sa.bInheritHandle = False

        # CreateNamedPipeW
        PIPE_ACCESS_DUPLEX = 0x00000003
        PIPE_TYPE_MESSAGE = 0x0004
        PIPE_READMODE_MESSAGE = 0x0002
        PIPE_WAIT = 0x0000
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel32.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)
        ]

        handle = kernel32.CreateNamedPipeW(
            pipe_name,
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            1,  # 最大实例数
            65536,  # 输出缓冲
            65536,  # 输入缓冲
            0,  # 默认超时
            ctypes.byref(sa),
        )

        if handle == INVALID_HANDLE_VALUE or handle is None:
            return None

        return handle
    except Exception:
        return None


# 三态通知结果
PIPE_DELIVERED = "pipe_delivered"        # 命名管道通知成功，执行器已运行
PIPE_UNAVAILABLE = "pipe_unavailable"    # 管道不存在，需要尝试 URI 唤醒
URI_FAILED = "uri_failed"                # URI 唤醒也失败，写通知文件


def notify_desktop(workspace: str | Path, request_id: str) -> str:
    """通知桌面执行器有新请求。

    返回三态结果：
    - PIPE_DELIVERED: 命名管道通知成功，执行器已在运行
    - PIPE_UNAVAILABLE: 管道不存在，需要尝试 URI 唤醒
    - URI_FAILED: URI 唤醒也失败（已写通知文件）
    """
    # 方式1：命名管道（ctypes）
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            pipe_name_w = _pipe_name(workspace)
            GENERIC_WRITE = 0x40000000
            OPEN_EXISTING = 3
            INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

            kernel32.CreateFileW.restype = wintypes.HANDLE
            kernel32.CreateFileW.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
            ]

            handle = kernel32.CreateFileW(
                pipe_name_w, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
            )
            if handle != INVALID_HANDLE_VALUE:
                msg = json.dumps({"request_id": request_id}).encode("utf-8")
                written = wintypes.DWORD(0)
                kernel32.WriteFile.restype = wintypes.BOOL
                kernel32.WriteFile.argtypes = [
                    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p
                ]
                kernel32.WriteFile(handle, msg, len(msg), ctypes.byref(written), None)
                kernel32.CloseHandle(handle)
                return PIPE_DELIVERED
        except Exception:
            pass

    # 管道不可用：返回 PIPE_UNAVAILABLE，由调用方决定是否尝试 URI 唤醒
    return PIPE_UNAVAILABLE


def write_notify_file(workspace: str | Path, request_id: str) -> bool:
    """写入通知文件（最终 fallback）。"""
    try:
        notify_path = Path(workspace).resolve() / ".memoryguard" / "notify.txt"
        notify_path.parent.mkdir(parents=True, exist_ok=True)
        notify_path.write_text(
            json.dumps({"request_id": request_id, "ts": time.time()}),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


def launch_uri(uri: str) -> bool:
    """尝试通过 URI 协议唤醒桌面执行器。"""
    try:
        import subprocess
        if sys.platform == "win32":
            os.startfile(uri)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", uri])
        else:
            subprocess.Popen(["xdg-open", uri])
        return True
    except Exception:
        return False


def launch_desktop_request(workspace: str | Path, request_id: str) -> bool:
    """直接启动桌面执行器处理指定请求。

    这是沙箱 GUI 的主唤醒路径，不依赖 URI 协议注册。
    Windows 下优先使用 pythonw.exe，避免黑框闪退。
    """
    def log(message: str) -> None:
        try:
            from datetime import datetime
            ws = Path(workspace).resolve()
            log_path = ws / ".memoryguard" / "desktop-executor.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} launcher {message}\n")
        except Exception:
            pass

    try:
        import subprocess

        ws = Path(workspace).resolve()
        python_exe = sys.executable
        if sys.platform == "win32" and python_exe.lower().endswith("python.exe"):
            pythonw = python_exe[:-len("python.exe")] + "pythonw.exe"
            if os.path.exists(pythonw):
                python_exe = pythonw

        env = os.environ.copy()
        src_root = Path(__file__).resolve().parents[2]
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(src_root) if not existing else str(src_root) + os.pathsep + existing
        env["MEMORYGUARD_WORKSPACE"] = str(ws)

        cmd = [
            python_exe,
            "-m", "memoryguard",
            "desktop",
            "--workspace", str(ws),
            "--request", request_id,
        ]
        log(f"launch request={request_id} cmd={cmd!r} cwd={ws}")
        if sys.platform == "win32" and _launch_desktop_request_windows(cmd, ws, env, log):
            return True
        subprocess.Popen(
            cmd,
            cwd=str(ws),
            env=env,
            close_fds=True,
        )
        return True
    except Exception as e:
        log(f"launch_failed request={request_id} error={e!r}")
        return False


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _launch_desktop_request_windows(cmd: list[str], ws: Path, env: dict[str, str], log) -> bool:
    try:
        import subprocess

        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            log("windows_start_process_unavailable powershell_not_found")
            return False
        env_assignments = [
            f"$env:PYTHONPATH = {_ps_quote(env.get('PYTHONPATH', ''))}",
            f"$env:MEMORYGUARD_WORKSPACE = {_ps_quote(env.get('MEMORYGUARD_WORKSPACE', str(ws)))}",
        ]
        quoted_args = ", ".join(_ps_quote(arg) for arg in cmd[1:])
        script = "; ".join(env_assignments + [
            f"Start-Process -FilePath {_ps_quote(cmd[0])} -ArgumentList @({quoted_args}) -WorkingDirectory {_ps_quote(str(ws))} -WindowStyle Normal"
        ])
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode == 0:
            log("windows_start_process_ok")
            return True
        log(f"windows_start_process_failed returncode={completed.returncode} stderr={completed.stderr!r}")
        return False
    except Exception as e:
        log(f"windows_start_process_exception error={e!r}")
        return False


def read_pipe_message(handle: Any, timeout_ms: int = 3000) -> dict | None:
    """从命名管道读取一条消息（阻塞等待连接）。

    返回解析后的 dict，或 None（超时/错误）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # ConnectNamedPipe：等待客户端连接
        kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.ConnectNamedPipe(handle, None)

        # ReadFile
        buf = (ctypes.c_byte * 65536)()
        bytes_read = wintypes.DWORD(0)
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p
        ]
        if kernel32.ReadFile(handle, buf, 65536, ctypes.byref(bytes_read), None):
            data = bytes(buf[:bytes_read.value])
            if data:
                # DisconnectNamedPipe
                kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
                kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
                kernel32.DisconnectNamedPipe(handle)
                return json.loads(data.decode("utf-8"))
    except Exception:
        pass
    return None


def check_notify_file(workspace: str | Path) -> str | None:
    """检查通知文件（fallback 模式）。

    返回 request_id，或 None。
    """
    try:
        notify_path = Path(workspace).resolve() / ".memoryguard" / "notify.txt"
        if not notify_path.exists():
            return None
        data = json.loads(notify_path.read_text(encoding="utf-8"))
        notify_path.unlink()  # 一次性消费
        return data.get("request_id")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# URI 协议注册（Windows）
# ---------------------------------------------------------------------------

def register_uri_protocol() -> bool:
    """注册 memoryguard:// URI 协议（仅 Windows）。

    注册后，memoryguard://request/<id> 会启动桌面执行器。
    使用 pythonw.exe（无控制台窗口），结果通过 tkinter 显示。
    URI 里只放不透明的请求编号。
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        # 优先使用 pythonw.exe（无控制台窗口），避免命令窗闪退
        python_exe = sys.executable
        if python_exe.endswith("python.exe"):
            pythonw = python_exe[:-len("python.exe")] + "pythonw.exe"
            if os.path.exists(pythonw):
                python_exe = pythonw
        elif python_exe.endswith("pythonw.exe"):
            pass  # 已经是 pythonw
        else:
            # 回退：用当前可执行文件
            pass

        # 构建命令：用 -m 方式启动模块，设置 PYTHONPATH
        # 使用一个临时脚本来确保 import 路径正确
        script = (
            f'"{python_exe}" -c "'
            "import sys, os; "
            f"sys.path.insert(0, r'{os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))}'); "
            "from memoryguard.desktop_executor import handle_uri; handle_uri(sys.argv[1])"
            '" "%1"'
        )

        key_path = r"Software\Classes\memoryguard"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:MemoryGuard Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, script)

        return True
    except Exception:
        return False


def unregister_uri_protocol() -> bool:
    """取消注册 memoryguard:// URI 协议。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\memoryguard\shell\open\command")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\memoryguard\shell\open")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\memoryguard\shell")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\memoryguard")
        return True
    except Exception:
        return False


def is_uri_protocol_registered() -> bool:
    """检查 URI 协议是否已注册。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\memoryguard"):
            return True
    except (FileNotFoundError, OSError):
        return False


def build_uri(request_id: str) -> str:
    """构建 memoryguard:// URI。"""
    return f"memoryguard://request/{request_id}"


def parse_uri(uri: str) -> str | None:
    """从 URI 解析 request_id。"""
    prefix = "memoryguard://request/"
    if uri.startswith(prefix):
        return uri[len(prefix):]
    return None
