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
import hashlib
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path


class BuildCancelled(Exception):
    """用户取消构建投影。"""


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


def _window_icon_path() -> Path | None:
    """Return the packaged raster icon derived from the UI brand orb."""
    suffix = ".ico" if sys.platform == "win32" else ".png"
    path = Path(__file__).parent / "static" / f"memoryguard-icon{suffix}"
    return path if path.is_file() else None


def _set_windows_app_user_model_id() -> bool:
    """Give hosted pythonw windows an independent Windows taskbar identity."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return (
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MemoryGuard.Desktop",
            )
            == 0
        )
    except (AttributeError, OSError):
        return False


def _start_webview(webview) -> None:
    """Start pywebview with the MemoryGuard window/taskbar icon."""
    icon_path = _window_icon_path()
    if icon_path:
        webview.start(icon=str(icon_path))
    else:
        webview.start()


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

    _set_windows_app_user_model_id()
    # pywebview 加载 HTML 字符串，无需临时文件、无需 HTTP server
    webview.create_window(
        title=title,
        html=html_content,
        width=1440,
        height=900,
        min_size=(800, 600),
    )
    _start_webview(webview)
    return 0


# ---------------------------------------------------------------------------
# 2. localhost 浏览器窗口（降级路径）
# ---------------------------------------------------------------------------

import json as _json
from urllib.parse import urlparse, unquote
from .interactive import render_interactive_html


def _find_free_port() -> int:
    """绑定 127.0.0.1 随机端口（spec §1.3 安全要求）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _auto_enrich_tasks(workspace: str | Path, tasks: list[dict],
                       on_progress=None, *, allow_host_cli: bool = True,
                       llm_agent: str = "", llm_cli: str = "") -> list[dict]:
    """自动整理 pending 任务,三层降级:

    1. Provider API (已配 API key 时)
    2. 宿主 Agent CLI (codex exec / claude --print,不需要 API key)
    3. Heuristic (兜底)

    返回 apply_results 所需的 results 列表。
    重构/共享组构建默认 allow_host_cli=True（质量依赖 LLM）。
    """
    from .semantic_enricher import get_enricher
    from .provider_api import get_provider

    def _note(msg: str) -> None:
        if callable(on_progress):
            try:
                on_progress(msg)
            except Exception:
                pass

    if not tasks:
        return []

    if allow_host_cli:
        provider = get_provider(workspace)
        if provider is not None and not (llm_agent and llm_cli):
            _note(f"正在用 Provider API 整理 {len(tasks)} 条记忆…")
            enricher = get_enricher("model", workspace)
            return _enrich_with_enricher(tasks, enricher, source="provider")

        # 第 2 层:宿主 Agent CLI (不需要 API key)
        try:
            from .host_agent_backend import batch_enrich_via_cli, detect_available_agents
            agent = llm_agent
            cli = llm_cli
            if not agent or not cli:
                agents = detect_available_agents()
                if agents:
                    agent = agents[0]["agent"]
                    cli = agents[0]["cli"]
            if agent and cli:
                label = agent
                _note(f"正在用 {label} 整理 {len(tasks)} 条记忆…")
                results = batch_enrich_via_cli(
                    tasks, agent=agent, cli_path=cli, workspace=workspace,
                )
                if results:
                    return results
        except Exception as e:
            import logging
            logging.getLogger("memoryguard").warning("host CLI enrich failed: %s", e)

    # 第 3 层:Heuristic 兜底
    _note(f"正在用本地规则整理 {len(tasks)} 条记忆…")
    enricher = get_enricher("heuristic")
    return _enrich_with_enricher(tasks, enricher, source="heuristic")


def _enrich_pending_during_build(
    workspace: str | Path,
    *,
    agent_instance_id: str = "",
    share_group_id: str = "",
    llm_agent: str = "",
    llm_cli: str = "",
    progress=None,
    enrich_mode: str = "auto",
) -> dict:
    """构建路径：对当前 scope 的 pending 做整理并 apply（不自动二次建图）。

    enrich_mode:
      - auto: Provider → 本机 CLI → heuristic（GUI/无 Skill 时）
      - host: 不调 CLI；把 pending 留给宿主 Skill/MCP 对话模型（主路径）
      - cli: 仅 Provider/CLI，不强制 heuristic apply
      - heuristic: 仅本地规则
    """
    from .host_enrichment import list_pending, apply_results, get_status

    mode = (enrich_mode or "auto").strip().lower()
    if mode not in {"auto", "host", "cli", "heuristic"}:
        mode = "auto"

    tasks = list_pending(
        workspace,
        limit=500,
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
    )
    if not tasks:
        st = get_status(
            workspace,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
        )
        return {
            "applied": 0,
            "rejected": 0,
            "pending_before": 0,
            "pending_count": st.get("pending", 0),
            "engine": "host_deferred" if mode == "host" else "none",
            "enrich_mode": mode,
            "pending_tasks": [],
            # host 模式即使无 pending：也要让 UI 知道「没调模型」
            "host_action_required": mode == "host",
            "hint": (
                "宿主 Skill 模式：GUI 未调用对话模型。"
                if mode == "host" else ""
            ),
        }

    def _note(msg: str) -> None:
        if callable(progress):
            try:
                progress("enrich", msg, None)
            except Exception:
                pass

    # Skill/MCP 主路径：宿主对话模型就是 LLM，构建进程不得抢走整理权
    if mode == "host":
        _note(f"已入队 {len(tasks)} 条，等待宿主 Skill 在对话中整理…")
        return {
            "applied": 0,
            "rejected": 0,
            "pending_before": len(tasks),
            "pending_count": len(tasks),
            "engine": "host_deferred",
            "enrich_mode": "host",
            "pending_tasks": tasks,
            "host_action_required": True,
            "hint": "宿主 Agent 须立即 classify+translate 后调用 apply_enrichments",
        }

    allow_cli = mode in {"auto", "cli"}
    if mode == "heuristic":
        allow_cli = False

    results = _auto_enrich_tasks(
        workspace,
        tasks,
        on_progress=_note,
        allow_host_cli=allow_cli,
        llm_agent=llm_agent,
        llm_cli=llm_cli,
    )
    engine = "heuristic"
    if results:
        sources = {r.get("source") for r in results}
        if "provider" in sources:
            engine = "provider"
        elif "host_cli" in sources or "model" in sources:
            engine = "host_cli"
    if not results:
        return {
            "applied": 0,
            "rejected": 0,
            "pending_before": len(tasks),
            "pending_count": len(tasks),
            "engine": "none",
            "enrich_mode": mode,
            "pending_tasks": tasks if mode == "cli" else [],
            "host_action_required": mode == "cli",
            "warning": "LLM 未返回结果，保留启发式/原文",
        }
    stats = apply_results(
        workspace,
        results,
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
    )
    st = get_status(
        workspace,
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
    )
    return {
        **stats,
        "pending_before": len(tasks),
        "pending_count": st.get("pending", 0),
        "engine": engine,
        "enrich_mode": mode,
        "pending_tasks": [],
        "host_action_required": False,
    }


def _enrich_with_enricher(tasks: list[dict], enricher, source: str = "model") -> list[dict]:
    """用 enricher 逐条处理 tasks。

    source: provider|host_cli|heuristic (调用方给的初始值)
    但最终 source 按 enriched.enrichment_mode 校正:
    - enrichment_mode == "heuristic" -> source = "heuristic" (即使走 provider 路径但内部 fallback)
    - enrichment_mode == "model" -> 保持调用方给的 source
    """
    results = []
    for task in tasks:
        inp = task.get("input", {})
        title = inp.get("title", "")
        body = inp.get("body", "")
        kind_hint = inp.get("kind_hint", "")
        try:
            enriched = enricher.enrich(title=title, body=body, kind_hint=kind_hint)
            # 按 enrichment_mode 校正 source
            final_source = source
            mode = getattr(enriched, "enrichment_mode", "")
            if mode == "heuristic":
                final_source = "heuristic"
            elif mode == "passthrough":
                final_source = "heuristic"
            results.append({
                "task_id": task["task_id"],
                "kind": enriched.kind,
                "title": enriched.title,
                "body": enriched.body,
                "confidence": enriched.confidence,
                "rationale": enriched.rationale,
                "source": final_source,
            })
        except Exception:
            continue
    return results


def open_localhost_window(
    workspace: str,
    *,
    auto_open: bool = True,
    native_webview: bool = False,
    native_title: str = "MemoryGuard 治理面板",
) -> tuple[int, str]:
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

    session_token = generate_session_token()
    from .access_context import AccessContext
    api = GovernanceApi(
        workspace,
        _trusted_access_context=AccessContext(
            trusted_agent_id="",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=session_token,
            session_source="transport",
            session_trusted=True,
        ),
    )
    # 网页环境绑定 127.0.0.1,等价于本地 GUI
    # 沙箱状态只读当前可信进程环境；GUI 不替调用方修改安全边界。
    is_sandbox = detect_sandbox_mode()
    request_queue = RequestQueue(workspace)

    def inject_runtime_context(html: str, *, session_token: str, sandbox: bool) -> str:
        """把会话令牌与沙箱标记注入任意 HTML 页面（知识书库等独立路由）。"""
        script = (
            "<script>"
            f"window.__MG_SESSION__={_json.dumps(session_token)};"
            f"window.__MG_SANDBOX__={str(sandbox).lower()};"
            "</script>"
        )
        return html.replace("</head>", script + "</head>")

    # 将 session_token 注入 HTML
    html = render_interactive_html()
    html = html.replace(
        "</head>",
        f'<script>window.__MG_SESSION__="{session_token}";'
        f'window.__MG_SANDBOX__={str(is_sandbox).lower()};</script></head>',
    )
    html_bytes = html.encode("utf-8")

    # 准备静态文件目录(cytoscape.min.js 等)
    import shutil as _shutil
    ui_dir = Path(workspace) / ".memoryguard" / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    static_src = Path(__file__).parent / "static" / "cytoscape.min.js"
    if static_src.exists():
        _shutil.copy2(static_src, ui_dir / "cytoscape.min.js")
    icon_src = Path(__file__).parent / "static" / "memoryguard-icon.png"
    if icon_src.exists():
        _shutil.copy2(icon_src, ui_dir / "memoryguard-icon.png")

    _MIME_TYPES = {
        ".js": "application/javascript",
        ".css": "text/css",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
        ".png": "image/png",
        ".svg": "image/svg+xml",
    }

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
            elif parsed.path == "/knowledge":
                # KB5 知识书库书架页（注入会话令牌与沙箱标记）
                from .knowledge_gui import render_bookshelf_html
                html = render_bookshelf_html()
                html = inject_runtime_context(html, session_token=session_token, sandbox=is_sandbox)
                page_bytes = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page_bytes)))
                self.end_headers()
                self.wfile.write(page_bytes)
            elif parsed.path.startswith("/knowledge/book/"):
                # KB5 知识书库详情页
                from .knowledge_gui import render_book_detail_html
                book_id = unquote(parsed.path[len("/knowledge/book/"):])
                html = render_book_detail_html(book_id)
                html = inject_runtime_context(html, session_token=session_token, sandbox=is_sandbox)
                page_bytes = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page_bytes)))
                self.end_headers()
                self.wfile.write(page_bytes)
            else:
                # 静态文件服务(cytoscape.min.js 等)
                req_path = parsed.path.lstrip("/")
                # 防路径穿越
                safe_name = Path(req_path).name
                file_path = ui_dir / safe_name
                if safe_name and file_path.exists() and file_path.is_file():
                    ext = file_path.suffix.lower()
                    mime = _MIME_TYPES.get(ext, "application/octet-stream")
                    data = file_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
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

            # 验证方法白名单（KB5 知识书库 API 走单独路由，跳过白名单）
            from .knowledge_gui import is_knowledge_method
            is_knowledge = is_knowledge_method(method)
            if not is_allowed_method(method) and not is_knowledge:
                self._json_response(501, {"error": f"unknown method: {method}"})
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                args = _json.loads(body.decode("utf-8")) if body else []
            except Exception as e:
                self.send_error(400, str(e))
                return

            # KB5 知识书库 API 路由（变更类在沙箱下进入请求队列，不单开特快通道）
            if is_knowledge:
                from .knowledge_gui import handle_knowledge_api, is_knowledge_mutation
                if is_knowledge_mutation(method) and is_sandbox:
                    req = request_queue.submit(method, args)
                    self._json_response(200, {
                        "ok": True,
                        "deferred": True,
                        "request": req.to_dict(),
                        "message": "请求已提交，等待桌面执行器确认",
                    })
                    return
                try:
                    result = handle_knowledge_api(method, args, workspace)
                    self._json_response(200, result if result is not None else {})
                except Exception as e:
                    self._json_response(500, {"error": str(e)})
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
                # 授权由 API 自己从 AccessContext 派生；HTTP/浏览器参数不得注入。
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

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"MemoryGuard GUI running at {url} (sandbox={is_sandbox})")
    if native_webview:
        if not has_native_gui():
            server.server_close()
            return 3, ""
        import threading as _threading
        import webview

        server_thread = _threading.Thread(
            target=server.serve_forever,
            name="memoryguard-localhost",
            daemon=True,
        )
        server_thread.start()
        try:
            _set_windows_app_user_model_id()
            bridge = SafeBridgeApi(
                workspace,
                direct_mutations=True,
                _trusted_access_context=api._trusted_access_context,
            )
            window = webview.create_window(
                native_title,
                url=url,
                js_api=bridge,
                width=1440,
                height=900,
                min_size=(800, 600),
            )
            bridge._set_window(window)
            _start_webview(webview)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
        return 0, url
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

from .sensitive_content import NAMED_SENSITIVE_PATTERNS

_SENSITIVE_PATTERNS = list(NAMED_SENSITIVE_PATTERNS)


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


def _resolve_publish_target_dir(target_file: str | Path) -> tuple[Path, Path | None, list[str]]:
    """Map a file or directory path to GenericMarkdownTarget's target directory."""
    target = Path(target_file)
    warnings: list[str] = []
    exact_file: Path | None = None
    if (target.suffix.lower() in {".md", ".markdown", ".txt"}
            or target.is_file()
            or (not target.exists() and target.suffix)):
        target_dir = target.parent
        exact_file = target.resolve()
        if target.name.lower() != "memory.md":
            warnings.append(
                f"GenericMarkdownTarget writes memory.md; also copying to {target.name}"
            )
    else:
        target_dir = target
    return target_dir, exact_file, warnings


def _build_publish_ir(ir, workspace, use_distilled: bool):
    """Prepare publish_ir from distilled groups or filtered raw records."""
    from dataclasses import replace

    from .distiller import MemoryDistiller, _is_publishable
    from .memory_ir import MemoryIR
    from .schema_v3 import (
        Completeness,
        MemoryKind,
        MemoryRecord,
        Provenance,
        MemoryStatus,
    )

    distill_stats = None
    source_record_map: dict[str, list[str]] = {}
    if use_distilled:
        distiller = MemoryDistiller(workspace)
        distilled = distiller.distill(ir)
        try:
            distiller.save(distilled)
        except OSError:
            pass
        distill_stats = distilled.stats
        records: list[MemoryRecord] = []
        for grp in distilled.groups:
            memory_id = grp.group_id or (
                grp.source_record_ids[0] if grp.source_record_ids else ""
            )
            try:
                kind = MemoryKind(grp.kind)
            except ValueError:
                kind = MemoryKind.FACT
            if grp.source_record_ids:
                source_record_map[memory_id] = list(grp.source_record_ids)
            completeness = grp.completeness or Completeness.VERIFIABLE.value
            if not isinstance(completeness, str):
                completeness = str(completeness)
            try:
                completeness_enum = Completeness(completeness)
            except ValueError:
                completeness_enum = Completeness.VERIFIABLE
            records.append(MemoryRecord(
                memory_id=memory_id,
                kind=kind,
                title=grp.title,
                body=grp.body,
                scope=grp.scope,
                confidence=grp.confidence,
                provenance=[Provenance(**p) for p in grp.provenance] if grp.provenance else [],
                completeness=completeness_enum,
                status=MemoryStatus.CANDIDATE,
            ))
    else:
        records = []
        for rec in ir.records:
            if not _is_publishable(rec):
                continue
            records.append(replace(rec))
    publish_ir = MemoryIR(
        records=records,
        snapshot_id=ir.snapshot_id,
        created_at=ir.created_at,
    )
    return publish_ir, distill_stats, source_record_map


def _redact_publish_ir(ir, source_record_map: dict[str, list[str]] | None = None):
    """Redact secrets in-place on publish_ir records; return redaction audit entries."""
    from .secrets import labels_in_redacted_text, redact_secrets

    source_record_map = source_record_map or {}
    redactions: list[dict] = []
    for rec in ir.records:
        redacted_title, title_labels = redact_secrets(rec.title)
        redacted_body, body_labels = redact_secrets(rec.body)
        rec.title = redacted_title
        rec.body = redacted_body
        fields: list[str] = []
        patterns: list[str] = []
        all_title_labels = sorted(set(title_labels) | set(labels_in_redacted_text(rec.title)))
        all_body_labels = sorted(set(body_labels) | set(labels_in_redacted_text(rec.body)))
        if all_title_labels:
            fields.append("title")
            patterns.extend(all_title_labels)
        if all_body_labels:
            fields.append("body")
            patterns.extend(all_body_labels)
        if fields:
            audit_id = (
                source_record_map[rec.memory_id][0]
                if rec.memory_id in source_record_map and source_record_map[rec.memory_id]
                else rec.memory_id
            )
            entry: dict = {
                "memory_id": audit_id,
                "fields": fields,
                "patterns": sorted(set(patterns)),
            }
            if rec.memory_id in source_record_map:
                entry["source_record_ids"] = source_record_map[rec.memory_id]
            redactions.append(entry)
    return ir, redactions


def _infer_target_dir_from_release(data: dict) -> Path:
    for cp in data.get("changed_paths", []):
        p = Path(cp)
        if p.name == "memory.md":
            return p.parent
    if data.get("changed_paths"):
        return Path(data["changed_paths"][0]).parent
    raise ValueError(f"cannot infer target directory for release {data.get('release_id', '')}")


def _release_ok(status) -> bool:
    from .schema_v3 import ReleaseStatus

    status_val = status.value if hasattr(status, "value") else str(status)
    return status_val in (ReleaseStatus.VERIFIED.value, ReleaseStatus.APPLIED.value)


def _verify_takeover(target_dir: Path, exact_file: Path | None, ir,
                     surface_id: str = "",
                     capability: str = "export_only") -> dict:
    """真实 Loader 复读验证(LRN-007):用 Profile 专用 Loader 解析目标文件。

    无效格式(二进制/无标题)Loader 返回空列表,验证失败。
    无 loader 时(export_only)返回 verified=False,不能声称已接管。
    """
    from .native_memory_loader import verify_takeover as _loader_verify
    from .schema_v3 import TargetCapability
    target = exact_file or (target_dir / "memory.md")
    try:
        cap = TargetCapability(capability) if capability else TargetCapability.EXPORT_ONLY
    except ValueError:
        cap = TargetCapability.EXPORT_ONLY
    result = _loader_verify(target, ir.records, surface_id=surface_id, capability=cap)
    return result.__dict__ if hasattr(result, "__dict__") else dict(result)


def _rewrite_release_json_for_exact_file(
    release,
    workspace: str | Path,
    *,
    published_target_file: str,
    exact_file_existed_before: bool,
) -> None:
    """Persist updated release paths plus exact_file sidecar metadata."""
    import json

    releases_dir = Path(workspace) / ".memoryguard" / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    release_path = releases_dir / f"{release.release_id}.json"
    if release_path.exists():
        data = json.loads(release_path.read_text(encoding="utf-8"))
    else:
        data = release.to_dict()
        data["schema_version"] = "3.1"
        data["record_type"] = "memory_release"
    data["changed_paths"] = list(release.changed_paths)
    data["backup_paths"] = list(release.backup_paths)
    data["published_target_file"] = published_target_file
    data["exact_file_existed_before"] = exact_file_existed_before
    # Atomic write: never truncate the live release JSON mid-write.
    tmp = release_path.with_name(release_path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, release_path)


def _sync_exact_file_into_release(release, target_dir: Path, exact_file: Path, workspace):
    """Copy memory.md → exact_file and fold the path into the same ReleaseChange.

    Backup naming matches GenericMarkdownTarget.install so rollback glob
    ``{name}.*.bak`` restores or unlinks correctly.
    """
    import json
    import shutil

    from .schema_v3 import stable_hash, _now_iso

    memory_md = target_dir / "memory.md"
    if not memory_md.exists():
        raise FileNotFoundError(f"memory.md missing after apply_build: {memory_md}")

    existed_before = exact_file.exists()
    exact_resolved = str(exact_file.resolve())
    backup_path: Path | None = None

    try:
        if existed_before:
            backup_dir = target_dir / ".memoryguard-backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{exact_file.name}.{stable_hash(_now_iso())}.bak"
            shutil.copy2(exact_file, backup_path)

        exact_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = exact_file.with_name(exact_file.name + ".memoryguard-publish.tmp")
        shutil.copy2(memory_md, tmp)
        os.replace(tmp, exact_file)

        release.changed_paths = list(release.changed_paths) + [exact_resolved]
        if backup_path is not None:
            release.backup_paths = list(release.backup_paths) + [str(backup_path)]

        _rewrite_release_json_for_exact_file(
            release,
            workspace,
            published_target_file=exact_resolved,
            exact_file_existed_before=existed_before,
        )
        return release
    except Exception:
        # Ensure rollback can see exact_file even if rewrite failed mid-sync.
        if exact_resolved not in release.changed_paths:
            release.changed_paths = list(release.changed_paths) + [exact_resolved]
        if backup_path is not None and str(backup_path) not in release.backup_paths:
            release.backup_paths = list(release.backup_paths) + [str(backup_path)]
        try:
            _rewrite_release_json_for_exact_file(
                release,
                workspace,
                published_target_file=exact_resolved,
                exact_file_existed_before=existed_before,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        raise


def _verify_exact_file_after_rollback(data: dict) -> list[str]:
    """Post-rollback checks for published_target_file sidecar state."""
    published = data.get("published_target_file")
    if not published:
        return []
    exact = Path(published)
    existed_before = bool(data.get("exact_file_existed_before"))
    if existed_before:
        if not exact.exists():
            return [f"published_target_file missing after rollback: {published}"]
    elif exact.exists():
        return [f"published_target_file still present after rollback: {published}"]
    return []


class GovernanceApi:
    """pywebview JS API 类（v3 五入口架构，spec §7.2）。

    v3.1 新增：
    - pick_path：系统目录/文件选择器（替代 prompt）
    - discover_agents：AgentLocator 有限候选发现
    - get_selection_tree / commit_selection：分类勾选授权
    - neuron_decide：图上治理操作 → DecisionEvent → 新规范版本
    - 神经图投影 meta：Agent 实例 / Profile / 规范版本 / Release / 接管状态 / 覆盖状态 / 漂移
    """

    def __init__(self, workspace: str, *, _trusted_access_context=None):
        self.workspace = workspace
        # Only the localhost/native UI server injects this server-owned
        # context after issuing its random session token. Browser arguments
        # are never accepted as authorization input.
        self._trusted_access_context = _trusted_access_context
        self._report = None
        self._window = None  # pywebview window 引用，由 open_interactive_window 注入
        self._build_jobs: dict[str, dict] = {}
        import threading
        self._build_lock = threading.Lock()
        self._active_build_job: str | None = None

    def _set_window(self, window) -> None:
        """注入 pywebview window 实例（用于 create_file_dialog）。"""
        self._window = window

    def _parse_scope(self, scope: dict | None = None, *,
                     agent_instance_id: str = "",
                     share_group_id: str = "",
                     mode: str = "") -> tuple[dict | None, str]:
        """显式 scope 校验。不读 preference 作授权。GUI/CLI/MCP 共用 resolver。"""
        from .governance_scope import resolve_governance_scope
        ok, err = resolve_governance_scope(
            scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            mode=mode,
        )
        if ok is None:
            return None, err or "missing_governance_scope"
        return ok.to_dict(), ""

    def _trusted_agent_id(self) -> str:
        from .access_context import load_access_context

        access = self._trusted_access_context or load_access_context()
        return str(getattr(access, "trusted_agent_id", "") or "").strip()

    def get_governance_scope(self) -> dict:
        """读取 UI 偏好 scope（非授权依据）。"""
        from .governance_scope import load_scope_preference
        pref = load_scope_preference(self.workspace)
        if pref is None:
            return {"empty": True, "reason": "no_preference"}
        return {"empty": False, "scope": pref.to_dict()}

    def get_governance_scope_state(self) -> dict:
        """Return the preference plus its binding-backed runtime state."""
        from .governance_scope import (
            list_active_scope_options,
            load_scope_preference,
            resolve_active_scope,
        )

        trusted_agent_id = self._trusted_agent_id()
        preference = load_scope_preference(self.workspace)
        resolution = resolve_active_scope(
            self.workspace,
            preference,
            trusted_agent_id=trusted_agent_id,
        )
        return {
            **resolution.to_dict(),
            "preference": preference.to_dict() if preference else None,
            "trusted_agent_instance_id": trusted_agent_id,
            "options": list_active_scope_options(
                self.workspace,
                trusted_agent_id=trusted_agent_id,
            ),
        }

    def set_governance_scope(self, scope: dict | None = None, *,
                             agent_instance_id: str = "",
                             share_group_id: str = "",
                             mode: str = "",
                             _admin_override: bool = False) -> dict:
        """写入 UI 偏好 scope。"""
        from .governance_scope import GovernanceScope, save_scope_preference
        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id,
            share_group_id=share_group_id, mode=mode,
        )
        if err:
            return {"error": err}
        return save_scope_preference(self.workspace, GovernanceScope.from_dict(parsed))

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
    # 知识书库变更（P0-4 Desktop Executor 路由）
    # ------------------------------------------------------------------

    def knowledge_add(self, path: str, title: str = "") -> dict:
        """添加知识书库文件夹并入库。"""
        from .knowledge_gui import handle_knowledge_api
        return handle_knowledge_api("knowledge_add", [path, title], self.workspace)

    def knowledge_reingest(self, book_id: str) -> dict:
        """重新整理知识书库。"""
        from .knowledge_gui import handle_knowledge_api
        return handle_knowledge_api("knowledge_reingest", [book_id], self.workspace)

    def knowledge_rebuild_smart(self, book_id: str) -> dict:
        """不重读源文件，重建整理、关系和向量索引。"""
        from .knowledge_gui import handle_knowledge_api
        return handle_knowledge_api(
            "knowledge_rebuild_smart",
            [book_id],
            self.workspace,
        )

    def knowledge_remove(self, book_id: str) -> dict:
        """把知识书库移入可恢复回收站。"""
        from .knowledge_gui import handle_knowledge_api
        return handle_knowledge_api("knowledge_remove", [book_id], self.workspace)

    def knowledge_restore(self, deletion_id: str) -> dict:
        """恢复已删除知识书库。"""
        from .knowledge_gui import handle_knowledge_api
        return handle_knowledge_api(
            "knowledge_restore",
            [deletion_id],
            self.workspace,
        )

    def knowledge_purge_deleted(self, deletion_id: str) -> dict:
        """永久清理知识书库恢复快照。"""
        from .knowledge_gui import handle_knowledge_api
        return handle_knowledge_api(
            "knowledge_purge_deleted",
            [deletion_id],
            self.workspace,
        )

    def knowledge_update_settings(
        self,
        book_id: str,
        settings: dict,
    ) -> dict:
        """更新知识书库远程处理和候选设置。"""
        from .knowledge_gui import handle_knowledge_api
        return handle_knowledge_api(
            "knowledge_update_settings",
            [book_id, settings],
            self.workspace,
        )

    def knowledge_candidate_review(
        self,
        candidate_id: str,
        decision: str,
        target_group_id: str = "",
    ) -> dict:
        """审核记忆候选（批准/拒绝）。"""
        from .knowledge_gui import handle_knowledge_api
        return handle_knowledge_api(
            "knowledge_candidate_review",
            [candidate_id, decision, target_group_id],
            self.workspace,
        )

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
            # 无 pywebview 时用系统文件对话框，禁止手填路径
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                if for_files:
                    selected = filedialog.askopenfilename(title="选择导入文件")
                else:
                    selected = filedialog.askdirectory(title="选择来源目录")
                root.destroy()
            except Exception as exc:
                return {"error": f"path_picker_unavailable: {exc}"}
            if not selected:
                return {"error": "cancelled"}
            from pathlib import Path
            p = Path(selected)
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
        """v3.1 §4.3 返回分类勾选树。

        修复:将 SourceRegistry 中 scope="project" 的 SourceRoot(如 src-project-default)
        也注入到勾选树中,让用户能在 Agent 下看到并勾选项目目录。
        """
        from .agent_locator import AgentLocator
        from .source_registry import SourceRegistry
        from .schema_v3 import SourceRootType
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

        # 修复:将 scope="project" 的 SourceRoot(含 src-project-default)注入勾选树
        # 这些 SourceRoot 的 agent_instance_id 可能为空,但用户仍需看到并勾选
        project_roots = [
            r for r in reg.list_all_sources()
            if r.scope == "project" and r.type == SourceRootType.PROJECT_DIRECTORY
        ]
        if project_roots:
            # 找到或创建 project scope 区块
            project_scope = None
            for s in tree.get("scopes", []):
                if s.get("scope") == "project":
                    project_scope = s
                    break
            if project_scope is None:
                project_scope = {
                    "scope": "project",
                    "scope_source": "source_registry",
                    "projects": [],
                    "categories": [],
                }
                tree.setdefault("scopes", []).append(project_scope)
            # 确保有 projects 列表
            project_scope.setdefault("projects", [])
            # 为每个 project root 创建一个 project 区块
            for root in project_roots:
                root_path = Path(root.path).resolve() if root.path else None
                if root_path and not root_path.exists():
                    continue
                # 检查是否已有同名 project
                existing_proj = next(
                    (p for p in project_scope["projects"]
                     if p.get("project_ref") == root.display_name),
                    None,
                )
                if existing_proj is None:
                    existing_proj = {
                        "project_ref": root.display_name,
                        "scope_source": "source_registry",
                        "categories": [],
                    }
                    project_scope["projects"].append(existing_proj)
                # 注入项目目录作为一个 category
                project_dir_cat = {
                    "category": "project_directory",
                    "category_label": "项目目录",
                    "files": [{
                        "surface_id": root.root_id,
                        "discovery_object_id": "",  # 项目目录不来自 discovery
                        "path": str(root_path) if root_path else root.path,
                        "display_name": root.display_name,
                        "root_id": root.root_id,
                        "default_selected": bool(root.enabled),
                        "saved_selected": bool(root.enabled),
                        "source_root_id": root.root_id,
                        "is_project_directory": True,
                    }],
                }
                existing_proj.setdefault("categories", []).append(project_dir_cat)

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
        from .governance_scope import (
            grant_root_to_agent, revoke_root_from_agent, root_authorizes_agent, save_scope_preference,
            GovernanceScope,
        )
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
            revoked_count = 0
            for root in reg.list_all_sources():
                if not root_authorizes_agent(root, instance_id):
                    continue
                root_path = str(Path(root.path).resolve()) if root.path else ""
                visible = (root.discovery_object_id and root.discovery_object_id in visible_discovery_ids) or root_path in visible_paths
                if not visible:
                    continue
                revoke_root_from_agent(root, instance_id)
                revoked_count += 1
                # 仅当没有任何 agent 仍授权时才禁用
                if not (getattr(root, "authorized_agent_ids", None) or []) and not root.agent_instance_id:
                    if root.enabled:
                        root.enabled = False
                        disabled_count += 1
            reg._save()
            save_scope_preference(
                self.workspace,
                GovernanceScope(mode="agent", agent_instance_id=instance_id),
            )
            return {
                "selection_id": selection_id,
                "added_source_count": 0,
                "updated_source_count": 0,
                "disabled_source_count": disabled_count,
                "revoked_authorization_count": revoked_count,
                "total_selected": 0,
                "governance_scope": {"mode": "agent", "agent_instance_id": instance_id},
            }
        # v3.2 改动包1 P0：服务端以 discovery_object_id 为唯一授权依据
        # 修复:对 is_project_directory=True 的条目(来自 SourceRegistry 的项目目录),
        # 跳过 discovery 验证,直接用 SourceRegistry 中的信息
        reg = SourceRegistry(self.workspace)
        discovery_object_ids = [item.get("discovery_object_id", "") for item in selected if item.get("discovery_object_id")]
        validation = locator.validate_discovery_objects(instance_id, discovery_object_ids) if discovery_object_ids else {}
        # 过滤掉验证失败的条目
        validated_selected = []
        for item in selected:
            dobj_id = item.get("discovery_object_id", "")
            if item.get("is_project_directory"):
                # 项目目录条目:不来自 discovery,直接通过
                # 从 SourceRegistry 回填信息
                root_id = item.get("source_root_id") or item.get("root_id", "")
                proj_root = next((r for r in reg.list_all_sources() if r.root_id == root_id), None)
                if proj_root:
                    item["path"] = proj_root.path
                    item["category"] = "project_directory"
                    item["scope"] = proj_root.scope or "project"
                    item["scope_source"] = "source_registry"
                    item["project_ref"] = proj_root.display_name or "项目目录"
                    item["surface_id"] = root_id
                    item["ingestion_policy"] = "extract_candidates"
                    item["ownership"] = "external_read_only"
                    item["target_role"] = "none"
                    validated_selected.append(item)
                elif not dobj_id:
                    continue
                else:
                    pass  # 有 dobj_id 的走下面正常流程
            elif dobj_id and validation.get(dobj_id, {}).get("valid"):
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
                # 项目目录不在枚举中,归为 KNOWLEDGE_SOURCE
                cat_enum = SourceCategory.KNOWLEDGE_SOURCE if cat_str == "project_directory" else SourceCategory.UNKNOWN
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

        # reg 已在前面创建(用于 is_project_directory 查找)
        selected_discovery_ids = {e.discovery_object_id for e in entries if e.discovery_object_id}
        selected_paths = {str(Path(e.resolved_path).resolve()) for e in entries if e.resolved_path}
        visible_discovery_ids = {surf.get("discovery_object_id", "") for surf in path_to_surface.values() if surf.get("discovery_object_id")}
        visible_paths = {str(Path(path).resolve()) for path in path_to_surface if path}
        disabled_count = 0
        revoked_count = 0
        for root in reg.list_all_sources():
            from .schema_v3 import SourceRootType
            is_proj_dir = (root.type == SourceRootType.PROJECT_DIRECTORY)
            # 多对多：按授权判断，不再要求唯一 agent_instance_id
            if not is_proj_dir and not root_authorizes_agent(root, instance_id) and root.agent_instance_id not in ("", instance_id):
                continue
            root_path = str(Path(root.path).resolve()) if root.path else ""
            visible = (root.discovery_object_id and root.discovery_object_id in visible_discovery_ids) or root_path in visible_paths
            selected_now = (root.discovery_object_id and root.discovery_object_id in selected_discovery_ids) or root_path in selected_paths
            if is_proj_dir:
                visible = visible or (root_path != "")
                selected_now = selected_now or any(
                    e.surface_id == root.root_id or
                    (hasattr(e, 'is_project_directory') and e.is_project_directory and
                     str(Path(e.resolved_path).resolve()) == root_path)
                    for e in entries
                )
            if visible and not selected_now and root_authorizes_agent(root, instance_id):
                revoke_root_from_agent(root, instance_id)
                revoked_count += 1
                if not (getattr(root, "authorized_agent_ids", None) or []) and not root.agent_instance_id:
                    if root.enabled:
                        root.enabled = False
                        disabled_count += 1
        added_count = 0
        updated_count = 0
        for entry in entries:
            p = Path(entry.resolved_path)
            if not p.exists():
                continue
            # 修复:对项目目录条目,更新已有的 src-project-default 而非创建新的
            if entry.category.value == "project_directory" or entry.surface_id == "src-project-default":
                existing_root = next(
                    (r for r in reg.list_all_sources()
                     if r.root_id == "src-project-default" or r.path == str(p.resolve())),
                    None,
                )
                if existing_root:
                    # 更新已有 root:多对多授权当前 agent（项目根可被多 agent 共享）
                    grant_root_to_agent(existing_root, instance_id)
                    existing_root.enabled = True
                    existing_root.scope = entry.scope or "project"
                    existing_root.scope_source = entry.scope_source or "source_registry"
                    existing_root.project_ref = entry.project_ref or existing_root.display_name
                    updated_count += 1
                    continue
            root_type = SourceRootType.SELECTED_DIRECTORY if p.is_dir() else SourceRootType.SELECTED_FILE
            try:
                root = reg.add(entry.resolved_path, root_type,
                               display_name=f"{tree.get('product', 'agent')}/{entry.surface_id}")
            except (ValueError, OSError):
                continue
            was_authorized = root_authorizes_agent(root, instance_id)
            root.enabled = True
            grant_root_to_agent(root, instance_id)
            root.surface_id = entry.surface_id or root.surface_id
            root.source_category = entry.category.value
            root.ingestion_policy = entry.ingestion_policy.value
            root.ownership = entry.ownership.value
            root.target_role = entry.target_role.value
            root.scope = entry.scope
            root.scope_source = entry.scope_source
            root.project_ref = entry.project_ref
            root.discovery_object_id = entry.discovery_object_id
            if was_authorized:
                updated_count += 1
            else:
                added_count += 1
        reg._save()
        save_scope_preference(
            self.workspace,
            GovernanceScope(mode="agent", agent_instance_id=instance_id),
        )
        return {
            "selection_id": selection_id,
            "added_source_count": added_count,
            "updated_source_count": updated_count,
            "disabled_source_count": disabled_count,
            "revoked_authorization_count": revoked_count,
            "total_selected": len(entries),
            "governance_scope": {"mode": "agent", "agent_instance_id": instance_id},
        }

    # ------------------------------------------------------------------
    # 图上治理操作（v3.1 §6.2）
    # ------------------------------------------------------------------

    def _shared_memory_decide(
        self,
        memory_id: str,
        action: str,
        reason: str,
        share_group_id: str,
    ) -> dict:
        from .governance_engine import GovernanceEngine
        from .shared_memory_store import SharedMemoryStore
        from .governance_scope import share_file_source_key

        store = SharedMemoryStore(self.workspace, share_group_id)
        engine = GovernanceEngine(
            self.workspace, share_group_id, store=store,
        )
        record = store.get_record(memory_id)
        if record is None:
            return {"error": f"memory not found in share group: {memory_id}"}

        act = (action or "").strip().lower()
        if act in ("exclude", "delete", "reject", "supersede"):
            transition = engine.human_delete(memory_id)
        elif act == "quarantine":
            transition = engine.quarantine(
                memory_id,
                reason=reason or "panel quarantine",
                pattern="user_quarantine",
                original_content=record.body,
                actor="user",
                manual_override=True,
            )
        elif act == "merge":
            return {"error": "merge not supported for shared memory; edit records via governance panel"}
        else:
            return {"error": f"unsupported action: {action}"}

        if not transition["ok"]:
            return {"error": transition["blocked_reason"]}
        version_id = transition["version_id"]
        # 决策后只做轻量出图，禁止再跑 LLM 整理（否则点排除会卡很久）
        self._rebuild_projection_light(
            {"mode": "share_group", "share_group_id": share_group_id},
            mode="reconstructed",
        )
        return {
            "ok": True,
            "memory_id": memory_id,
            "action": act,
            "version_id": version_id,
            "share_group_id": share_group_id,
            "scope": {"mode": "share_group", "share_group_id": share_group_id},
        }

    def neuron_decide(self, node_id: str, action: str,
                      reason: str = "", confirmed: bool = False,
                      scope: dict | None = None,
                      agent_instance_id: str = "",
                      share_group_id: str = "") -> dict:
        """v3.1 §6.2 图上操作 → DecisionEvent → 新规范版本。

        agent scope：ManagedStore。
        share_group scope：SharedMemoryStore（MCP 正式接管）。
        """
        if not confirmed:
            return {"error": "需要确认才能执行治理操作"}
        parsed, err = self._parse_scope(
            scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
        )
        if err or not parsed:
            return {"error": err or "missing_governance_scope"}

        if parsed.get("mode") == "share_group":
            return self._shared_memory_decide(
                memory_id=node_id,
                action=action,
                reason=reason,
                share_group_id=parsed["share_group_id"],
            )

        if parsed.get("mode") != "agent":
            return {"error": "agent_scope_required"}
        agent_id = parsed["agent_instance_id"]
        from .managed_store import ManagedStore, find_record_by_node_id
        _vid, record = find_record_by_node_id(
            self.workspace, node_id, agent_instance_id=agent_id,
        )
        if record is None:
            return {"error": f"node not found in managed store for agent: {agent_id}"}
        store = ManagedStore(self.workspace, agent_id)
        new_version = store.apply_decision(
            action=action, target_ids=[record.memory_id],
            reason=reason, actor="user",
        )
        # 决策后轻量刷新投影（不入队、不调 LLM），图上立刻消失
        self._rebuild_projection_light(parsed, mode="reconstructed")
        return {
            "memory_version": new_version.version_id,
            "action": action,
            "target_id": record.memory_id,
            "decision_count": new_version.decision_count,
            "agent_instance_id": agent_id,
            "scope": parsed,
        }

    def _rebuild_projection_light(self, parsed: dict, mode: str = "reconstructed") -> dict:
        """治理决策后的快速投影刷新：只重画出图，跳过扫描入队与 LLM。"""
        from .governance_scope import (
            GovernanceScope, build_shared_memory_graph, scope_storage_key,
            share_group_projection_path, authorized_roots_digest,
            resolve_scoped_roots,
        )
        from .projection import ProjectionBuilder
        from .memory_ir import MemoryIR
        from .managed_store import ManagedStore
        from .schema_v3 import MemoryStatus, _now_iso
        import json as _json

        gscope = GovernanceScope.from_dict(parsed)
        if gscope is None:
            return {"error": "invalid_governance_scope"}

        if gscope.mode == "share_group":
            graph = build_shared_memory_graph(self.workspace, gscope.share_group_id)
            out_path = share_group_projection_path(self.workspace, gscope)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tomb = out_path.with_suffix(out_path.suffix + ".deleted")
            if tomb.exists():
                tomb.unlink()
            graph["built"] = True
            graph["scope"] = parsed
            graph["enrichment"] = {
                "mode": "skipped_light_rebuild",
                "hint": "决策刷新未跑 LLM；完整整理请用「重建投影」",
            }
            out_path.write_text(_json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
            return graph

        agent_id = gscope.agent_instance_id
        store = ManagedStore(self.workspace, agent_id)
        hide = {
            MemoryStatus.REJECTED,
            MemoryStatus.QUARANTINED,
            MemoryStatus.SUPERSEDED,
        }
        records = [r for r in store.list_records() if r.status not in hide]
        light_ir = MemoryIR(
            records=records,
            snapshot_id=f"light-{agent_id}",
            created_at=_now_iso(),
            duplicate_groups=[],
        )
        from .source_registry import SourceRegistry
        reg = SourceRegistry(self.workspace)
        scoped_roots, _ = resolve_scoped_roots(reg.list_all_sources(), gscope, enabled_only=True)
        allowed_ids = {r.root_id for r in scoped_roots}
        graph_mode = "native" if mode == "native" else "reconstructed"
        key = scope_storage_key(gscope)
        pb = ProjectionBuilder(self.workspace, graph_mode, scope_key=key)
        meta = {
            "governance_scope": parsed,
            "authorized_root_ids": sorted(allowed_ids),
            "authorized_roots_digest": authorized_roots_digest(allowed_ids),
            "light_rebuild": True,
        }
        proj = pb.build(light_ir, meta=meta)
        pb.save(proj)
        result = proj.to_dict()
        result["mode"] = graph_mode
        result["scope"] = parsed
        result["enrichment"] = {
            "mode": "skipped_light_rebuild",
            "hint": "决策刷新未跑 LLM；完整整理请用「重建投影」",
        }
        return result

    # ------------------------------------------------------------------
    # ProjectionApi（spec §7.3）：神经图纯投影
    # ------------------------------------------------------------------

    def set_projection_source_enabled(self, root_id: str, enabled: bool,
                                      scope: dict | None = None,
                                      agent_instance_id: str = "") -> dict:
        from .source_registry import SourceRegistry
        from .governance_scope import (
            GovernanceScope, root_authorizes_agent, set_root_enabled_for_agent,
        )
        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id, mode="agent",
        )
        if err or not parsed or parsed.get("mode") != "agent":
            return {"ok": False, "error": err or "agent_scope_required", "root_id": root_id}
        gscope = GovernanceScope.from_dict(parsed)
        reg = SourceRegistry(self.workspace)
        root = next((r for r in reg.list_all_sources() if r.root_id == root_id), None)
        if root is None:
            return {"ok": False, "error": "source root not found", "root_id": root_id}
        if not root_authorizes_agent(root, gscope.agent_instance_id):
            return {"ok": False, "error": "root_not_authorized_for_agent", "root_id": root_id}
        set_root_enabled_for_agent(root, gscope.agent_instance_id, bool(enabled))
        reg._save()
        return {
            "ok": True,
            "root": root.to_dict(),
            "source_map": self.get_projection_source_map(scope=parsed),
            "scope": parsed,
        }

    def get_projection_source_map(self, scope: dict | None = None,
                                  agent_instance_id: str = "",
                                  share_group_id: str = "",
                                  mode: str = "") -> dict:
        from .source_registry import SourceRegistry
        from .governance_scope import resolve_scoped_roots, root_authorizes_agent
        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id,
            share_group_id=share_group_id, mode=mode,
        )
        if err:
            return {"error": err, "entries": [], "summary": {}}
        reg = SourceRegistry(self.workspace)
        if parsed["mode"] == "share_group":
            return self._get_share_group_projection_source_map(
                parsed["share_group_id"], parsed, reg,
            )
        agent_id = parsed["agent_instance_id"]
        roots, _ = resolve_scoped_roots(reg.list_all_sources(), parsed, enabled_only=False)
        native_categories = {"native_memory", "project_memory"}
        excluded_categories = {"conversation_history", "runtime_evidence", "ignored_runtime_data"}
        excluded_policies = {"evidence_only", "govern_only", "ignore"}
        from .governance_scope import is_root_enabled_for_agent
        entries = []
        for root in roots:
            enabled = is_root_enabled_for_agent(root, agent_id)
            source_category = root.source_category
            surface_id = root.surface_id
            project_ref = root.project_ref
            scope_source = root.scope_source
            if root.scope == "project" and root.source_category in {"", "unknown"}:
                source_category = "knowledge_source"
                if not project_ref:
                    project_ref = Path(root.path).resolve().name if root.path else "当前项目"
                if not scope_source or scope_source == "fallback":
                    scope_source = "project_workspace"
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
                "agent_instance_id": agent_id if root_authorizes_agent(root, agent_id) else root.agent_instance_id,
                "authorized_agent_ids": list(getattr(root, "authorized_agent_ids", []) or []),
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
            "scope": parsed,
        }

    def _get_share_group_projection_source_map(
        self,
        share_group_id: str,
        parsed_scope: dict,
        registry,
    ) -> dict:
        """列出共享库 active 记录的真实入库来源。

        共享图不直接扫描 SourceRoot，而是读取 SharedMemoryStore。来源映射因此
        必须从记录 provenance → MemoryEvent.metadata 反查，不能复用单 Agent
        的“当前勾选根”口径，更不能固定返回 0。
        """
        from .shared_memory_store import SharedMemoryStore
        from .governance_scope import share_file_source_key

        try:
            store = SharedMemoryStore(
                self.workspace, share_group_id, read_only=True,
            )
        except FileNotFoundError:
            return {
                "entries": [],
                "summary": {
                    "total": 0, "enabled": 0, "shared_memory": 0,
                    "logical_reconstruction": 0, "native_memory": 0,
                    "evidence_only": 0,
                },
                "scope": parsed_scope,
                "projection_kind": "shared_memory_projection",
            }

        records = store.list_records(status="active")
        event_by_id = {
            event.event_id: event for event in store.list_events()
        }
        events_by_relative_path: dict[str, list] = {}
        events_by_share_key: dict[str, list] = {}
        for event in event_by_id.values():
            metadata = event.metadata if isinstance(event.metadata, dict) else {}
            relative = str(metadata.get("relative_path", "") or "").strip().replace("\\", "/")
            share_key = share_file_source_key(metadata)
            if relative:
                events_by_relative_path.setdefault(relative, []).append(event)
            if share_key:
                events_by_share_key.setdefault(share_key, []).append(event)
        roots_by_id = {
            root.root_id: root for root in registry.list_all_sources()
        }
        origins: dict[str, dict] = {}

        for record in records:
            record_origins: dict[str, tuple[object | None, dict]] = {}
            for provenance in list(record.provenance or []):
                source_id = str(provenance.source_object_id or "")
                events = []
                direct_event = event_by_id.get(source_id)
                if direct_event is not None:
                    events.append(direct_event)
                elif source_id.startswith("share-file:"):
                    from .governance_scope import parse_share_file_source_key
                    root_hint, relative = parse_share_file_source_key(source_id)
                    events.extend(events_by_share_key.get(source_id, []))
                    if not events and relative:
                        fallback_key = (
                            f"share-file:{relative}" if not root_hint
                            else f"share-file:{root_hint}:{relative}"
                        )
                        events.extend(events_by_share_key.get(fallback_key, []))
                        events.extend(events_by_relative_path.get(relative, []))
                for event in events:
                    metadata = (
                        dict(event.metadata or {})
                        if isinstance(event.metadata, dict)
                        else {}
                    )
                    root_id = str(metadata.get("source_root_id", "") or "")
                    if root_id:
                        record_origins.setdefault(root_id, (event, metadata))
            if not record_origins:
                runtime_agent = str(record.agent_instance_id or "unknown")
                record_origins[f"mcp:{runtime_agent}"] = (None, {})

            for origin_id, (event, metadata) in record_origins.items():
                item = origins.setdefault(origin_id, {
                    "record_ids": set(),
                    "agent_ids": set(),
                    "metadata": metadata,
                    "first_imported_at": "",
                })
                item["record_ids"].add(record.memory_id)
                agent_id = str(
                    getattr(event, "agent_instance_id", "")
                    or record.agent_instance_id
                    or ""
                )
                if agent_id:
                    item["agent_ids"].add(agent_id)
                created_at = str(
                    getattr(event, "created_at", "")
                    or record.created_at
                    or ""
                )
                if created_at and (
                    not item["first_imported_at"]
                    or created_at < item["first_imported_at"]
                ):
                    item["first_imported_at"] = created_at

        entries: list[dict] = []
        for origin_id, info in sorted(origins.items()):
            agent_ids = sorted(info["agent_ids"])
            if origin_id.startswith("mcp:"):
                agent_id = origin_id.split(":", 1)[1]
                entries.append({
                    "root_id": origin_id,
                    "display_name": f"MCP 实时写入 · {agent_id}",
                    "path": "SharedMemoryStore",
                    "enabled": True,
                    "participates": True,
                    "agent_instance_id": agent_id,
                    "authorized_agent_ids": [agent_id],
                    "surface_id": "memoryguard_mcp",
                    "scope": "share_group",
                    "scope_source": "mcp_runtime",
                    "project_ref": share_group_id,
                    "source_category": "shared_memory",
                    "ingestion_policy": "mcp_write",
                    "target_role": "shared_memory_truth",
                    "ownership": "memoryguard_managed",
                    "projection_mode": "shared_memory_projection",
                    "logical_eligible": True,
                    "native_eligible": False,
                    "is_shared_memory_origin": True,
                    "record_count": len(info["record_ids"]),
                    "first_imported_at": info["first_imported_at"],
                })
                continue

            root = roots_by_id.get(origin_id)
            metadata = info["metadata"]
            entries.append({
                "root_id": origin_id,
                "display_name": (
                    root.display_name if root is not None
                    else str(metadata.get("relative_path", "") or origin_id)
                ),
                "path": root.path if root is not None else "",
                "enabled": bool(root.enabled) if root is not None else False,
                "participates": True,
                "agent_instance_id": (
                    agent_ids[0] if len(agent_ids) == 1
                    else "、".join(agent_ids)
                ),
                "authorized_agent_ids": (
                    list(root.authorized_agent_ids or [])
                    if root is not None else agent_ids
                ),
                "surface_id": (
                    root.surface_id if root is not None
                    else str(metadata.get("relative_path", "") or "")
                ),
                "scope": root.scope if root is not None else "unknown",
                "scope_source": (
                    root.scope_source if root is not None
                    else "historical_event"
                ),
                "project_ref": (
                    root.project_ref if root is not None else ""
                ),
                "source_category": (
                    root.source_category if root is not None
                    else str(metadata.get("source_category", "") or "unknown")
                ),
                "ingestion_policy": (
                    root.ingestion_policy if root is not None
                    else str(metadata.get("extraction_origin", "") or "historical")
                ),
                "target_role": (
                    root.target_role if root is not None else "historical_input"
                ),
                "ownership": (
                    root.ownership if root is not None else "historical"
                ),
                "projection_mode": "shared_memory_projection",
                "logical_eligible": True,
                "native_eligible": False,
                "is_shared_memory_origin": True,
                "record_count": len(info["record_ids"]),
                "first_imported_at": info["first_imported_at"],
            })

        return {
            "entries": entries,
            "summary": {
                "total": len(entries),
                "enabled": sum(1 for entry in entries if entry["participates"]),
                "shared_memory": len(entries),
                "logical_reconstruction": 0,
                "native_memory": sum(
                    1 for entry in entries
                    if entry["source_category"] in {
                        "native_memory", "project_memory",
                    }
                ),
                "evidence_only": 0,
            },
            "scope": parsed_scope,
            "projection_kind": "shared_memory_projection",
        }

    def get_neuron_graph(self, mode: str = "reconstructed",
                           scope: dict | None = None,
                           agent_instance_id: str = "",
                           share_group_id: str = "") -> dict:
        """读取 scoped 神经图投影。缺 scope 时 fail closed。"""
        from .governance_scope import (
            GovernanceScope, scope_storage_key, share_group_projection_path,
            resolve_scoped_roots, filter_ir_for_agent, projection_auth_matches,
            authorized_roots_digest, share_group_status_meta,
        )
        from .projection import ProjectionBuilder
        from .memory_ir import MemoryNormalizer
        from .source_registry import SourceRegistry
        import json as _json_mod

        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
        )
        if err:
            return {"empty": True, "reason": err, "error": err}
        gscope = GovernanceScope.from_dict(parsed)

        if gscope.mode == "share_group":
            path = share_group_projection_path(self.workspace, gscope)
            status_meta = share_group_status_meta(self.workspace, gscope.share_group_id)
            tomb = path.with_suffix(path.suffix + ".deleted")
            if tomb.exists() or not path.exists():
                empty = {
                    "empty": True,
                    "reason": "not_built",
                    "scope": parsed,
                    "mode": "share_group",
                    "projection_kind": "shared_memory_projection",
                    "meta": status_meta,
                }
                empty["source_map"] = self.get_projection_source_map(scope=parsed)
                return self._with_virtual_neuron_categories(empty, parsed)
            try:
                graph = _json_mod.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return self._with_virtual_neuron_categories({
                    "empty": True, "reason": "not_built", "error": "projection_corrupt",
                    "scope": parsed, "meta": status_meta,
                }, parsed)
            if graph.get("empty"):
                graph["scope"] = parsed
                graph["source_map"] = self.get_projection_source_map(scope=parsed)
                meta = graph.get("meta") if isinstance(graph.get("meta"), dict) else {}
                meta.update(status_meta)
                graph["meta"] = meta
                return self._with_virtual_neuron_categories(graph, parsed)
            expected = authorized_roots_digest([f"share:{gscope.share_group_id}"])
            meta = graph.get("meta") if isinstance(graph.get("meta"), dict) else {}
            if str(meta.get("authorized_roots_digest", "") or "") != expected:
                return self._with_virtual_neuron_categories({
                    "empty": True,
                    "reason": "not_built",
                    "error": "projection_auth_stale",
                    "scope": parsed,
                    "mode": "share_group",
                    "projection_kind": "shared_memory_projection",
                    "source_map": self.get_projection_source_map(scope=parsed),
                    "meta": status_meta,
                }, parsed)
            meta.update(status_meta)
            graph["meta"] = meta
            graph["scope"] = parsed
            graph["source_map"] = self.get_projection_source_map(scope=parsed)
            # 共享组无原生 IR：从投影边/子节点补 members + related，供详情跳转
            graph = self._hydrate_neuron_graph_from_projection(graph)
            return self._with_virtual_neuron_categories(graph, parsed)

        graph_mode = "native" if mode == "native" else "reconstructed"
        key = scope_storage_key(gscope)
        pb = ProjectionBuilder(self.workspace, graph_mode, scope_key=key)
        graph = pb.get_or_empty()
        if graph.get("empty"):
            empty = {
                "empty": True,
                "reason": graph.get("reason", "not_built"),
                "scope": parsed,
                "mode": graph_mode,
                "projection_kind": (
                    "native_memory_projection" if graph_mode == "native"
                    else "reconstructed_governance_projection"
                ),
            }
            empty["source_map"] = self.get_projection_source_map(scope=parsed)
            return self._with_virtual_neuron_categories(empty, parsed)

        reg = SourceRegistry(self.workspace)
        scoped_roots, _ = resolve_scoped_roots(reg.list_all_sources(), gscope, enabled_only=True)
        allowed_ids = {r.root_id for r in scoped_roots}
        meta = graph.get("meta") if isinstance(graph.get("meta"), dict) else {}
        if not projection_auth_matches(meta, allowed_ids):
            return self._with_virtual_neuron_categories({
                "empty": True,
                "reason": "not_built",
                "error": "projection_auth_stale",
                "scope": parsed,
                "mode": graph_mode,
                "projection_kind": (
                    "native_memory_projection" if graph_mode == "native"
                    else "reconstructed_governance_projection"
                ),
                "source_map": self.get_projection_source_map(scope=parsed),
            }, parsed)
        if not allowed_ids:
            return self._with_virtual_neuron_categories({
                "empty": True,
                "reason": "not_built",
                "error": "projection_auth_stale",
                "scope": parsed,
                "mode": graph_mode,
                "projection_kind": (
                    "native_memory_projection" if graph_mode == "native"
                    else "reconstructed_governance_projection"
                ),
                "source_map": self.get_projection_source_map(scope=parsed),
            }, parsed)

        # 投影本身已按 scope 构建；hydrate 补正文。related 限制在当前授权 IR 内。
        norm = MemoryNormalizer(self.workspace)
        global_ir = norm.load()
        related_allow: set[str] | None = None
        if global_ir is not None:
            snap = None
            try:
                from .source_registry import ScanBudget
                snap = reg.scan(ScanBudget())
            except Exception:
                snap = None
            scoped_ir = filter_ir_for_agent(global_ir, allowed_ids, snap)
            related_allow = {r.memory_id for r in scoped_ir.records}
            # 投影上已有的 memory_id 也允许补齐（即使 provenance 暂未映射到 root）
            for node in graph.get("nodes", []):
                mid = node.get("memory_id")
                if mid:
                    related_allow.add(mid)
                for mid in node.get("member_ids") or []:
                    related_allow.add(mid)
            for grp in global_ir.duplicate_groups:
                members = set(grp.member_ids or [])
                if members & related_allow:
                    related_allow |= members

        graph = self._hydrate_neuron_graph_from_ir(graph, allowed_memory_ids=related_allow)
        graph = self._hydrate_neuron_graph_from_projection(graph)
        graph["source_map"] = self.get_projection_source_map(scope=parsed)
        graph["projection_kind"] = (
            "native_memory_projection" if graph_mode == "native"
            else "reconstructed_governance_projection"
        )
        graph["mode"] = graph_mode
        graph["scope"] = parsed
        return self._with_virtual_neuron_categories(graph, parsed)

    @staticmethod
    def _virtual_graph_root(nodes: list[dict]) -> None:
        """Ensure virtual overlays remain visible before a projection exists."""
        if any(str(node.get("id") or "") == "main" for node in nodes):
            return
        nodes.append({
            "id": "main", "parent_id": "", "node_kind": "root",
            "label": "MemoryGuard", "kind": "root", "virtual": True,
        })

    def _with_virtual_neuron_categories(self, graph: dict, scope: dict) -> dict:
        """Add governed indexes without modifying ProjectionBuilder or durable data.

        Rule references point at existing shared records.  Conversation sessions
        contribute only a bounded metadata index; raw turns stay in history.sqlite.
        """
        graph = dict(graph or {})
        nodes = [dict(node) for node in (graph.get("nodes") or [])]
        edges = [dict(edge) for edge in (graph.get("edges") or [])]
        self._virtual_graph_root(nodes)
        node_ids = {str(node.get("id") or "") for node in nodes}

        def add_node(node: dict) -> None:
            if node["id"] not in node_ids:
                nodes.append(node)
                node_ids.add(node["id"])

        def add_edge(source: str, target: str) -> None:
            edge_id = f"virtual-index:{source}:{target}"
            if not any(str(edge.get("id") or "") == edge_id for edge in edges):
                edges.append({
                    "id": edge_id, "source": source, "target": target,
                    "edge_type": "virtual_index", "virtual": True,
                })

        group_id = ""
        rule_scope_error = ""
        if str(scope.get("mode") or "") == "share_group":
            group_id = str(scope.get("share_group_id") or "")
        else:
            try:
                from .agent_binding import AgentBindingStore
                agent_id = str(scope.get("agent_instance_id") or "")
                groups = {
                    str(binding.share_group_id)
                    for binding in AgentBindingStore(self.workspace).find_by_agent(agent_id)
                    if binding.share_group_id
                }
                if len(groups) == 1:
                    group_id = next(iter(groups))
                elif not groups:
                    rule_scope_error = "rules_require_bound_agent"
                else:
                    rule_scope_error = "rules_ambiguous_agent_group"
            except Exception as exc:
                rule_scope_error = str(exc)
        rules_id = "virtual-rules-habits"
        add_node({
            "id": rules_id, "parent_id": "main", "node_kind": "virtual_category",
            "virtual_category": "rules_habits", "label": "规则与习惯",
            "kind": "rules_habits", "count": 0, "virtual": True,
        })
        add_edge("main", rules_id)
        bucket_labels = {
            "mandatory": "强制规则", "preferences": "长期习惯与偏好",
            "procedures": "工作流程", "corrections": "纠错与禁忌",
            "projects": "项目决策",
        }
        try:
            rule_view = (
                self.list_rules_habits(group_id)
                if group_id and not rule_scope_error else {"error": rule_scope_error}
            )
        except Exception as exc:
            rule_view = {"error": str(exc)}
        buckets = rule_view.get("buckets", {}) if isinstance(rule_view, dict) else {}
        rule_total = 0
        for bucket, label in bucket_labels.items():
            all_records = list(buckets.get(bucket) or [])
            records = all_records[:50]
            rule_total += len(all_records)
            bucket_id = f"{rules_id}:{bucket}"
            add_node({
                "id": bucket_id, "parent_id": rules_id, "node_kind": "virtual_bucket",
                "virtual_category": "rules_habits", "bucket": bucket,
                "label": label, "kind": bucket, "count": len(all_records),
                "has_more": len(all_records) > len(records), "virtual": True,
            })
            add_edge(rules_id, bucket_id)
            for record in records:
                memory_id = str(record.get("memory_id") or "")
                if not memory_id:
                    continue
                # This is a governed-memory reference, not a second durable
                # record.  Keep enough fields on the virtual node for the
                # graph rail to govern the original record in place.
                body = str(record.get("body") or "")
                body_preview = " ".join(body.split())[:96]
                ref_id = f"virtual-rule-ref:{bucket}:{memory_id}"
                add_node({
                    "id": ref_id, "parent_id": bucket_id, "node_kind": "virtual_rule_ref",
                    "virtual_category": "rules_habits", "memory_id": memory_id,
                    "kind": str(record.get("kind") or ""),
                    "label": body_preview or str(record.get("title") or "未命名规则"),
                    "body": body,
                    "status": str(record.get("status") or ""),
                    "injection_policy": str(record.get("injection_policy") or "relevant"),
                    "priority": int(record.get("priority") or 0),
                    "assignments": list(record.get("assignments") or []),
                    "audience": str(record.get("audience") or ""),
                    "confidence": record.get("confidence"),
                    "locked": bool(record.get("locked")),
                    "virtual": True,
                })
                add_edge(bucket_id, ref_id)
        for node in nodes:
            if node.get("id") == rules_id:
                node["count"] = rule_total
                if rule_view.get("error"):
                    node["load_error"] = str(rule_view["error"])

        history_id = "virtual-conversation-history"
        history_node = {
            "id": history_id, "parent_id": "main", "node_kind": "virtual_category",
            "virtual_category": "conversation_history", "label": "对话历史",
            "kind": "conversation_history", "count": 0, "virtual": True,
        }
        add_node(history_node)
        add_edge("main", history_id)
        try:
            from .agent_binding import AgentBindingStore
            from .conversation_history import ConversationHistoryStore, HistoryAccessResolver

            mode = str(scope.get("mode") or "agent")
            agent_id = str(scope.get("agent_instance_id") or "")
            requested_group = str(scope.get("share_group_id") or "") if mode == "share_group" else ""
            if requested_group and not agent_id:
                members = AgentBindingStore(self.workspace).find_by_group(
                    requested_group, include_inactive=False,
                )
                agent_id = str(members[0].agent_instance_id) if members else ""
            if not agent_id:
                raise PermissionError("history_active_binding_required")
            history_request = (
                {
                    "mode": "share_group",
                    "share_group_id": requested_group,
                }
                if requested_group
                else {
                    "mode": "agent",
                    "agent_instance_id": agent_id,
                }
            )
            history_scope = HistoryAccessResolver(self.workspace).resolve(agent_id, {
                **history_request,
            })
            history = ConversationHistoryStore(self.workspace).list_sessions(
                history_scope, limit=51, offset=0,
            )
            sessions = list(history.get("sessions") or [])
            total = int(history.get("total") or len(sessions))
            visible = sessions[:50]
            history_node["count"] = total
            history_node["total"] = total
            history_node["has_more"] = total > len(visible)
            history_node["project_groups"] = list(history.get("project_groups") or [])
            for session in visible:
                session_id = str(session.get("session_id") or "")
                project_key = str(session.get("project_key") or "")
                owner = str(session.get("owner_agent_instance_id") or "")
                if not session_id or not project_key or not owner:
                    continue
                project_id = f"virtual-history-project:{project_key}"
                agent_id = "virtual-history-agent:" + hashlib.sha256(
                    f"{project_key}\x1f{owner}".encode("utf-8")
                ).hexdigest()[:20]
                add_node({
                    "id": project_id, "parent_id": history_id,
                    "node_kind": "history_project", "virtual_category": "conversation_history",
                    "project_key": project_key, "project_ref": str(session.get("project_ref") or ""),
                    "project_status": str(session.get("project_status") or "unknown"),
                    "project_parent": str(session.get("project_parent") or ""),
                    "label": str(session.get("project_label") or "未识别项目"), "virtual": True,
                })
                add_edge(history_id, project_id)
                add_node({
                    "id": agent_id, "parent_id": project_id,
                    "node_kind": "history_agent", "virtual_category": "conversation_history",
                    "owner_agent_instance_id": owner, "label": owner, "virtual": True,
                })
                add_edge(project_id, agent_id)
                node_id = f"virtual-history-session:{session_id}"
                add_node({
                    "id": node_id, "parent_id": agent_id,
                    "node_kind": "history_session", "virtual_category": "conversation_history",
                    "session_id": session_id, "title": str(session.get("title") or ""),
                    "label": str(session.get("title") or session_id[:8]),
                    "owner_agent_instance_id": owner, "provider": str(session.get("provider") or ""),
                    "project_key": project_key, "project_ref": str(session.get("project_ref") or ""),
                    "project_status": str(session.get("project_status") or "unknown"),
                    "created_at": str(session.get("created_at") or ""),
                    "imported_at": str(session.get("imported_at") or ""),
                    "summary": str(session.get("summary") or ""),
                    "turn_count": int(session.get("turn_count") or 0),
                    "evidence_count": int(session.get("evidence_count") or 0), "virtual": True,
                })
                add_edge(agent_id, node_id)
        except Exception as exc:
            history_node["load_error"] = str(exc)

        graph["nodes"] = nodes
        graph["edges"] = edges
        # Preserve the projection's fail-closed truth value.  The overlay is
        # browseable UI metadata, not evidence that a missing/stale projection
        # may be treated as valid by a caller.
        graph["base_empty"] = bool(graph.get("empty") or graph.get("base_empty"))
        graph["virtual_overlay_available"] = True
        stats = dict(graph.get("stats") or {})
        stats["node_count"] = len(nodes)
        stats["edge_count"] = len(edges)
        graph["stats"] = stats
        return graph

    def _localized_record_fields(self, rec: dict) -> dict:
        from .memory_ir import localized_record_fields
        return localized_record_fields(rec)

    def _hydrate_neuron_graph_from_projection(self, graph: dict) -> dict:
        """从投影自身的边/子节点补齐 members 与 related（共享组与跨类型边）。

        不依赖 Memory IR；已有 related/members 时追加去重，不覆盖已有条目。
        """
        if not graph or graph.get("empty"):
            return graph
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        by_id = {n.get("id"): n for n in nodes if n.get("id")}
        children: dict[str, list[dict]] = {}
        by_memory: dict[str, dict] = {}
        for n in nodes:
            children.setdefault(str(n.get("parent_id") or ""), []).append(n)
            mid = n.get("memory_id")
            if mid:
                by_memory[mid] = n

        label_map = {
            "related": "相似关联",
            "shared_source": "同源跨类型",
            "duplicate": "重复候选",
        }
        related_by_mid: dict[str, list[dict]] = {}
        for edge in edges:
            etype = str(edge.get("edge_type") or "")
            if etype not in label_map:
                continue
            a = by_id.get(edge.get("source"))
            b = by_id.get(edge.get("target"))
            if not a or not b:
                continue
            for src, dst in ((a, b), (b, a)):
                mid = src.get("memory_id")
                oid = dst.get("memory_id")
                if not mid or not oid or mid == oid:
                    continue
                bucket = related_by_mid.setdefault(mid, [])
                if any(x.get("memory_id") == oid and x.get("relation") == etype for x in bucket):
                    continue
                bucket.append({
                    "memory_id": oid,
                    "title": dst.get("title") or dst.get("label") or oid[:8],
                    "kind": dst.get("kind") or "",
                    "body_preview": (dst.get("body") or "")[:160],
                    "relation": etype,
                    "relation_label": label_map[etype],
                    "relation_reason": str(edge.get("reason") or edge.get("label") or ""),
                })

        for node in nodes:
            kind = node.get("node_kind")
            member_ids = list(node.get("member_ids") or [])
            if not member_ids and kind == "source_hub":
                member_ids = [
                    c.get("memory_id")
                    for c in children.get(node.get("id") or "", [])
                    if c.get("node_kind") == "claim_anchor" and c.get("memory_id")
                ]
                node["member_ids"] = member_ids
            if member_ids and not node.get("members"):
                members = []
                for mid in member_ids:
                    claim = by_memory.get(mid)
                    if not claim:
                        continue
                    members.append({
                        "memory_id": mid,
                        "title": claim.get("title") or claim.get("label") or mid[:8],
                        "kind": claim.get("kind") or "",
                        "body_preview": (claim.get("body") or "")[:180],
                    })
                if members:
                    node["members"] = members
            mid = node.get("memory_id")
            if not mid:
                continue
            extra = related_by_mid.get(mid) or []
            if not extra:
                continue
            existing = list(node.get("related") or [])
            seen = {(x.get("memory_id"), x.get("relation") or x.get("relation_label")) for x in existing}
            for item in extra:
                key = (item.get("memory_id"), item.get("relation"))
                if key in seen:
                    continue
                existing.append(item)
                seen.add(key)
            node["related"] = existing[:12]
        return graph

    def _hydrate_neuron_graph_from_ir(self, graph: dict,
                                     allowed_memory_ids: set | None = None) -> dict:
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
        if allowed_memory_ids is not None:
            records = {mid: rec for mid, rec in records.items() if mid in allowed_memory_ids}
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
                        "relation_label": "重复候选",
                        "relation_reason": f"同属重复组 {str(group.get('group_id', ''))[:8]}",
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

    def build_projection(self, confirmed: bool = False, mode: str = "reconstructed",
                         scope: dict | None = None,
                         agent_instance_id: str = "",
                         share_group_id: str = "",
                         progress=None,
                         llm_agent: str = "",
                         llm_cli: str = "",
                         enrich_mode: str = "auto") -> dict:
        """构建 scoped 神经图投影。重构/共享组路径会入队并用 LLM 整理后再出图。

        enrich_mode: auto|host|cli|heuristic
        llm_agent=host/skill/mcp 时强制 host（留给 Skill 对话模型整理）。
        """
        if not confirmed:
            return {"error": "需要确认才能构建投影"}

        resolved_enrich = (enrich_mode or "auto").strip().lower()
        if (llm_agent or "").strip().lower() in {"host", "skill", "mcp"}:
            resolved_enrich = "host"
            llm_agent, llm_cli = "host", ""
        elif llm_agent and llm_cli and resolved_enrich == "auto":
            resolved_enrich = "cli"

        def _progress(phase: str, message: str, percent: int | None = None) -> None:
            if not callable(progress):
                return
            # 允许 BuildCancelled 向上抛出以便中断构建
            progress(phase, message, percent)

        from .governance_scope import (
            GovernanceScope, build_shared_memory_graph, scope_storage_key,
            resolve_scoped_roots, filter_ir_for_agent, save_scope_preference,
            share_group_projection_path, authorized_roots_digest,
        )
        from .memory_ir import MemoryNormalizer
        from .projection import ProjectionBuilder
        from .source_registry import SourceRegistry, ScanBudget
        from .managed_store import ManagedStore
        from .agent_locator import AgentLocator, compute_takeover_state
        from pathlib import Path
        import json as _json

        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
        )
        if err:
            return {"error": err}
        gscope = GovernanceScope.from_dict(parsed)
        save_scope_preference(self.workspace, gscope)

        if gscope.mode == "share_group":
            apply_stats: dict = {"applied": 0, "rejected": 0, "engine": "none"}
            if mode != "native":
                from .host_enrichment import enqueue_from_shared_store, get_status
                _progress("enrich_queue", "正在入队待整理项…", 25)
                enqueued = enqueue_from_shared_store(
                    self.workspace, gscope.share_group_id, reason="share_group_rebuild",
                )
                if resolved_enrich == "host":
                    _progress("enrich", "宿主 Skill 模式：不在 GUI 内调模型，等待对话整理…", 45)
                else:
                    _progress("enrich", "正在用 LLM 整理共享记忆…", 45)
                apply_stats = _enrich_pending_during_build(
                    self.workspace,
                    share_group_id=gscope.share_group_id,
                    llm_agent=llm_agent,
                    llm_cli=llm_cli,
                    progress=progress,
                    enrich_mode=resolved_enrich,
                )
                apply_stats["enqueued"] = enqueued
                # host：只要选了宿主 Skill，就算暂无 pending 也不能谎称「已模型整理」
                if resolved_enrich == "host":
                    apply_stats["engine"] = apply_stats.get("engine") or "host_deferred"
                    if apply_stats.get("pending_count", 0) > 0:
                        apply_stats["host_action_required"] = True
                    apply_stats["hint"] = (
                        "宿主 Skill 未在对话中执行：请让 Cursor 对话调用 "
                        "memoryguard_list_pending_enrichments → 整理 → apply → 再 build。"
                        "GUI 无法自动唤起当前聊天。"
                    )
            _progress("graph", "正在构建共享组投影…", 75)
            graph = build_shared_memory_graph(self.workspace, gscope.share_group_id)
            out_path = share_group_projection_path(self.workspace, gscope)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tomb = out_path.with_suffix(out_path.suffix + ".deleted")
            if tomb.exists():
                tomb.unlink()
            out_path.write_text(_json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
            graph["built"] = True
            from .host_enrichment import get_status
            enr_status = get_status(self.workspace, share_group_id=gscope.share_group_id)
            graph["enrichment"] = {
                "pending_count": enr_status["pending"],
                "applied_count": enr_status["applied"],
                "enqueued": apply_stats.get("enqueued", 0),
                "auto_applied": apply_stats.get("applied", 0),
                "auto_rejected": apply_stats.get("rejected", 0),
                "engine": apply_stats.get("engine", "none"),
                "enrich_mode": apply_stats.get("enrich_mode", resolved_enrich),
                "host_action_required": bool(apply_stats.get("host_action_required")),
                "pending_tasks": apply_stats.get("pending_tasks") or [],
                "mode": "build_integrated",
                "hint": apply_stats.get("hint") or (
                    "宿主 Skill 请立即整理 pending 后 apply"
                    if apply_stats.get("host_action_required")
                    else "构建内已整理；残留 pending 可用 MCP list/apply 补做"
                ),
            }
            graph["scope"] = parsed
            _progress("done", "共享组投影已生成", 100)
            return graph

        _progress("scan", "正在扫描已授权来源…", 8)
        reg = SourceRegistry(self.workspace)
        snap = reg.scan(ScanBudget())
        all_roots = reg.list_sources()
        root_map = {r.root_id: r.path for r in all_roots}
        root_policies = {
            r.root_id: {
                "source_category": r.source_category,
                "ingestion_policy": r.ingestion_policy,
            }
            for r in all_roots
        }
        _progress("normalize", "正在规范化记忆 IR…", 22)
        norm = MemoryNormalizer(self.workspace)
        global_ir = norm.load()
        if global_ir is None or global_ir.snapshot_id != snap.snapshot_id:
            global_ir = norm.normalize(snap, root_map=root_map, root_policies=root_policies)
            norm.ensure_localized(global_ir)
            norm.save(global_ir)
        else:
            changed = norm.filter_by_source_policies(global_ir, snap, root_policies)
            changed = norm.ensure_localized(global_ir) or changed
            if changed:
                norm.save(global_ir)

        apply_stats = {"applied": 0, "rejected": 0, "engine": "none"}
        _progress("scope", "正在按治理范围筛选记忆…", 40)
        scoped_roots, rerr = resolve_scoped_roots(reg.list_all_sources(), gscope, enabled_only=True)
        if rerr:
            return {"error": rerr}
        allowed_ids = {r.root_id for r in scoped_roots}
        scoped_ir = filter_ir_for_agent(global_ir, allowed_ids, snap)

        agent_id = gscope.agent_instance_id
        # 重构路径：入队 → LLM 整理 → 重载 IR 再出图（避免用旧内存建图）
        if mode != "native":
            from .host_enrichment import enqueue_from_ir
            _progress("enrich_queue", "正在入队待整理项…", 48)
            enqueue_from_ir(
                self.workspace,
                scoped_ir,
                scope={"mode": "agent", "agent_instance_id": agent_id},
                reason="projection_rebuild",
            )
            if resolved_enrich == "host":
                _progress("enrich", "宿主 Skill 模式：不在 GUI 内调模型，等待对话整理…", 58)
            else:
                _progress("enrich", "正在用 LLM 整理记忆…", 58)
            apply_stats = _enrich_pending_during_build(
                self.workspace,
                agent_instance_id=agent_id,
                llm_agent=llm_agent,
                llm_cli=llm_cli,
                progress=progress,
                enrich_mode=resolved_enrich,
            )
            if resolved_enrich == "host":
                apply_stats["engine"] = apply_stats.get("engine") or "host_deferred"
                if apply_stats.get("pending_count", 0) > 0:
                    apply_stats["host_action_required"] = True
                apply_stats["hint"] = (
                    "宿主 Skill 未在对话中执行：请让 Cursor 对话调用 "
                    "memoryguard_list_pending_enrichments → 整理 → apply → 再 build。"
                    "GUI 无法自动唤起当前聊天。"
                )
            if apply_stats.get("applied", 0) > 0:
                reloaded = norm.load()
                if reloaded is not None:
                    global_ir = reloaded
                    scoped_ir = filter_ir_for_agent(global_ir, allowed_ids, snap)

        store = ManagedStore(self.workspace, agent_id)
        if store.get_active_version_id() is None:
            store.create_initial_version(scoped_ir.records)
        else:
            store.sync_records_from_ir(scoped_ir.records, notes="projection rebuild sync")
        active = store.get_active_version()
        managed_meta = {
            agent_id: {
                "version_id": active.version_id if active else "",
                "record_count": len(scoped_ir.records),
                "decision_count": active.decision_count if active else 0,
            }
        }

        _progress("graph", "正在生成神经图投影…", 78)
        locator = AgentLocator(self.workspace)
        instances, ledgers = locator.detect_instances()
        cov_counts = snap.coverage.counts()
        cov_status = snap.coverage.status().value
        releases_list: list[dict] = []
        try:
            from .release_manager import ReleaseManager
            releases_list = ReleaseManager(self.workspace).list_releases()
        except Exception:
            pass

        agent_instances_meta = []
        for inst in instances:
            if inst.instance_id != agent_id:
                continue
            inst_ledger = ledgers.get(inst.instance_id)
            mm = managed_meta.get(inst.instance_id, {})
            has_managed = bool(mm)
            takeover_state = compute_takeover_state(
                instance=inst,
                ledger=inst_ledger,
                selection_committed=has_managed,
                canonicalized=has_managed,
                release_planned=any(r.get("instance_id") == inst.instance_id for r in releases_list),
                published=any(
                    r.get("instance_id") == inst.instance_id and r.get("status") == "applied"
                    for r in releases_list
                ),
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
            "governance_scope": parsed,
            "authorized_root_ids": sorted(allowed_ids),
            "authorized_roots_digest": authorized_roots_digest(allowed_ids),
        }
        graph_mode = "native" if mode == "native" else "reconstructed"
        key = scope_storage_key(gscope)
        pb = ProjectionBuilder(self.workspace, graph_mode, scope_key=key)
        proj = pb.build(scoped_ir, meta=meta)
        _progress("save", "正在保存投影…", 92)
        pb.save(proj)
        result = proj.to_dict()
        result["mode"] = graph_mode
        result["scope"] = parsed
        result["projection_kind"] = (
            "native_memory_projection" if graph_mode == "native"
            else "reconstructed_governance_projection"
        )
        result["scoped_record_count"] = len(scoped_ir.records)
        result["scoped_root_count"] = len(scoped_roots)
        from .host_enrichment import get_status
        enr_status = get_status(self.workspace, agent_instance_id=agent_id)
        result["enrichment"] = {
            "pending_count": enr_status["pending"],
            "applied_count": enr_status["applied"],
            "auto_applied": apply_stats.get("applied", 0),
            "auto_rejected": apply_stats.get("rejected", 0),
            "engine": apply_stats.get("engine", "none"),
            "enrich_mode": apply_stats.get("enrich_mode", resolved_enrich if mode != "native" else "none"),
            "host_action_required": bool(apply_stats.get("host_action_required")),
            "pending_tasks": apply_stats.get("pending_tasks") or [],
            "mode": "build_integrated" if mode != "native" else "skipped",
            "hint": apply_stats.get("hint") or (
                "宿主 Skill 请立即整理 pending 后 apply"
                if apply_stats.get("host_action_required")
                else "构建内已整理；残留 pending 可用 MCP list/apply 补做"
            ),
        }
        _progress("done", "构建完成", 100)
        return result

    def start_build_projection(self, confirmed: bool = False, mode: str = "reconstructed",
                               scope: dict | None = None,
                               agent_instance_id: str = "",
                               share_group_id: str = "",
                               llm_agent: str = "",
                               llm_cli: str = "",
                               enrich_mode: str = "auto") -> dict:
        """后台启动构建（含 LLM 整理），立即返回 job_id。"""
        if not confirmed:
            return {"error": "需要确认才能构建投影"}
        import threading
        import uuid

        with self._build_lock:
            if self._active_build_job:
                active = self._build_jobs.get(self._active_build_job) or {}
                if active.get("status") == "running":
                    return {
                        "error": "构建进行中，请勿重复点击",
                        "busy": True,
                        "job_id": self._active_build_job,
                        "status": "running",
                    }
            job_id = uuid.uuid4().hex[:12]
            self._build_jobs[job_id] = {
                "job_id": job_id,
                "status": "running",
                "phase": "starting",
                "message": "正在启动构建…",
                "percent": 0,
                "result": None,
                "error": "",
                "cancel_requested": False,
            }
            self._active_build_job = job_id

        def _run() -> None:
            def progress(phase: str, message: str, percent: int | None = None) -> None:
                with self._build_lock:
                    job = self._build_jobs.get(job_id)
                    if not job:
                        return
                    if job.get("cancel_requested"):
                        raise BuildCancelled("用户取消构建")
                    job["phase"] = phase
                    job["message"] = message
                    if percent is not None:
                        job["percent"] = percent

            try:
                result = self.build_projection(
                    confirmed=True,
                    mode=mode,
                    scope=scope,
                    agent_instance_id=agent_instance_id,
                    share_group_id=share_group_id,
                    progress=progress,
                    llm_agent=llm_agent,
                    llm_cli=llm_cli,
                    enrich_mode=enrich_mode,
                )
                with self._build_lock:
                    job = self._build_jobs.get(job_id)
                    if not job:
                        return
                    if job.get("cancel_requested"):
                        job["status"] = "cancelled"
                        job["message"] = "构建已取消"
                        job["phase"] = "cancelled"
                        job["percent"] = job.get("percent") or 0
                        job["error"] = ""
                    elif result.get("error"):
                        job["status"] = "error"
                        job["error"] = str(result["error"])
                        job["message"] = str(result["error"])
                        job["percent"] = 100
                    else:
                        job["status"] = "done"
                        job["result"] = result
                        job["message"] = "构建完成"
                        job["phase"] = "done"
                        job["percent"] = 100
            except BuildCancelled:
                with self._build_lock:
                    job = self._build_jobs.get(job_id)
                    if job:
                        job["status"] = "cancelled"
                        job["message"] = "构建已取消"
                        job["phase"] = "cancelled"
                        job["error"] = ""
            except Exception as exc:
                with self._build_lock:
                    job = self._build_jobs.get(job_id)
                    if job:
                        if job.get("cancel_requested"):
                            job["status"] = "cancelled"
                            job["message"] = "构建已取消"
                            job["phase"] = "cancelled"
                            job["error"] = ""
                        else:
                            job["status"] = "error"
                            job["error"] = str(exc)
                            job["message"] = str(exc)
                            job["percent"] = 100
            finally:
                with self._build_lock:
                    if self._active_build_job == job_id:
                        self._active_build_job = None

        threading.Thread(target=_run, daemon=True, name=f"mg-build-{job_id}").start()
        return {"job_id": job_id, "status": "running", "message": "正在启动构建…"}

    def cancel_build_projection(self, job_id: str = "", confirmed: bool = False) -> dict:
        """请求取消正在进行的构建任务。"""
        if not confirmed:
            return {"error": "需要确认才能取消构建"}
        with self._build_lock:
            jid = job_id or self._active_build_job or ""
            job = self._build_jobs.get(jid) if jid else None
            if not job:
                return {"error": "没有可取消的构建任务", "job_id": jid}
            if job.get("status") != "running":
                return {
                    "ok": True,
                    "job_id": jid,
                    "status": job.get("status"),
                    "message": "任务已不在运行",
                }
            job["cancel_requested"] = True
            job["message"] = "正在取消…"
            return {"ok": True, "job_id": jid, "status": "cancelling", "message": "正在取消…"}

    def get_build_progress(self, job_id: str = "") -> dict:
        """读取构建任务进度（只读）。"""
        with self._build_lock:
            jid = job_id or self._active_build_job or ""
            job = self._build_jobs.get(jid) if jid else None
            if not job:
                return {"status": "unknown", "error": "job not found", "job_id": jid}
            out = {
                "job_id": job.get("job_id", jid),
                "status": job.get("status", "unknown"),
                "phase": job.get("phase", ""),
                "message": job.get("message", ""),
                "percent": job.get("percent", 0),
                "error": job.get("error", ""),
                "cancel_requested": bool(job.get("cancel_requested")),
            }
            if job.get("status") == "done":
                out["result"] = job.get("result")
            return out

    def delete_projection(self, confirmed: bool = False, mode: str = "reconstructed",
                          scope: dict | None = None,
                          agent_instance_id: str = "",
                          share_group_id: str = "") -> dict:
        """删除当前 scope 的投影文件。"""
        if not confirmed:
            return {"error": "需要确认才能删除投影"}
        from .governance_scope import GovernanceScope, scope_storage_key, share_group_projection_path
        from .projection import ProjectionBuilder

        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
        )
        if err:
            return {"error": err}
        gscope = GovernanceScope.from_dict(parsed)
        graph_mode = "native" if mode == "native" else "reconstructed"
        if gscope.mode == "share_group":
            path = share_group_projection_path(self.workspace, gscope)
            if path.exists():
                path.unlink()
            tomb = path.with_suffix(path.suffix + ".deleted")
            tomb.parent.mkdir(parents=True, exist_ok=True)
            tomb.write_text("deleted", encoding="utf-8")
            return {"ok": True, "deleted": True, "mode": "share_group", "scope": parsed}
        key = scope_storage_key(gscope)
        pb = ProjectionBuilder(self.workspace, graph_mode, scope_key=key)
        pb.delete()
        return {"ok": True, "deleted": True, "mode": graph_mode, "scope": parsed}

    # ------------------------------------------------------------------
    # SourceApi（spec §7.2）
    # ------------------------------------------------------------------

    def list_publish_targets(self, scope: dict | None = None,
                               agent_instance_id: str = "") -> dict:
        """列出当前 agent scope 可写回的 native 目标。"""
        from .source_registry import SourceRegistry
        from .governance_scope import root_authorizes_agent, resolve_scoped_roots, GovernanceScope

        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id, mode="agent",
        )
        if err:
            return {"error": err, "targets": [], "total": 0}
        if parsed["mode"] != "agent":
            return {"error": "agent_scope_required", "targets": [], "total": 0}
        gscope = GovernanceScope.from_dict(parsed)
        native_categories = {"native_memory", "project_memory"}
        roots, _ = resolve_scoped_roots(
            SourceRegistry(self.workspace).list_all_sources(), gscope, enabled_only=True,
        )
        from .governance_scope import derive_publish_target_file, is_root_enabled_for_agent
        targets = []
        for root in roots:
            if root.source_category not in native_categories:
                continue
            if not root_authorizes_agent(root, gscope.agent_instance_id):
                continue
            if not is_root_enabled_for_agent(root, gscope.agent_instance_id):
                continue
            target_file = derive_publish_target_file(root)
            path = Path(root.path)
            targets.append({
                "root_id": root.root_id,
                "display_name": root.display_name,
                "target_file": str(target_file),
                "source_category": root.source_category,
                "agent_instance_id": gscope.agent_instance_id,
                "authorized_agent_ids": list(getattr(root, "authorized_agent_ids", []) or []),
                "surface_id": root.surface_id,
                "scope": root.scope,
                "project_ref": root.project_ref,
                "ownership": root.ownership,
                "target_role": root.target_role,
                "is_agent_native_memory": (
                    root.source_category == "native_memory"
                    and root.ownership == "agent_managed"
                    and root.target_role == "takeover_input"
                ),
                "path_kind": "file" if path.suffix else "folder_default_memory_md",
            })
        return {"targets": targets, "total": len(targets), "scope": parsed}

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

    def publish_reconstructed_memory(self, target_file: str = "", confirmed: bool = False,
                                   use_distilled: bool = True,
                                   scope: dict | None = None,
                                   agent_instance_id: str = "",
                                   target_root_id: str = "") -> dict:
        if not confirmed:
            return {"error": "需要确认才能发布重构记忆"}
        import os
        from .adapters import GenericMarkdownTarget
        from .memory_ir import MemoryIR, MemoryNormalizer
        from .release_manager import ReleaseManager
        from .source_registry import SourceRegistry, ScanBudget
        from .managed_store import ManagedStore
        from .governance_scope import (
            GovernanceScope, filter_ir_for_agent, resolve_scoped_roots,
            root_authorizes_agent, derive_publish_target_file, is_root_enabled_for_agent,
        )
        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id, mode="agent",
        )
        if err or not parsed or parsed.get("mode") != "agent":
            return {"error": err or "agent_scope_required"}
        gscope = GovernanceScope.from_dict(parsed)
        if not target_root_id:
            return {"error": "target_root_id_required"}
        reg = SourceRegistry(self.workspace)
        root = next((r for r in reg.list_all_sources() if r.root_id == target_root_id), None)
        if root is None:
            return {"error": f"target_root_not_found: {target_root_id}"}
        if not root_authorizes_agent(root, gscope.agent_instance_id):
            return {"error": "target_root_not_authorized_for_agent"}
        if not is_root_enabled_for_agent(root, gscope.agent_instance_id):
            return {"error": "target_root_disabled_for_agent"}
        native_categories = {"native_memory", "project_memory"}
        if root.source_category not in native_categories:
            return {"error": "target_root_not_publishable_category"}
        # 服务端派生路径；客户端 target_file 仅作一致性校验（可空）
        derived = derive_publish_target_file(root)
        if target_file:
            try:
                if Path(target_file).resolve() != derived:
                    return {"error": "target_file_mismatch_root"}
            except OSError:
                return {"error": "target_file_mismatch_root"}
        target_file = str(derived)
        if os.environ.get("MEMORYGUARD_PUBLISH_RAW") == "1":
            use_distilled = False

        agent_id = gscope.agent_instance_id
        store = ManagedStore(self.workspace, agent_id)
        norm = MemoryNormalizer(self.workspace)
        global_ir = norm.load()
        snap = reg.scan(ScanBudget())
        scoped_roots, _ = resolve_scoped_roots(reg.list_all_sources(), gscope, enabled_only=True)
        allowed_ids = {r.root_id for r in scoped_roots}
        if not allowed_ids:
            return {"error": "no_authorized_roots"}
        if global_ir is None:
            return {"error": "没有可发布的重构记忆"}
        if norm.ensure_localized(global_ir):
            norm.save(global_ir)
        # 合成 obj→root：文件缺失时 scan 可能漏对象，仍按稳定 hash 归属（防误拒）
        from .source_registry import normalize_rel_path
        from .schema_v3 import stable_hash
        obj_to_root = {obj.source_object_id: obj.source_root_id for obj in snap.source_objects}
        for r in scoped_roots:
            p = Path(r.path)
            if p.suffix:
                oid = stable_hash(r.root_id, normalize_rel_path(p.name))
                obj_to_root.setdefault(oid, r.root_id)
            else:
                for name in ("memory.md", p.name):
                    oid = stable_hash(r.root_id, normalize_rel_path(name))
                    obj_to_root.setdefault(oid, r.root_id)
        scoped_ir = filter_ir_for_agent(
            global_ir, allowed_ids, snap, obj_to_root=obj_to_root,
        )
        # ManagedStore 仅覆盖 status；rejected 不发布；不得整条替换绕过过滤
        if store.get_active_version_id() is not None:
            import copy as _copy
            managed_by_id = {r.memory_id: r for r in store.list_records()}
            merged = []
            for rec in scoped_ir.records:
                managed = managed_by_id.get(rec.memory_id)
                if managed is None:
                    merged.append(rec)
                    continue
                status_val = getattr(managed.status, "value", str(managed.status))
                if status_val == "rejected":
                    continue
                overlay = _copy.deepcopy(rec)
                overlay.status = managed.status
                merged.append(overlay)
            ir = MemoryIR(
                records=merged,
                duplicate_groups=list(scoped_ir.duplicate_groups),
                snapshot_id=scoped_ir.snapshot_id or f"managed-{agent_id}",
            )
        else:
            ir = scoped_ir
        if not ir.records:
            return {"error": "scoped_ir_empty"}
        publish_ir, distill_stats, source_map = _build_publish_ir(
            ir, self.workspace, use_distilled,
        )
        publish_ir, redactions = _redact_publish_ir(publish_ir, source_map)
        target_dir, exact_file, path_warnings = _resolve_publish_target_dir(target_file)
        target_dir.mkdir(parents=True, exist_ok=True)
        rm = ReleaseManager(self.workspace)
        target_adapter = GenericMarkdownTarget()
        try:
            plan = rm.create_build_plan(
                publish_ir, target_adapter, target_dir,
                governance_scope=parsed, target_root_id=target_root_id,
            )
            release = rm.apply_build(
                plan.plan_id, target_adapter, target_dir, approval=True,
                expected_scope=parsed, expected_target_root_id=target_root_id,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        ok = _release_ok(release.status)
        errors: list[str] = []
        if ok and exact_file is not None and exact_file.name.lower() != "memory.md":
            try:
                release = _sync_exact_file_into_release(
                    release, target_dir, exact_file, self.workspace,
                )
            except Exception as exc:
                try:
                    rb = rm.rollback_release(
                        release.release_id,
                        target_adapter,
                        target_dir,
                        release_override=release,
                        expected_scope=parsed,
                        expected_target_root_id=target_root_id,
                        expected_target_path=target_dir,
                    )
                    ok = False
                    errors = [f"exact_file sync failed: {exc}"]
                    rb_result = (rb.verify_result or {}).get("rollback_result", {})
                    errors.extend(rb_result.get("errors", []))
                    release = rb
                except Exception as rb_exc:
                    vr = target_adapter.rollback(release, target_dir)
                    ok = False
                    errors = [
                        f"exact_file sync failed: {exc}",
                        f"rollback: {rb_exc}",
                        *list(vr.errors or []),
                    ]
        releases_dir = Path(self.workspace) / ".memoryguard" / "releases"
        manifest_path = str(releases_dir / f"{release.release_id}.json")
        if not ok and not errors:
            vr = release.verify_result or {}
            errors = list(vr.get("errors", []))
        out: dict = {
            "ok": ok,
            "release_id": release.release_id,
            "status": release.status.value if hasattr(release.status, "value") else str(release.status),
            "build_id": release.build_id,
            "record_type": "memory_release",
            "distilled": use_distilled,
            "verify_result": release.verify_result,
            "manifest_path": manifest_path,
            "errors": errors,
            "published_record_count": plan.manifest.published_record_count,
            "record_mapping_count": len(plan.manifest.record_mappings),
            "scope": parsed,
            "target_root_id": target_root_id,
            "published_target_file": str(derived),
        }
        if ok and exact_file is not None and exact_file.name.lower() != "memory.md":
            out["published_target_file"] = str(exact_file.resolve())
            out["sidecar_memory_md"] = str(target_dir / "memory.md")
        if distill_stats is not None:
            out["distill_stats"] = distill_stats
        if redactions:
            out["redactions"] = redactions
        if path_warnings:
            out["warnings"] = path_warnings
        if ok:
            targets = self.list_publish_targets(scope=parsed).get("targets", [])
            matched_target = next(
                (t for t in targets if t.get("root_id") == target_root_id),
                None,
            )
            if matched_target:
                surface_id = matched_target.get("surface_id", "") or "generic_markdown"
                is_native = matched_target.get("is_agent_native_memory", False)
                capability = "native_takeover" if is_native else "export_only"
                verify_result = _verify_takeover(
                    target_dir, exact_file, publish_ir,
                    surface_id=surface_id, capability=capability,
                )
                out["takeover_verify"] = verify_result
                try:
                    releases_dir = Path(self.workspace) / ".memoryguard" / "releases"
                    release_path = releases_dir / f"{release.release_id}.json"
                    if release_path.exists():
                        rdata = _json.loads(release_path.read_text(encoding="utf-8"))
                        rdata["takeover_verify"] = verify_result
                        rdata["runtime_verified"] = verify_result.get("runtime_verified", False)
                        tmp = release_path.with_name(release_path.name + ".tmp")
                        tmp.write_text(
                            _json.dumps(rdata, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        os.replace(tmp, release_path)
                except Exception:
                    pass
        return out

    def rollback_native_memory_release(
        self,
        release_id: str,
        force: bool = False,
        confirmed: bool = False,
        scope: dict | None = None,
        agent_instance_id: str = "",
        target_root_id: str = "",
    ) -> dict:
        if not confirmed:
            return {"error": "需要确认才能回滚原生记忆"}
        from .adapters import GenericMarkdownTarget
        from .change_history import get_release
        from .native_file_release import SafeNativeFilePublisher
        from .release_manager import ReleaseManager
        from .schema_v3 import ReleaseStatus
        from .source_registry import SourceRegistry
        from .governance_scope import (
            GovernanceScope, derive_publish_target_file, root_authorizes_agent,
        )
        parsed, err = self._parse_scope(scope, agent_instance_id=agent_instance_id, mode="agent")
        if err or not parsed or parsed.get("mode") != "agent":
            return {"error": err or "agent_scope_required"}
        if not target_root_id:
            return {"error": "target_root_id_required"}
        gscope = GovernanceScope.from_dict(parsed)
        reg = SourceRegistry(self.workspace)
        root = next((r for r in reg.list_all_sources() if r.root_id == target_root_id), None)
        if root is None or not root_authorizes_agent(root, gscope.agent_instance_id):
            return {"error": "target_root_not_authorized_for_agent"}
        derived = derive_publish_target_file(root)
        derived_dir = derived.parent if derived.suffix else derived
        file_root = bool(Path(root.path).suffix) or bool(derived.suffix)
        try:
            derived_res = derived.resolve()
            derived_dir_res = derived_dir.resolve()
        except OSError:
            return {"error": "target_root_path_unresolvable"}

        def _path_authorized(path: Path) -> bool:
            try:
                p = path.resolve()
            except OSError:
                return False
            if file_root:
                # 单文件 root：只允许精确命中目标文件，禁止同目录兄弟
                return p == derived_res
            if p == derived_dir_res:
                return True
            try:
                p.relative_to(derived_dir_res)
                return True
            except ValueError:
                return False

        if release_id.startswith("nrel-"):
            pub = SafeNativeFilePublisher(self.workspace)
            native_manifest = Path(self.workspace) / ".memoryguard" / "native_releases" / release_id / "manifest.json"
            if not native_manifest.exists():
                return {"error": "release not found"}
            import json as _json
            manifest = _json.loads(native_manifest.read_text(encoding="utf-8"))
            targets = [Path(item.get("target_path", "")) for item in manifest.get("files", [])]
            if not targets or any(not _path_authorized(t) for t in targets if str(t)):
                return {"error": "native_release_not_authorized_for_agent"}
            return pub.rollback(release_id, force=force).to_dict()

        data = get_release(Path(self.workspace), release_id)
        if data is not None:
            try:
                ReleaseManager.validate_release_binding(
                    data,
                    expected_scope=parsed,
                    expected_target_root_id=target_root_id,
                    expected_target_path=derived_dir,
                )
            except ValueError as exc:
                return {"error": str(exc)}
            target_dir = derived_dir
            rm = ReleaseManager(self.workspace)
            target = GenericMarkdownTarget()
            rb = rm.rollback_release(
                release_id, target, target_dir,
                expected_scope=parsed,
                expected_target_root_id=target_root_id,
                expected_target_path=derived_dir,
            )
            rb_result = rb.verify_result.get("rollback_result", {})
            errors = list(rb_result.get("errors", []))
            errors.extend(_verify_exact_file_after_rollback(data))
            ok = (
                rb.status == ReleaseStatus.ROLLED_BACK
                and not errors
                and rb_result.get("rescan_match", False)
            )
            return {
                "ok": ok,
                "release_id": rb.release_id,
                "status": rb.status.value if hasattr(rb.status, "value") else str(rb.status),
                "errors": errors,
            }
        native_manifest = (
            Path(self.workspace) / ".memoryguard" / "native_releases" / release_id / "manifest.json"
        )
        if native_manifest.exists():
            return {"error": "native_release_missing_scope_binding"}
        return {"error": f"release not found: {release_id}"}

    def list_native_memory_releases(
        self,
        scope: dict | None = None,
        agent_instance_id: str = "",
    ) -> dict:
        from .change_history import get_release
        from .native_file_release import SafeNativeFilePublisher
        from .release_manager import ReleaseManager
        parsed, err = self._parse_scope(scope, agent_instance_id=agent_instance_id, mode="agent")
        # 无 scope 时仍可列出，但仅返回带绑定且匹配当前 agent 的项；无 scope → 空列表 fail closed
        agent_filter = ""
        if parsed and parsed.get("mode") == "agent":
            agent_filter = parsed.get("agent_instance_id", "")
        elif agent_instance_id:
            agent_filter = agent_instance_id
        native = SafeNativeFilePublisher(self.workspace).list_releases()
        for item in native:
            item["source"] = "native_file"
            # 旧 native 无 scope 绑定：仅在未过滤时可见；有 agent_filter 时隐藏
            item["governance_scope"] = {}
            item["target_root_id"] = ""
        rm_items: list[dict] = []
        for event in ReleaseManager(self.workspace).list_releases():
            release_id = event.get("event_id", "")
            raw = get_release(Path(self.workspace), release_id) if release_id else None
            status = event.get("status", "")
            changed = list(raw.get("changed_paths", [])) if raw else []
            can_rollback = status in ("verified", "applied")
            scope_data = (raw or {}).get("governance_scope") or event.get("governance_scope") or {}
            root_id = str((raw or {}).get("target_root_id") or event.get("target_root_id") or "")
            rm_items.append({
                "release_id": release_id,
                "label": raw.get("label", "") if raw else "",
                "created_at": event.get("applied_at", ""),
                "applied_at": event.get("applied_at", ""),
                "status": status,
                "file_count": len(changed) or event.get("changed_count", 0),
                "targets": changed,
                "can_rollback": can_rollback,
                "rollback_reason": (
                    "可恢复" if can_rollback
                    else "已经恢复过" if status == "rolled_back"
                    else ""
                ),
                "source": "release_manager",
                "target_profile": event.get("target_profile", ""),
                "governance_scope": scope_data,
                "target_root_id": root_id,
            })
        seen: set[str] = set()
        merged: list[dict] = []
        for item in native + rm_items:
            rid = item.get("release_id", "")
            if rid and rid in seen:
                continue
            if agent_filter:
                item_agent = str((item.get("governance_scope") or {}).get("agent_instance_id", "") or "")
                if item.get("source") == "native_file" and not item_agent:
                    continue  # 无绑定的旧 native 在显式 agent 过滤下不可见
                if item_agent and item_agent != agent_filter:
                    continue
                if item.get("source") == "release_manager" and not item_agent:
                    continue
            if rid:
                seen.add(rid)
            merged.append(item)
        merged.sort(key=lambda x: x.get("applied_at") or x.get("created_at", ""), reverse=True)
        return {"releases": merged, "total": len(merged)}

    def list_sources(self) -> dict:
        from .source_registry import SourceRegistry
        reg = SourceRegistry(self.workspace)
        sources = []
        for source in reg.list_sources():
            item = source.to_dict()
            source_path = Path(source.path)
            item["path_exists"] = source_path.exists()
            item["path_kind"] = (
                "directory" if source_path.is_dir()
                else "file" if source_path.is_file()
                else "missing"
            )
            sources.append(item)
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
        from .schema_v3 import SourceRootType
        reg = SourceRegistry(self.workspace)
        snap = reg.scan(ScanBudget())
        # 该 Agent 的 SourceRoot 列表
        agent_roots = [r for r in reg.list_sources() if r.agent_instance_id == instance_id]
        # 修复:项目目录(src-project-default)的 agent_instance_id 可能为空,
        # 但只要 enabled=True 就应该出现在数据视图中
        project_dir_roots = [
            r for r in reg.list_sources()
            if r.scope == "project" and r.type == SourceRootType.PROJECT_DIRECTORY
            and r.enabled and r not in agent_roots
        ]
        agent_roots.extend(project_dir_roots)
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
        enriched = []
        for b in bindings:
            item = b.to_dict()
            item.update(store.group_status(b.share_group_id, agent_instance_id=b.agent_instance_id))
            enriched.append(item)
        return {"bindings": enriched, "total": len(enriched)}

    def ensure_personal_memory_group(self, agent_instance_id: str,
                                     confirmed: bool = False,
                                     *, _admin_override: bool = False) -> dict:
        """正式单 Agent 接管入口：未绑定才创建个人组，已有共享绑定保持不动。"""
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        err = self._require_admin()
        if err:
            return err
        from .agent_binding import AgentBindingStore
        return AgentBindingStore(self.workspace).ensure_personal_memory_group(agent_instance_id)

    def leave_shared_group_to_personal(self, agent_instance_id: str,
                                       confirmed: bool = False,
                                       *, _admin_override: bool = False) -> dict:
        """显式退出共享组；个人库与共享库均保留，不做合并。"""
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        err = self._require_admin()
        if err:
            return err
        from .agent_binding import AgentBindingStore
        return AgentBindingStore(self.workspace).leave_shared_group_to_personal(
            agent_instance_id, confirmed=True,
        )

    def bind_agent(self, agent_instance_id: str, share_group_id: str,
                   mcp_server_name: str = "memoryguard",
                   native_memory_mode: str = "observed",
                   redirect_paths: list[str] | None = None,
                   *, _admin_override: bool = False) -> dict:
        # A2: GUI/桌面 bind_agent 与 MCP 对齐,非 admin 拒绝
        # GUI 与 MCP 使用同一 AccessContext 管理员校验。
        admin_error = self._require_admin()
        if admin_error:
            return admin_error
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
                                    redirect_paths: dict[str, list[str]] | None = None,
                                    *, _admin_override: bool = False) -> dict:
        # A2: 与 bind_agent 对齐
        admin_error = self._require_admin()
        if admin_error:
            return admin_error
        from .agent_binding import AgentBindingStore
        store = AgentBindingStore(self.workspace)
        return store.bind_agents_to_group(
            agent_instance_ids=agent_instance_ids,
            share_group_id=share_group_id,
            mcp_server_name=mcp_server_name,
            native_memory_modes=native_memory_modes or {},
            redirect_paths=redirect_paths or {},
        )

    def install_shared_group_mcp_redirects(
        self,
        share_group_id: str,
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        """为指定记忆组（personal 或 shared）内已绑定 Agent 安装用户级 MCP 重定向。

        配置写到各宿主的全局用户目录；MEMORYGUARD_WORKSPACE 固定指向
        本控制目录，因此 Agent 从任意项目启动都访问同一份绑定与共享记忆。
        """
        if not confirmed:
            return {"error": "需要确认才能安装 MCP 重定向"}
        err = self._require_admin()
        if err:
            return err
        from .agent_binding import AgentBindingStore
        from .agent_locator import AgentLocator
        from .provider_adapters import get_provider_adapter_class

        bindings = AgentBindingStore(self.workspace).find_by_group(share_group_id, include_inactive=False)
        if not bindings:
            return {"error": f"no active bindings for group: {share_group_id}"}
        instances, _ = AgentLocator(self.workspace).detect_instances()
        product_by_id = {i.instance_id: i.product for i in instances}
        installed: list[dict] = []
        for b in bindings:
            product = product_by_id.get(b.agent_instance_id, "")
            cls = get_provider_adapter_class(product)
            if cls is None:
                installed.append({
                    "agent_instance_id": b.agent_instance_id,
                    "product": product,
                    "status": "skipped",
                    "skipped": True,
                    "reason": "automatic_install_adapter_not_implemented",
                    "mcp_support": "unknown_or_manual",
                })
                continue
            try:
                result = cls(self.workspace).install(
                    workspace=self.workspace,
                    share_group_id=share_group_id,
                    agent_instance_id=b.agent_instance_id,
                    global_scope=True,
                )
                result["agent_instance_id"] = b.agent_instance_id
                result["product"] = product
                result["status"] = result.get("status", "configured")
                installed.append(result)
            except Exception as exc:
                installed.append({
                    "agent_instance_id": b.agent_instance_id,
                    "product": product,
                    "status": "error",
                    "error": str(exc),
                })
        configured_count = sum(item["status"] == "configured" for item in installed)
        skipped_count = sum(item["status"] == "skipped" for item in installed)
        error_count = sum(item["status"] == "error" for item in installed)
        hook_configured_count = sum(
            bool(item.get("hook", {}).get("configured"))
            for item in installed
            if item["status"] == "configured"
        )
        hook_unsupported_count = sum(
            item.get("hook", {}).get("supported") is False
            for item in installed
            if item["status"] == "configured"
        )
        hook_error_count = sum(
            item.get("hook", {}).get("status") == "error"
            for item in installed
            if item["status"] == "configured"
        )
        warning_count = sum(
            len(item.get("warnings", []))
            for item in installed
            if item["status"] == "configured"
        )
        if configured_count == len(installed):
            status = (
                "partial"
                if hook_error_count or hook_unsupported_count
                else "configured"
            )
        elif configured_count:
            status = "partial"
        else:
            status = "failure"
        return {
            "ok": (
                configured_count == len(installed)
                and hook_error_count == 0
            ),
            "status": status,
            "share_group_id": share_group_id,
            "installed": installed,
            "configured_count": configured_count,
            # 兼容旧客户端；这里表示配置文件已写入，不代表运行时已连接。
            "installed_count": configured_count,
            "skipped_count": skipped_count,
            "error_count": error_count,
            "hook_configured_count": hook_configured_count,
            "hook_unsupported_count": hook_unsupported_count,
            "hook_error_count": hook_error_count,
            "warning_count": warning_count,
            "restart_required": configured_count > 0,
            "runtime_verified": False,
        }

    def get_host_hook_status(
        self,
        provider: str = "",
        agent_instance_id: str = "",
    ) -> dict:
        """Read-only user-level Hook status and last runtime receipt."""
        from .host_hooks import HostHookManager

        manager = HostHookManager(self.workspace)
        if provider or agent_instance_id:
            return manager.status(
                provider,
                agent_instance_id=agent_instance_id,
            )
        result = manager.status()
        try:
            from .agent_binding import AgentBindingStore
            from .agent_locator import AgentLocator

            instances, _ = AgentLocator(self.workspace).detect_instances()
            product_by_id = {
                item.instance_id: item.product.lower() for item in instances
            }
            aliases = {
                "claude-code": "claude",
                "claude": "claude",
                "codex": "codex",
                "cursor": "cursor",
                "trae": "trae",
            }
            agent_statuses = []
            for binding in AgentBindingStore(self.workspace).list_bindings(
                include_inactive=False,
            ):
                product = product_by_id.get(binding.agent_instance_id, "")
                hook_provider = aliases.get(product, "")
                if not hook_provider:
                    continue
                item = manager.status(
                    hook_provider,
                    agent_instance_id=binding.agent_instance_id,
                )
                item["agent_instance_id"] = binding.agent_instance_id
                item["product"] = product
                agent_statuses.append(item)
            result["agents"] = agent_statuses
        except Exception as exc:
            result["agent_status_error"] = str(exc)
        return result

    def set_host_hook_mode(
        self,
        provider: str,
        agent_instance_id: str,
        mode: str,
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        """Switch enforce/observe/paused without deleting host configuration."""
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        err = self._require_admin()
        if err:
            return err
        from .host_hooks import set_hook_mode

        return {
            "ok": True,
            **set_hook_mode(
                self.workspace,
                provider,
                agent_instance_id,
                mode,
            ),
        }

    def uninstall_host_hook(
        self,
        provider: str,
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        """Remove only MemoryGuard-owned Hook handlers."""
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        err = self._require_admin()
        if err:
            return err
        from .host_hooks import HostHookManager

        return {
            "ok": True,
            **HostHookManager(self.workspace).uninstall(provider),
        }

    def import_native_memories_to_group(
        self,
        share_group_id: str = "",
        agent_instance_ids: list[str] | None = None,
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        """从各 Agent 已授权原生记忆根导入共享组（正式接管的数据迁移）。"""
        if not confirmed:
            return {"error": "需要确认才能导入原生记忆"}
        err = self._require_admin()
        if err:
            return err
        if not share_group_id:
            return {"error": "share_group_id_required"}
        from .agent_binding import AgentBindingStore
        from .shared_memory_import import import_native_memories_to_group

        store = AgentBindingStore(self.workspace)
        bindings = store.find_by_group(share_group_id, include_inactive=False)
        agent_ids = list(agent_instance_ids or [])
        if not agent_ids:
            agent_ids = [b.agent_instance_id for b in bindings]
        if not agent_ids:
            return {"error": "no_agents_in_group"}
        result = import_native_memories_to_group(self.workspace, share_group_id, agent_ids)
        # 重建共享组投影
        self.build_projection(
            confirmed=True,
            scope={"mode": "share_group", "share_group_id": share_group_id},
            share_group_id=share_group_id,
        )
        return result

    def commit_shared_memory_governance(
        self,
        share_group_id: str = "",
        reason: str = "",
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        """共享组正式接管：对 SharedMemoryStore 打版本快照（面板重构/分类确认）。"""
        if not confirmed:
            return {"error": "需要确认才能提交共享记忆治理"}
        err = self._require_admin()
        if err:
            return err
        if not share_group_id:
            return {"error": "share_group_id_required"}
        from .governance_engine import GovernanceEngine

        engine = GovernanceEngine(self.workspace, share_group_id)
        store = engine.store
        records = store.list_records(status="active")
        governance = engine.record_governance_decision(
            actor="user",
            action="commit_shared_governance",
            target_ids=[r.memory_id for r in records[:200]],
            reason=reason or "panel governance commit",
        )
        version_id = governance["version_id"]
        projection_warning = ""
        try:
            projection_result = self.build_projection(
                confirmed=True,
                scope={"mode": "share_group", "share_group_id": share_group_id},
                share_group_id=share_group_id,
            )
            if projection_result.get("error"):
                projection_warning = str(projection_result["error"])
        except Exception as exc:
            # 快照和决策已持久化；投影只是可重建视图，不能反向误报接管失败。
            projection_warning = str(exc)
        return {
            "ok": True,
            "share_group_id": share_group_id,
            "version_id": version_id,
            "active_records": len(records),
            "takeover_mode": "shared_mcp",
            "projection_warning": projection_warning,
        }

    def unbind_agent(self, binding_id: str, *, _admin_override: bool = False) -> dict:
        err = self._require_admin()
        if err:
            return err
        from .agent_binding import AgentBindingStore
        store = AgentBindingStore(self.workspace)
        binding = store.unbind_agent(binding_id)
        if binding is None:
            return {"error": f"binding not found: {binding_id}"}
        return {"ok": True, "binding": binding.to_dict()}

    def dissolve_shared_group(
        self,
        share_group_id: str,
        confirmed: bool = False,
        archive_data: bool = True,
        *,
        _admin_override: bool = False,
    ) -> dict:
        """解散共享组：解绑全部 Agent，删投影，可选归档共享记忆目录。"""
        if not confirmed:
            return {"error": "需要确认才能解散共享组"}
        if not share_group_id:
            return {"error": "share_group_id_required"}
        err = self._require_admin()
        if err:
            return err

        from pathlib import Path
        import shutil
        from datetime import datetime, timezone

        from .agent_binding import AgentBindingStore
        from .governance_scope import (
            GovernanceScope,
            load_scope_preference,
            preference_path,
            share_group_projection_path,
        )

        bind_store = AgentBindingStore(self.workspace)
        unbound = bind_store.dissolve_group(share_group_id)

        proj_path = share_group_projection_path(
            self.workspace,
            GovernanceScope(mode="share_group", share_group_id=share_group_id),
        )
        projection_deleted = False
        if proj_path.exists():
            proj_path.unlink()
            projection_deleted = True
        tomb = proj_path.with_suffix(proj_path.suffix + ".deleted")
        tomb.parent.mkdir(parents=True, exist_ok=True)
        tomb.write_text("deleted", encoding="utf-8")

        archived_to = ""
        sm_dir = Path(self.workspace) / ".memoryguard" / "shared-memory" / share_group_id
        if archive_data and sm_dir.is_dir():
            archive_root = Path(self.workspace) / ".memoryguard" / "shared-memory-archived"
            archive_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = archive_root / f"{share_group_id}-{stamp}"
            shutil.move(str(sm_dir), str(dest))
            archived_to = str(dest)

        scope_cleared = False
        pref = load_scope_preference(self.workspace)
        if (
            pref is not None
            and pref.mode == "share_group"
            and pref.share_group_id == share_group_id
        ):
            pref_file = preference_path(self.workspace)
            if pref_file.exists():
                pref_file.unlink()
            scope_cleared = True

        return {
            "ok": True,
            "share_group_id": share_group_id,
            "unbound_count": unbound.get("unbound_count", 0),
            "bindings": unbound.get("bindings", []),
            "projection_deleted": projection_deleted,
            "archived_to": archived_to,
            "scope_cleared": scope_cleared,
        }

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

    def _require_admin(self) -> dict | None:
        """Require administrator state from the trusted process context."""
        from .access_context import load_access_context
        ctx = self._trusted_access_context or load_access_context()
        ok, err = ctx.require_admin()
        if not ok:
            return {"ok": False, "error": err}
        return None

    def _open_store(
        self,
        share_group_id: str,
        *,
        read_only: bool = False,
        must_exist: bool = False,
    ):
        """打开 SharedMemoryStore,处理 FileNotFoundError。返回 (store, error_dict)。"""
        from .shared_memory_store import SharedMemoryStore
        try:
            store = SharedMemoryStore(
                self.workspace,
                share_group_id,
                read_only=read_only,
                must_exist=must_exist,
            )
            return (store, None)
        except FileNotFoundError:
            return (None, {"error": f"group not found: {share_group_id}"})
        except ValueError as e:
            return (None, {"error": str(e)})

    def list_share_groups(self) -> dict:
        """全局治理入口:列出所有 share_group 及其记忆统计。

        扫描 .memoryguard/shared-memory/*/memory.db,
        返回每个 group 的记录数、冲突数、隔离数、绑定 Agent 数。
        """
        from pathlib import Path
        sm_root = Path(self.workspace) / ".memoryguard" / "shared-memory"
        if not sm_root.is_dir():
            return {"groups": [], "total": 0}
        groups: list[dict] = []
        for group_dir in sorted(sm_root.iterdir()):
            if not group_dir.is_dir():
                continue
            group_id = group_dir.name
            try:
                from .shared_memory_store import SharedMemoryStore
                store = SharedMemoryStore(self.workspace, group_id)
                records = store.list_records()
                active = [r for r in records if r.status.value == "active"]
                conflicts = store.list_conflicts()
                quarantine = store.list_quarantine()
                from .agent_binding import AgentBindingStore, group_kind
                bindings = AgentBindingStore(self.workspace).list_bindings()
                agents = [b.agent_instance_id for b in bindings
                          if b.share_group_id == group_id and b.status.value == "active"]
                groups.append({
                    "share_group_id": group_id,
                    "group_kind": group_kind(group_id),
                    "total_records": len(records),
                    "active_records": len(active),
                    "conflict_count": len(conflicts),
                    "quarantine_count": len(quarantine),
                    "bound_agents": agents,
                    "agent_count": len(agents),
                })
            except Exception as e:
                import logging
                logging.warning(f"group {group_id} load failed: {e}")
                continue
        return {"groups": groups, "total": len(groups)}

    def get_memory_source_map(self, share_group_id: str) -> dict:
        """逐条记忆追溯到本地文件、MCP 事件或衍生记忆。

        只返回定位元数据与短预览，不读取或改写来源文件。
        """
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        from .memory_ir import MemoryNormalizer
        from .source_registry import SourceRegistry
        from .governance_scope import share_file_source_key

        records = store.list_records()
        record_by_id = {record.memory_id: record for record in records}
        events = store.list_events()
        event_by_id = {event.event_id: event for event in events}
        events_by_relative_path: dict[str, list] = {}
        events_by_share_key: dict[str, list] = {}
        for event in events:
            metadata = event.metadata if isinstance(event.metadata, dict) else {}
            relative = str(metadata.get("relative_path", "") or "").strip().replace("\\", "/")
            share_key = share_file_source_key(metadata)
            if relative:
                events_by_relative_path.setdefault(relative, []).append(event)
            if share_key:
                events_by_share_key.setdefault(share_key, []).append(event)
        roots_by_id = {
            root.root_id: root
            for root in SourceRegistry(self.workspace).list_all_sources()
        }
        source_objects = {}
        try:
            ir = MemoryNormalizer(self.workspace).load()
            if ir is not None:
                ir_source_objects = list(getattr(ir, "source_objects", []) or [])
                if not ir_source_objects and ir.snapshot_id:
                    # MemoryIR 当前只持久化 records/决策；来源对象属于扫描快照。
                    # 兼容旧实验对象上的 source_objects，同时优先从真实快照读取。
                    import json
                    from .schema_v3 import SourceObject
                    snapshot_sources = (
                        Path(self.workspace)
                        / ".memoryguard" / "snapshots"
                        / ir.snapshot_id / "sources.json"
                    )
                    if snapshot_sources.is_file():
                        payload = json.loads(
                            snapshot_sources.read_text(encoding="utf-8")
                        )
                        ir_source_objects = [
                            SourceObject(**item)
                            for item in payload
                            if isinstance(item, dict)
                        ]
                source_objects = {
                    obj.source_object_id: obj for obj in ir_source_objects
                }
        except (OSError, TypeError, ValueError):
            source_objects = {}

        def source_from_event(event, locator: str) -> dict:
            metadata = event.metadata if isinstance(event.metadata, dict) else {}
            root_id = str(metadata.get("source_root_id", "") or "")
            relative = str(metadata.get("relative_path", "") or "").strip().replace("\\", "/")
            root = roots_by_id.get(root_id)
            absolute_path = ""
            path_valid = False
            exists = False
            if root is not None:
                try:
                    root_path = Path(root.path).expanduser().resolve()
                    if getattr(root.type, "value", str(root.type)) == "selected_file":
                        candidate = root_path
                    else:
                        candidate = (root_path / relative).resolve()
                    candidate.relative_to(
                        root_path.parent if getattr(root.type, "value", str(root.type)) == "selected_file"
                        else root_path
                    )
                    absolute_path = str(candidate)
                    path_valid = True
                    exists = candidate.exists()
                except (OSError, ValueError):
                    absolute_path = ""
            if root is None and not root_id and not relative:
                return {
                    "origin_kind": "mcp_runtime",
                    "display_name": f"MCP 对话写入 · {event.agent_instance_id or 'unknown'}",
                    "event_id": event.event_id,
                    "agent_instance_id": event.agent_instance_id,
                    "locator": locator or "event",
                    "source_root_id": "",
                    "relative_path": "",
                    "absolute_path": "",
                    "path_valid": False,
                    "exists": False,
                    "scope": "memory_group",
                    "project_ref": "",
                    "authorized": True,
                }
            return {
                "origin_kind": "local_file",
                "display_name": (
                    root.display_name if root is not None
                    else relative or root_id or "历史文件来源"
                ),
                "event_id": event.event_id,
                "agent_instance_id": event.agent_instance_id,
                "locator": str(metadata.get("locator", "") or locator or "file"),
                "source_root_id": root_id,
                "relative_path": relative,
                "absolute_path": absolute_path,
                "path_valid": path_valid,
                "exists": exists,
                "scope": root.scope if root is not None else "unknown",
                "project_ref": root.project_ref if root is not None else "",
                "authorized": bool(root is not None and root.enabled),
                "source_category": (
                    root.source_category if root is not None
                    else str(metadata.get("source_category", "") or "unknown")
                ),
            }

        def resolve_provenance(source_id: str, locator: str, visited: set[str]) -> list[dict]:
            if not source_id or source_id in visited:
                return []
            next_visited = set(visited)
            next_visited.add(source_id)
            event = event_by_id.get(source_id)
            if event is not None:
                return [source_from_event(event, locator)]
            if source_id.startswith("share-file:"):
                from .governance_scope import parse_share_file_source_key
                root_hint, relative = parse_share_file_source_key(source_id)
                events_for_source = events_by_share_key.get(source_id, [])
                if not events_for_source and relative:
                    fallback_key = (
                        f"share-file:{relative}" if not root_hint else
                        f"share-file:{root_hint}:{relative}"
                    )
                    events_for_source.extend(events_by_share_key.get(fallback_key, []))
                    events_for_source.extend(events_by_relative_path.get(relative, []))
                if not events_for_source:
                    return []
                return [source_from_event(item, locator) for item in events_for_source]
            source_record = record_by_id.get(source_id)
            if source_record is not None:
                resolved: list[dict] = []
                for provenance in source_record.provenance or []:
                    resolved.extend(resolve_provenance(
                        provenance.source_object_id,
                        provenance.locator,
                        next_visited,
                    ))
                return resolved
            source_object = source_objects.get(source_id)
            if source_object is not None:
                root = roots_by_id.get(source_object.source_root_id)
                synthetic_event = type("_SourceEvent", (), {
                    "event_id": "",
                    "agent_instance_id": root.agent_instance_id if root is not None else "",
                    "metadata": {
                        "source_root_id": source_object.source_root_id,
                        "relative_path": source_object.relative_path,
                    },
                })()
                return [source_from_event(synthetic_event, locator)]
            return []

        mappings: list[dict] = []
        mapped_count = 0
        file_count = 0
        for record in records:
            sources: list[dict] = []
            seen: set[tuple] = set()
            for provenance in record.provenance or []:
                for source in resolve_provenance(
                    provenance.source_object_id,
                    provenance.locator,
                    set(),
                ):
                    key = (
                        source.get("origin_kind"),
                        source.get("event_id"),
                        source.get("source_root_id"),
                        source.get("relative_path"),
                    )
                    if key not in seen:
                        seen.add(key)
                        sources.append(source)
            if not sources:
                sources.append({
                    "origin_kind": "mcp_runtime",
                    "display_name": f"MCP/衍生写入 · {record.agent_instance_id or 'unknown'}",
                    "event_id": "",
                    "agent_instance_id": record.agent_instance_id,
                    "locator": "memory",
                    "source_root_id": "",
                    "relative_path": "",
                    "absolute_path": "",
                    "path_valid": False,
                    "exists": False,
                    "scope": "memory_group",
                    "project_ref": "",
                    "authorized": True,
                })
            if sources:
                mapped_count += 1
            file_count += sum(source["origin_kind"] == "local_file" for source in sources)
            mappings.append({
                "memory_id": record.memory_id,
                "body_preview": _mask_content(record.body, 120),
                "kind": record.kind.value,
                "status": record.status.value,
                "agent_instance_id": record.agent_instance_id,
                "sources": sources,
            })
        return {
            "share_group_id": share_group_id,
            "mappings": mappings,
            "total_records": len(records),
            "mapped_records": mapped_count,
            "file_source_count": file_count,
        }

    def _export_memory_group_impl(self, share_group_id: str) -> dict:
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        import json
        import zipfile
        from datetime import datetime, timezone
        from .agent_binding import AgentBindingStore, group_kind
        from .schema_v3 import stable_hash, _now_iso

        state = store.export_state()
        records = state["records"]
        events = state["events"]
        decisions = state["decisions"]
        conflicts = state["conflicts"]
        quarantine = state["quarantine"]
        versions = state["versions"]
        bindings = [
            binding.to_dict()
            for binding in AgentBindingStore(self.workspace).find_by_group(
                share_group_id, include_inactive=True,
            )
        ]
        source_map = self.get_memory_source_map(share_group_id)
        exported_at = _now_iso()
        export_id = stable_hash("memory_group_export", share_group_id, exported_at)
        manifest = {
            "schema": "memoryguard.memory-group-export.v1",
            "export_id": export_id,
            "exported_at": exported_at,
            "share_group_id": share_group_id,
            "group_kind": group_kind(share_group_id),
            "canonical_store_path": str(store.db_path),
            "counts": {
                "records": len(records),
                "events": len(events),
                "decisions": len(decisions),
                "conflicts": len(conflicts),
                "quarantine": len(quarantine),
                "versions": len(versions),
                "bindings": len(bindings),
            },
            "native_files_included": False,
        }
        export_root = Path(self.workspace) / ".memoryguard" / "exports"
        export_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        export_path = export_root / f"{share_group_id}-{stamp}-{export_id[:8]}.zip"
        temp_path = export_path.with_suffix(".zip.tmp")
        payloads = {
            "manifest.json": manifest,
            "records.json": records,
            "events.json": events,
            "decisions.json": decisions,
            "conflicts.json": conflicts,
            "quarantine.json": quarantine,
            "versions.json": versions,
            "bindings.json": bindings,
            "source-map.json": source_map,
        }
        try:
            with zipfile.ZipFile(
                temp_path, "w", compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for name, payload in payloads.items():
                    archive.writestr(
                        name,
                        json.dumps(payload, ensure_ascii=False, indent=2),
                    )
            temp_path.replace(export_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise
        return {
            "ok": True,
            "share_group_id": share_group_id,
            "group_kind": manifest["group_kind"],
            "export_id": export_id,
            "export_path": str(export_path),
            "counts": manifest["counts"],
        }

    def export_memory_group(
        self,
        share_group_id: str,
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        err = self._require_admin()
        if err:
            return err
        store, open_err = self._open_store(
            share_group_id, must_exist=True,
        )
        if open_err:
            return open_err
        with store.maintenance("export_memory_group"):
            return self._export_memory_group_impl(share_group_id)

    def clear_memory_group(
        self,
        share_group_id: str,
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        """先导出，再清空当前组；保留 binding、MCP 配置和空数据库。"""
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        err = self._require_admin()
        if err:
            return err
        store, open_err = self._open_store(
            share_group_id, must_exist=True,
        )
        if open_err:
            return open_err
        with store.maintenance("clear_memory_group"):
            exported = self._export_memory_group_impl(share_group_id)
            if not exported.get("ok"):
                return exported
            cleared = store.clear_all()
        projection_warning = ""
        try:
            self.build_projection(
                confirmed=True,
                scope={"mode": "share_group", "share_group_id": share_group_id},
                share_group_id=share_group_id,
            )
        except Exception as exc:
            projection_warning = str(exc)
        return {
            "ok": True,
            "share_group_id": share_group_id,
            "export_path": exported["export_path"],
            "before": cleared["before"],
            "after": cleared["after"],
            "cleanup_warnings": cleared["warnings"],
            "projection_warning": projection_warning,
            "binding_preserved": True,
            "native_files_changed": False,
        }

    def archive_memory_group(
        self,
        share_group_id: str,
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        """先导出，再解绑并归档整个个人/共享记忆层。"""
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        err = self._require_admin()
        if err:
            return err
        store, open_err = self._open_store(
            share_group_id, must_exist=True,
        )
        if open_err:
            return open_err
        with store.maintenance("archive_memory_group"):
            exported = self._export_memory_group_impl(share_group_id)
            if not exported.get("ok"):
                return exported
            archived = self.dissolve_shared_group(
                share_group_id,
                confirmed=True,
                archive_data=True,
            )
        archived_marker = Path(archived.get("archived_to", "")) / ".maintenance"
        if archived.get("archived_to") and archived_marker.exists():
            archived_marker.unlink()
        archived["export_path"] = exported["export_path"]
        archived["native_files_changed"] = False
        return archived

    def get_global_memory_status(self) -> dict:
        """全局治理入口:跨所有 share_group 的记忆总览。

        聚合所有 group 的记录数、冲突数、隔离数,
        并检测跨 group 的重复记忆(相同 body hash)。
        """
        groups_data = self.list_share_groups()
        all_records: list[dict] = []
        for g in groups_data["groups"]:
            gid = g["share_group_id"]
            try:
                from .shared_memory_store import SharedMemoryStore
                store = SharedMemoryStore(self.workspace, gid)
                for r in store.list_records():
                    all_records.append({
                        "memory_id": r.memory_id,
                        "share_group_id": gid,
                        "body": r.body[:200],
                        "kind": r.kind.value,
                        "status": r.status.value,
                        "agent_instance_id": r.agent_instance_id,
                    })
            except Exception as e:
                import logging
                logging.warning(f"group {gid} load failed: {e}")
                continue
        # A1: 跨 group 重复检测改用 canonical_hash(全哈希,非 body[:100] 前缀)
        from .shared_memory_store import SharedMemoryStore
        hash_map: dict[str, list[dict]] = {}
        for r in all_records:
            c_hash = SharedMemoryStore._canonical_hash(r["body"])
            hash_map.setdefault(c_hash, []).append(r)
        cross_group_dups = [
            {
                "canonical_hash": k,
                "body_preview": v[0]["body"][:80],
                "memory_ids": [r["memory_id"] for r in v],
                "share_group_ids": list(set(r["share_group_id"] for r in v)),
                "count": len(v),
            }
            for k, v in hash_map.items() if len(v) > 1
        ]
        return {
            "total_groups": groups_data["total"],
            "total_records": len(all_records),
            "active_records": sum(1 for r in all_records if r["status"] == "active"),
            "conflict_total": sum(g["conflict_count"] for g in groups_data["groups"]),
            "quarantine_total": sum(g["quarantine_count"] for g in groups_data["groups"]),
            "cross_group_duplicates": cross_group_dups,
            "groups": groups_data["groups"],
        }

    def list_memory(self, status: str = "", kind: str = "",
                    share_group_id: str = "default") -> dict:
        """列出共享记忆，可按 status/kind 过滤。"""
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        records = store.list_records(status=status or None, kind=kind or None)
        return {
            "records": [r.to_dict() for r in records],
            "total": len(records),
            "status": store.status(),
        }

    def get_memory(self, memory_id: str, share_group_id: str = "default") -> dict:
        """读取单条记忆。"""
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        record = store.get_record(memory_id)
        if record is None:
            return {"error": f"memory not found: {memory_id}"}
        return record.to_dict()

    def search_memory(self, query: str, share_group_id: str = "default",
                      *, semantic: str = "off", limit: int = 20) -> dict:
        """B1: FTS5 全文搜索 + BM25 排序 + 可选语义召回。

        semantic: off(默认)/heuristic/model
        """
        from .shared_memory_store import SharedMemoryStore
        try:
            store = SharedMemoryStore(self.workspace, share_group_id, read_only=True)
        except FileNotFoundError:
            return {"records": [], "total": 0, "error": "group not found"}
        results = store.search_fts(query, status="active", limit=limit)
        # 可选语义召回
        if semantic in ("heuristic", "model") and query:
            try:
                from .semantic_dedup import SemanticDedup
                dedup = SemanticDedup(self.workspace, share_group_id)
                sem_dups = dedup.find_semantic_duplicates(query, threshold=0.60)
                fts_ids = {r["record"]["memory_id"] for r in results}
                for dup in sem_dups:
                    if dup.memory_id not in fts_ids:
                        rec = store.get_record(dup.memory_id)
                        if rec:
                            results.append({
                                "record": rec.to_dict(),
                                "bm25_score": 0.0,
                                "semantic_score": dup.similarity,
                                "share_group_id": share_group_id,
                                "agent_instance_id": rec.agent_instance_id,
                                "kind": rec.kind.value,
                                "provenance": rec.provenance,
                                "confidence": rec.confidence,
                            })
            except Exception:
                pass
        return {"records": results, "total": len(results)}

    def edit_memory(self, memory_id: str, body: str,
                    share_group_id: str = "default", *, _admin_override: bool = False) -> dict:
        """编辑记忆正文。"""
        err = self._require_admin()
        if err:
            return err
        from .governance_engine import GovernanceEngine
        from .mcp_server import _redact_secret
        safe_body, secret_hit = _redact_secret(body)
        if secret_hit:
            body = safe_body
        return GovernanceEngine(
            self.workspace, share_group_id,
        ).human_edit(memory_id, body)

    def lock_memory(self, memory_id: str, share_group_id: str = "default", *, _admin_override: bool = False) -> dict:
        """锁定记忆。"""
        err = self._require_admin()
        if err:
            return err
        from .governance_engine import GovernanceEngine
        return GovernanceEngine(
            self.workspace, share_group_id,
        ).human_lock(memory_id)

    def unlock_memory(self, memory_id: str, share_group_id: str = "default", *, _admin_override: bool = False) -> dict:
        """解锁记忆。"""
        err = self._require_admin()
        if err:
            return err
        from .governance_engine import GovernanceEngine
        return GovernanceEngine(
            self.workspace, share_group_id,
        ).human_unlock(memory_id)

    def set_memory_injection_policy(
        self,
        memory_id: str,
        injection_policy: str,
        priority: int = 0,
        share_group_id: str = "default",
        *,
        _admin_override: bool = False,
    ) -> dict:
        """Toggle a governed memory between on-demand and mandatory injection."""
        err = self._require_admin()
        if err:
            return err
        from .governance_engine import GovernanceEngine
        engine = GovernanceEngine(self.workspace, share_group_id)
        return engine.human_set_injection_policy(
            memory_id,
            injection_policy=injection_policy,
            priority=priority,
        )

    def restore_memory(self, memory_id: str, share_group_id: str = "default", *, _admin_override: bool = False) -> dict:
        """恢复 shadowed 记忆为 active。"""
        err = self._require_admin()
        if err:
            return err
        from .governance_engine import GovernanceEngine
        return GovernanceEngine(
            self.workspace, share_group_id,
        ).human_restore(memory_id)

    def delete_memory(self, memory_id: str, share_group_id: str = "default", *, _admin_override: bool = False) -> dict:
        """软删除记忆。"""
        err = self._require_admin()
        if err:
            return err
        from .governance_engine import GovernanceEngine
        return GovernanceEngine(
            self.workspace, share_group_id,
        ).human_delete(memory_id)

    def rollback_memory(self, version_id: str, share_group_id: str = "default", *, _admin_override: bool = False) -> dict:
        """回滚到指定版本。"""
        err = self._require_admin()
        if err:
            return err
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(self.workspace, share_group_id)
        store.rollback_to_version(version_id)
        return {"ok": True, "version_id": version_id}

    def list_memory_versions(self, share_group_id: str = "default") -> dict:
        """列出所有版本。"""
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        return {"versions": store.list_versions()}

    # ------------------------------------------------------------------
    # AutoOrganizeApi（v3.2 §8.2）：自动整理观察
    # ------------------------------------------------------------------

    def get_recent_events(self, share_group_id: str = "default") -> dict:
        """最近自动写入事件。"""
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        events = store.list_events()
        # 最近 50 条
        recent = events[-50:] if len(events) > 50 else events
        return {"events": [e.to_dict() for e in recent], "total": len(events)}

    def get_auto_actions(self, share_group_id: str = "default") -> dict:
        """自动整理记录（从 events 的 auto_actions 聚合）。"""
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
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
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
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
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        conflicts = [
            item for item in store.list_conflicts()
            if item.status.value == "unresolved"
        ]
        return {"conflicts": [c.to_dict() for c in conflicts], "total": len(conflicts)}

    def get_quarantine(self, share_group_id: str = "default") -> dict:
        """隔离队列（安全修复：后端返回 masked_preview，前端永远拿不到原文）。"""
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        entries = store.list_quarantine()
        result = []
        for e in entries:
            if e.released:
                continue
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
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err

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
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        return store.status()

    def get_supersede_decisions(self, share_group_id: str = "default") -> dict:
        """获取所有 auto_supersede 决策及关联记录内容预览。"""
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
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
                         share_group_id: str = "default", *, _admin_override: bool = False) -> dict:
        """解决冲突：保留指定记忆，其他成员软删除。"""
        err = self._require_admin()
        if err:
            return err
        from .governance_engine import GovernanceEngine
        return GovernanceEngine(
            self.workspace, share_group_id,
        ).resolve_conflict(group_id, keep_memory_id)

    def release_quarantine(self, quarantine_id: str,
                           share_group_id: str = "default", *, _admin_override: bool = False) -> dict:
        """释放隔离：恢复记忆为 active。"""
        err = self._require_admin()
        if err:
            return err
        from .governance_engine import GovernanceEngine
        return GovernanceEngine(
            self.workspace, share_group_id,
        ).resolve_quarantine(quarantine_id, resolution="release")

    def delete_quarantine(self, quarantine_id: str,
                          share_group_id: str = "default", *, _admin_override: bool = False) -> dict:
        """软删除隔离记忆；历史与版本保留，可恢复。"""
        err = self._require_admin()
        if err:
            return err
        from .governance_engine import GovernanceEngine
        return GovernanceEngine(
            self.workspace, share_group_id,
        ).resolve_quarantine(quarantine_id, resolution="delete")

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
        # 系统文件夹选择器只会传 "directory"；识别 Vault 后保留其专属扫描策略。
        # 显式 selected_directory 仍按调用者指定，不擅自改变来源类型。
        if source_type == "directory" and (Path(path).expanduser() / ".obsidian").is_dir():
            enum_type = SourceRootType.OBSIDIAN_VAULT
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
        # 与 preview 一致：文件夹选择器发现 .obsidian 后创建 Vault 来源。
        if source_type == "directory" and (Path(path).expanduser() / ".obsidian").is_dir():
            enum_type = SourceRootType.OBSIDIAN_VAULT
        reg = SourceRegistry(self.workspace)
        root = reg.add(path, enum_type, display_name=display_name)
        # 手工添加的文件/目录是独立知识库，不属于当前 Agent 的授权来源。
        # 幂等 add 可能返回既有根；只补全没有 Agent 或发现归属的未知映射。
        is_unowned_root = (
            not root.agent_instance_id
            and not root.authorized_agent_ids
            and not root.discovery_object_id
            and not root.surface_id
            and root.target_role in {"", "none"}
            and root.ownership != "agent_managed"
        )
        if root.source_category in {"", "unknown"} and is_unowned_root:
            root.source_category = "knowledge_source"
            reg._save()
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
        from .governance_engine import GovernanceEngine
        from .schema_v3 import MemoryEvent, stable_hash, _now_iso
        content = file_result.get("content", "")
        segments = self._extract_memory_segments(content, max_segments=max_segments)
        engine = GovernanceEngine(self.workspace, share_group_id)
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
            result = engine.auto_write(
                event,
                idempotency_key=stable_hash(
                    "doc_extract",
                    root_id,
                    relative_path,
                    str(idx),
                    segment,
                ),
            )
            extracted.append({
                "memory_id": result["memory_id"],
                "body": (result.get("after") or {}).get("body", segment),
                "kind": result["kind"],
                "status": result["status"],
                "auto_actions": result["auto_actions"],
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
        from .governance_engine import GovernanceEngine
        from .schema_v3 import stable_hash, _now_iso

        file_result = self.get_source_file_content(root_id, relative_path)
        if "error" in file_result:
            return file_result
        content = file_result.get("content", "")
        segments = self._extract_memory_segments(content, max_segments=max_segments)

        # 通过统一治理层做只读分类 + 风险扫描。
        engine = GovernanceEngine(self.workspace, "default")
        candidates = []
        for idx, segment in enumerate(segments):
            preview = engine.preview_content(segment)
            kind = preview["kind"]
            confidence = preview["confidence"]
            secret = preview["secret_pattern"]
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
                "kind": kind,
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

    def extract_preview_by_path(
        self,
        abs_path: str,
        agent_instance_id: str = "",
        max_segments: int = 20,
    ) -> dict:
        """对已发现但未授权勾选的会话/证据路径做萃取预览。

        安全边界：路径必须落在当前探测到的 Agent 表面之下（或任意已发现表面），
        且类别属于 conversation_history / runtime_evidence / knowledge_source /
        native_memory / project_memory。禁止任意路径读取。
        """
        import json
        import time
        from pathlib import Path
        from .agent_locator import (
            AgentLocator,
            EXTRACT_DISPLAY_CATEGORIES,
            MEMORY_SELECTABLE_CATEGORIES,
        )
        from .governance_engine import GovernanceEngine
        from .schema_v3 import stable_hash, _now_iso

        target = Path(abs_path).expanduser()
        try:
            target = target.resolve()
        except OSError as e:
            return {"error": f"resolve failed: {e}"}
        if not target.is_file():
            return {"error": "file not found"}
        try:
            if target.stat().st_size > 5 * 1024 * 1024:
                return {"error": "file too large (max 5MB)"}
        except OSError as e:
            return {"error": f"stat failed: {e}"}

        locator = AgentLocator(self.workspace)
        instances, _ = locator.detect_instances()
        if agent_instance_id:
            instances = [i for i in instances if i.instance_id == agent_instance_id]
        allowed_cats = MEMORY_SELECTABLE_CATEGORIES | EXTRACT_DISPLAY_CATEGORIES
        matched: dict | None = None
        for inst in instances:
            tree = locator.get_selection_tree(inst.instance_id)
            for scope_obj in tree.get("scopes", []):
                cats = list(scope_obj.get("categories") or [])
                for proj in scope_obj.get("projects") or []:
                    cats.extend(proj.get("categories") or [])
                for cat in cats:
                    category = cat.get("category", "")
                    if category not in allowed_cats:
                        continue
                    for f in cat.get("files") or []:
                        fpath = Path(f.get("path") or "")
                        try:
                            fpath = fpath.resolve()
                        except OSError:
                            continue
                        if fpath == target:
                            matched = {
                                "instance_id": inst.instance_id,
                                "category": category,
                                "surface_id": f.get("surface_id", ""),
                                "path": str(fpath),
                            }
                            break
                        if fpath.is_dir():
                            try:
                                target.relative_to(fpath)
                            except ValueError:
                                continue
                            matched = {
                                "instance_id": inst.instance_id,
                                "category": category,
                                "surface_id": f.get("surface_id", ""),
                                "path": str(fpath),
                            }
                            break
                    if matched:
                        break
                if matched:
                    break
            if matched:
                break
        if not matched:
            return {"error": "path_not_in_discovered_extractable_surfaces"}

        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return {"error": f"read failed: {e}"}

        synthetic_root = stable_hash("discover_path", matched["instance_id"], str(target))
        relative_path = target.name
        segments = self._extract_memory_segments(content, max_segments=max_segments)
        engine = GovernanceEngine(self.workspace, "default")
        candidates = []
        for idx, segment in enumerate(segments):
            preview = engine.preview_content(segment)
            kind = preview["kind"]
            confidence = preview["confidence"]
            secret = preview["secret_pattern"]
            risk_level = "high" if secret else ("medium" if confidence < 0.45 else "low")
            candidate_id = stable_hash(
                "candidate", synthetic_root, relative_path, str(idx), segment,
            )
            candidates.append({
                "candidate_id": candidate_id,
                "body": segment,
                "kind": kind,
                "risk_level": risk_level,
                "preview": segment[:200],
            })

        extract_id = stable_hash("extract_path", str(target), _now_iso())
        staging_dir = Path(self.workspace) / ".memoryguard" / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
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
            "root_id": synthetic_root,
            "relative_path": relative_path,
            "abs_path": str(target),
            "discovery": matched,
            "created_at": _now_iso(),
            "candidates": candidates,
        }
        staging_file.write_text(
            json.dumps(staging_data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return {
            "ok": True,
            "extract_id": extract_id,
            "root_id": synthetic_root,
            "relative_path": relative_path,
            "abs_path": str(target),
            "discovery": matched,
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
        from .governance_engine import GovernanceEngine
        from .schema_v3 import MemoryEvent, stable_hash, _now_iso

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

        engine = GovernanceEngine(self.workspace, share_group_id)
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
            result = engine.auto_write(
                event,
                idempotency_key=(
                    f"accept_extract:{extract_id}:{candidate['candidate_id']}"
                ),
            )
            if not result["ok"]:
                return {"error": result["blocked_reason"]}
            written_ids.append(result["memory_id"])
            results.append({
                "memory_id": result["memory_id"],
                "status": result["status"],
                "kind": result["kind"],
                "auto_actions": result["auto_actions"],
            })

        # 记录 DecisionEvent
        engine.record_governance_decision(
            actor="user",
            action="accept_extract",
            target_ids=written_ids,
            reason="user confirmed",
            idempotency_key=f"accept_extract:{extract_id}",
        )

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
                        "notes": d.notes, "inventory": inv,
                        "destination": "local_conversation_history",
                        "writes_long_term_memory": False}
        return {"error": "unsupported bundle format"}

    def create_import(self, path: str, confirmed: bool = False,
                      agent_instance_id: str = "", project_ref: str = "",
                      share_group_id: str = "") -> dict:
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
                # Import is evidence-only.  Raw messages never become
                # SharedMemoryRecord candidates without an explicit extract.
                requested_scope = {}
                if share_group_id:
                    requested_scope = {
                        "mode": "share_group",
                        "share_group_id": share_group_id,
                    }
                elif agent_instance_id:
                    requested_scope = {
                        "mode": "agent",
                        "agent_instance_id": agent_instance_id,
                    }
                try:
                    history_scope = self._history_scope(requested_scope)
                except PermissionError as exc:
                    return {"error": str(exc)}
                agent_id = history_scope.agent_instance_id
                archived = ad.archive_history(
                    convs, workspace=self.workspace, agent_instance_id=agent_id,
                    project_ref=project_ref,
                    share_group_id=history_scope.share_group_id,
                )
                return {"provider": d.provider,
                        "conversation_count": archived["conversation_count"],
                        "turn_count": archived["turn_count"],
                        "extract_candidate_count": 0,
                        "memory_record_count": 0,
                        "written_to_ir": False,
                        "written_to_history": True,
                        "history_agent_instance_id": agent_id}
        return {"error": "unsupported bundle format"}

    # ------------------------------------------------------------------
    # Conversation history: raw evidence only; no bootstrap integration.
    # ------------------------------------------------------------------

    def _history_scope(self, scope: dict | None = None) -> "HistoryScope":
        from .conversation_history import HistoryAccessResolver
        from .governance_scope import load_scope_preference

        requested = dict(scope or {})
        trusted_agent_id = self._trusted_agent_id()
        if not requested:
            preference = load_scope_preference(self.workspace)
            requested = preference.to_dict() if preference else {}
        if not requested and trusted_agent_id:
            requested = {
                "mode": "agent",
                "agent_instance_id": trusted_agent_id,
            }
        return HistoryAccessResolver(self.workspace).resolve(
            trusted_agent_id,
            requested,
        )

    def list_history_sessions(self, scope: dict | None = None, limit: int = 50,
                              offset: int = 0, extracted: bool | None = None,
                              date_from: str = "", date_to: str = "") -> dict:
        from .conversation_history import ConversationHistoryStore
        return ConversationHistoryStore(self.workspace).list_sessions(
            self._history_scope(scope), limit=limit, offset=offset,
            extracted=extracted, date_from=date_from, date_to=date_to,
        )

    def search_history(self, query: str, scope: dict | None = None,
                       limit: int = 20, offset: int = 0) -> dict:
        from .conversation_history import ConversationHistoryStore
        return ConversationHistoryStore(self.workspace).search(
            self._history_scope(scope), query, limit=limit, offset=offset)

    def history_timeline(self, session_id: str, anchor_turn_id: str,
                         scope: dict | None = None, radius: int = 4) -> dict:
        from .conversation_history import ConversationHistoryStore
        return ConversationHistoryStore(self.workspace).timeline(
            self._history_scope(scope), session_id, anchor_turn_id, radius=radius)

    def history_read(self, session_id: str = "", turn_id: str = "",
                     scope: dict | None = None, limit: int = 100, offset: int = 0) -> dict:
        from .conversation_history import ConversationHistoryStore
        return ConversationHistoryStore(self.workspace).read(
            self._history_scope(scope), session_id=session_id, turn_id=turn_id,
            limit=limit, offset=offset)

    def history_extract_preview(self, session_id: str, turn_ids: list[str] | None = None,
                                scope: dict | None = None, limit: int = 20) -> dict:
        from .conversation_history import ConversationHistoryStore
        return ConversationHistoryStore(self.workspace).extract_preview(
            self._history_scope(scope), session_id, turn_ids=turn_ids, limit=limit)

    def export_history(self, session_ids: list[str], scope: dict | None = None) -> dict:
        from .conversation_history import ConversationHistoryStore
        return ConversationHistoryStore(self.workspace).export(self._history_scope(scope), session_ids=session_ids)

    def delete_history(self, session_ids: list[str], scope: dict | None = None,
                       invalidate_evidence: bool = False, confirmed: bool = False) -> dict:
        if not confirmed:
            return {"error": "confirmation_required"}
        from .conversation_history import ConversationHistoryStore
        return ConversationHistoryStore(self.workspace).delete(
            self._history_scope(scope), session_ids=session_ids,
            invalidate_evidence=invalidate_evidence)

    def _history_backfill_agent_ids(self) -> dict[str, str]:
        """Only map a provider to its own discovered/bound Agent instance."""
        from .agent_binding import AgentBindingStore
        from .agent_locator import AgentLocator

        try:
            instances, _ = AgentLocator(self.workspace).detect_instances()
        except Exception:
            instances = []
        active_ids = {
            str(binding.agent_instance_id)
            for binding in AgentBindingStore(self.workspace).list_bindings(include_inactive=False)
            if binding.agent_instance_id
        }
        mapping: dict[str, str] = {}
        aliases = {"claude-code": "claude", "codex": "codex", "cursor": "cursor", "trae": "trae"}
        for instance in instances:
            agent_id = str(getattr(instance, "instance_id", "") or "")
            product = str(getattr(instance, "product", "") or "").strip().casefold()
            provider = aliases.get(product, product)
            if provider and agent_id and agent_id in active_ids:
                mapping.setdefault(provider, agent_id)
        return mapping

    def discover_local_history_sources(self) -> dict:
        """Discover old local logs; discovery is read-only and never imports text."""
        from .history_importers import discover_local_history_sources
        return discover_local_history_sources(
            workspace=self.workspace,
            agent_ids_by_provider=self._history_backfill_agent_ids(),
        )

    def backfill_local_history(self, continuation: dict | None = None,
                               confirmed: bool = False) -> dict:
        """Import a bounded, explicitly confirmed local-history batch."""
        if not confirmed:
            return {"error": "confirmation_required"}
        from .history_importers import backfill_local_history
        return backfill_local_history(
            self.workspace,
            agent_ids_by_provider=self._history_backfill_agent_ids(),
            continuation=continuation,
        )

    def _rule_scope_options(self, share_group_id: str) -> dict:
        """Return *discovered* audience values.  Never accept UI-invented IDs."""
        from .agent_binding import AgentBindingStore
        from .agent_locator import AgentLocator

        agents: dict[str, str] = {}
        providers: dict[str, str] = {}
        # A project label is not an identity.  Only values emitted by the
        # locator's project resolver are valid targets; do not fabricate the
        # workspace basename here.
        projects: set[str] = set()
        locator = AgentLocator(self.workspace)
        try:
            instances, _ = locator.detect_instances()
        except Exception:
            instances = []
        for instance in instances:
            agent_id = str(instance.instance_id or "")
            if not agent_id:
                continue
            agents[agent_id] = str(instance.product or agent_id)
            raw_provider = str(instance.product or "").strip().casefold()
            provider = {"claude-code": "claude"}.get(raw_provider, raw_provider)
            if provider:
                providers[provider] = str(instance.product or provider)
            # Reuse the locator's project resolver instead of guessing paths.
            try:
                tree = locator.get_selection_tree(agent_id)
            except Exception:
                tree = {}
            for scope in tree.get("scopes", []):
                for project in scope.get("projects", []):
                    project_ref = str(project.get("project_ref") or "").strip()
                    if project_ref:
                        projects.add(project_ref)

        binding_store = AgentBindingStore(self.workspace)
        bindings = binding_store.list_bindings(include_inactive=False)
        groups = {str(binding.share_group_id) for binding in bindings if binding.share_group_id}
        # A group may exist before any Agent is bound to it.  It is still a
        # discovered local target, so expose it rather than accepting a typed
        # ID later.
        try:
            groups.update(
                str(item.get("share_group_id") or "")
                for item in self.list_share_groups().get("groups", [])
                if item.get("share_group_id")
            )
        except Exception:
            pass
        for binding in bindings:
            # A bound ID is a verified, usable target even if its host is not
            # currently running and therefore not returned by live discovery.
            if binding.agent_instance_id:
                agents.setdefault(str(binding.agent_instance_id), str(binding.agent_instance_id))
        if share_group_id:
            groups.add(str(share_group_id))

        # Runtime roles are a finite, product-defined context enum.  Existing
        # assignment values are deliberately *not* fed back as valid choices.
        # They are reported separately as legacy_unknown below.
        roles = {"root", "subagent"}
        legacy_unknown: list[dict] = []
        try:
            store, err = self._open_store(share_group_id, read_only=True)
            if not err:
                provisional = {
                    "agents": [{"id": key, "label": value} for key, value in agents.items()],
                    "groups": [{"id": key, "label": key} for key in groups],
                    "projects": [{"id": key, "label": key} for key in projects],
                    "providers": [{"id": key, "label": value} for key, value in providers.items()],
                    "runtime_roles": [{"id": key, "label": key} for key in roles],
                }
                for assignment in store.list_rule_assignments():
                    if not self._assignment_is_verified(assignment, provisional):
                        legacy_unknown.append({
                            **assignment.to_dict(),
                            "reason": "legacy_unknown_target",
                        })
        except Exception:
            pass
        return {
            "agents": [
                {"id": key, "label": value} for key, value in sorted(agents.items())
            ],
            "groups": [{"id": key, "label": key} for key in sorted(groups)],
            "projects": [{"id": key, "label": key} for key in sorted(projects)],
            "providers": [
                {"id": key, "label": value} for key, value in sorted(providers.items())
            ],
            "runtime_roles": [{"id": key, "label": key} for key in sorted(roles)],
            # Existing historical relations are visible so a human can remove
            # them, but they never become selectable values for a new rule.
            "legacy_unknown": legacy_unknown,
            "target_types": [
                "agent", "group", "project", "agent_project", "provider",
                "runtime_role", "system",
            ],
        }

    @staticmethod
    def _assignment_key(assignment) -> tuple[str, str, str, str]:
        if isinstance(assignment, dict):
            get = assignment.get
        else:
            get = lambda name, default="": getattr(assignment, name, default)
        return (
            str(get("target_type", "")), str(get("target_id", "")),
            str(get("project_ref", "")), str(get("effect", "include")),
        )

    @classmethod
    def _assignment_is_verified(cls, assignment, options: dict) -> bool:
        target_type, target_id, project_ref, _effect = cls._assignment_key(assignment)
        known = {
            "agent": {item["id"] for item in options.get("agents", [])},
            "group": {item["id"] for item in options.get("groups", [])},
            "project": {item["id"] for item in options.get("projects", [])},
            "provider": {item["id"].casefold() for item in options.get("providers", [])},
            "runtime_role": {item["id"].casefold() for item in options.get("runtime_roles", [])},
        }
        if target_type == "system":
            return not target_id and not project_ref
        if target_type == "agent":
            return target_id in known["agent"]
        if target_type == "group":
            return target_id in known["group"]
        if target_type == "project":
            return (project_ref or target_id) in known["project"]
        if target_type == "agent_project":
            return target_id in known["agent"] and project_ref in known["project"]
        if target_type == "provider":
            return target_id.casefold() in known["provider"]
        if target_type == "runtime_role":
            return target_id.casefold() in known["runtime_role"]
        return False

    def get_rule_scope_options(self, share_group_id: str = "default") -> dict:
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        return {"ok": True, **self._rule_scope_options(share_group_id)}

    @staticmethod
    def _rule_audience_label(assignment) -> str:
        labels = {
            "agent": "Agent", "group": "共享组", "project": "项目",
            "agent_project": "Agent + 项目", "provider": "宿主",
            "runtime_role": "运行角色", "system": "系统",
        }
        suffix = assignment.target_id or assignment.project_ref or "全部"
        if assignment.target_type == "agent_project":
            suffix = f"{assignment.target_id} / {assignment.project_ref}"
        return f"{labels.get(assignment.target_type, assignment.target_type)}: {suffix}"

    def _validated_rule_assignments(
        self, assignments: list[dict], options: dict, memory_id: str,
        *, existing_assignments: list | None = None,
    ) -> list[dict]:
        """Validate GUI audience choices against server-side discovery data."""
        from .rule_scope import normalize_assignment

        if not isinstance(assignments, list):
            raise ValueError("assignments must be a list")
        allowed = {
            "agent": {x["id"] for x in options["agents"]},
            "group": {x["id"] for x in options["groups"]},
            "project": {x["id"] for x in options["projects"]},
            "provider": {x["id"].casefold() for x in options["providers"]},
            "runtime_role": {x["id"].casefold() for x in options["runtime_roles"]},
        }
        result: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()
        existing_keys = {
            self._assignment_key(item) for item in (existing_assignments or [])
        }
        for raw in assignments:
            if not isinstance(raw, dict):
                raise ValueError("assignment must be an object")
            value = dict(raw)
            value["memory_id"] = memory_id
            target_type = str(value.get("target_type") or "")
            target_id = str(value.get("target_id") or "")
            project_ref = str(value.get("project_ref") or "")
            key_before_normalize = (target_type, target_id, project_ref, str(value.get("effect", "include")))
            legacy_retained = key_before_normalize in existing_keys
            if target_type == "agent" and target_id not in allowed["agent"] and not legacy_retained:
                raise ValueError("unknown_agent_target")
            if target_type == "group" and target_id not in allowed["group"] and not legacy_retained:
                raise ValueError("unknown_group_target")
            if target_type == "project" and (project_ref or target_id) not in allowed["project"] and not legacy_retained:
                raise ValueError("unknown_project_target")
            if target_type == "agent_project":
                if (target_id not in allowed["agent"] or project_ref not in allowed["project"]) and not legacy_retained:
                    raise ValueError("unknown_agent_project_target")
            if target_type == "provider" and target_id.casefold() not in allowed["provider"] and not legacy_retained:
                raise ValueError("unknown_provider_target")
            if target_type == "runtime_role" and target_id.casefold() not in allowed["runtime_role"] and not legacy_retained:
                raise ValueError("unknown_runtime_role_target")
            if target_type == "system" and (target_id or project_ref):
                raise ValueError("system_target_must_be_empty")
            normalized = normalize_assignment(value).to_dict()
            normalized["memory_id"] = memory_id
            key = (
                normalized["target_type"], normalized["target_id"],
                normalized["project_ref"], normalized["effect"],
            )
            if key in seen:
                raise ValueError("duplicate_rule_assignment")
            seen.add(key)
            result.append(normalized)
        return result

    def preview_effective_rules(
        self, agent_instance_id: str, share_group_id: str = "default",
        project_ref: str = "", provider: str = "", runtime_role: str = "",
    ) -> dict:
        """Read-only audience preview; uses the shared matcher, not UI logic."""
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        options = self._rule_scope_options(share_group_id)
        agent_ids = {item["id"] for item in options["agents"]}
        if agent_instance_id not in agent_ids:
            return {"error": "unknown_agent_target"}
        if project_ref and project_ref not in {item["id"] for item in options["projects"]}:
            return {"error": "unknown_project_target"}
        if provider and provider.casefold() not in {item["id"].casefold() for item in options["providers"]}:
            return {"error": "unknown_provider_target"}
        if runtime_role and runtime_role.casefold() not in {item["id"].casefold() for item in options["runtime_roles"]}:
            return {"error": "unknown_runtime_role_target"}

        from .rule_scope import effective_assignments
        from .schema_v3 import EffectiveAgentContext, SharedMemoryStatus
        context = EffectiveAgentContext(
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            project_ref=project_ref,
            provider=provider,
            runtime_role=runtime_role,
        )
        effective, excluded, unavailable = [], [], []
        for record in store.list_records(status=SharedMemoryStatus.ACTIVE.value):
            if record.injection_policy != "always":
                continue
            assignments = store.list_rule_assignments(record.memory_id)
            payload = record.to_dict()
            payload["assignments"] = [item.to_dict() for item in assignments]
            includes, excludes = effective_assignments(assignments, context)
            payload["matched_sources"] = [self._rule_audience_label(item) for item in includes]
            payload["excluded_sources"] = [self._rule_audience_label(item) for item in excludes]
            if excludes:
                excluded.append(payload)
            elif includes:
                effective.append(payload)
            else:
                payload["audience_label"] = "旧规则未定范围" if not assignments else "当前 Agent 不在适用范围"
                unavailable.append(payload)
        return {
            "ok": True, "context": {
                "agent_instance_id": agent_instance_id, "share_group_id": share_group_id,
                "project_ref": project_ref, "provider": provider,
                "runtime_role": runtime_role,
            },
            "effective": effective, "excluded": excluded, "unavailable": unavailable,
        }

    def update_rule_audience(
        self, memory_id: str, assignments: list[dict],
        share_group_id: str = "default", injection_policy: str = "",
        priority: int = 0, confirmed: bool = False, *,
        _admin_override: bool = False,
    ) -> dict:
        """Confirmed local governance edit; assignment removal never deletes memory."""
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        err = self._require_admin()
        if err:
            return err
        store, open_err = self._open_store(share_group_id, must_exist=True)
        if open_err:
            return open_err
        record = store.get_record(memory_id)
        if record is None:
            return {"ok": False, "error": "memory_not_found"}
        if str(record.status.value if hasattr(record.status, "value") else record.status) != "active":
            return {"ok": False, "error": "rule_memory_must_be_active"}
        target_policy = injection_policy or record.injection_policy
        if target_policy not in {"relevant", "always"}:
            return {"ok": False, "error": "invalid_injection_policy"}
        try:
            normalized = self._validated_rule_assignments(
                assignments, self._rule_scope_options(share_group_id), memory_id,
                existing_assignments=store.list_rule_assignments(memory_id),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if target_policy == "always" and not any(item["effect"] == "include" for item in normalized):
            return {"ok": False, "error": "always_rule_requires_include_audience"}
        if target_policy == "relevant" and normalized:
            return {"ok": False, "error": "relevant_rule_cannot_have_assignments"}

        # The engine owns the single transaction and its auditable decision:
        # do not emulate it with two writes in the GUI bridge.
        from .governance_engine import GovernanceEngine
        result = GovernanceEngine(self.workspace, share_group_id).human_set_injection_policy(
            memory_id, target_policy, priority, assignments=normalized,
        )
        if result.get("ok") is False or result.get("error"):
            return result
        result.update({
            "ok": True, "memory_id": memory_id,
            "injection_policy": target_policy,
            "message": "已更新适用范围；删除适用范围不会删除这条记忆。",
        })
        return result

    def list_rules_habits(self, share_group_id: str = "default") -> dict:
        """Virtual view over existing governed records; no new MemoryKind."""
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        options = self._rule_scope_options(share_group_id)
        buckets = {"mandatory": [], "preferences": [], "procedures": [], "corrections": [], "projects": []}
        for record in store.list_records():
            data = record.to_dict()
            policy = data.get("injection_policy", "relevant")
            kind = data.get("kind", "")
            if policy == "always":
                bucket = "mandatory"
            elif kind == "preference":
                bucket = "preferences"
            elif kind == "procedure":
                bucket = "procedures"
            elif kind == "correction":
                bucket = "corrections"
            elif kind == "project":
                bucket = "projects"
            else:
                continue
            assignments = store.list_rule_assignments(data["memory_id"])
            data["assignments"] = [item.to_dict() for item in assignments]
            data["legacy_unknown_assignment_ids"] = [
                item.assignment_id for item in assignments
                if not self._assignment_is_verified(item, options)
            ]
            data["audience_label"] = "旧规则未定范围" if policy == "always" and not data["assignments"] else ""
            buckets[bucket].append(data)
        return {"buckets": buckets, "total": sum(map(len, buckets.values()))}

    # ------------------------------------------------------------------
    # Rule cockpit bridge (lazy, feature-detected)
    # ------------------------------------------------------------------

    @staticmethod
    def _rule_bridge_payload(value) -> dict:
        """Best-effort conversion for service dataclasses/mappings.

        The rule lifecycle service is intentionally optional for older
        installations.  Keeping conversion here lets the GUI bridge accept
        both the dataclass objects used by the service and the JSON mappings
        returned by a remote/compatibility implementation.
        """
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                result = to_dict()
                if isinstance(result, dict):
                    return dict(result)
            except Exception:
                pass
        try:
            result = dict(value)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        try:
            return dict(vars(value))
        except Exception:
            return {"value": value}

    def _rule_bridge_service(self, share_group_id: str = "default", *, is_admin: bool = False):
        """Return an optional RuleCreationService without importing at module load.

        Constructor signatures changed during the v3.2 rollout.  Try the
        supported forms in order and return ``None`` when the service is not
        installed; callers then expose a stable ``service_unavailable``
        response instead of breaking the existing GUI.
        """
        # Never trust a caller-supplied boolean.  The service receives admin
        # state only from the process AccessContext.
        from .access_context import load_access_context
        trusted_is_admin = load_access_context().is_admin
        try:
            from .rule_creation import RuleCreationService
        except (ImportError, ModuleNotFoundError):
            return None
        attempts = (
            ((self.workspace, share_group_id), {"is_admin": trusted_is_admin}),
            ((self.workspace,), {"share_group_id": share_group_id, "is_admin": trusted_is_admin}),
            ((), {"workspace": self.workspace, "share_group_id": share_group_id, "is_admin": trusted_is_admin}),
            ((self.workspace,), {}),
            ((), {"workspace": self.workspace}),
            ((), {}),
        )
        last_type_error = None
        for args, kwargs in attempts:
            try:
                return RuleCreationService(*args, **kwargs)
            except TypeError as exc:
                last_type_error = exc
                continue
            except Exception:
                # A present service which cannot initialise should be reported
                # as unavailable by the bridge, not retried with a different
                # scope (which could accidentally broaden writes).
                return None
        _ = last_type_error
        return None

    def _rule_bridge_context(
        self,
        context: dict | None = None,
        *,
        agent_instance_id: str = "",
        share_group_id: str = "default",
        project_ref: str = "",
        provider: str = "",
        runtime_role: str = "",
    ):
        """Validate a concrete current-agent context for rule creation.

        UI values must originate from ``get_rule_scope_options``.  In
        particular, this bridge never accepts ``system`` or a guessed
        cross-Agent identifier for an automatic decision.
        """
        from .schema_v3 import EffectiveAgentContext

        raw = dict(context or {}) if isinstance(context, dict) else {}
        agent = str(raw.get("agent_instance_id") or agent_instance_id or "").strip()
        group = str(raw.get("share_group_id") or share_group_id or "default").strip()
        project = str(raw.get("project_ref") or project_ref or "").strip()
        provider_value = str(raw.get("provider") or provider or "").strip()
        role = str(raw.get("runtime_role") or runtime_role or "").strip()
        if not agent:
            return None, {"ok": False, "error": "agent_context_required"}
        options = self._rule_scope_options(group)
        known_agents = {str(item.get("id") or "") for item in options.get("agents", [])}
        if known_agents and agent not in known_agents:
            return None, {"ok": False, "error": "unknown_agent_target"}
        known_groups = {str(item.get("id") or "") for item in options.get("groups", [])}
        if known_groups and group not in known_groups:
            return None, {"ok": False, "error": "unknown_group_target"}
        known_projects = {str(item.get("id") or "") for item in options.get("projects", [])}
        if project and known_projects and project not in known_projects:
            return None, {"ok": False, "error": "unknown_project_target"}
        known_providers = {str(item.get("id") or "").casefold() for item in options.get("providers", [])}
        if provider_value and known_providers and provider_value.casefold() not in known_providers:
            return None, {"ok": False, "error": "unknown_provider_target"}
        known_roles = {str(item.get("id") or "").casefold() for item in options.get("runtime_roles", [])}
        if role and known_roles and role.casefold() not in known_roles:
            return None, {"ok": False, "error": "unknown_runtime_role_target"}
        return EffectiveAgentContext(
            agent_instance_id=agent,
            share_group_id=group,
            provider=provider_value,
            project_ref=project,
            runtime_role=role,
        ), None

    @staticmethod
    def _resolve_current_group_scope(preference, env_group: str) -> str:
        """Resolve an explicit group from env or persisted scope preference."""
        if env_group:
            return env_group
        if preference is not None and preference.mode == "share_group":
            return str(preference.share_group_id or "").strip()
        return ""

    def _resolve_current_rule_agent(self, preference) -> tuple[str, str, str | None]:
        """Resolve a trusted current agent from runtime state, falling back to
        persisted governance scope and finally to a unique active binding.

        Returns ``(agent_id, group_id, error_payload)``.  The Agent is always
        resolved *before* the group: when a personal ``GovernanceScope`` stores
        ``mode="agent"`` without a ``share_group_id``, the group must be
        re-derived from the Agent's active binding ledger (``personal-<hash>``),
        never defaulted to ``"default"``.
        """
        from .access_context import load_access_context
        from .agent_binding import AgentBindingStore
        from .schema_v3 import BindingStatus

        access = load_access_context()
        env_agent = str(access.trusted_agent_id or "").strip()
        env_group = str(os.environ.get("MEMORYGUARD_SHARE_GROUP_ID", "") or "").strip()

        scoped_agent = ""
        if preference is not None and preference.mode == "agent":
            scoped_agent = str(preference.agent_instance_id or "").strip()
        explicit_group = self._resolve_current_group_scope(preference, env_group)

        agent = env_agent or scoped_agent
        store = AgentBindingStore(self.workspace)

        def _active(binding) -> bool:
            status = getattr(binding, "status", BindingStatus.ACTIVE)
            return status == BindingStatus.ACTIVE

        if agent:
            active = [
                b for b in store.find_by_agent(agent, include_inactive=False)
                if _active(b)
            ]
            if explicit_group:
                matches = [b for b in active if str(b.share_group_id or "") == explicit_group]
                if len(matches) == 1:
                    return agent, explicit_group, None
                return None, explicit_group, {
                    "ok": False,
                    "error": "agent_not_bound_to_group",
                    "reason": f"governed agent {agent!r} has no active binding in group {explicit_group!r}",
                }
            if len(active) == 1:
                return agent, str(active[0].share_group_id or "default"), None
            if len(active) > 1:
                return None, "", {
                    "ok": False,
                    "error": "multiple_active_bindings",
                    "reason": f"agent {agent!r} has multiple active bindings; set MEMORYGUARD_SHARE_GROUP_ID or governance scope",
                }
            # First-run trusted-agent path: no binding files exist yet, so we
            # safely establish a personal memory group before creating rules.
            ensure = getattr(store, "ensure_personal_memory_group", None)
            if callable(ensure):
                try:
                    created = ensure(agent)
                except Exception:
                    created = None
                if created and created.get("share_group_id"):
                    return agent, str(created["share_group_id"]), None
            return agent, explicit_group or "default", None

        if explicit_group:
            members = [
                b for b in store.find_by_group(explicit_group, include_inactive=False)
                if _active(b)
            ]
            if len(members) == 1:
                return str(members[0].agent_instance_id or "").strip(), explicit_group, None
            if len(members) > 1:
                return None, explicit_group, {
                    "ok": False,
                    "error": "ambiguous_agent_context",
                    "reason": "multiple active agents in group; set governance scope once",
                }
        return None, explicit_group or "", {
            "ok": False,
            "error": "agent_context_required",
            "reason": "rule creation requires a trusted current agent "
            "(env id, agent governance scope, or unique active binding)",
        }

    def _trusted_rule_bridge_context(self):
        """Resolve rule-creation context from trusted runtime state only.

        The rule page's diagnostic selectors are intentionally untrusted
        preview state.  Auto-creation therefore cannot accept a context
        payload from the browser; identity and project come from the local
        host environment/current working directory (with the persisted
        governance scope used only to choose the storage group).  Missing
        Agent identity fails closed.
        """
        from .governance_scope import load_scope_preference
        from .rule_scope import canonical_project_ref

        preference = load_scope_preference(self.workspace)
        agent, group, error = self._resolve_current_rule_agent(preference)
        if error:
            return None, error

        project_raw = str(
            os.environ.get("MEMORYGUARD_PROJECT_CWD") or os.getcwd()
        ).strip()
        project = canonical_project_ref(project_raw) if project_raw else ""
        provider = str(os.environ.get("MEMORYGUARD_PROVIDER", "") or "").strip().lower()
        runtime_role = str(os.environ.get("MEMORYGUARD_RUNTIME_ROLE", "") or "").strip()
        return self._rule_bridge_context(
            None,
            agent_instance_id=agent,
            share_group_id=group or "default",
            project_ref=project,
            provider=provider,
            runtime_role=runtime_role,
        )

    @staticmethod
    def _rule_bridge_call(service, names: tuple[str, ...], *args, **kwargs):
        """Invoke the first implemented service method.

        ``None`` means no compatible method was found; exceptions are left to
        the caller so an implementation error is visible in the UI response.
        """
        for name in names:
            fn = getattr(service, name, None)
            if not callable(fn):
                continue
            try:
                return True, fn(*args, **kwargs)
            except TypeError:
                # Compatibility implementations may use keyword-only context
                # or omit optional arguments.  Retry conservative forms only.
                try:
                    return True, fn(*args)
                except TypeError:
                    continue
        return False, None

    @staticmethod
    def _trusted_gui_feedback_call(service, receipt_id: str, outcome: str,
                                   evidence: str, confidence: float, context):
        """Call feedback service only through trusted GUI producer boundary.

        Do not retry without ``producer`` or ``effective_context``: that
        compatibility path would downgrade GUI input to an agent event or skip
        receipt ownership validation.
        """
        import inspect

        for name in ("submit_feedback", "feedback", "submit_rule_feedback"):
            fn = getattr(service, name, None)
            if not callable(fn):
                continue
            try:
                parameters = tuple(inspect.signature(fn).parameters.values())
            except (TypeError, ValueError):
                parameters = ()
            accepts_kwargs = any(
                item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters
            )
            names = {item.name for item in parameters}
            if not accepts_kwargs and not {"producer", "effective_context"}.issubset(names):
                # Older lifecycle services cannot prove this GUI boundary;
                # caller may use the ownership-checked store fallback instead.
                continue
            call_kwargs = {
                "evidence": evidence,
                "confidence": confidence,
                "effective_context": context,
                "producer": "user",
            }
            if accepts_kwargs or "actor_id" in names:
                call_kwargs["actor_id"] = "user"
            return True, fn(
                receipt_id,
                outcome,
                "user",
                **call_kwargs,
            )
        return False, None

    def create_rule_from_text(
        self,
        text: str,
        context: dict | None = None,
        agent_instance_id: str = "",
        share_group_id: str = "default",
        project_ref: str = "",
        provider: str = "",
        runtime_role: str = "",
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        """Create one rule from a sentence using the optional lifecycle service.

        Scope is always derived from trusted host context, never browser
        preview selectors.  A low-confidence service result is returned
        verbatim (including its narrow candidates); this bridge never upgrades
        it to a group/system audience.
        """
        sentence = str(text or "").strip()
        if not sentence:
            return {"ok": False, "error": "rule_text_required"}
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        # Ignore all request-supplied context for automatic creation.  The
        # browser may provide only sentence text; trusted Agent/project state
        # is resolved from the host process above.
        ctx, error = self._trusted_rule_bridge_context()
        if error:
            return error
        service = self._rule_bridge_service(ctx.share_group_id)
        if service is None:
            return {"ok": False, "error": "service_unavailable"}
        called, raw = self._rule_bridge_call(
            service, ("create_rule_from_text", "create_rule"), sentence, ctx,
        )
        if not called:
            return {"ok": False, "error": "service_method_unavailable"}
        payload = self._rule_bridge_payload(raw)
        if payload.get("status") == "blocked" or payload.get("blocked_reason"):
            payload.setdefault("ok", False)
            payload.setdefault("error", payload.get("blocked_reason") or payload.get("scope_reason") or "rule_creation_blocked")
        else:
            payload.setdefault("ok", True)
        payload.setdefault("rule_id", payload.get("memory_id", ""))
        payload.setdefault("memory_id", payload.get("rule_id", ""))
        payload.setdefault("assignments", payload.get("assignment", []))
        payload.setdefault("scope_confidence", payload.get("confidence", None))
        payload.setdefault("scope_reason", payload.get("reason", ""))
        payload.setdefault("decision_id", payload.get("event_id", ""))
        payload.setdefault("undo_id", "")
        payload["context"] = {
            "agent_instance_id": ctx.agent_instance_id,
            "share_group_id": ctx.share_group_id,
            "project_ref": ctx.project_ref,
            "provider": ctx.provider,
            "runtime_role": ctx.runtime_role,
        }
        return payload

    def list_rule_decisions(self, share_group_id: str = "default", limit: int = 50) -> dict:
        service = self._rule_bridge_service(share_group_id)
        if service is not None:
            called, raw = self._rule_bridge_call(service, ("list_decisions", "list_rule_decisions"), limit=limit)
            if called:
                payload = self._rule_bridge_payload(raw)
                if isinstance(raw, (list, tuple)):
                    payload = {"decisions": [self._rule_bridge_payload(item) for item in raw]}
                payload.setdefault("decisions", payload.get("items", []))
                payload.setdefault("total", len(payload.get("decisions", [])))
                return payload
        # Older stores may expose persistence helpers without the service.
        try:
            store, err = self._open_store(share_group_id, read_only=True)
            if err:
                return err
            fn = getattr(store, "list_rule_decisions", None)
            if callable(fn):
                rows = fn()
                rows = list(rows)[-max(1, int(limit or 50)):]
                return {"decisions": [self._rule_bridge_payload(item) for item in rows], "total": len(rows)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"decisions": [], "total": 0, "service_unavailable": True}

    def list_rule_cockpit(
        self,
        share_group_id: str = "default",
        agent_instance_id: str = "",
        limit: int = 50,
    ) -> dict:
        """Single read-only snapshot for desktop/localhost cockpit clients."""
        rules = self.list_rules_habits(share_group_id)
        options = self.get_rule_scope_options(share_group_id)
        decisions = self.list_rule_decisions(share_group_id, limit=limit)
        metrics = self.get_rule_auto_scope_metrics(share_group_id)
        receipts = self.list_rule_match_receipts(
            share_group_id, agent_instance_id=agent_instance_id, limit=limit,
        )
        exceptions = self.list_rule_exceptions(share_group_id)
        return {
            "ok": True,
            "share_group_id": share_group_id,
            "rules": rules,
            "scope_options": options,
            "decisions": decisions,
            "metrics": metrics,
            "receipts": receipts,
            "exceptions": exceptions,
        }

    def read_rule_decision(self, decision_id: str, share_group_id: str = "default") -> dict:
        if not str(decision_id or "").strip():
            return {"ok": False, "error": "decision_id_required"}
        service = self._rule_bridge_service(share_group_id)
        if service is not None:
            called, raw = self._rule_bridge_call(service, ("read_decision", "get_decision"), decision_id)
            if called:
                payload = self._rule_bridge_payload(raw)
                return payload or {"ok": False, "error": "decision_not_found"}
        return {"ok": False, "error": "service_unavailable"}

    def undo_rule_decision(
        self,
        decision_id: str,
        share_group_id: str = "default",
        confirmed: bool = False,
        context: dict | None = None,
        agent_instance_id: str = "",
        project_ref: str = "",
        provider: str = "",
        runtime_role: str = "",
        *,
        _admin_override: bool = False,
    ) -> dict:
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        if not str(decision_id or "").strip():
            return {"ok": False, "error": "decision_id_required"}
        # Ignore request-supplied context for undo authorization.  Undo of a
        # governed decision is authorized by the trusted host context, not by
        # browser-supplied agent/project claims.
        ctx, error = self._trusted_rule_bridge_context()
        if error:
            return error
        service = self._rule_bridge_service(ctx.share_group_id)
        if service is None:
            return {"ok": False, "error": "service_unavailable"}
        # The real service contract is ``undo_rule_decision(decision_id,
        # context)``: it loads the structured decision by ID and computes the
        # inverse atomically.  Resolving decision_id -> undo_id here would hand
        # the service a legacy snapshot token that read_decision cannot find.
        undo_by_decision = getattr(service, "undo_rule_decision", None)
        if callable(undo_by_decision):
            raw = undo_by_decision(
                str(decision_id),
                ctx,
            )
        else:
            # Compatibility path: only callers without the new contract may use
            # the legacy ``undo_rule`` with a resolved undo_id.
            legacy_undo = getattr(service, "undo_rule", None)
            if not callable(legacy_undo):
                return {"ok": False, "error": "service_method_unavailable"}
            decision = service.read_decision(str(decision_id))
            if decision is None or not getattr(decision, "undo_id", ""):
                return {"ok": False, "error": "structured_decision_required"}
            raw = legacy_undo(
                str(getattr(decision, "undo_id", "")),
                ctx,
            )
        payload = self._rule_bridge_payload(raw)
        if payload.get("status") == "blocked" or payload.get("blocked_reason"):
            payload.setdefault("ok", False)
            payload.setdefault("error", payload.get("blocked_reason") or payload.get("reason") or "rule_undo_blocked")
        else:
            payload.setdefault("ok", True)
        payload.setdefault("decision_id", decision_id)
        return payload

    def revoke_rule_exception(
        self,
        exception_id: str,
        share_group_id: str = "default",
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        if not str(exception_id or "").strip():
            return {"ok": False, "error": "exception_id_required"}
        # Exception revocation mutates shared rules.  Authorization comes from
        # the trusted host context, never browser-supplied group claims.
        # Admins may proceed without a trusted Agent identity; everyone else
        # must resolve one or fail closed.
        ctx, error = self._trusted_rule_bridge_context()
        if error:
            return error
        service = self._rule_bridge_service(
            ctx.share_group_id if ctx is not None else share_group_id,
        )
        if service is None:
            return {"ok": False, "error": "atomic_rule_exception_service_required"}
        revoke = getattr(service, "revoke_exception", None)
        if not callable(revoke):
            # The product path must never fall back to the legacy
            # ``rollback_rule_exception`` store helper: that path only flips a
            # relation's active flag without restoring the parent exclude or
            # deleting the child rule.  When the atomic revert is unavailable
            # we report failure honestly instead of pretending success.
            return {"ok": False, "error": "atomic_rule_exception_revert_unavailable"}
        raw = revoke(
            str(exception_id),
            effective_context=ctx,
        )
        payload = self._rule_bridge_payload(raw)
        if payload.get("status") == "blocked" or payload.get("blocked_reason"):
            payload.setdefault("ok", False)
            payload.setdefault("error", payload.get("blocked_reason") or payload.get("reason") or "rule_exception_revoke_blocked")
        else:
            payload.setdefault("ok", True)
        payload.setdefault("exception_id", exception_id)
        return payload

    def get_rule_auto_scope_metrics(self, share_group_id: str = "default") -> dict:
        service = self._rule_bridge_service(share_group_id)
        if service is not None:
            called, raw = self._rule_bridge_call(service, ("scope_stats", "get_scope_stats", "get_rule_scope_stats"))
            if called:
                payload = self._rule_bridge_payload(raw)
                if isinstance(raw, (list, tuple)):
                    payload = {"stats": [self._rule_bridge_payload(item) for item in raw]}
                payload.setdefault("stats", payload.get("items", []))
                if not payload.get("auto_scope"):
                    payload["auto_scope"] = payload.get("metrics") or {
                        "assignment_count": payload.get("assignment_count", 0),
                        "by_target_type": payload.get("by_target_type", {}),
                        "active_by_target_type": payload.get("active_by_target_type", {}),
                        "inference_policy": payload.get("inference_policy", ""),
                    }
                # RuleCreationService exposes aggregate assignment counts;
                # enrich the cockpit with persisted per-rule counters when
                # the store supports the v3.2 stats table.
                if not payload.get("stats"):
                    try:
                        store, store_err = self._open_store(share_group_id, read_only=True)
                        stats_fn = getattr(store, "list_rule_scope_stats", None) if not store_err else None
                        if callable(stats_fn):
                            payload["stats"] = [self._rule_bridge_payload(item) for item in stats_fn()]
                    except Exception:
                        pass
                return payload
        try:
            store, err = self._open_store(share_group_id, read_only=True)
            if err:
                return err
            fn = getattr(store, "list_rule_scope_stats", None)
            if callable(fn):
                rows = list(fn())
                stats = [self._rule_bridge_payload(item) for item in rows]
                return {"stats": stats, "auto_scope": {"total": sum(int(x.get("total", 0) or 0) for x in stats)}}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"stats": [], "auto_scope": {}, "service_unavailable": True}

    def list_rule_match_receipts(
        self,
        share_group_id: str = "default",
        memory_id: str = "",
        agent_instance_id: str = "",
        limit: int = 50,
    ) -> dict:
        store, err = self._open_store(share_group_id, read_only=True)
        if err:
            return err
        fn = getattr(store, "list_rule_match_receipts", None)
        if not callable(fn):
            return {"receipts": [], "total": 0, "service_unavailable": True}
        rows = fn(
            memory_id=memory_id or None,
            agent_instance_id=agent_instance_id or None,
            share_group_id=share_group_id,
        )
        rows = list(rows)[-max(1, int(limit or 50)):]
        receipts = []
        for row in rows:
            item = self._rule_bridge_payload(row)
            receipt_id = str(item.get("receipt_id") or "")
            feedback_fn = getattr(store, "get_rule_match_feedback_by_receipt", None)
            if callable(feedback_fn) and receipt_id:
                feedback = feedback_fn(receipt_id)
                if feedback is not None:
                    item["feedback"] = self._rule_bridge_payload(feedback)
            receipts.append(item)
        return {"receipts": receipts, "total": len(receipts)}

    def submit_rule_feedback(
        self,
        receipt_id: str,
        outcome: str,
        actor: str = "",
        evidence: str = "",
        share_group_id: str = "default",
        confidence: float = 1.0,
        *,
        _admin_override: bool = False,
    ) -> dict:
        if not str(receipt_id or "").strip():
            return {"ok": False, "error": "receipt_id_required"}
        # ``actor`` is a legacy display argument.  GUI feedback is a trusted
        # human boundary; never infer producer/authority from browser actor.
        ctx, context_error = self._trusted_rule_bridge_context()
        if context_error:
            return context_error
        service = self._rule_bridge_service(ctx.share_group_id)
        if service is not None:
            called, raw = self._trusted_gui_feedback_call(
                service, str(receipt_id), str(outcome), str(evidence or ""),
                float(confidence), ctx,
            )
            if called:
                payload = self._rule_bridge_payload(raw)
                payload.setdefault("ok", True)
                payload["producer"] = "user"
                payload["source"] = "user"
                payload["authority"] = 4
                payload["actor"] = "user"
                return payload
        # Lifecycle feedback (narrowing / exception split) cannot be satisfied
        # by the plain append-only store fallback: it would record the event
        # without applying the narrowing or exception behavior.  Fail closed
        # instead of silently degrading a behavioral decision.
        if str(outcome) in {"not_applicable", "exception"}:
            return {"ok": False, "error": "lifecycle_feedback_requires_rule_service"}
        # Stable fallback for stores that already implement explicit receipts.
        try:
            from .schema_v3 import RuleMatchFeedback
            store, err = self._open_store(ctx.share_group_id, must_exist=True)
            if err:
                return err
            receipt_fn = getattr(store, "get_rule_match_receipt", None)
            receipt = receipt_fn(str(receipt_id)) if callable(receipt_fn) else None
            if receipt is None:
                return {"ok": False, "error": "receipt_not_found"}
            if str(receipt.share_group_id or ctx.share_group_id) != str(ctx.share_group_id):
                return {"ok": False, "error": "feedback_share_group_mismatch"}
            if str(receipt.agent_instance_id or "") != str(ctx.agent_instance_id):
                return {"ok": False, "error": "feedback_agent_does_not_own_receipt"}
            effective_fn = getattr(store, "get_effective_rule_match_feedback", None)
            if not callable(effective_fn):
                effective_fn = getattr(store, "get_rule_match_feedback_by_receipt", None)
            prior = effective_fn(str(receipt_id)) if callable(effective_fn) else None
            feedback = RuleMatchFeedback(
                feedback_id="", receipt_id=str(receipt_id), outcome=str(outcome),
                actor="user", evidence=str(evidence or ""), confidence=float(confidence),
                source="user", authority=4,
                supersedes_feedback_id=str(getattr(prior, "feedback_id", "") or ""),
            )
            fn = getattr(store, "append_rule_match_feedback", None)
            if not callable(fn):
                return {"ok": False, "error": "service_unavailable"}
            saved = fn(feedback)
            return {
                "ok": True,
                "producer": "user",
                "source": "user",
                "authority": 4,
                "feedback": self._rule_bridge_payload(saved),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def list_rule_exceptions(self, share_group_id: str = "default", parent_rule: str = "") -> dict:
        service = self._rule_bridge_service(share_group_id)
        if service is not None:
            called, raw = self._rule_bridge_call(service, ("list_exceptions", "list_rule_exceptions"), parent_rule)
            if called:
                payload = self._rule_bridge_payload(raw)
                if isinstance(raw, (list, tuple)):
                    payload = {"exceptions": [self._rule_bridge_payload(item) for item in raw]}
                payload.setdefault("exceptions", payload.get("items", []))
                payload.setdefault("total", len(payload.get("exceptions", [])))
                return payload
        try:
            store, err = self._open_store(share_group_id, read_only=True)
            if err:
                return err
            fn = getattr(store, "list_rule_exceptions", None)
            if callable(fn):
                rows = fn(parent_rule=parent_rule or None)
                rows = list(rows)
                return {"exceptions": [self._rule_bridge_payload(item) for item in rows], "total": len(rows)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"exceptions": [], "total": 0, "service_unavailable": True}

    def create_child_exception(
        self,
        parent_rule: str,
        child_exception: str,
        priority: int = 0,
        reason: str = "",
        share_group_id: str = "default",
        confirmed: bool = False,
        *,
        _admin_override: bool = False,
    ) -> dict:
        if not confirmed:
            return {"ok": False, "error": "confirmation_required"}
        if not str(parent_rule or "").strip() or not str(child_exception or "").strip():
            return {"ok": False, "error": "parent_and_child_required"}
        if str(parent_rule).strip() == str(child_exception).strip():
            return {"ok": False, "error": "rule_exception_cannot_reference_itself"}
        service = self._rule_bridge_service(share_group_id)
        # The service's ``create_child_exception`` is receipt-driven (it
        # creates a narrower rule after an ``exception`` feedback).  This GUI
        # action is the explicit parent/child relation editor, so prefer a
        # dedicated service method when available and otherwise use the stable
        # v3.2 store bridge.
        if service is not None:
            called, raw = self._rule_bridge_call(
                service,
                ("create_rule_exception", "add_rule_exception"),
                parent_rule, child_exception, priority, reason,
            )
            if called:
                payload = self._rule_bridge_payload(raw)
                payload.setdefault("ok", True)
                payload.setdefault("parent_rule", parent_rule)
                payload.setdefault("child_exception", child_exception)
                payload.setdefault("priority", int(priority))
                payload.setdefault("reason", reason)
                return payload
        try:
            from .schema_v3 import RuleException
            store, err = self._open_store(share_group_id, must_exist=True)
            if err:
                return err
            fn = getattr(store, "append_rule_exception", None)
            if not callable(fn):
                return {"ok": False, "error": "service_unavailable"}
            relation = RuleException(
                parent_rule=str(parent_rule), child_exception=str(child_exception),
                priority=int(priority), reason=str(reason),
            )
            saved = fn(relation)
            return {"ok": True, **self._rule_bridge_payload(saved), "parent_rule": parent_rule,
                    "child_exception": child_exception, "priority": int(priority), "reason": reason}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # Compatibility alias used by the security registry and early cockpit
    # clients.  Keep one implementation so parent/child validation cannot
    # drift between endpoint names.
    def create_rule_exception(self, *args, **kwargs) -> dict:
        return self.create_child_exception(*args, **kwargs)

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

    def create_build_plan(self, target_path: str = "",
                          scope: dict | None = None,
                          agent_instance_id: str = "",
                          target_root_id: str = "") -> dict:
        from .adapters import GenericMarkdownTarget
        from .release_manager import ReleaseManager
        from .source_registry import ScanBudget, SourceRegistry
        from .governance_scope import (
            GovernanceScope, filter_ir_for_agent, resolve_scoped_roots,
            derive_publish_target_file, root_authorizes_agent,
        )
        from pathlib import Path
        parsed, err = self._parse_scope(scope, agent_instance_id=agent_instance_id, mode="agent")
        if err or not parsed or parsed.get("mode") != "agent":
            return {"error": err or "agent_scope_required"}
        if not target_root_id:
            return {"error": "target_root_id_required"}
        gscope = GovernanceScope.from_dict(parsed)
        reg = SourceRegistry(self.workspace)
        root = next((r for r in reg.list_all_sources() if r.root_id == target_root_id), None)
        if root is None or not root_authorizes_agent(root, gscope.agent_instance_id):
            return {"error": "target_root_not_authorized_for_agent"}
        rm = ReleaseManager(self.workspace)
        target = GenericMarkdownTarget()
        derived = derive_publish_target_file(root)
        tp = derived.parent if derived.suffix else derived
        if target_path:
            # 仅当与派生路径一致时允许
            try:
                if Path(target_path).resolve() not in {tp.resolve(), derived.resolve()}:
                    return {"error": "target_file_mismatch_root"}
            except OSError:
                return {"error": "target_file_mismatch_root"}
        snap, ir = rm.scan_and_normalize(ScanBudget())
        roots, _ = resolve_scoped_roots(reg.list_all_sources(), gscope, enabled_only=True)
        scoped_ir = filter_ir_for_agent(ir, {r.root_id for r in roots}, snap)
        try:
            plan = rm.create_build_plan(
                scoped_ir, target, tp,
                governance_scope=parsed, target_root_id=target_root_id,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return plan.to_dict()

    def apply_build(self, plan_id: str, confirmed: bool = False,
                    target_path: str = "",
                    scope: dict | None = None,
                    agent_instance_id: str = "",
                    target_root_id: str = "") -> dict:
        if not confirmed:
            return {"error": "需要确认才能应用构建"}
        from .adapters import GenericMarkdownTarget
        from .release_manager import ReleaseManager
        from .source_registry import SourceRegistry
        from .governance_scope import (
            GovernanceScope, derive_publish_target_file, root_authorizes_agent,
        )
        from pathlib import Path
        parsed, err = self._parse_scope(scope, agent_instance_id=agent_instance_id, mode="agent")
        if err or not parsed or parsed.get("mode") != "agent":
            return {"error": err or "agent_scope_required"}
        if not target_root_id:
            return {"error": "target_root_id_required"}
        gscope = GovernanceScope.from_dict(parsed)
        reg = SourceRegistry(self.workspace)
        root = next((r for r in reg.list_all_sources() if r.root_id == target_root_id), None)
        if root is None or not root_authorizes_agent(root, gscope.agent_instance_id):
            return {"error": "target_root_not_authorized_for_agent"}
        derived = derive_publish_target_file(root)
        tp = derived.parent if derived.suffix else derived
        if target_path:
            try:
                if Path(target_path).resolve() not in {tp.resolve(), derived.resolve()}:
                    return {"error": "target_file_mismatch_root"}
            except OSError:
                return {"error": "target_file_mismatch_root"}
        rm = ReleaseManager(self.workspace)
        target = GenericMarkdownTarget()
        try:
            release = rm.apply_build(
                plan_id, target, tp, approval=True,
                expected_scope=parsed, expected_target_root_id=target_root_id,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return release.to_dict()

    def verify_release(self, release_id: str, target_path: str = "",
                       scope: dict | None = None,
                       agent_instance_id: str = "",
                       target_root_id: str = "") -> dict:
        from .adapters import GenericMarkdownTarget
        from .release_manager import ReleaseManager
        from .source_registry import SourceRegistry
        from .governance_scope import (
            GovernanceScope, derive_publish_target_file, root_authorizes_agent,
        )
        from pathlib import Path
        import json
        parsed, err = self._parse_scope(scope, agent_instance_id=agent_instance_id, mode="agent")
        if err or not parsed or parsed.get("mode") != "agent":
            return {"error": err or "agent_scope_required"}
        if not target_root_id:
            return {"error": "target_root_id_required"}
        gscope = GovernanceScope.from_dict(parsed)
        reg = SourceRegistry(self.workspace)
        root = next((r for r in reg.list_all_sources() if r.root_id == target_root_id), None)
        if root is None or not root_authorizes_agent(root, gscope.agent_instance_id):
            return {"error": "target_root_not_authorized_for_agent"}
        derived = derive_publish_target_file(root)
        tp = derived.parent if derived.suffix else derived
        if target_path:
            try:
                if Path(target_path).resolve() not in {tp.resolve(), derived.resolve()}:
                    return {"error": "target_file_mismatch_root"}
            except OSError:
                return {"error": "target_file_mismatch_root"}
        release_path = Path(self.workspace) / ".memoryguard" / "releases" / f"{release_id}.json"
        if not release_path.exists():
            release_path = Path(self.workspace) / ".memoryguard" / "changes" / f"{release_id}.json"
        if not release_path.exists():
            return {"error": "release not found"}
        data = json.loads(release_path.read_text(encoding="utf-8"))
        try:
            ReleaseManager.validate_release_binding(
                data,
                expected_scope=parsed,
                expected_target_root_id=target_root_id,
                expected_target_path=tp,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        manifest = data.get("manifest")
        if manifest is None:
            build_id = data.get("build_id", "")
            plans_dir = Path(self.workspace) / ".memoryguard" / "plans"
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
        rm = ReleaseManager(self.workspace)
        target = GenericMarkdownTarget()
        return rm.verify_release(release_id, target, tp, mm)

    def rollback_release(self, release_id: str, confirmed: bool = False,
                         target_path: str = "",
                         scope: dict | None = None,
                         agent_instance_id: str = "",
                         target_root_id: str = "") -> dict:
        if not confirmed:
            return {"error": "需要确认才能回滚"}
        from .adapters import GenericMarkdownTarget
        from .release_manager import ReleaseManager
        from .source_registry import SourceRegistry
        from .governance_scope import (
            GovernanceScope, derive_publish_target_file, root_authorizes_agent,
        )
        from pathlib import Path
        import json
        parsed, err = self._parse_scope(scope, agent_instance_id=agent_instance_id, mode="agent")
        if err or not parsed or parsed.get("mode") != "agent":
            return {"error": err or "agent_scope_required"}
        if not target_root_id:
            return {"error": "target_root_id_required"}
        gscope = GovernanceScope.from_dict(parsed)
        reg = SourceRegistry(self.workspace)
        root = next((r for r in reg.list_all_sources() if r.root_id == target_root_id), None)
        if root is None or not root_authorizes_agent(root, gscope.agent_instance_id):
            return {"error": "target_root_not_authorized_for_agent"}
        derived = derive_publish_target_file(root)
        tp = derived.parent if derived.suffix else derived
        if target_path:
            try:
                if Path(target_path).resolve() not in {tp.resolve(), derived.resolve()}:
                    return {"error": "target_file_mismatch_root"}
            except OSError:
                return {"error": "target_file_mismatch_root"}
        release_path = Path(self.workspace) / ".memoryguard" / "releases" / f"{release_id}.json"
        if not release_path.exists():
            return {"error": "release not found"}
        data = json.loads(release_path.read_text(encoding="utf-8"))
        try:
            ReleaseManager.validate_release_binding(
                data,
                expected_scope=parsed,
                expected_target_root_id=target_root_id,
                expected_target_path=tp,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        rm = ReleaseManager(self.workspace)
        target = GenericMarkdownTarget()
        try:
            rb = rm.rollback_release(
                release_id, target, tp,
                expected_scope=parsed,
                expected_target_root_id=target_root_id,
                expected_target_path=tp,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return rb.to_dict()

    def list_releases(self) -> dict:
        from .release_manager import ReleaseManager
        rm = ReleaseManager(self.workspace)
        return {"releases": rm.list_releases()}

    def plan_memoryguard_gc(
        self,
        older_than_days: int = 30,
        keep_releases: int = 20,
        keep_snapshots: int = 3,
    ) -> dict:
        from .gc import MemoryGuardGc

        gc = MemoryGuardGc(
            self.workspace,
            older_than_days=older_than_days,
            keep_releases=keep_releases,
            keep_snapshots=keep_snapshots,
        )
        return gc.plan(dry_run=True).to_dict()

    def apply_memoryguard_gc(
        self,
        confirmed: bool = False,
        older_than_days: int = 30,
        keep_releases: int = 20,
        keep_snapshots: int = 3,
    ) -> dict:
        if not confirmed:
            return {"error": "需要确认才能执行 GC"}
        from .gc import MemoryGuardGc

        gc = MemoryGuardGc(
            self.workspace,
            older_than_days=older_than_days,
            keep_releases=keep_releases,
            keep_snapshots=keep_snapshots,
        )
        plan = gc.plan(dry_run=False)
        result = gc.apply(plan, confirmed=True)
        # P2.1: GC 后重扫,确保 IR 与清理后的状态一致
        if result.get("ok"):
            try:
                from .memory_ir import MemoryNormalizer
                from .source_registry import SourceRegistry, ScanBudget
                reg = SourceRegistry(self.workspace)
                snap = reg.scan(ScanBudget())
                roots = reg.list_sources()
                root_map = {r.root_id: r.path for r in roots}
                root_policies = {r.root_id: {"source_category": r.source_category,
                                             "ingestion_policy": r.ingestion_policy} for r in roots}
                norm = MemoryNormalizer(self.workspace)
                ir = norm.load()
                if ir is None or ir.snapshot_id != snap.snapshot_id:
                    ir = norm.normalize(snap, root_map=root_map, root_policies=root_policies)
                    norm.save(ir)
                result["rescan"] = {"ok": True, "snapshot_id": snap.snapshot_id}
            except Exception as exc:
                # P2.1: 重扫失败必须令顶层 ok 失败,不能只附加 rescan.ok=False
                result["rescan"] = {"ok": False, "error": str(exc)}
                result["ok"] = False
                if not result.get("errors"):
                    result["errors"] = []
                result["errors"].append(f"post-GC rescan failed: {exc}")
        return result

    def list_history(self) -> dict:
        """v3.1 §8.4：统一历史时间线（rule_change + memory_release + warnings）。

        损坏 JSON 不会让页面崩溃，会在 warnings 中显示。
        """
        from .change_history import list_change_history
        from pathlib import Path
        return list_change_history(Path(self.workspace))

    def get_storage_overview(self) -> dict:
        """P2.1: 存储概览,供 GUI 存储页展示 .memoryguard/ 各子目录大小。"""
        from pathlib import Path
        mg_dir = Path(self.workspace) / ".memoryguard"
        if not mg_dir.is_dir():
            return {"total_bytes": 0, "categories": {}, "has_prev_ir": False}
        categories: dict[str, int] = {}
        total = 0
        for child in mg_dir.iterdir():
            if not child.is_dir() and not child.is_file():
                continue
            name = child.name if child.is_dir() else child.stem
            size = 0
            if child.is_file():
                try:
                    size = child.stat().st_size
                except OSError:
                    pass
            else:
                for f in child.rglob("*"):
                    if f.is_file():
                        try:
                            size += f.stat().st_size
                        except OSError:
                            pass
            categories[name] = categories.get(name, 0) + size
            total += size
        has_prev = (mg_dir / "ir" / "current.prev.json").exists()
        return {"total_bytes": total, "categories": categories, "has_prev_ir": has_prev}

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

    # -----------------------------------------------------------------------
    # v3.3 Host AI Enrichment
    # -----------------------------------------------------------------------

    def list_pending_enrichments(
        self,
        limit: int = 50,
        agent_instance_id: str = "",
        share_group_id: str = "",
        scope: dict | None = None,
    ) -> dict:
        """列出待宿主 AI 整理的记忆任务（与 MCP list 同一队列）。"""
        from .host_enrichment import list_pending as _list
        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id, share_group_id=share_group_id,
        ) if (scope or agent_instance_id or share_group_id) else (None, "")
        if err:
            return {"error": err}
        aid = ""
        gid = ""
        if parsed:
            if parsed.get("mode") == "share_group":
                gid = parsed.get("share_group_id", "")
            else:
                aid = parsed.get("agent_instance_id", "")
        else:
            aid = agent_instance_id
            gid = share_group_id
        tasks = _list(
            self.workspace, limit=limit,
            agent_instance_id=aid, share_group_id=gid,
        )
        return {
            "pending_count": len(tasks),
            "tasks": tasks,
            "mode": "build_integrated",
            "hint": "primary: build_projection; residual: MCP list/apply",
        }

    def apply_enrichments(
        self,
        results: list,
        agent_instance_id: str = "",
        share_group_id: str = "",
        scope: dict | None = None,
    ) -> dict:
        """宿主 AI 回写整理结果到 IR / SharedMemoryStore。默认不自动 rebuild。"""
        from .host_enrichment import apply_results as _apply
        if not results or not isinstance(results, list):
            return {"error": "results must be a non-empty list"}
        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id, share_group_id=share_group_id,
        ) if (scope or agent_instance_id or share_group_id) else (None, "")
        if err:
            return {"error": err}
        aid = ""
        gid = ""
        if parsed:
            if parsed.get("mode") == "share_group":
                gid = parsed.get("share_group_id", "")
            else:
                aid = parsed.get("agent_instance_id", "")
        else:
            aid = agent_instance_id
            gid = share_group_id
        return _apply(
            self.workspace, results,
            agent_instance_id=aid, share_group_id=gid,
        )

    def list_host_llm_agents(self) -> dict:
        """列出整理引擎：宿主 Skill（主路径）+ 本机 Agent CLI（可选加速）。

        多 Agent / 共享组 GUI 必须弹窗选择；Skill/MCP 调用默认走 host。
        """
        from .host_agent_backend import detect_available_agents
        agents = [{
            "agent": "host",
            "cli": "",
            "label": "当前宿主 Skill（需在对话中继续整理）",
        }]
        agents.extend(detect_available_agents())
        return {
            "agents": agents,
            "primary": "host",
            "hint": "host=对话 Skill 自动环；GUI 无法唤起聊天。多 Agent 弹窗必选。Cursor Agent CLI 可本机同步调用。",
        }

    def get_host_enrichment_guide(
        self,
        agent_instance_id: str = "",
        share_group_id: str = "",
        scope: dict | None = None,
    ) -> dict:
        """残留 pending 的 MCP 补做提示（主路径已并入构建）。"""
        status = self.get_enrichment_status(
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            scope=scope,
        )
        if status.get("error"):
            return status
        pending = status.get("pending", 0)
        return {
            "ok": True,
            "mode": "host_skill_primary",
            "pending_count": pending,
            "applied_count": status.get("applied", 0),
            "message": (
                f"Skill/MCP 主路径：宿主对话模型整理。另有 {pending} 条 pending。"
                if pending else "无残留待整理项；可直接构建/重建投影。"
            ),
            "steps": [
                "GUI 多 Agent：弹窗选「宿主 Skill」或本机 CLI",
                "Skill/MCP：memoryguard_build_and_enrich（默认 host）→ 你整理 → apply → 再 build",
                "不要要求用户另开 AI 整理按钮",
            ],
            "mcp_tools": [
                "memoryguard_build_and_enrich",
                "memoryguard_list_pending_enrichments",
                "memoryguard_apply_enrichments",
                "memoryguard_enrichment_status",
            ],
            "hint": "primary: host Skill auto loop; GUI multi-agent: LLM pick modal",
        }

    def get_enrichment_status(
        self,
        agent_instance_id: str = "",
        share_group_id: str = "",
        scope: dict | None = None,
    ) -> dict:
        """返回整理队列状态摘要。"""
        from .host_enrichment import get_status as _status
        parsed, err = self._parse_scope(
            scope, agent_instance_id=agent_instance_id, share_group_id=share_group_id,
        ) if (scope or agent_instance_id or share_group_id) else (None, "")
        if err:
            return {"error": err}
        aid = ""
        gid = ""
        if parsed:
            if parsed.get("mode") == "share_group":
                gid = parsed.get("share_group_id", "")
            else:
                aid = parsed.get("agent_instance_id", "")
        else:
            aid = agent_instance_id
            gid = share_group_id
        out = _status(self.workspace, agent_instance_id=aid, share_group_id=gid)
        out["mode"] = "build_integrated"
        return out


class SafeBridgeApi:
    """受限桥接 API：pywebview js_api 的安全代理。

    只暴露两个真实方法（pywebview 可枚举）：
    - call_readonly(method, args): 只读方法，直接转发
    - request_mutation(method, args): 变更方法，沙箱模式下走请求队列

    前端统一通过这两个方法调用，不再直接访问 method 属性。
    """

    def __init__(
        self,
        workspace: str,
        *,
        direct_mutations: bool = False,
        _trusted_access_context=None,
    ):
        self._inner = GovernanceApi(
            workspace,
            _trusted_access_context=_trusted_access_context,
        )
        self._workspace = workspace
        # 原生桌面窗口本身即执行端，变更应直接执行，不被 IDE 沙箱启发式推迟
        self._direct_mutations = bool(direct_mutations)

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

        沙箱模式下走请求队列；非沙箱 / 原生 GUI 直接执行模式下注入 confirmed=True 后执行。
        """
        from .security import is_mutation_method, detect_sandbox_mode

        if not is_mutation_method(method):
            return {"error": f"not a mutation method: {method}"}

        # 沙箱模式：走请求队列，返回 deferred 标记。原生桌面窗口同样遵守真实沙箱状态。
        if detect_sandbox_mode() and not self._direct_mutations:
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
            # Authorization is derived by target API from AccessContext/capability.
            result = fn(*call_args)
            return result if result is not None else {}
        except Exception as e:
            return {"error": str(e)}

    def get_api_method_registry(self) -> dict:
        """返回 API 方法注册表，供前端动态加载。"""
        return self._inner.get_api_method_registry()

    def get_sandbox_status(self) -> dict:
        """返回沙箱状态。原生 GUI 直接执行时对外报告非沙箱。"""
        from .security import detect_sandbox_mode

        return {
            "sandbox": detect_sandbox_mode(),
            "direct_mutations": self._direct_mutations,
        }

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
    rc, _ = open_localhost_window(
        workspace,
        auto_open=False,
        native_webview=True,
        native_title=title,
    )
    return rc
