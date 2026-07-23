"""原生桌面窗口 GUI（spec §6.1 第3步的桌面形态）。

依赖策略冲突解决（PREFERENCES §6）:
- spec §1.3/§10 要求零依赖、无供应链风险
- 用户需求要求原生桌面窗口，需 pywebview（第三方）
- 选择: pywebview 作为可选依赖 `memoryguard[gui]`，Core 本体保持零依赖。
  未安装时 open 自动降级到 localhost/HTML/文本。

能力降级链（spec §6.1）:
1. 桌面原生窗口 (pywebview 已装)  -- 本模块
2. localhost 浏览器窗口 (标准库 http.server)  -- 本模块
3. 静态 HTML 文件 (webbrowser.open file://)  -- cli.py
4. 结构化文本 + JSON 路径  -- cli.py
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------


def has_native_gui() -> bool:
    """探测 pywebview 是否可用（可选依赖）。"""
    try:
        import webview  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# 1. 桌面原生窗口
# ---------------------------------------------------------------------------


def open_native_window(html_content: str, title: str = "MemoryGuard") -> int:
    """用 pywebview 弹原生桌面窗口加载 HTML 内容。

    返回退出码：0 成功，3 不可用需回退。
    阻塞调用：窗口关闭前不返回。
    """
    if not has_native_gui():
        return 3
    import webview

    # pywebview 加载 HTML 字符串，无需临时文件、无需 HTTP server
    webview.create_window(
        title=title,
        html=html_content,
        width=1440,
        height=900,
        min_size=(800, 600),
    )
    webview.start()
    return 0


# ---------------------------------------------------------------------------
# 2. localhost 浏览器窗口（降级路径）
# ---------------------------------------------------------------------------

import json as _json
from urllib.parse import urlparse
from .interactive import render_interactive_html


def _find_free_port() -> int:
    """绑定 127.0.0.1 随机端口（spec §1.3 安全要求）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def open_localhost_window(workspace: str, *, auto_open: bool = True) -> tuple[int, str]:
    """启动临时本地 HTTP server + JSON API，返回 (退出码, URL)。

    安全加固：
    - 无通配 CORS（同源 only）
    - 会话令牌验证
    - API 方法白名单
    - 变更 API 走请求队列（沙箱模式）
    """
    from .security import (
        generate_session_token,
        READONLY_API_METHODS,
        MUTATION_API_METHODS,
        ALL_ALLOWED_METHODS,
        is_allowed_method,
        is_mutation_method,
        detect_sandbox_mode,
        RequestQueue,
    )

    port = _find_free_port()
    if port == 0:
        return 3, ""

    api = GovernanceApi(workspace)
    session_token = generate_session_token()
    is_sandbox = detect_sandbox_mode()
    request_queue = RequestQueue(workspace)

    # 将 session_token 注入 HTML
    html = render_interactive_html()
    html = html.replace(
        "</head>",
        f'<script>window.__MG_SESSION__="{session_token}";'
        f'window.__MG_SANDBOX__={str(is_sandbox).lower()};</script></head>',
    )
    html_bytes = html.encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
            elif parsed.path == "/api/health":
                self._json_response(200, {"ok": True, "sandbox": is_sandbox})
            else:
                self.send_error(404)

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_error(404)
                return
            method = parsed.path[len("/api/"):]

            # 验证会话令牌
            token = self.headers.get("X-Session-Token", "")
            if token != session_token:
                self._json_response(403, {"error": "invalid_session_token"})
                return

            # 验证方法白名单
            if not is_allowed_method(method):
                self._json_response(501, {"error": f"unknown method: {method}"})
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                args = _json.loads(body.decode("utf-8")) if body else []
            except Exception as e:
                self.send_error(400, str(e))
                return

            # 特殊方法：请求队列管理
            if method == "submit_request":
                target_method = args[0] if args else ""
                target_args = args[1] if len(args) > 1 else []
                if not is_mutation_method(target_method):
                    self._json_response(400, {"error": "not a mutation method"})
                    return
                req = request_queue.submit(target_method, target_args)
                self._json_response(200, {"ok": True, "request": req.to_dict()})
                return

            if method == "get_request_status":
                req_id = args[0] if args else ""
                req = request_queue.get(req_id)
                if req:
                    self._json_response(200, req.to_dict())
                else:
                    self._json_response(404, {"error": "request not found"})
                return

            if method == "list_pending_requests":
                pending = request_queue.list_pending()
                self._json_response(200, {"requests": [r.to_dict() for r in pending]})
                return

            # 变更 API：沙箱模式下只创建请求
            if is_mutation_method(method):
                if is_sandbox:
                    req = request_queue.submit(method, args)
                    self._json_response(200, {
                        "ok": True,
                        "deferred": True,
                        "request": req.to_dict(),
                        "message": "请求已提交，等待桌面执行器确认",
                    })
                    return
                # 非沙箱模式：直接执行，注入 confirmed=True
                import inspect as _inspect
                _fn = getattr(api, method, None)
                if _fn and callable(_fn):
                    _sig = _inspect.signature(_fn)
                    _params = list(_sig.parameters.keys())
                    if "confirmed" in _params:
                        _cidx = _params.index("confirmed")
                        args = list(args)
                        while len(args) <= _cidx:
                            args.append(_sig.parameters[_params[len(args)]].default)
                        args[_cidx] = True

            # 只读 API 或非沙箱变更：直接执行
            fn = getattr(api, method, None)
            if not callable(fn):
                self._json_response(501, {"error": f"method not implemented: {method}"})
                return
            try:
                result = fn(*args) if args else fn()
                result = result if result is not None else {}
                self._json_response(200, result)
            except Exception as e:
                self._json_response(500, {"error": str(e)})

        def _json_response(self, status: int, data: dict) -> None:
            payload = _json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            # 无通配 CORS：同源 only
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self):  # noqa: N802
            # 同源 only，不需要 CORS 预检
            self.send_error(405)

        def log_message(self, *args):  # 静默
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"MemoryGuard GUI running at {url} (sandbox={is_sandbox})")
    if auto_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0, url


# ---------------------------------------------------------------------------
# 统一入口：按能力降级
# ---------------------------------------------------------------------------


def open_report_window(html_content: str, *, title: str = "MemoryGuard") -> int:
    """按 spec §6.1 降级链打开报告窗口。

    顺序: 桌面原生窗口 -> localhost 浏览器 -> (调用方继续降级到 HTML 文件)
    返回退出码：0 成功，3 需调用方继续降级。
    """
    if has_native_gui():
        return open_native_window(html_content, title=title)
    # 无 pywebview 时，由调用方决定是否走 localhost 还是直接 HTML 文件
    return 3


# ---------------------------------------------------------------------------
# 交互式治理面板（参考 merakagent Tab 布局，非平面报告）
# ---------------------------------------------------------------------------

# 敏感内容正则模式（用于 _mask_content）
import re as _re

_SENSITIVE_PATTERNS: list[tuple[str, _re.Pattern]] = [
    ("aws_access_key", _re.compile(r'AKIA[0-9A-Z]{16}')),
    ("aws_secret_key", _re.compile(r'(?i)aws_secret_access_key["\']?\s*[:=]\s*["\']?[A-Za-z0-9/+=]{40}')),
    ("generic_api_key", _re.compile(r'(?i)(api[_-]?key|apikey|token|secret|password|passwd|pwd)["\']?\s*[:=]\s*["\']?[A-Za-z0-9+/=_\-]{16,}')),
    ("bearer_token", _re.compile(r'(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*')),
    ("private_key", _re.compile(r'-----BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----')),
    ("connection_string", _re.compile(r'(?i)(mongodb|postgres|postgresql|redis|amqp)://[^\s"\']+')),
    ("jwt", _re.compile(r'eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+')),
]

# 隔离规则匹配的模式（与 SharedMemoryStore 隔离规则保持一致）
_QUARANTINE_PATTERNS: list[tuple[str, _re.Pattern]] = [
    ("aws_key", _re.compile(r'AKIA[0-9A-Z]{16}')),
    ("private_key", _re.compile(r'-----BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----')),
    ("api_key", _re.compile(r'(?i)(api[_-]?key|apikey)["\']?\s*[:=]\s*["\']?[A-Za-z0-9+/=_\-]{16,}')),
    ("password", _re.compile(r'(?i)(password|passwd|pwd)["\']?\s*[:=]\s*["\']?[^\s"\']{8,}')),
    ("token", _re.compile(r'(?i)(token|secret)["\']?\s*[:=]\s*["\']?[A-Za-z0-9+/=_\-]{16,}')),
    ("bearer", _re.compile(r'(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*')),
    ("connection_string", _re.compile(r'(?i)(mongodb|postgres|postgresql|redis|amqp)://[^\s"\']+')),
    ("jwt", _re.compile(r'eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+')),
]


def _mask_content(content: str, max_len: int = 120) -> str:
    """统一脱敏：先正则替换敏感模式，再截断，再掩码。

    任何包含敏感模式的内容都会被完全掩码，不泄露原文。
    """
    if not content:
        return ""
    for pattern_name, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(content):
            return "••••[REDACTED:" + pattern_name + "]••••"
    return content[:max_len]


def _mask_preview(content: str) -> str:
    """隔离队列脱敏：前6 + 中间省略 + 后4。"""
    if not content:
        return ""
    for pattern_name, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(content):
            return "••••[REDACTED:" + pattern_name + "]••••"
    if len(content) > 20:
        return content[:6] + "••••••" + content[-4:]
    return "••••"


def _private_data_paths(instance) -> list[str]:
    paths = [
        Path(s.get("resolved_path", "")).resolve()
        for s in instance.surfaces
        if s.get("status") == "found"
        and s.get("resolved_path")
        and s.get("evidence_role") == "private_data_evidence"
    ]
    roots: list[Path] = []
    seen: set[str] = set()
    for path in sorted(paths, key=lambda item: len(item.parts)):
        key = str(path).casefold()
        if key in seen:
            continue
        if any(root == path or root in path.parents for root in roots):
            continue
        roots.append(path)
        seen.add(key)
    return [str(path) for path in roots]


def _found_surface_count(instance, evidence_role: str = "") -> int:
    return sum(
        1 for s in instance.surfaces
        if s.get("status") == "found" and (not evidence_role or s.get("evidence_role") == evidence_role)
    )


def _support_level_from_capability(target_capability) -> str:
    """根据 target_capability 推导 support_level。

    EXPORT_ONLY -> "C"（仅发现）
    SKILL_GATEWAY -> "B"（可读）
    NATIVE_TAKEOVER -> "A"（已接管）
    其他 -> "C"
    """
    value = getattr(target_capability, "value", target_capability)
    return {
        "export_only": "C",
        "skill_gateway": "B",
        "native_takeover": "A",
    }.get(value, "C")


class GovernanceApi:
    """pywebview JS API 类（v3 五入口架构，spec §7.2）。

    v3.1 新增：
    - pick_path：系统目录/文件选择器（替代 prompt）
    - discover_agents：AgentLocator 有限候选发现
    - get_selection_tree / commit_selection：分类勾选授权
    - neuron_decide：图上治理操作 → DecisionEvent → 新规范版本
    - 神经图投影 meta：Agent 实例 / Profile / 规范版本 / Release / 接管状态 / 覆盖状态 / 漂移
    """

    def __init__(self, workspace: str):
        self.workspace = workspace
        self._report = None
        self._window = None  # pywebview window 引用，由 open_interactive_window 注入

    def _set_window(self, window) -> None:
        """注入 pywebview window 实例（用于 create_file_dialog）。"""
        self._window = window

    # ------------------------------------------------------------------
    # 安全层：请求队列管理（pywebview 模式下也需要）
    # ------------------------------------------------------------------

    def get_api_method_registry(self) -> dict:
        """返回 API 方法注册表，供前端动态生成变更方法列表。

        统一单一来源：前端不再维护自己的 MUTATION_METHODS 列表。
        """
        from .security import READONLY_API_METHODS, MUTATION_API_METHODS, _SECURITY_API_METHODS
        return {
            "readonly": sorted(READONLY_API_METHODS),
            "mutation": sorted(MUTATION_API_METHODS),
            "security": sorted(_SECURITY_API_METHODS),
        }

    def get_sandbox_status(self) -> dict:
        """返回当前沙箱状态。"""
        from .security import detect_sandbox_mode
        return {"sandbox": detect_sandbox_mode()}

    def submit_request(self, method: str, args: list | None = None) -> dict:
        """提交变更请求到请求队列。"""
        from .security import RequestQueue, is_mutation_method
        if not is_mutation_method(method):
            return {"error": "not a mutation method"}
        rq = RequestQueue(self.workspace)
        req = rq.submit(method, args or [])
        return {"ok": True, "request": req.to_dict()}

    def get_request_status(self, request_id: str) -> dict:
        """查询请求状态。"""
        from .security import RequestQueue
        rq = RequestQueue(self.workspace)
        req = rq.get(request_id)
        return req.to_dict() if req else {"error": "request not found"}

    def list_pending_requests(self) -> dict:
        """列出所有待执行请求。"""
        from .security import RequestQueue
        rq = RequestQueue(self.workspace)
        pending = rq.list_pending()
        return {"requests": [r.to_dict() for r in pending]}

    # ------------------------------------------------------------------
    # 路径选择器（替代 prompt()）
    # ------------------------------------------------------------------

    def pick_path(self, for_files: bool = False) -> dict:
        """v3.1 §4.3 系统目录/文件选择器。

        for_files=False：选目录（默认，用于添加来源）
        for_files=True：选文件（用于导入离线导出包）

        返回 {path, is_directory} 或 {error: 'cancelled'}。
        """
        if self._window is None:
            # 无 pywebview window 时回退到 prompt
            path = input("输入路径：" if not for_files else "输入文件路径：")
            if not path:
                return {"error": "cancelled"}
            from pathlib import Path
            p = Path(path)
            return {"path": str(p.resolve()), "is_directory": p.is_dir()}
        try:
            import webview
            if for_files:
                file_types = (
                    "All files (*.*)|*.*|"
                    "Zip files (*.zip)|*.zip|"
                    "JSON files (*.json)|*.json|"
                    "JSONL files (*.jsonl)|*.jsonl"
                )
                result = self._window.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=False, file_types=file_types,
                )
            else:
                result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if not result:
                return {"error": "cancelled"}
            path = result if isinstance(result, str) else result[0]
            from pathlib import Path
            p = Path(path)
            return {"path": str(p.resolve()), "is_directory": p.is_dir() if p.exists() else (not for_files)}
        except Exception as e:
            return {"error": f"dialog failed: {e}"}

    # ------------------------------------------------------------------
    # AgentLocator API（v3.1 §3.2）
    # ------------------------------------------------------------------

    def discover_agents(self) -> dict:
        """v3.2 改动包2：检测本机 Agent，集成安装检测器。"""
        from .agent_locator import AgentLocator
        from .agent_install_detector import AgentInstallDetector, DEFAULT_INSTALL_PROBES
        from .agent_cleanup import AgentCleanup
        locator = AgentLocator(self.workspace)
        instances, ledgers = locator.detect_instances()
        detector = AgentInstallDetector(self.workspace)
        cleanup = AgentCleanup(self.workspace)
        ignored_candidates = cleanup._load_uninstalled_candidates()

        enriched_instances = []
        for inst in instances:
            install_probes = DEFAULT_INSTALL_PROBES.get(inst.product, [])
            data_paths = _private_data_paths(inst)
            assessment = detector.assess_lifecycle(inst.product, install_probes, data_paths, profile_id=inst.profile_id)
            # 检查是否被忽略
            is_ignored = assessment.candidate_id in ignored_candidates
            if is_ignored:
                assessment = detector.assess_lifecycle(inst.product, install_probes, data_paths, marked_ignored=True, profile_id=inst.profile_id)
            # not_detected 不显示
            if assessment.lifecycle_state == "not_detected":
                continue
            inst_dict = inst.to_dict()
            inst_dict["lifecycle_state"] = assessment.lifecycle_state
            inst_dict["install_confidence"] = assessment.install_confidence
            inst_dict["support_level"] = _support_level_from_capability(inst.target_capability)
            inst_dict["last_activity_at"] = assessment.last_activity_at
            inst_dict["candidate_id"] = assessment.candidate_id
            inst_dict["install_evidence"] = [e.to_dict() for e in assessment.install_evidence]
            enriched_instances.append(inst_dict)

        if enriched_instances:
            locator.save_discovery(instances, ledgers)
        agg_counts = {"found": 0, "missing": 0, "unsupported": 0,
                      "permission_denied": 0, "excluded_by_user": 0,
                      "not_applicable": 0, "unaccounted_count": 0}
        for ledger in ledgers.values():
            cnt = ledger.counts()
            for k in agg_counts:
                agg_counts[k] += cnt.get(k, 0)
        return {
            "instances": enriched_instances,
            "discovery_ledger": agg_counts,
            "platform": locator.context.platform,
            "host_id": locator.context.host_id,
        }

    def get_selection_tree(self, instance_id: str) -> dict:
        """v3.1 §4.3 返回分类勾选树。"""
        from .agent_locator import AgentLocator
        from .source_registry import SourceRegistry
        locator = AgentLocator(self.workspace)
        tree = locator.get_selection_tree(instance_id)
        if "error" in tree:
            return tree
        reg = SourceRegistry(self.workspace)
        roots = [r for r in reg.list_all_sources() if r.agent_instance_id == instance_id]
        by_discovery = {r.discovery_object_id: r for r in roots if r.discovery_object_id}
        by_path = {str(Path(r.path).resolve()): r for r in roots if r.path}

        def mark_file(f: dict) -> None:
            root = None
            dobj = f.get("discovery_object_id", "")
            if dobj:
                root = by_discovery.get(dobj)
            if root is None and f.get("path"):
                try:
                    root = by_path.get(str(Path(f["path"]).resolve()))
                except OSError:
                    root = None
            if root is not None:
                f["default_selected"] = bool(root.enabled)
                f["saved_selected"] = bool(root.enabled)
                f["source_root_id"] = root.root_id
            else:
                f["saved_selected"] = None

        for scope_obj in tree.get("scopes", []):
            for proj in scope_obj.get("projects", []):
                for cat in proj.get("categories", []):
                    for f in cat.get("files", []):
                        mark_file(f)
            for cat in scope_obj.get("categories", []):
                for f in cat.get("files", []):
                    mark_file(f)
        return tree

    def commit_selection(self, instance_id: str, selected: list, confirmed: bool = False) -> dict:
        """v3.2 改动包1：写入 SelectionManifest + 授权 SourceRoot（含 scope）。

        selected 是 [{category, path, scope, scope_source, project_ref, discovery_object_id}, ...] 列表。
        """
        if not confirmed:
            return {"error": "需要确认才能提交勾选"}
        from pathlib import Path
        from .schema_v3 import (
            SourceRoot, SourceRootType, stable_hash, _now_iso,
            SelectionManifest, SelectionEntry,
            SourceCategory, IngestionPolicy, Ownership, TargetRole,
        )
        from .source_registry import SourceRegistry
        import json

        from .agent_locator import AgentLocator
        locator = AgentLocator(self.workspace)
        tree = locator.get_selection_tree(instance_id)
        if "error" in tree:
            return tree
        # v3.2 改动包1：从 scopes 结构提取 path -> surface 映射
        path_to_surface: dict[str, dict] = {}
        for scope_obj in tree.get("scopes", []):
            scope = scope_obj.get("scope", "unknown")
            scope_source = scope_obj.get("scope_source", "fallback")
            for proj in scope_obj.get("projects", []):
                project_ref = proj.get("project_ref", "")
                proj_ss = proj.get("scope_source", scope_source)
                for cat in proj.get("categories", []):
                    for f in cat.get("files", []):
                        path_to_surface[f["path"]] = {
                            "surface_id": f.get("surface_id", ""),
                            "category": cat["category"],
                            "ingestion_policy": f.get("ingestion_policy", "extract_candidates"),
                            "ownership": f.get("ownership", "unknown"),
                            "target_role": f.get("target_role", "none"),
                            "scope": f.get("scope", scope),
                            "scope_source": f.get("scope_source", proj_ss),
                            "project_ref": f.get("project_ref", project_ref),
                            "discovery_object_id": f.get("discovery_object_id", ""),
                        }
            for cat in scope_obj.get("categories", []):
                for f in cat.get("files", []):
                    path_to_surface[f["path"]] = {
                        "surface_id": f.get("surface_id", ""),
                        "category": cat["category"],
                        "ingestion_policy": f.get("ingestion_policy", "extract_candidates"),
                        "ownership": f.get("ownership", "unknown"),
                        "target_role": f.get("target_role", "none"),
                        "scope": f.get("scope", scope),
                        "scope_source": f.get("scope_source", scope_source),
                        "project_ref": f.get("project_ref", ""),
                        "discovery_object_id": f.get("discovery_object_id", ""),
                    }

        sel_dir = Path(self.workspace) / ".memoryguard" / "selections"
        sel_dir.mkdir(parents=True, exist_ok=True)
        selection_id = stable_hash("sel", instance_id, _now_iso())
        entries: list[SelectionEntry] = []
        if not selected:
            reg = SourceRegistry(self.workspace)
            visible_discovery_ids = {surf.get("discovery_object_id", "") for surf in path_to_surface.values() if surf.get("discovery_object_id")}
            visible_paths = {str(Path(path).resolve()) for path in path_to_surface if path}
            disabled_count = 0
            for root in reg.list_all_sources():
                if root.agent_instance_id != instance_id:
                    continue
                root_path = str(Path(root.path).resolve()) if root.path else ""
                visible = (root.discovery_object_id and root.discovery_object_id in visible_discovery_ids) or root_path in visible_paths
                if visible and root.enabled:
                    root.enabled = False
                    disabled_count += 1
            reg._save()
            return {"selection_id": selection_id, "added_source_count": 0, "updated_source_count": 0, "disabled_source_count": disabled_count, "total_selected": 0}
        # v3.2 改动包1 P0：服务端以 discovery_object_id 为唯一授权依据
        discovery_object_ids = [item.get("discovery_object_id", "") for item in selected if item.get("discovery_object_id")]
        validation = locator.validate_discovery_objects(instance_id, discovery_object_ids)
        # 过滤掉验证失败的条目
        validated_selected = []
        for item in selected:
            dobj_id = item.get("discovery_object_id", "")
            if dobj_id and validation.get(dobj_id, {}).get("valid"):
                # 从服务端回填所有字段，不信任客户端
                server_info = validation[dobj_id].get("file_info") or validation[dobj_id].get("surface") or {}
                item["path"] = server_info["path"]
                item["category"] = server_info.get("category", "unknown")
                item["scope"] = server_info.get("scope", "unknown")
                item["scope_source"] = server_info.get("scope_source", "fallback")
                item["project_ref"] = server_info.get("project_ref", "")
                item["surface_id"] = server_info.get("surface_id", "")
                item["ingestion_policy"] = server_info.get("ingestion_policy", "extract_candidates")
                item["ownership"] = server_info.get("ownership", "external_read_only")
                item["target_role"] = server_info.get("target_role", "none")
                validated_selected.append(item)
            elif not dobj_id:
                # 没有 discovery_object_id 的条目直接拒绝
                continue
            else:
                # 验证失败的条目直接拒绝
                continue
        selected = validated_selected
        if not selected:
            return {"error": "所有 discovery_object_id 验证失败，无有效条目"}
        for item in selected:
            path = item.get("path", "")
            cat_str = item.get("category", "unknown")
            surf = dict(path_to_surface.get(path, {}))
            surf.update({
                "surface_id": item.get("surface_id", surf.get("surface_id", "")),
                "ingestion_policy": item.get("ingestion_policy", surf.get("ingestion_policy", "extract_candidates")),
                "ownership": item.get("ownership", surf.get("ownership", "external_read_only")),
                "target_role": item.get("target_role", surf.get("target_role", "none")),
            })
            scope = item.get("scope", "unknown")
            scope_source = item.get("scope_source", "fallback")
            project_ref = item.get("project_ref", "")
            dobj_id = item.get("discovery_object_id", "")
            try:
                cat_enum = SourceCategory(cat_str)
            except ValueError:
                cat_enum = SourceCategory.UNKNOWN
            try:
                ing = IngestionPolicy(surf.get("ingestion_policy", "extract_candidates"))
            except ValueError:
                ing = IngestionPolicy.EXTRACT_CANDIDATES
            try:
                own = Ownership(surf.get("ownership", "external_read_only"))
            except ValueError:
                own = Ownership.EXTERNAL_READ_ONLY
            try:
                tr = TargetRole(surf.get("target_role", "none"))
            except ValueError:
                tr = TargetRole.NONE
            entries.append(SelectionEntry(
                surface_id=surf.get("surface_id", ""),
                resolved_path=path, category=cat_enum,
                ingestion_policy=ing, ownership=own, target_role=tr,
                selected=True,
                scope=scope, scope_source=scope_source,
                project_ref=project_ref,
                discovery_object_id=dobj_id,
            ))
        manifest = SelectionManifest(
            selection_id=selection_id, instance_id=instance_id,
            profile_id=tree.get("profile_id", ""),
            created_at=_now_iso(), entries=entries,
            authorization_summary={
                "selected_count": len(entries),
                "native_memory_count": sum(1 for e in entries if e.category == SourceCategory.NATIVE_MEMORY),
                "control_surface_count": sum(1 for e in entries if e.category == SourceCategory.CONTROL_SURFACE),
                "user_scope_count": sum(1 for e in entries if e.scope == "user"),
                "project_scope_count": sum(1 for e in entries if e.scope == "project"),
            },
        )
        (sel_dir / f"{selection_id}.json").write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        reg = SourceRegistry(self.workspace)
        selected_discovery_ids = {e.discovery_object_id for e in entries if e.discovery_object_id}
        selected_paths = {str(Path(e.resolved_path).resolve()) for e in entries if e.resolved_path}
        visible_discovery_ids = {surf.get("discovery_object_id", "") for surf in path_to_surface.values() if surf.get("discovery_object_id")}
        visible_paths = {str(Path(path).resolve()) for path in path_to_surface if path}
        disabled_count = 0
        for root in reg.list_all_sources():
            if root.agent_instance_id != instance_id:
                continue
            root_path = str(Path(root.path).resolve()) if root.path else ""
            visible = (root.discovery_object_id and root.discovery_object_id in visible_discovery_ids) or root_path in visible_paths
            selected_now = (root.discovery_object_id and root.discovery_object_id in selected_discovery_ids) or root_path in selected_paths
            if visible and not selected_now and root.enabled:
                root.enabled = False
                disabled_count += 1
        added_count = 0
        updated_count = 0
        for entry in entries:
            p = Path(entry.resolved_path)
            if not p.exists():
                continue
            root_type = SourceRootType.SELECTED_DIRECTORY if p.is_dir() else SourceRootType.SELECTED_FILE
            try:
                root = reg.add(entry.resolved_path, root_type,
                               display_name=f"{tree.get('product', 'agent')}/{entry.surface_id}")
            except (ValueError, OSError):
                continue
            if root.agent_instance_id and root.agent_instance_id != instance_id:
                continue
            root.enabled = True
            if not root.agent_instance_id:
                root.agent_instance_id = instance_id
                root.surface_id = entry.surface_id
                root.source_category = entry.category.value
                root.ingestion_policy = entry.ingestion_policy.value
                root.ownership = entry.ownership.value
                root.target_role = entry.target_role.value
                root.scope = entry.scope
                root.scope_source = entry.scope_source
                root.project_ref = entry.project_ref
                root.discovery_object_id = entry.discovery_object_id
                added_count += 1
            else:
                root.surface_id = entry.surface_id or root.surface_id
                root.source_category = entry.category.value
                root.ingestion_policy = entry.ingestion_policy.value
                root.ownership = entry.ownership.value
                root.target_role = entry.target_role.value
                root.scope = entry.scope
                root.scope_source = entry.scope_source
                root.project_ref = entry.project_ref
                root.discovery_object_id = entry.discovery_object_id
                updated_count += 1
        reg._save()
        return {
            "selection_id": selection_id,
            "added_source_count": added_count,
            "updated_source_count": updated_count,
            "disabled_source_count": disabled_count,
            "total_selected": len(entries),
        }

    # ------------------------------------------------------------------
    # 图上治理操作（v3.1 §6.2）
    # ------------------------------------------------------------------

    def neuron_decide(self, node_id: str, action: str,
                      reason: str = "", confirmed: bool = False) -> dict:
        """v3.1 §6.2 图上操作 → 追加 DecisionEvent → 新规范版本。

        action ∈ {accept, exclude, quarantine, supersede, merge, rescope, plan}
        """
        if not confirmed:
            return {"error": "需要确认才能执行治理操作"}
        from .managed_store import ManagedStore, find_record_by_node_id
        # 找到记录对应的 agent_instance_id
        vid, record = find_record_by_node_id(self.workspace, node_id)
        if record is None:
            return {"error": f"node not found in any managed store: {node_id}"}
        # 找 agent_instance_id
        from pathlib import Path
        ws = Path(self.workspace).resolve()
        mm_root = ws / ".memoryguard" / "managed-memory"
        agent_instance_id = None
        for inst_dir in mm_root.iterdir():
            if not inst_dir.is_dir():
                continue
            store = ManagedStore(ws, inst_dir.name)
            recs = store.list_records()
            if any(r.memory_id == record.memory_id for r in recs):
                agent_instance_id = inst_dir.name
                break
        if agent_instance_id is None:
            return {"error": "agent instance not found for record"}
        store = ManagedStore(ws, agent_instance_id)
        new_version = store.apply_decision(
            action=action, target_ids=[record.memory_id],
            reason=reason, actor="user",
        )
        return {
            "memory_version": new_version.version_id,
            "action": action,
            "target_id": record.memory_id,
            "decision_count": new_version.decision_count,
        }

    # ------------------------------------------------------------------
    # ProjectionApi（spec §7.3）：神经图纯投影
    # ------------------------------------------------------------------

    def set_projection_source_enabled(self, root_id: str, enabled: bool) -> dict:
        from .source_registry import SourceRegistry
        reg = SourceRegistry(self.workspace)
        root = reg.set_enabled(root_id, enabled)
        if not root:
            return {"ok": False, "error": "source root not found", "root_id": root_id}
        return {"ok": True, "root": root.to_dict(), "source_map": self.get_projection_source_map()}

    def get_projection_source_map(self) -> dict:
        from .source_registry import SourceRegistry
        reg = SourceRegistry(self.workspace)
        roots = reg.list_all_sources()
        known_agents = sorted({r.agent_instance_id for r in roots if r.agent_instance_id})
        single_agent_id = known_agents[0] if len(known_agents) == 1 else ""
        native_categories = {"native_memory", "project_memory"}
        excluded_categories = {"conversation_history", "runtime_evidence", "ignored_runtime_data"}
        excluded_policies = {"evidence_only", "govern_only", "ignore"}
        entries = []
        for root in roots:
            enabled = bool(root.enabled)
            source_category = root.source_category
            agent_instance_id = root.agent_instance_id
            surface_id = root.surface_id
            project_ref = root.project_ref
            scope_source = root.scope_source
            if root.scope == "project" and root.source_category in {"", "unknown"}:
                source_category = "knowledge_source"
                if not project_ref:
                    project_ref = Path(root.path).resolve().name if root.path else "当前项目"
                if not scope_source or scope_source == "fallback":
                    scope_source = "project_workspace"
                if not agent_instance_id and single_agent_id:
                    agent_instance_id = single_agent_id
                if not surface_id:
                    surface_id = "project_workspace"
            native_eligible = enabled and source_category in native_categories
            logical_eligible = enabled and not native_eligible and source_category not in excluded_categories and root.ingestion_policy not in excluded_policies
            if native_eligible:
                projection_mode = "native_memory_projection"
            elif logical_eligible:
                projection_mode = "logical_reconstruction_projection"
            else:
                projection_mode = "evidence_only"
            entries.append({
                "root_id": root.root_id,
                "display_name": root.display_name,
                "path": root.path,
                "enabled": enabled,
                "agent_instance_id": agent_instance_id,
                "surface_id": surface_id,
                "scope": root.scope,
                "scope_source": scope_source,
                "project_ref": project_ref,
                "source_category": source_category,
                "ingestion_policy": root.ingestion_policy,
                "target_role": root.target_role,
                "ownership": root.ownership,
                "projection_mode": projection_mode,
                "logical_eligible": logical_eligible,
                "native_eligible": native_eligible,
            })
        return {
            "entries": entries,
            "summary": {
                "total": len(entries),
                "enabled": sum(1 for e in entries if e["enabled"]),
                "logical_reconstruction": sum(1 for e in entries if e["logical_eligible"]),
                "native_memory": sum(1 for e in entries if e["native_eligible"]),
                "evidence_only": sum(1 for e in entries if e["projection_mode"] == "evidence_only"),
            },
        }

    def get_neuron_graph(self, mode: str = "reconstructed") -> dict:
        """纯读取神经图投影。未构建时返回 {empty: true, reason: 'not_built'}。"""
        from .projection import ProjectionBuilder
        graph_mode = "native" if mode == "native" else "reconstructed"
        pb = ProjectionBuilder(self.workspace, graph_mode)
        graph = pb.get_or_empty()
        graph = self._hydrate_neuron_graph_from_ir(graph)
        graph["source_map"] = self.get_projection_source_map()
        graph["projection_kind"] = "native_memory_projection" if graph_mode == "native" else "reconstructed_governance_projection"
        graph["mode"] = graph_mode
        return graph

    def _looks_english_text(self, text: str) -> bool:
        if not text:
            return False
        latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return latin >= 12 and latin > cjk * 2

    def _compact_english_snippet(self, text: str, limit: int = 80) -> str:
        text = " ".join(str(text or "").replace("\n", " ").split())
        replacements = {
            "memory": "记忆", "project": "项目", "preference": "偏好", "rule": "规则",
            "workflow": "流程", "procedure": "流程", "constraint": "约束", "fact": "事实",
            "use": "使用", "should": "应", "must": "必须", "avoid": "避免",
            "file": "文件", "folder": "文件夹", "source": "来源", "agent": "智能体",
        }
        words = text[:limit].split()
        mapped = [replacements.get(w.strip(".,:;()[]{}\"'").lower(), w) for w in words[:16]]
        return " ".join(mapped).strip()

    def _localized_record_fields(self, rec: dict) -> dict:
        kind_labels = {
            "fact": "事实", "preference": "偏好", "project": "项目", "episode": "事件",
            "procedure": "流程", "correction": "纠错", "workflow": "流程", "constraint": "约束",
        }
        title = rec.get("title") or rec.get("memory_id", "")[:8]
        body = rec.get("body") or ""
        kind = rec.get("kind", "")
        original_title = rec.get("original_title") or title
        original_body = rec.get("original_body") or body
        if rec.get("display_language") == "zh" and (rec.get("original_body") or not self._looks_english_text(title + " " + body)):
            return {
                "original_title": original_title,
                "original_body": original_body,
                "title_zh": title,
                "body_zh": body,
            }
        if self._looks_english_text(title + " " + body):
            base = self._compact_english_snippet(title or body, 64)
            summary = self._compact_english_snippet(body or title, 220)
            return {
                "original_title": original_title,
                "original_body": original_body,
                "title_zh": f"{kind_labels.get(kind, '记忆')}：{base}",
                "body_zh": f"中文辅助摘要：{summary}",
            }
        return {
            "original_title": original_title,
            "original_body": original_body,
            "title_zh": title,
            "body_zh": body,
        }

    def _hydrate_neuron_graph_from_ir(self, graph: dict) -> dict:
        if not graph or graph.get("empty"):
            return graph
        ir_path = Path(self.workspace).resolve() / ".memoryguard" / "ir" / "current.json"
        if not ir_path.exists():
            return graph
        try:
            data = _json.loads(ir_path.read_text(encoding="utf-8"))
        except Exception:
            return graph
        records = {r.get("memory_id"): r for r in data.get("records", []) if r.get("memory_id")}
        duplicate_groups = data.get("duplicate_groups", [])
        related_by_id: dict[str, list[dict]] = {mid: [] for mid in records}
        for group in duplicate_groups:
            members = [mid for mid in group.get("member_ids", []) if mid in records]
            if len(members) < 2:
                continue
            for mid in members:
                related_by_id.setdefault(mid, [])
                for other in members:
                    if other == mid:
                        continue
                    rec = records[other]
                    localized = self._localized_record_fields(rec)
                    related_by_id[mid].append({
                        "memory_id": other,
                        "title": rec.get("title") or other[:8],
                        "kind": rec.get("kind", ""),
                        "body_preview": (rec.get("body") or "")[:160],
                        "relation": "duplicate_candidate",
                        **localized,
                    })
        for node in graph.get("nodes", []):
            memory_id = node.get("memory_id")
            member_ids = node.get("member_ids") or []
            if memory_id and memory_id in records:
                rec = records[memory_id]
                localized = self._localized_record_fields(rec)
                node.update({k: v for k, v in localized.items() if v})
                if not node.get("title"):
                    node["title"] = rec.get("title", "")
                if not node.get("body"):
                    node["body"] = rec.get("body", "")
                if not node.get("scope"):
                    node["scope"] = rec.get("scope", "project")
                if not node.get("kind"):
                    node["kind"] = rec.get("kind", "")
                if node.get("confidence") in (None, ""):
                    node["confidence"] = rec.get("confidence", 0.0)
                if not node.get("completeness"):
                    node["completeness"] = rec.get("completeness", "")
                node["related"] = related_by_id.get(memory_id, [])[:12]
            if member_ids:
                members = []
                for mid in member_ids:
                    rec = records.get(mid)
                    if not rec:
                        continue
                    localized = self._localized_record_fields(rec)
                    members.append({
                        "memory_id": mid,
                        "title": rec.get("title") or mid[:8],
                        "kind": rec.get("kind", ""),
                        "body_preview": (rec.get("body") or "")[:180],
                        **localized,
                    })
                node["members"] = members
                if not node.get("body") and members:
                    node["body"] = "\n".join(f"- {m['title']}: {m['body_preview']}" for m in members)
        return graph

    def build_projection(self, confirmed: bool = False, mode: str = "reconstructed") -> dict:
        """构建神经图投影（需用户确认）。

        v3.1 §6.3：构建时同步为每个 agent_instance 创建 ManagedStore initial
        version（若不存在），并聚合 7 项 meta 信息注入投影。
        """
        if not confirmed:
            return {"error": "需要确认才能构建投影"}
        from .memory_ir import MemoryNormalizer
        from .projection import ProjectionBuilder
        from .source_registry import SourceRegistry, ScanBudget
        from .managed_store import ManagedStore
        from .agent_locator import AgentLocator, compute_takeover_state
        from .schema_v3 import TakeoverState
        from pathlib import Path

        reg = SourceRegistry(self.workspace)
        snap = reg.scan(ScanBudget())
        roots = reg.list_sources()
        root_map = {r.root_id: r.path for r in roots}
        root_policies = {r.root_id: {"source_category": r.source_category, "ingestion_policy": r.ingestion_policy} for r in roots}

        norm = MemoryNormalizer(self.workspace)
        ir = norm.load()
        if ir is None or ir.snapshot_id != snap.snapshot_id:
            ir = norm.normalize(snap, root_map=root_map, root_policies=root_policies)
            norm.save(ir)
        else:
            changed = norm.filter_by_source_policies(ir, snap, root_policies)
            changed = norm.ensure_localized(ir) or changed
            if changed:
                norm.save(ir)

        # 建立 source_object_id → source_root_id → agent_instance_id 映射
        obj_to_root = {obj.source_object_id: obj.source_root_id
                       for obj in snap.source_objects}
        root_to_instance = {r.root_id: r.agent_instance_id
                            for r in reg.list_sources() if r.agent_instance_id}

        # 按 agent_instance_id 分组 records
        instance_records: dict[str, list] = {}
        for rec in ir.records:
            for prov in rec.provenance:
                root_id = obj_to_root.get(prov.source_object_id, "")
                inst_id = root_to_instance.get(root_id, "")
                if inst_id:
                    instance_records.setdefault(inst_id, []).append(rec)
                    break

        # 为每个 agent_instance 创建/更新 ManagedStore initial version
        managed_meta: dict[str, dict] = {}
        for inst_id, recs in instance_records.items():
            store = ManagedStore(self.workspace, inst_id)
            if store.get_active_version_id() is None:
                store.create_initial_version(recs)
            active = store.get_active_version()
            managed_meta[inst_id] = {
                "version_id": active.version_id if active else "",
                "record_count": len(recs),
                "decision_count": active.decision_count if active else 0,
            }

        # 聚合 7 项状态 meta
        locator = AgentLocator(self.workspace)
        instances, ledgers = locator.detect_instances()
        cov_counts = snap.coverage.counts()
        cov_status = snap.coverage.status().value
        # 读取已发布的 release（若有）
        releases_list: list[dict] = []
        try:
            from .release_manager import ReleaseManager
            rm = ReleaseManager(self.workspace)
            releases_list = rm.list_releases()
        except Exception:
            pass

        agent_instances_meta = []
        for inst in instances:
            inst_ledger = ledgers.get(inst.instance_id)
            mm = managed_meta.get(inst.instance_id, {})
            has_managed = bool(mm)
            # 接管状态机
            takeover_state = compute_takeover_state(
                instance=inst,
                ledger=inst_ledger,
                selection_committed=has_managed,
                canonicalized=has_managed,
                release_planned=any(r.get("instance_id") == inst.instance_id
                                    for r in releases_list),
                published=any(r.get("instance_id") == inst.instance_id
                              and r.get("status") == "applied"
                              for r in releases_list),
                runtime_verified=False,
                drifted=False,
            )
            agent_instances_meta.append({
                "instance_id": inst.instance_id,
                "product": inst.product,
                "profile_id": inst.profile_id,
                "target_capability": inst.target_capability.value,
                "managed_version": mm.get("version_id", ""),
                "record_count": mm.get("record_count", 0),
                "decision_count": mm.get("decision_count", 0),
                "takeover_state": takeover_state.value,
            })

        meta = {
            "agent_instances": agent_instances_meta,
            "instance_count": len(agent_instances_meta),
            "coverage": cov_counts,
            "coverage_status": cov_status,
            "release_count": len(releases_list),
            "drifted": False,
        }

        graph_mode = "native" if mode == "native" else "reconstructed"
        pb = ProjectionBuilder(self.workspace, graph_mode)
        proj = pb.build(ir, meta=meta)
        pb.save(proj)
        result = proj.to_dict()
        result["mode"] = graph_mode
        result["projection_kind"] = "native_memory_projection" if graph_mode == "native" else "reconstructed_governance_projection"
        return result

    def delete_projection(self, confirmed: bool = False, mode: str = "reconstructed") -> dict:
        """删除神经图投影文件。投影可从 IR + DecisionLog 完整重建。"""
        if not confirmed:
            return {"error": "需要确认才能删除投影"}
        from .projection import ProjectionBuilder
        graph_mode = "native" if mode == "native" else "reconstructed"
        pb = ProjectionBuilder(self.workspace, graph_mode)
        pb.delete()
        return {"ok": True, "deleted": True, "mode": graph_mode}

    # ------------------------------------------------------------------
    # SourceApi（spec §7.2）
    # ------------------------------------------------------------------

    def list_publish_targets(self) -> dict:
        from .source_registry import SourceRegistry
        native_categories = {"native_memory", "project_memory"}
        targets = []
        for root in SourceRegistry(self.workspace).list_all_sources():
            if not root.enabled or root.source_category not in native_categories:
                continue
            path = Path(root.path)
            target_file = path if path.suffix else path / "memory.md"
            targets.append({
                "root_id": root.root_id,
                "display_name": root.display_name,
                "target_file": str(target_file),
                "source_category": root.source_category,
                "agent_instance_id": root.agent_instance_id,
                "surface_id": root.surface_id,
                "scope": root.scope,
                "project_ref": root.project_ref,
                "ownership": root.ownership,
                "target_role": root.target_role,
                "is_agent_native_memory": root.source_category == "native_memory" and root.ownership == "agent_managed" and root.target_role == "takeover_input",
                "path_kind": "file" if path.suffix else "folder_default_memory_md",
            })
        return {"targets": targets, "total": len(targets)}

    def choose_publish_target_path(self, kind: str = "file") -> dict:
        import platform
        import subprocess
        if platform.system().lower() == "windows":
            try:
                if kind == "folder":
                    script = "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = '选择写回记忆文件夹'; $d.ShowNewFolderButton = $true; if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
                else:
                    script = "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.OpenFileDialog; $d.Title = '选择写回记忆文件'; if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.FileName }"
                completed = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=300)
                if completed.returncode != 0:
                    return {"ok": False, "error": (completed.stderr or "PowerShell 文件选择框失败").strip()}
                selected = (completed.stdout or "").strip().splitlines()[-1] if (completed.stdout or "").strip() else ""
                target = str(Path(selected) / "memory.md") if selected and kind == "folder" else selected
                return {"ok": bool(target), "target_file": target}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            if kind == "folder":
                selected = filedialog.askdirectory(title="选择写回记忆文件夹")
                target = str(Path(selected) / "memory.md") if selected else ""
            else:
                selected = filedialog.askopenfilename(title="选择写回记忆文件")
                target = selected or ""
            root.destroy()
            return {"ok": bool(target), "target_file": target}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def publish_reconstructed_memory(self, target_file: str, confirmed: bool = False) -> dict:
        if not confirmed:
            return {"error": "需要确认才能发布重构记忆"}
        from .memory_ir import MemoryNormalizer
        from .native_file_release import SafeNativeFilePublisher
        norm = MemoryNormalizer(self.workspace)
        ir = norm.load()
        if ir is None:
            return {"error": "没有可发布的重构记忆"}
        lines = ["# Memory", ""]
        for rec in ir.records:
            status = rec.status.value if hasattr(rec.status, "value") else str(rec.status)
            if status in {"rejected", "quarantined"}:
                continue
            lines.extend([f"## {rec.title}", "", rec.body, ""])
        content = "\n".join(lines).encode("utf-8")
        result = SafeNativeFilePublisher(self.workspace).apply({Path(target_file): content}, label="reconstructed-memory")
        return result.to_dict()

    def rollback_native_memory_release(self, release_id: str, force: bool = False, confirmed: bool = False) -> dict:
        if not confirmed:
            return {"error": "需要确认才能回滚原生记忆"}
        from .native_file_release import SafeNativeFilePublisher
        return SafeNativeFilePublisher(self.workspace).rollback(release_id, force=force).to_dict()

    def list_native_memory_releases(self) -> dict:
        from .native_file_release import SafeNativeFilePublisher
        releases = SafeNativeFilePublisher(self.workspace).list_releases()
        return {"releases": releases, "total": len(releases)}

    def list_sources(self) -> dict:
        from .source_registry import SourceRegistry
        reg = SourceRegistry(self.workspace)
        sources = [s.to_dict() for s in reg.list_sources()]
        return {"sources": sources, "total": len(sources)}

    # ------------------------------------------------------------------
    # DataApi（v3.2 §8.2）：Agent 卡片数据页
    # ------------------------------------------------------------------

    def list_agent_candidates(self, include_uninstalled: bool = False,
                              include_stale: bool = True,
                              include_unknown: bool = True) -> dict:
        """v3.2 扫描当前系统 HOME 下的所有 Agent 候选。

        返回候选列表，含 stale 状态、是否已标记卸载、是否有 Profile。
        """
        from .agent_locator import AgentLocator
        locator = AgentLocator(self.workspace)
        candidates = locator.discover_candidates(
            include_uninstalled=include_uninstalled,
            include_stale=include_stale,
            include_unknown=include_unknown,
        )
        return {
            "candidates": [c.to_dict() for c in candidates],
            "total": len(candidates),
        }

    def mark_agent_uninstalled(self, product: str, dir_path: str = "",
                               reason: str = "", candidate_id: str = "") -> dict:
        """v3.2 改动包2：标记 Agent 为已卸载，以 candidate_id 为单位。"""
        from .agent_cleanup import AgentCleanup
        cleanup = AgentCleanup(self.workspace)
        cid = candidate_id or product  # 向后兼容
        return cleanup.mark_uninstalled(cid, product=product, dir_path=dir_path, reason=reason)

    def unmark_agent_uninstalled(self, product: str, candidate_id: str = "") -> dict:
        """v3.2 改动包2：取消已卸载标记，以 candidate_id 为单位。"""
        from .agent_cleanup import AgentCleanup
        cleanup = AgentCleanup(self.workspace)
        cid = candidate_id or product
        return cleanup.unmark_uninstalled(cid, product=product)

    def _resolve_candidate_context(self, candidate_id: str) -> dict:
        from .agent_locator import AgentLocator
        from .agent_install_detector import AgentInstallDetector, DEFAULT_INSTALL_PROBES
        locator = AgentLocator(self.workspace)
        instances, _ = locator.detect_instances()
        detector = AgentInstallDetector(self.workspace)
        for inst in instances:
            data_paths = _private_data_paths(inst)
            assessment = detector.assess_lifecycle(
                inst.product, DEFAULT_INSTALL_PROBES.get(inst.product, []), data_paths,
                profile_id=inst.profile_id,
            )
            if assessment.candidate_id == candidate_id:
                return {"instance": inst, "assessment": assessment, "data_paths": data_paths}
        return {}

    def archive_agent_dir(self, product: str = "", dir_path: str = "",
                          reason: str = "", candidate_id: str = "",
                          dry_run: bool = False,
                          allowed_data_paths: list | None = None) -> dict:
        """v3.2 改动包2：归档 Agent 目录，以 candidate_id 为单位。可恢复。"""
        from .agent_cleanup import AgentCleanup
        if isinstance(dry_run, str):
            dry_run = dry_run.lower() in ("true", "1", "yes")
        if not candidate_id:
            return {"error": "candidate_id_required"}
        context = self._resolve_candidate_context(candidate_id)
        if not context:
            return {"error": "candidate_not_found", "candidate_id": candidate_id}
        data_paths = context["data_paths"]
        if not data_paths:
            return {"error": "no_private_data_evidence", "candidate_id": candidate_id}
        target_path = dir_path or data_paths[0]
        from .agent_cleanup import AgentCleanup
        cleanup = AgentCleanup(self.workspace)
        return cleanup.archive_agent_dir(
            candidate_id, context["instance"].product, target_path, reason=reason,
            dry_run=dry_run, allowed_data_paths=data_paths,
        )

    def restore_archived_agent(self, archive_id: str) -> dict:
        """从归档恢复 Agent 目录。"""
        from .agent_cleanup import AgentCleanup
        cleanup = AgentCleanup(self.workspace)
        return cleanup.restore_archived(archive_id)

    def delete_archived_agent(self, archive_id: str) -> dict:
        """永久删除归档（不可恢复）。"""
        from .agent_cleanup import AgentCleanup
        cleanup = AgentCleanup(self.workspace)
        return cleanup.delete_archived(archive_id)

    def open_agent_folder(self, dir_path: str = "", candidate_id: str = "") -> dict:
        if not dir_path:
            if not candidate_id:
                return {"error": "path_required"}
            context = self._resolve_candidate_context(candidate_id)
            if not context or not context.get("data_paths"):
                return {"error": "candidate_path_not_found", "candidate_id": candidate_id}
            dir_path = context["data_paths"][0]
        path = Path(dir_path).expanduser().resolve()
        if not path.exists():
            return {"error": "path_not_found", "path": str(path)}
        target = path if path.is_dir() else path.parent
        try:
            if sys.platform == "win32":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as exc:
            return {"error": "open_folder_failed", "reason": str(exc), "path": str(target)}
        return {"ok": True, "path": str(target)}

    def _purge_agent_dir_disabled(self, product: str = "", dir_path: str = "",
                                  candidate_id: str = "", dry_run: bool = False) -> dict:
        return {
            "error": "direct_purge_disabled",
            "reason": "直接删除在当前沙箱/系统权限环境下不可靠，已改为打开文件夹由用户手动处理。",
            "dir_path": dir_path,
            "candidate_id": candidate_id,
        }

    def list_archived_agents(self) -> dict:
        """列出所有归档的 Agent。"""
        from .agent_cleanup import AgentCleanup
        cleanup = AgentCleanup(self.workspace)
        return {"archives": cleanup.list_archives(), "total": len(cleanup.list_archives())}

    def list_cleanup_history(self) -> dict:
        """读取清理操作历史。"""
        from .agent_cleanup import AgentCleanup
        cleanup = AgentCleanup(self.workspace)
        return {"history": cleanup.list_cleanup_history()}

    def list_agents(self) -> dict:
        """v3.2 数据页 Agent 卡片：返回已发现的 Agent 实例列表（含生命周期）。"""
        from .agent_locator import AgentLocator
        from .agent_install_detector import AgentInstallDetector, DEFAULT_INSTALL_PROBES
        from .source_registry import SourceRegistry
        from .agent_cleanup import AgentCleanup
        locator = AgentLocator(self.workspace)
        instances, _ledgers = locator.detect_instances()
        detector = AgentInstallDetector(self.workspace)
        cleanup = AgentCleanup(self.workspace)
        ignored_candidates = cleanup._load_uninstalled_candidates()
        reg = SourceRegistry(self.workspace)
        # root_id -> agent_instance_id 映射，统计每个 agent 的 SourceRoot 数量
        agent_root_counts: dict[str, int] = {}
        for r in reg.list_sources():
            if r.agent_instance_id:
                agent_root_counts[r.agent_instance_id] = agent_root_counts.get(r.agent_instance_id, 0) + 1
        cards = []
        residuals = []
        for inst in instances:
            install_probes = DEFAULT_INSTALL_PROBES.get(inst.product, [])
            data_paths = _private_data_paths(inst)
            assessment = detector.assess_lifecycle(inst.product, install_probes, data_paths, profile_id=inst.profile_id)
            is_ignored = assessment.candidate_id in ignored_candidates
            if is_ignored:
                assessment = detector.assess_lifecycle(inst.product, install_probes, data_paths, marked_ignored=True, profile_id=inst.profile_id)
            if assessment.lifecycle_state == "not_detected":
                continue
            found_count = _found_surface_count(inst)
            card = {
                "instance_id": inst.instance_id,
                "product": inst.product,
                "profile_id": inst.profile_id,
                "target_capability": inst.target_capability.value,
                "surface_count": len(inst.surfaces),
                "found_surface_count": found_count,
                "private_data_surface_count": _found_surface_count(inst, "private_data_evidence"),
                "shared_surface_count": _found_surface_count(inst, "shared_surface"),
                "bound_source_count": agent_root_counts.get(inst.instance_id, 0),
                "platform": inst.platform,
                "host_id": inst.host_id,
                "lifecycle_state": assessment.lifecycle_state,
                "install_confidence": assessment.install_confidence,
                "support_level": _support_level_from_capability(inst.target_capability),
                "candidate_id": assessment.candidate_id,
                "last_activity_at": assessment.last_activity_at,
            }
            if assessment.lifecycle_state in {"installed", "installed_no_data"}:
                cards.append(card)
            elif assessment.lifecycle_state in {"data_only", "uncertain", "ignored"}:
                residuals.append(card)
        return {"agents": cards, "residuals": residuals, "total": len(cards), "residual_total": len(residuals)}

    def get_residual_cleanup(self, instance_id: str = "", candidate_id: str = "") -> dict:
        """v3.2 改动包2：残留与清理页面数据。"""
        from .agent_locator import AgentLocator
        from .agent_install_detector import AgentInstallDetector, DEFAULT_INSTALL_PROBES
        from .agent_cleanup import AgentCleanup
        locator = AgentLocator(self.workspace)
        instances, _ = locator.detect_instances()
        detector = AgentInstallDetector(self.workspace)
        cleanup = AgentCleanup(self.workspace)

        target_inst = None
        target_assessment = None
        target_data_paths: list[str] = []
        for inst in instances:
            data_paths = _private_data_paths(inst)
            assessment = detector.assess_lifecycle(
                inst.product, DEFAULT_INSTALL_PROBES.get(inst.product, []), data_paths,
                profile_id=inst.profile_id,
            )
            if (instance_id and inst.instance_id == instance_id) or (candidate_id and assessment.candidate_id == candidate_id):
                target_inst = inst
                target_assessment = assessment
                target_data_paths = data_paths
                break
        if not target_inst or not target_assessment:
            target = candidate_id or instance_id
            return {"error": f"candidate not found: {target}"}

        archive_previews = []
        items = []
        for idx, dp in enumerate(target_data_paths):
            evidence_id = target_assessment.data_evidence[idx].dir_path if idx < len(target_assessment.data_evidence) else dp
            dry_run = cleanup.archive_agent_dir(
                target_assessment.candidate_id, target_inst.product, dp, dry_run=True,
                allowed_data_paths=target_data_paths,
            )
            archive_previews.append({
                "data_evidence_id": evidence_id,
                "path": dp,
                "dry_run": dry_run,
            })
            items.append({
                "data_evidence_id": evidence_id,
                "path": dp,
                "residual_type": "private_data_evidence",
                "description": "产品私有数据残留，可归档到 MemoryGuard 可恢复归档区。",
                "archive_preview": dry_run,
            })

        archives = cleanup.list_archives()

        return {
            "instance_id": target_inst.instance_id,
            "product": target_inst.product,
            "candidate_id": target_assessment.candidate_id,
            "lifecycle_state": target_assessment.lifecycle_state,
            "install_confidence": target_assessment.install_confidence,
            "install_evidence": [e.to_dict() for e in target_assessment.install_evidence],
            "data_evidence": [e.to_dict() for e in target_assessment.data_evidence],
            "archive_previews": archive_previews,
            "items": items,
            "archives": archives,
        }

    def get_agent_data(self, instance_id: str) -> dict:
        """v3.2 数据页：返回单个 Agent 的完整数据视图。

        按 scope -> project_ref -> category 三层分组：
        - user / unknown scope：直接挂 categories
        - project scope：按 project_ref 拆 projects，每个 project 下挂 categories

        如果 SourceRegistry 中没有该 Agent 的授权来源，
        回退到展示 discovered surfaces（标注"待授权"）。
        """
        from .source_registry import SourceRegistry, ScanBudget
        reg = SourceRegistry(self.workspace)
        snap = reg.scan(ScanBudget())
        # 该 Agent 的 SourceRoot 列表
        agent_roots = [r for r in reg.list_sources() if r.agent_instance_id == instance_id]
        root_map = {r.root_id: r for r in agent_roots}
        has_authorized_sources = len(agent_roots) > 0
        # 按 scope -> project_ref -> category 三层分组
        scope_map: dict[str, dict[str, dict[str, list[dict]]]] = {}
        if has_authorized_sources:
            for obj in snap.source_objects:
                root = root_map.get(obj.source_root_id)
                if not root:
                    continue
                scope = root.scope or "unknown"
                project_ref = root.project_ref or ""
                cat = root.source_category or "unknown"
                root_path = Path(root.path).expanduser().resolve()
                full_path = root_path if root.type.value == "selected_file" else (root_path / obj.relative_path).resolve()
                if not full_path.exists():
                    continue
                file_info = {
                    "root_id": obj.source_root_id,
                    "root_path": root.path,
                    "display_name": root.display_name,
                    "relative_path": obj.relative_path,
                    "media_type": obj.media_type,
                    "content_hash": obj.content_hash,
                    "read_status": obj.read_status,
                    "captured_at": obj.captured_at,
                    "scope": scope,
                    "scope_source": root.scope_source,
                    "project_ref": project_ref,
                    "discovery_object_id": root.discovery_object_id,
                    "authorized": True,
                    "exists": True,
                }
                scope_map.setdefault(scope, {})
                pr_key = project_ref or "_no_project"
                scope_map[scope].setdefault(pr_key, {})
                scope_map[scope][pr_key].setdefault(cat, [])
                scope_map[scope][pr_key][cat].append(file_info)
        # Agent 基本信息
        from .agent_locator import AgentLocator
        locator = AgentLocator(self.workspace)
        instances, _ = locator.detect_instances()
        inst = next((i for i in instances if i.instance_id == instance_id), None)
        agent_info = {
            "instance_id": instance_id,
            "product": inst.product if inst else "unknown",
            "profile_id": inst.profile_id if inst else "",
            "surfaces": inst.surfaces if inst else [],
        }
        # 如果没有授权来源，回退到 discovered surfaces
        if not has_authorized_sources and inst:
            cat_labels = {
                "native_memory": "原生记忆", "control_surface": "控制面",
                "skill_surface": "Skill 表面", "conversation_history": "会话历史",
                "runtime_evidence": "运行证据", "knowledge_source": "知识来源",
                "project_memory": "项目记忆", "unknown": "其他",
            }
            for s in inst.surfaces:
                if s.get("status") != "found":
                    continue
                resolved = s.get("resolved_path", "")
                if resolved and not Path(resolved).expanduser().exists():
                    continue
                scope = s.get("scope", "unknown")
                cat = s.get("category") or s.get("surface_role") or "unknown"
                label = cat_labels.get(cat, cat)
                file_info = {
                    "root_id": "",
                    "root_path": s.get("resolved_path", ""),
                    "display_name": s.get("surface_id", ""),
                    "relative_path": s.get("resolved_path", ""),
                    "media_type": "text/plain",
                    "content_hash": "",
                    "read_status": "discovered",
                    "captured_at": "",
                    "scope": scope,
                    "scope_source": "profile_declared",
                    "project_ref": "",
                    "discovery_object_id": s.get("surface_id", ""),
                    "authorized": False,
                }
                scope_map.setdefault(scope, {})
                scope_map[scope].setdefault("_no_project", {})
                scope_map[scope]["_no_project"].setdefault(label, [])
                scope_map[scope]["_no_project"][label].append(file_info)
        # 构建返回结构
        scopes_output = []
        for scope in ["user", "project", "unknown"]:
            if scope not in scope_map:
                continue
            projects = scope_map[scope]
            if scope == "project":
                project_list = []
                for pr_key, cat_map in projects.items():
                    project_ref = pr_key if pr_key != "_no_project" else ""
                    categories = [{"category": cat, "files": files} for cat, files in cat_map.items()]
                    project_list.append({"project_ref": project_ref or "(未归属)", "categories": categories})
                scopes_output.append({"scope": scope, "projects": project_list})
            else:
                cat_map = projects.get("_no_project", {})
                categories = [{"category": cat, "files": files} for cat, files in cat_map.items()]
                scopes_output.append({"scope": scope, "categories": categories})
        total_files = 0
        category_set = set()
        all_categories = []
        for scope_obj in scopes_output:
            if "projects" in scope_obj:
                for proj in scope_obj["projects"]:
                    for cat in proj.get("categories", []):
                        total_files += len(cat.get("files", []))
                        category_set.add(cat.get("category", ""))
                        all_categories.append(cat)
            else:
                for cat in scope_obj.get("categories", []):
                    total_files += len(cat.get("files", []))
                    category_set.add(cat.get("category", ""))
                    all_categories.append(cat)
        # 兼容旧契约：扁平 categories 字典，同名分类合并而非覆盖
        flat_categories: dict[str, list] = {}
        for cat in all_categories:
            key = cat.get("category", "unknown")
            flat_categories.setdefault(key, []).extend(cat.get("files", []))
        return {
            "agent": agent_info,
            "scopes": scopes_output,
            "categories": flat_categories,
            "total_files": total_files,
            "category_count": len(category_set),
            "has_authorized_sources": has_authorized_sources,
        }

    def enter_multi_agent_mode(self) -> dict:
        """v3.2 进入多 Agent 共享 MCP 模式。"""
        return {"mode": "multi_agent_shared_mcp", "ok": True}

    def exit_multi_agent_mode(self) -> dict:
        """v3.2 退回单 Agent 模式。"""
        return {"mode": "single_agent", "ok": True}

    # ------------------------------------------------------------------
    # BindingApi（v3.2 §8.2）：AgentBinding 与共享组
    # ------------------------------------------------------------------

    def list_bindings(self, include_inactive: bool = True) -> dict:
        from .agent_binding import AgentBindingStore
        store = AgentBindingStore(self.workspace)
        bindings = store.list_bindings(include_inactive=include_inactive)
        return {"bindings": [b.to_dict() for b in bindings], "total": len(bindings)}

    def bind_agent(self, agent_instance_id: str, share_group_id: str,
                   mcp_server_name: str = "memoryguard",
                   native_memory_mode: str = "observed",
                   redirect_paths: list[str] | None = None) -> dict:
        from .agent_binding import AgentBindingStore
        store = AgentBindingStore(self.workspace)
        binding = store.bind_agent(
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            mcp_server_name=mcp_server_name,
            native_memory_mode=native_memory_mode,
            redirect_paths=redirect_paths or [],
        )
        return {"ok": True, "binding": binding.to_dict()}

    def bind_agents_to_shared_group(self, agent_instance_ids: list[str],
                                    share_group_id: str = "",
                                    mcp_server_name: str = "memoryguard",
                                    native_memory_modes: dict[str, str] | None = None,
                                    redirect_paths: dict[str, list[str]] | None = None) -> dict:
        from .agent_binding import AgentBindingStore
        store = AgentBindingStore(self.workspace)
        return store.bind_agents_to_group(
            agent_instance_ids=agent_instance_ids,
            share_group_id=share_group_id,
            mcp_server_name=mcp_server_name,
            native_memory_modes=native_memory_modes or {},
            redirect_paths=redirect_paths or {},
        )

    def unbind_agent(self, binding_id: str) -> dict:
        from .agent_binding import AgentBindingStore
        store = AgentBindingStore(self.workspace)
        binding = store.unbind_agent(binding_id)
        if binding is None:
            return {"error": f"binding not found: {binding_id}"}
        return {"ok": True, "binding": binding.to_dict()}

    def check_binding_drift(self, binding_id: str) -> dict:
        from .agent_binding import AgentBindingStore
        store = AgentBindingStore(self.workspace)
        return store.check_drift(binding_id)

    def get_shared_group_preview(self, share_group_id: str) -> dict:
        from .agent_binding import AgentBindingStore
        store = AgentBindingStore(self.workspace)
        return store.shared_group_preview(share_group_id)

    # ------------------------------------------------------------------
    # ExternalMCPApi（v3.2 §7）：外部 MCP 检测/导入
    # ------------------------------------------------------------------

    def detect_external_mcp(self, server_id: str, descriptor: dict) -> dict:
        from .external_mcp_detector import ExternalMCPDetector
        detector = ExternalMCPDetector(self.workspace)
        return detector.detect_server(server_id, descriptor)

    def list_external_mcp_servers(self) -> dict:
        from .external_mcp_detector import ExternalMCPDetector
        detector = ExternalMCPDetector(self.workspace)
        servers = detector.list_servers()
        return {"servers": servers, "total": len(servers)}

    def preview_external_mcp_import(self, server_id: str, descriptor: dict | None = None) -> dict:
        from .external_mcp_detector import ExternalMCPDetector
        detector = ExternalMCPDetector(self.workspace)
        return detector.preview_import(server_id, descriptor)

    def import_external_mcp_entries(self, server_id: str, share_group_id: str,
                                    entries: list[dict],
                                    agent_instance_id: str = "external-mcp") -> dict:
        from .external_mcp_detector import ExternalMCPDetector
        detector = ExternalMCPDetector(self.workspace)
        return detector.import_entries(
            server_id=server_id,
            share_group_id=share_group_id,
            entries=entries,
            agent_instance_id=agent_instance_id,
        )

    # ------------------------------------------------------------------
    # MemoryApi（v3.2 §8.2）：记忆治理
    # ------------------------------------------------------------------

    def list_memory(self, status: str = "", kind: str = "",
                    share_group_id: str = "default") -> dict:
        """列出共享记忆，可按 status/kind 过滤。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        records = store.list_records(status=status or None, kind=kind or None)
        return {
            "records": [r.to_dict() for r in records],
            "total": len(records),
            "status": store.status(),
        }

    def get_memory(self, memory_id: str, share_group_id: str = "default") -> dict:
        """读取单条记忆。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        record = store.get_record(memory_id)
        if record is None:
            return {"error": f"memory not found: {memory_id}"}
        return record.to_dict()

    def search_memory(self, query: str, share_group_id: str = "default") -> dict:
        """搜索记忆。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        records = store.list_records(status="active")
        query_lower = query.lower()
        matched = [r for r in records if query_lower in r.body.lower()]
        return {"records": [r.to_dict() for r in matched], "total": len(matched)}

    def edit_memory(self, memory_id: str, body: str,
                    share_group_id: str = "default") -> dict:
        """编辑记忆正文。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        store.edit(memory_id, body)
        return {"ok": True, "memory_id": memory_id}

    def lock_memory(self, memory_id: str, share_group_id: str = "default") -> dict:
        """锁定记忆。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        store.lock(memory_id)
        return {"ok": True, "memory_id": memory_id}

    def unlock_memory(self, memory_id: str, share_group_id: str = "default") -> dict:
        """解锁记忆。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        store.unlock(memory_id)
        return {"ok": True, "memory_id": memory_id}

    def restore_memory(self, memory_id: str, share_group_id: str = "default") -> dict:
        """恢复 shadowed 记忆为 active。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        store.restore(memory_id)
        return {"ok": True, "memory_id": memory_id}

    def delete_memory(self, memory_id: str, share_group_id: str = "default") -> dict:
        """软删除记忆。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        store.delete(memory_id)
        return {"ok": True, "memory_id": memory_id}

    def rollback_memory(self, version_id: str, share_group_id: str = "default") -> dict:
        """回滚到指定版本。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        store.rollback_to_version(version_id)
        return {"ok": True, "version_id": version_id}

    def list_memory_versions(self, share_group_id: str = "default") -> dict:
        """列出所有版本。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        return {"versions": store.list_versions()}

    # ------------------------------------------------------------------
    # AutoOrganizeApi（v3.2 §8.2）：自动整理观察
    # ------------------------------------------------------------------

    def get_recent_events(self, share_group_id: str = "default") -> dict:
        """最近自动写入事件。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        events = store.list_events()
        # 最近 50 条
        recent = events[-50:] if len(events) > 50 else events
        return {"events": [e.to_dict() for e in recent], "total": len(events)}

    def get_auto_actions(self, share_group_id: str = "default") -> dict:
        """自动整理记录（从 events 的 auto_actions 聚合）。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        events = store.list_events()
        actions = []
        for e in events:
            for a in e.auto_actions:
                actions.append({**a, "event_id": e.event_id,
                               "agent": e.agent_instance_id, "created_at": e.created_at})
        return {"actions": actions, "total": len(actions)}

    def get_supersede_chain(self, memory_id: str,
                            share_group_id: str = "default") -> dict:
        """获取覆盖链。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        record = store.get_record(memory_id)
        if record is None:
            return {"error": "memory not found"}
        chain = list(record.supersedes)
        # 递归查找
        all_records = store.list_records()
        for r in all_records:
            if memory_id in r.supersedes:
                chain.append(r.memory_id)
        return {"memory_id": memory_id, "supersedes": record.supersedes, "superseded_by": chain}

    def get_conflicts(self, share_group_id: str = "default") -> dict:
        """冲突队列。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        conflicts = store.list_conflicts()
        return {"conflicts": [c.to_dict() for c in conflicts], "total": len(conflicts)}

    def get_quarantine(self, share_group_id: str = "default") -> dict:
        """隔离队列（安全修复：后端返回 masked_preview，前端永远拿不到原文）。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        entries = store.list_quarantine()
        result = []
        for e in entries:
            masked = _mask_preview(e.original_content or "")
            result.append({
                "quarantine_id": e.quarantine_id,
                "memory_id": e.memory_id,
                "masked_preview": masked,
                "reason": e.reason,
                "detected_pattern": e.detected_pattern,
                "quarantined_at": e.quarantined_at,
                "released": e.released,
            })
        return {"quarantine": result, "total": len(result)}

    def get_governance_snapshot(self, share_group_id: str = "default") -> dict:
        """只读聚合：总览状态栏 + 概念图事件卡所需的所有数据，一次调用返回。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)

        # 状态统计
        status = store.status()

        # 最近写入事件（取最新 5 条）
        events = store.list_events()
        recent_events = events[-5:] if len(events) > 5 else events
        latest_event = None
        if recent_events:
            e = recent_events[-1]
            latest_event = {
                "event_id": e.event_id,
                "agent_instance_id": e.agent_instance_id,
                "created_at": e.created_at,
                "raw_content_preview": _mask_content(e.raw_content or "", 120),
                "auto_actions": [{"action": a.get("action", a.get("type", "auto")),
                                  "target": a.get("target", "")} for a in (e.auto_actions or [])],
            }

        # 最新覆盖决策
        decisions = [d for d in store.list_decisions() if d.action == "auto_supersede"]
        latest_supersede = None
        if decisions:
            d = decisions[-1]
            target_ids = d.target_ids or []
            records = {r.memory_id: r for r in store.list_records()}
            old_id = target_ids[0] if len(target_ids) > 0 else ""
            new_id = target_ids[1] if len(target_ids) > 1 else ""
            old_rec = records.get(old_id)
            new_rec = records.get(new_id)
            latest_supersede = {
                "decision_id": d.event_id,
                "old_memory_id": old_id,
                "new_memory_id": new_id,
                "old_content_preview": _mask_content(old_rec.body if old_rec else "", 100),
                "new_content_preview": _mask_content(new_rec.body if new_rec else "", 100),
                "reason": d.reason,
                "created_at": d.created_at,
            }

        # 未解决冲突
        conflicts = [c for c in store.list_conflicts() if c.status == "unresolved"]
        first_conflict_reason = conflicts[0].reason if conflicts else ""

        # 未释放隔离项（脱敏）
        quarantine_entries = [e for e in store.list_quarantine() if not e.released]
        quarantine_summary = []
        for e in quarantine_entries[:5]:
            masked = _mask_preview(e.original_content or "")
            quarantine_summary.append({
                "quarantine_id": e.quarantine_id,
                "masked_preview": masked,
                "reason": e.reason,
                "detected_pattern": e.detected_pattern,
                "quarantined_at": e.quarantined_at,
            })

        # 可回滚版本数
        versions = store.list_versions()

        return {
            "status": {
                "active_count": status.get("active_count", 0),
                "total_count": status.get("total_count", 0),
            },
            "latest_event": latest_event,
            "latest_supersede": latest_supersede,
            "conflicts": {
                "count": len(conflicts),
                "first_reason": first_conflict_reason,
            },
            "quarantine": {
                "count": len(quarantine_entries),
                "items": quarantine_summary,
            },
            "rollback_ready": len(versions),
            "has_events": len(events) > 0,
        }

    def get_memory_status(self, share_group_id: str = "default") -> dict:
        """共享组状态统计。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        return store.status()

    def get_supersede_decisions(self, share_group_id: str = "default") -> dict:
        """获取所有 auto_supersede 决策及关联记录内容预览。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        decisions = store.list_decisions()
        records = {r.memory_id: r for r in store.list_records()}
        result = []
        for d in decisions:
            if d.action != "auto_supersede":
                continue
            target_ids = d.target_ids or []
            old_id = target_ids[0] if len(target_ids) > 0 else ""
            new_id = target_ids[1] if len(target_ids) > 1 else ""
            old_rec = records.get(old_id)
            new_rec = records.get(new_id)
            result.append({
                "decision_id": d.event_id,
                "old_memory_id": old_id,
                "new_memory_id": new_id,
                "old_content_preview": _mask_content(old_rec.body if old_rec else "", 100),
                "new_content_preview": _mask_content(new_rec.body if new_rec else "", 100),
                "reason": d.reason,
                "created_at": d.created_at,
            })
        return {"decisions": result, "total": len(result)}

    def resolve_conflict(self, group_id: str, keep_memory_id: str,
                         share_group_id: str = "default") -> dict:
        """解决冲突：保留指定记忆，其他成员软删除。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        conflicts = store.list_conflicts()
        group = next((c for c in conflicts if c.group_id == group_id), None)
        if group is None:
            return {"error": "conflict group not found"}
        for mid in group.member_ids:
            if mid != keep_memory_id:
                store.delete(mid)
        store.restore(keep_memory_id)
        return {"ok": True, "kept": keep_memory_id}

    def release_quarantine(self, quarantine_id: str,
                           share_group_id: str = "default") -> dict:
        """释放隔离：恢复记忆为 active。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        entries = store.list_quarantine()
        entry = next((e for e in entries if e.quarantine_id == quarantine_id), None)
        if entry is None:
            return {"error": "quarantine entry not found"}
        store.restore(entry.memory_id)
        return {"ok": True, "memory_id": entry.memory_id}

    def delete_quarantine(self, quarantine_id: str,
                          share_group_id: str = "default") -> dict:
        """永久删除隔离记忆。"""
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        entries = store.list_quarantine()
        entry = next((e for e in entries if e.quarantine_id == quarantine_id), None)
        if entry is None:
            return {"error": "quarantine entry not found"}
        store.delete(entry.memory_id)
        return {"ok": True, "memory_id": entry.memory_id}

    def _preview_source_impl(self, path: str, source_type: str = "selected_directory") -> dict:
        """v3.1 §1.1 P0：添加来源前必须先 preview。type 别名映射防止 ValueError。"""
        from .source_registry import SourceRegistry
        from .schema_v3 import SourceRootType
        type_alias = {
            "directory": SourceRootType.SELECTED_DIRECTORY,
            "selected_directory": SourceRootType.SELECTED_DIRECTORY,
            "file": SourceRootType.SELECTED_FILE,
            "selected_file": SourceRootType.SELECTED_FILE,
            "obsidian": SourceRootType.OBSIDIAN_VAULT,
            "obsidian_vault": SourceRootType.OBSIDIAN_VAULT,
        }
        enum_type = type_alias.get(source_type, SourceRootType.SELECTED_DIRECTORY)
        reg = SourceRegistry(self.workspace)
        return reg.preview(path, enum_type)

    def _add_source_impl(self, path: str, source_type: str = "selected_directory",
                         display_name: str = "", confirmed: bool = False) -> dict:
        """v3.1 §1.1 P0：type 别名映射 + confirmed 强制。"""
        if not confirmed:
            return {"error": "需要确认才能添加来源"}
        from .source_registry import SourceRegistry
        from .schema_v3 import SourceRootType
        type_alias = {
            "directory": SourceRootType.SELECTED_DIRECTORY,
            "selected_directory": SourceRootType.SELECTED_DIRECTORY,
            "file": SourceRootType.SELECTED_FILE,
            "selected_file": SourceRootType.SELECTED_FILE,
            "obsidian": SourceRootType.OBSIDIAN_VAULT,
            "obsidian_vault": SourceRootType.OBSIDIAN_VAULT,
        }
        enum_type = type_alias.get(source_type, SourceRootType.SELECTED_DIRECTORY)
        reg = SourceRegistry(self.workspace)
        root = reg.add(path, enum_type, display_name=display_name)
        return {"ok": True, "root_id": root.root_id}

    def preview_source(self, path: str, source_type: str = "selected_directory") -> dict:
        """v3.1 §1.1 P0：添加来源前必须先 preview，展示预计范围、文件数、排除项。"""
        return self._preview_source_impl(path, source_type)

    def add_source(self, path: str, source_type: str = "selected_directory",
                   display_name: str = "", confirmed: bool = False) -> dict:
        """v3.1 §1.1 P0：type 别名映射 + confirmed 强制。"""
        return self._add_source_impl(path, source_type, display_name, confirmed)

    def remove_source(self, source_id: str, confirmed: bool = False) -> dict:
        if not confirmed:
            return {"error": "需要确认才能删除来源"}
        from .source_registry import SourceRegistry
        reg = SourceRegistry(self.workspace)
        ok = reg.remove(source_id)
        return {"ok": ok}

    def scan_sources(self) -> dict:
        """执行扫描，返回快照 + 覆盖率。"""
        from .source_registry import SourceRegistry, ScanBudget
        reg = SourceRegistry(self.workspace)
        snap = reg.scan(ScanBudget())
        cov = snap.coverage.counts()
        cov["coverage_status"] = snap.coverage.status().value
        return {
            "snapshot_id": snap.snapshot_id,
            "created_at": snap.created_at,
            "source_object_count": len(snap.source_objects),
            "coverage": cov,
        }

    def get_raw_memory(self) -> dict:
        """返回原始记忆：按 agent（SourceRoot display_name）分组展示所有 SourceObject。

        spec §7.2 SourceApi：用户能直接看到每个 agent 的原始记忆文件，不做任何萃取。
        """
        from .source_registry import SourceRegistry, ScanBudget
        reg = SourceRegistry(self.workspace)
        snap = reg.scan(ScanBudget())
        # root_id -> display_name 映射
        root_map = {r.root_id: r for r in reg.list_sources()}
        groups: dict[str, dict] = {}
        for obj in snap.source_objects:
            root = root_map.get(obj.source_root_id)
            agent_name = root.display_name if root else obj.source_root_id
            group = groups.setdefault(agent_name, {
                "agent": agent_name,
                "root_id": obj.source_root_id,
                "root_path": root.path if root else "",
                "scope": root.scope if root else "unknown",
                "files": [],
            })
            group["files"].append({
                "relative_path": obj.relative_path,
                "media_type": obj.media_type,
                "content_hash": obj.content_hash,
                "read_status": obj.read_status,
                "captured_at": obj.captured_at,
            })
        # 加入覆盖率统计
        cov = snap.coverage.counts()
        cov["coverage_status"] = snap.coverage.status().value
        return {
            "snapshot_id": snap.snapshot_id,
            "created_at": snap.created_at,
            "groups": list(groups.values()),
            "group_count": len(groups),
            "total_files": len(snap.source_objects),
            "coverage": cov,
        }

    def get_source_file_content(self, root_id: str, relative_path: str) -> dict:
        """读取某个原始记忆文件的完整内容（只读，用于 UI 查看）。

        v3.1 §1.5 P0：必须做 canonical containment 和符号链接防护，
        防止 ../ 或越界 symlink 读取授权根之外的文件。
        """
        from .source_registry import SourceRegistry
        from pathlib import Path
        import os
        reg = SourceRegistry(self.workspace)
        root = reg.get(root_id)
        if root is None:
            return {"error": "source root not found"}
        root_path = Path(root.path).resolve()
        if root.type.value == "selected_file":
            full = root_path
            containment_root = root_path.parent
        else:
            containment_root = root_path
            full = (root_path / relative_path).resolve()
            try:
                full.relative_to(containment_root)
            except ValueError:
                return {"error": "path escapes source root (containment violation)"}
        if not full.exists() or not full.is_file():
            return {"error": "file not found"}
        # 符号链接防护：不允许 symlink 指向 root 之外
        if full.is_symlink():
            target = Path(os.readlink(full)).resolve() if os.readlink(full) else None
            if target is None:
                return {"error": "symlink target unreadable"}
            try:
                target.relative_to(containment_root)
            except ValueError:
                return {"error": "symlink escapes source root"}
        # 文件大小限制（防止误读大文件）
        try:
            stat = full.stat()
            if stat.st_size > 5 * 1024 * 1024:  # 5MB
                return {"error": f"file too large ({stat.st_size} bytes, max 5MB)"}
        except OSError as e:
            return {"error": f"stat failed: {e}"}
        try:
            content = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {"error": f"read failed: {e}"}
        return {
            "root_id": root_id,
            "relative_path": relative_path,
            "display_name": root.display_name,
            "content": content,
            "size": len(content),
        }

    def _extract_source_file_memories(self, root_id: str, relative_path: str,
                                     share_group_id: str = "default",
                                     agent_instance_id: str = "document-extractor",
                                     max_segments: int = 20) -> dict:
        """[PRIVATE] 旧直接写入方法，已由 extract_preview + accept_candidates 两步流程替代。

        保留向后兼容，新代码应使用 extract_preview（只读预览）+ accept_candidates（确认写入）。
        """
        file_result = self.get_source_file_content(root_id, relative_path)
        if "error" in file_result:
            return file_result
        from .auto_organizer import AutoOrganizer
        from .schema_v3 import MemoryEvent, stable_hash, _now_iso
        from .shared_memory_store import SharedMemoryStore
        content = file_result.get("content", "")
        segments = self._extract_memory_segments(content, max_segments=max_segments)
        store = SharedMemoryStore(self.workspace, share_group_id)
        organizer = AutoOrganizer(self.workspace, share_group_id)
        extracted = []
        for idx, segment in enumerate(segments):
            event = MemoryEvent(
                event_id=stable_hash("doc_extract_event", root_id, relative_path, str(idx), segment, _now_iso()),
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
                raw_content=segment,
                metadata={
                    "source_root_id": root_id,
                    "relative_path": relative_path,
                    "extraction_origin": "source_file",
                },
                auto_actions=[],
                created_at=_now_iso(),
            )
            store.append_event(event)
            record, actions = organizer.organize(event)
            event.auto_actions = actions
            store.update_event(event)
            extracted.append({
                "memory_id": record.memory_id,
                "body": record.body,
                "kind": record.kind.value,
                "status": record.status.value,
                "auto_actions": actions,
            })
        return {
            "ok": True,
            "root_id": root_id,
            "relative_path": relative_path,
            "share_group_id": share_group_id,
            "document_promoted_as_memory": False,
            "extracted": extracted,
            "total": len(extracted),
        }

    # ------------------------------------------------------------------
    # §8.5 两步萃取流程：extract_preview（只读）-> accept_candidates（写入）
    # ------------------------------------------------------------------

    def extract_preview(self, root_id: str, relative_path: str,
                        max_segments: int = 20) -> dict:
        """§8.5 步骤 1：萃取预览（只读，不写入 SharedMemoryStore）。

        提取文档片段、分类、风险扫描，返回候选列表。
        候选缓存到 .memoryguard/staging/extract-{hash}.json 供 accept_candidates 引用。
        """
        import json
        import time
        from pathlib import Path
        from .auto_organizer import AutoOrganizer
        from .schema_v3 import stable_hash, _now_iso

        file_result = self.get_source_file_content(root_id, relative_path)
        if "error" in file_result:
            return file_result
        content = file_result.get("content", "")
        segments = self._extract_memory_segments(content, max_segments=max_segments)

        # 用 AutoOrganizer 的只读方法做分类 + 风险扫描（不调 organize，避免写入）
        organizer = AutoOrganizer(self.workspace, "default")
        candidates = []
        for idx, segment in enumerate(segments):
            kind = organizer._classify(segment)
            confidence = organizer._confidence(segment, kind)
            secret = organizer._detect_secret(segment)
            if secret:
                risk_level = "high"
            elif confidence < 0.45:
                risk_level = "medium"
            else:
                risk_level = "low"
            # candidate_id 稳定：同一文档同一片段每次萃取 ID 相同（不含时间戳）
            candidate_id = stable_hash("candidate", root_id, relative_path, str(idx), segment)
            candidates.append({
                "candidate_id": candidate_id,
                "body": segment,
                "kind": kind.value,
                "risk_level": risk_level,
                "preview": segment[:200],
            })

        # 生成 extract_id 并缓存到 staging 文件
        extract_id = stable_hash("extract", root_id, relative_path, _now_iso())
        staging_dir = Path(self.workspace) / ".memoryguard" / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        # 清理过期 staging 文件（>24h）
        cutoff = time.time() - 24 * 3600
        for f in staging_dir.glob("extract-*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue

        staging_file = staging_dir / f"extract-{extract_id}.json"
        staging_data = {
            "extract_id": extract_id,
            "root_id": root_id,
            "relative_path": relative_path,
            "created_at": _now_iso(),
            "candidates": candidates,
        }
        staging_file.write_text(json.dumps(staging_data, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "ok": True,
            "extract_id": extract_id,
            "root_id": root_id,
            "relative_path": relative_path,
            "candidates": candidates,
            "total": len(candidates),
        }

    def accept_candidates(self, extract_id: str, candidate_ids: list[str],
                          share_group_id: str = "default",
                          agent_instance_id: str = "document-extractor") -> dict:
        """§8.5 步骤 2：接受候选（写入 SharedMemoryStore，需用户确认）。

        读取 staging 文件，只接受 candidate_ids 中的候选，写入后删除 staging 文件。
        """
        import json
        from pathlib import Path
        from .auto_organizer import AutoOrganizer
        from .schema_v3 import MemoryEvent, DecisionEvent, stable_hash, _now_iso
        from .shared_memory_store import SharedMemoryStore

        staging_dir = Path(self.workspace) / ".memoryguard" / "staging"
        staging_file = staging_dir / f"extract-{extract_id}.json"
        if not staging_file.exists():
            return {"error": f"staging file not found (extract_id={extract_id}); it may have expired"}

        staging_data = json.loads(staging_file.read_text(encoding="utf-8"))
        all_candidates = staging_data.get("candidates", [])
        candidate_map = {c["candidate_id"]: c for c in all_candidates}
        accepted = [candidate_map[cid] for cid in candidate_ids if cid in candidate_map]
        if not accepted:
            return {"error": "no matching candidates found in staging file"}

        root_id = staging_data.get("root_id", "")
        relative_path = staging_data.get("relative_path", "")

        store = SharedMemoryStore(self.workspace, share_group_id)
        organizer = AutoOrganizer(self.workspace, share_group_id)
        results = []
        written_ids = []
        for candidate in accepted:
            segment = candidate["body"]
            event = MemoryEvent(
                event_id=stable_hash("doc_extract_event", root_id, relative_path,
                                     candidate["candidate_id"], segment, _now_iso()),
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
                raw_content=segment,
                metadata={
                    "source_root_id": root_id,
                    "relative_path": relative_path,
                    "extraction_origin": "source_file",
                    "candidate_id": candidate["candidate_id"],
                },
                auto_actions=[],
                created_at=_now_iso(),
            )
            store.append_event(event)
            record, actions = organizer.organize(event)
            event.auto_actions = actions
            store.update_event(event)
            written_ids.append(record.memory_id)
            results.append({
                "memory_id": record.memory_id,
                "status": record.status.value,
                "kind": record.kind.value,
                "auto_actions": actions,
            })

        # 记录 DecisionEvent
        decision = DecisionEvent(
            event_id=stable_hash("accept_extract", extract_id, _now_iso()),
            actor="user",
            action="accept_extract",
            target_ids=written_ids,
            reason="user confirmed",
            created_at=_now_iso(),
        )
        store.append_decision(decision)

        # 写入完成后删除 staging 文件
        try:
            staging_file.unlink()
        except OSError:
            pass

        return {
            "ok": True,
            "extract_id": extract_id,
            "share_group_id": share_group_id,
            "accepted": results,
            "total": len(results),
        }

    def _extract_memory_segments(self, content: str, max_segments: int = 20) -> list[str]:
        import re
        blocks = []
        current: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                if current:
                    blocks.append("\n".join(current).strip())
                    current = []
                continue
            if stripped.startswith("#") and current:
                blocks.append("\n".join(current).strip())
                current = [stripped]
            else:
                current.append(stripped)
        if current:
            blocks.append("\n".join(current).strip())
        candidates = []
        signal = re.compile(r"偏好|喜欢|习惯|步骤|流程|项目|事实|规则|必须|不要|应该|prefer|like|procedure|step|project|must|should", re.I)
        for block in blocks:
            clean = block.strip()
            if len(clean) < 8:
                continue
            if signal.search(clean) or clean.startswith("#"):
                candidates.append(clean[:1200])
            if len(candidates) >= max_segments:
                break
        if not candidates and content.strip():
            candidates.append(content.strip()[:1200])
        return candidates

    # ------------------------------------------------------------------
    # ImportApi（spec §7.2）
    # ------------------------------------------------------------------

    def preview_import(self, path: str) -> dict:
        from .adapters import GenericImportAdapter, ChatGPTImportAdapter
        from pathlib import Path
        bundle = Path(path)
        if not bundle.exists():
            return {"error": "bundle not found"}
        for ad in (ChatGPTImportAdapter(), GenericImportAdapter()):
            d = ad.detect(bundle)
            if d.supported:
                inv = ad.inventory(bundle)
                return {"provider": d.provider, "confidence": d.confidence,
                        "notes": d.notes, "inventory": inv}
        return {"error": "unsupported bundle format"}

    def create_import(self, path: str, confirmed: bool = False) -> dict:
        if not confirmed:
            return {"error": "需要确认才能创建导入"}
        from .adapters import GenericImportAdapter, ChatGPTImportAdapter
        from pathlib import Path
        bundle = Path(path)
        if not bundle.exists():
            return {"error": "bundle not found"}
        for ad in (ChatGPTImportAdapter(), GenericImportAdapter()):
            d = ad.detect(bundle)
            if d.supported:
                convs = ad.parse(bundle)
                records = ad.normalize(convs)
                return {"provider": d.provider,
                        "conversation_count": len(convs),
                        "extract_candidate_count": len(records),
                        "memory_record_count": 0,
                        "written_to_ir": False}
        return {"error": "unsupported bundle format"}

    # ------------------------------------------------------------------
    # MemoryApi（spec §7.2）
    # ------------------------------------------------------------------

    def get_memory_ir(self) -> dict:
        """读取当前 Memory IR。"""
        from .memory_ir import MemoryNormalizer
        norm = MemoryNormalizer(self.workspace)
        ir = norm.load()
        if ir is None:
            return {"empty": True, "reason": "not_built"}
        return {
            "records": [r.to_dict() for r in ir.records],
            "duplicate_groups": [g.to_dict() for g in ir.duplicate_groups],
            "snapshot_id": ir.snapshot_id,
            "record_count": len(ir.records),
        }

    def create_build_plan(self, target_path: str = "") -> dict:
        from .adapters import GenericMarkdownTarget
        from .release_manager import ReleaseManager
        from .source_registry import ScanBudget
        from pathlib import Path
        rm = ReleaseManager(self.workspace)
        target = GenericMarkdownTarget()
        tp = Path(target_path) if target_path else Path(self.workspace) / ".memoryguard" / "memory-target"
        snap, ir = rm.scan_and_normalize(ScanBudget())
        plan = rm.create_build_plan(ir, target, tp)
        return plan.to_dict()

    def apply_build(self, plan_id: str, confirmed: bool = False,
                    target_path: str = "") -> dict:
        if not confirmed:
            return {"error": "需要确认才能应用构建"}
        from .adapters import GenericMarkdownTarget
        from .release_manager import ReleaseManager
        from pathlib import Path
        rm = ReleaseManager(self.workspace)
        target = GenericMarkdownTarget()
        tp = Path(target_path) if target_path else Path(self.workspace) / ".memoryguard" / "memory-target"
        release = rm.apply_build(plan_id, target, tp, approval=True)
        return release.to_dict()

    def verify_release(self, release_id: str, target_path: str = "") -> dict:
        from .adapters import GenericMarkdownTarget
        from .release_manager import ReleaseManager
        from pathlib import Path
        import json
        rm = ReleaseManager(self.workspace)
        target = GenericMarkdownTarget()
        tp = Path(target_path) if target_path else Path(self.workspace) / ".memoryguard" / "memory-target"
        # 读 manifest
        change_path = Path(self.workspace) / ".memoryguard" / "changes" / f"{release_id}.json"
        if not change_path.exists():
            return {"error": "release not found"}
        data = json.loads(change_path.read_text(encoding="utf-8"))
        build_id = data.get("build_id", "")
        # 找 plan
        plans_dir = Path(self.workspace) / ".memoryguard" / "plans"
        manifest = None
        for pf in plans_dir.glob("*.json"):
            pd = json.loads(pf.read_text(encoding="utf-8"))
            if pd.get("manifest", {}).get("build_id") == build_id:
                manifest = pd["manifest"]
                break
        if manifest is None:
            return {"error": "manifest not found"}
        from .schema_v3 import BuildManifest
        mm = BuildManifest(
            build_id=manifest["build_id"], release_hash=manifest.get("release_hash", ""),
            target_profile=manifest.get("target_profile", ""),
        )
        return rm.verify_release(release_id, target, tp, mm)

    def rollback_release(self, release_id: str, confirmed: bool = False,
                         target_path: str = "") -> dict:
        if not confirmed:
            return {"error": "需要确认才能回滚"}
        from .adapters import GenericMarkdownTarget
        from .release_manager import ReleaseManager
        from pathlib import Path
        rm = ReleaseManager(self.workspace)
        target = GenericMarkdownTarget()
        tp = Path(target_path) if target_path else Path(self.workspace) / ".memoryguard" / "memory-target"
        rb = rm.rollback_release(release_id, target, tp)
        return rb.to_dict()

    def list_releases(self) -> dict:
        from .release_manager import ReleaseManager
        rm = ReleaseManager(self.workspace)
        return {"releases": rm.list_releases()}

    def list_history(self) -> dict:
        """v3.1 §8.4：统一历史时间线（rule_change + memory_release + warnings）。

        损坏 JSON 不会让页面崩溃，会在 warnings 中显示。
        """
        from .change_history import list_change_history
        from pathlib import Path
        return list_change_history(Path(self.workspace))

    # ------------------------------------------------------------------
    # 规则级修复闭环（v2.1 保留）
    # ------------------------------------------------------------------

    def get_audit(self) -> dict:
        """返回当前审计报告（dict）。若无则先跑一次。"""
        if self._report is None:
            self._report = self.run_audit()
        return self._report

    def run_audit(self) -> dict:
        """执行只读扫描 + 规则引擎，返回 Report dict。"""
        from .cli import run_audit

        report = run_audit(Path(self.workspace))
        self._report = report.to_dict()
        return self._report

    def generate_plan(self, finding_id: str) -> dict:
        """为指定 Finding 生成修复 Plan。"""
        import json
        from .cli import PLANS_DIR, _generate_patch, _load_report
        from .schema import Plan, RiskLevel, stable_id

        report = _load_report(Path(self.workspace))
        if report is None:
            return {"error": "no report found"}
        finding = next((f for f in report.findings if f.id == finding_id), None)
        if finding is None:
            return {"error": f"finding not found: {finding_id}"}
        if not finding.fixable:
            return {"error": "finding not fixable"}

        from .schema import Patch, sha256_file

        patch = _generate_patch(finding)
        if patch is None:
            return {"error": "could not generate patch"}

        risk = RiskLevel.HIGH if finding.severity.value in ("high", "critical") else RiskLevel.LOW
        plan = Plan(
            plan_id=stable_id("plan", finding_id),
            finding_ids=[finding_id],
            intent=f"fix {finding.rule_id}",
            risk_level=risk,
            patches=[patch],
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            preconditions=[f"file hash matches: {patch.before_hash}"],
            verification=[finding.verification],
            requires_approval=True,
        )
        # 写 plan 文件
        ws = Path(self.workspace)
        (ws / PLANS_DIR).mkdir(parents=True, exist_ok=True)
        plan_path = ws / PLANS_DIR / f"{plan.plan_id}.json"
        plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return {"plan": plan.to_dict()}

    def apply_plan(self, plan_id: str) -> dict:
        """应用 Plan: 备份 + 补丁 + 重扫验证。"""
        import json
        from .cli import (
            PLANS_DIR, CHANGES_DIR, BACKUPS_DIR, REPORTS_DIR, run_audit,
        )
        from .schema import (
            Change, ChangeStatus, Patch, Plan, RiskLevel, now_iso,
            sha256_file, stable_id,
        )

        ws = Path(self.workspace)
        plan_path = ws / PLANS_DIR / f"{plan_id}.json"
        if not plan_path.exists():
            return {"error": f"plan not found: {plan_id}"}
        plan_dict = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = Plan(
            plan_id=plan_dict["plan_id"],
            finding_ids=plan_dict["finding_ids"],
            intent=plan_dict["intent"],
            risk_level=RiskLevel(plan_dict["risk_level"]),
            patches=[Patch(**p) for p in plan_dict["patches"]],
            created_at=plan_dict.get("created_at", ""),
            preconditions=plan_dict.get("preconditions", []),
            verification=plan_dict.get("verification", []),
            requires_approval=plan_dict.get("requires_approval", True),
        )

        # 校验 hash
        for patch in plan.patches:
            current = sha256_file(Path(patch.path))
            if current != patch.before_hash:
                return {"error": f"file changed: {patch.path}"}

        # 备份 + 应用
        (ws / BACKUPS_DIR).mkdir(parents=True, exist_ok=True)
        (ws / CHANGES_DIR).mkdir(parents=True, exist_ok=True)
        backup_paths, changed_paths = [], []
        for patch in plan.patches:
            src = Path(patch.path)
            backup = ws / BACKUPS_DIR / f"{src.name}.{stable_id('bak', patch.path)[:8]}"
            backup.write_bytes(src.read_bytes())
            backup_paths.append(str(backup))
            if patch.operation == "delete":
                src.unlink()
            elif patch.operation == "insert":
                content = src.read_text(encoding="utf-8")
                src.write_text(patch.diff.lstrip("+ ") + "\n" + content, encoding="utf-8")
            elif patch.operation == "replace":
                src.write_text(patch.diff, encoding="utf-8")
            changed_paths.append(patch.path)

        # 重扫验证
        verify_report = run_audit(ws)
        remaining = [f for f in verify_report.findings if f.id in plan.finding_ids]
        status = ChangeStatus.VERIFIED if not remaining else ChangeStatus.FAILED

        change = Change(
            change_id=stable_id("change", plan.plan_id),
            plan_id=plan.plan_id,
            applied_at=now_iso(),
            backup_paths=backup_paths,
            changed_paths=changed_paths,
            status=status,
        )
        (ws / CHANGES_DIR / f"{change.change_id}.json").write_text(
            json.dumps(change.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 刷新 report
        self._report = verify_report.to_dict()
        return {"change": change.to_dict()}

    def undo_change(self, change_id: str) -> dict:
        """撤销 Change: 从备份恢复 + 重扫。"""
        import json
        from .cli import CHANGES_DIR, run_audit
        from .schema import ChangeStatus

        ws = Path(self.workspace)
        change_path = ws / CHANGES_DIR / f"{change_id}.json"
        if not change_path.exists():
            return {"error": f"change not found: {change_id}"}
        change_dict = json.loads(change_path.read_text(encoding="utf-8"))
        for backup_path, changed_path in zip(change_dict["backup_paths"], change_dict["changed_paths"]):
            Path(changed_path).write_bytes(Path(backup_path).read_bytes())
        change_dict["status"] = ChangeStatus.UNDONE.value
        change_path.write_text(json.dumps(change_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        # 刷新 report
        report = run_audit(ws)
        self._report = report.to_dict()
        return {"ok": True}


class SafeBridgeApi:
    """受限桥接 API：pywebview js_api 的安全代理。

    只暴露两个真实方法（pywebview 可枚举）：
    - call_readonly(method, args): 只读方法，直接转发
    - request_mutation(method, args): 变更方法，沙箱模式下走请求队列

    前端统一通过这两个方法调用，不再直接访问 method 属性。
    """

    def __init__(self, workspace: str):
        self._inner = GovernanceApi(workspace)
        self._workspace = workspace

    def _set_window(self, window) -> None:
        self._inner._set_window(window)

    def call_readonly(self, method: str, args: list | None = None) -> dict:
        """调用只读方法。

        严格校验方法是否在只读注册表中，拒绝变更方法。
        """
        from .security import is_readonly_method

        if not is_readonly_method(method):
            return {"error": f"not a readonly method: {method}"}

        fn = getattr(self._inner, method, None)
        if not callable(fn):
            return {"error": f"method not found: {method}"}

        try:
            result = fn(*(args or []))
            return result if result is not None else {}
        except Exception as e:
            return {"error": str(e)}

    def request_mutation(self, method: str, args: list | None = None) -> dict:
        """调用变更方法。

        沙箱模式下走请求队列；非沙箱模式下注入 confirmed=True 后直接执行。
        """
        from .security import is_mutation_method, detect_sandbox_mode

        if not is_mutation_method(method):
            return {"error": f"not a mutation method: {method}"}

        # 沙箱模式：走请求队列，返回 deferred 标记
        if detect_sandbox_mode():
            result = self._inner.submit_request(method, args or [])
            return {
                "ok": True,
                "deferred": True,
                "request": result.get("request", result),
                "message": "请求已提交，等待桌面执行器确认",
            }

        # 非沙箱：注入 confirmed=True 后直接执行
        fn = getattr(self._inner, method, None)
        if not callable(fn):
            return {"error": f"method not found: {method}"}

        try:
            import inspect
            sig = inspect.signature(fn)
            params = list(sig.parameters.keys())
            call_args = list(args or [])
            if "confirmed" in params:
                cidx = params.index("confirmed")
                while len(call_args) <= cidx:
                    call_args.append(sig.parameters[params[len(call_args)]].default)
                call_args[cidx] = True
            result = fn(*call_args)
            return result if result is not None else {}
        except Exception as e:
            return {"error": str(e)}

    def get_api_method_registry(self) -> dict:
        """返回 API 方法注册表，供前端动态加载。"""
        return self._inner.get_api_method_registry()

    def get_sandbox_status(self) -> dict:
        """返回沙箱状态。"""
        return self._inner.get_sandbox_status()

    def pick_path(self, for_files: bool = False) -> dict:
        """系统目录/文件选择器。"""
        return self._inner.pick_path(for_files)


def open_interactive_window(workspace: str, title: str = "MemoryGuard 治理面板") -> int:
    """打开交互式治理面板（非平面报告）。

    通过 pywebview js_api 暴露 SafeBridgeApi（受限桥接 API）：
    - 只读方法直接转发到 GovernanceApi
    - 变更方法在沙箱模式下走请求队列
    - 不直接暴露完整 GovernanceApi 对象
    返回退出码：0 成功，3 pywebview 不可用。
    """
    if not has_native_gui():
        return 3
    import shutil
    import webview
    from .interactive import render_interactive_html

    api = SafeBridgeApi(workspace)
    html = render_interactive_html()

    # 写 HTML + cytoscape.js 到 .memoryguard/ui/ 目录，用 url= 加载本地文件
    ui_dir = Path(workspace) / ".memoryguard" / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    # 复制 cytoscape.js
    static_src = Path(__file__).parent / "static" / "cytoscape.min.js"
    if static_src.exists():
        shutil.copy2(static_src, ui_dir / "cytoscape.min.js")
    # 写 HTML
    html_path = ui_dir / "index.html"
    html_path.write_text(html, encoding="utf-8")

    window = webview.create_window(
        title, url=str(html_path), js_api=api,
        width=1440, height=900, min_size=(800, 600),
    )
    # v3.1：注入 window 引用，使 pick_path 能调用 create_file_dialog
    api._set_window(window)
    webview.start()
    return 0
