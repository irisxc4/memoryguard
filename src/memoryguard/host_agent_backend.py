"""宿主 Agent LLM 后端:通过 CLI subprocess 调用本机已安装的 Agent LLM。

不配 API key,不调外部 API,直接用本机 Agent CLI 完成分类和翻译。

支持的 Agent CLI:
- Codex: `codex exec --skip-git-repo-check -` (stdin 传 prompt)
- Claude Code: `claude --print "prompt"`
- Cursor Agent: `agent -p --force` / `cursor-agent -p --force`
- TRAE CLI: `trae chat --print`（若已安装）

说明：从 Cursor/IDE 打开 GUI ≠ 当前聊天模型可被 GUI 进程调用。
「安装接上」的 MCP 通道管记忆读写；同步整理走本机 CLI / Provider API。
无 CLI 时只能启发式，或对话里走 MCP list/apply 队列。

多 Agent 协同时:弹窗让用户选择用哪个 Agent 的 LLM。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rule_reconciliation import (
    ScopeBundle,
    build_bundles,
    validate_bundles,
)
from .runtime_v2.task_coordinator import TaskCancelled


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    """Windows 下隐藏控制台窗口，避免构建时弹出空白 cmd。"""
    if sys.platform != "win32":
        return {}
    kwargs: dict[str, Any] = {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    }
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    except Exception:
        pass
    return kwargs


# ---------------------------------------------------------------------------
# Agent CLI 检测
# ---------------------------------------------------------------------------


def _probe_cli_launch(cli_path: str, *args: str) -> bool:
    """Return whether a discovered CLI can actually be spawned by this process."""

    try:
        result = subprocess.run(
            [cli_path, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            **_hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _find_codex_cli() -> str | None:
    """查找当前进程真正可启动的 Codex CLI 路径。

    Windows Store / AppX 版 Codex 会把包内 ``WindowsApps`` 资源目录放进
    PATH。文件虽然存在，普通 Python ``CreateProcess`` 却可能得到 WinError 5。
    因此先尝试 Codex 自己暴露的用户级 launcher，再把 PATH 结果作为最后候选，
    并用 ``--version`` 做无网络、无副作用的启动能力探测。
    """
    candidates: list[Path] = []
    try:
        config_toml = Path.home() / ".codex" / "config.toml"
        if config_toml.exists():
            for line in config_toml.read_text(encoding="utf-8").splitlines():
                if "CODEX_CLI_PATH" in line and "=" in line:
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    candidates.append(Path(val))
    except Exception:
        pass

    candidates.extend([
        Path.home() / ".codex" / ".sandbox-bin" / "codex.exe",
        Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe",
    ])
    path_match = shutil.which("codex")
    if path_match:
        candidates.append(Path(path_match))

    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(os.fspath(candidate)))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file() and _probe_cli_launch(str(candidate), "--version"):
            return str(candidate)
    return None


def _find_claude_cli() -> str | None:
    """查找 Claude Code CLI 路径。"""
    p = shutil.which("claude")
    if p:
        return p
    # npm 全局安装
    npm_global = Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"
    if npm_global.exists():
        return str(npm_global)
    return None


def _find_trae_cli() -> str | None:
    """查找 TRAE CLI 路径。"""
    return shutil.which("trae")


def _find_cursor_agent_cli() -> str | None:
    """查找 Cursor Agent CLI（安装 Cursor 后常见为 agent / cursor-agent）。

    注意：不要把编辑器启动器 `cursor.cmd` 当成 LLM CLI。
    """
    for name in ("agent", "cursor-agent"):
        p = shutil.which(name)
        if p:
            return p
    local = Path(os.environ.get("LOCALAPPDATA", "") or "") / "cursor-agent"
    for name in ("agent.CMD", "agent.cmd", "agent.exe",
                 "cursor-agent.CMD", "cursor-agent.cmd", "cursor-agent.exe"):
        candidate = local / name
        if candidate.exists():
            return str(candidate)
    return None


def detect_available_agents() -> list[dict[str, str]]:
    """检测本机可用的 LLM Agent CLI。

    返回: [{"agent": "codex", "cli": "/path/to/codex", "label": "Codex"}, ...]
    """
    agents = []
    cursor = _find_cursor_agent_cli()
    if cursor:
        agents.append({"agent": "cursor", "cli": cursor, "label": "Cursor Agent"})
    codex = _find_codex_cli()
    if codex:
        agents.append({"agent": "codex", "cli": codex, "label": "Codex"})
    claude = _find_claude_cli()
    if claude:
        agents.append({"agent": "claude", "cli": claude, "label": "Claude Code"})
    trae = _find_trae_cli()
    if trae:
        agents.append({"agent": "trae", "cli": trae, "label": "TRAE CLI"})
    return agents


# ---------------------------------------------------------------------------
# 引擎选择持久化 (P1-5: 避免 HTTP 线程弹窗)
# ---------------------------------------------------------------------------

_ENGINE_PREF_FILE = ".memoryguard/enrichment_engine.json"


def _load_engine_pref(workspace: str | Path | None = None) -> dict[str, str] | None:
    """读取已保存的引擎选择。"""
    if workspace is None:
        return None
    pref_path = Path(workspace) / _ENGINE_PREF_FILE
    if not pref_path.exists():
        return None
    try:
        data = json.loads(pref_path.read_text(encoding="utf-8"))
        if data.get("agent") and data.get("cli"):
            return data
    except Exception:
        pass
    return None


def _save_engine_pref(workspace: str | Path, agent: str, cli: str) -> None:
    """持久化引擎选择。"""
    pref_path = Path(workspace) / _ENGINE_PREF_FILE
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    pref_path.write_text(json.dumps({"agent": agent, "cli": cli}, ensure_ascii=False), encoding="utf-8")


PRODUCT_TO_LLM: dict[str, str] = {
    "codex": "codex",
    "claude-code": "claude",
    "claude": "claude",
    "trae": "trae",
    "cursor": "cursor",
}


def resolve_llm_backend(
    workspace: str | Path | None,
    *,
    agent_instance_id: str = "",
    llm_agent: str = "",
    llm_cli: str = "",
    interactive_pick: bool = False,
) -> dict[str, Any]:
    """解析用于 AI 整理的 LLM 后端。

    - 单 Agent 治理：优先匹配该 Agent 产品对应的 CLI
    - 多 Agent / 无匹配 / interactive_pick：返回 need_pick 供前端弹窗
    """
    agents = detect_available_agents()
    if not agents:
        return {"error": "未检测到可用的 Agent CLI（Codex / Claude Code 等）"}
    if llm_agent and llm_cli:
        label = next((a.get("label") for a in agents if a["agent"] == llm_agent), llm_agent)
        return {"agent": llm_agent, "cli": llm_cli, "label": label}

    preferred_key = ""
    preferred_label = ""
    if agent_instance_id and workspace:
        try:
            from .agent_locator import AgentLocator
            locator = AgentLocator(workspace)
            instances, _ = locator.detect_instances()
            inst = next((i for i in instances if i.instance_id == agent_instance_id), None)
            if inst:
                preferred_label = inst.product or ""
                preferred_key = PRODUCT_TO_LLM.get(preferred_label.lower().replace("_", "-"), "")
        except Exception:
            pass

    if preferred_key and not interactive_pick:
        for a in agents:
            if a["agent"] == preferred_key:
                return {
                    "agent": a["agent"],
                    "cli": a["cli"],
                    "label": a.get("label") or preferred_label or a["agent"],
                }

    if len(agents) == 1 and not interactive_pick:
        a = agents[0]
        return {"agent": a["agent"], "cli": a["cli"], "label": a.get("label") or a["agent"]}

    if interactive_pick or len(agents) > 1 or (preferred_key and not any(a["agent"] == preferred_key for a in agents)):
        return {"need_pick": True, "agents": agents, "suggested": preferred_key or agents[0]["agent"]}

    a = agents[0]
    return {"agent": a["agent"], "cli": a["cli"], "label": a.get("label") or a["agent"]}


def select_agent_for_llm(
    agents: list[dict[str, str]] | None = None,
    workspace: str | Path | None = None,
) -> dict[str, str] | None:
    """选择用于 LLM 调用的 Agent。

    优先级:
    1. 已保存的引擎选择 (避免 HTTP 线程弹窗)
    2. 默认选第一个并落盘 (不弹窗,避免 HTTP 请求线程卡死)
    3. 弹窗选择仅留给显式 CLI/设置入口 (interactive=True 时才弹)
    """
    if agents is None:
        agents = detect_available_agents()
    if not agents:
        return None

    # 1. 已保存的选择
    saved = _load_engine_pref(workspace)
    if saved:
        for a in agents:
            if a["agent"] == saved["agent"] and a["cli"] == saved["cli"]:
                return a

    # 2. 默认选第一个并落盘 (不弹窗)
    first = agents[0]
    if workspace:
        _save_engine_pref(workspace, first["agent"], first["cli"])
    return first


def _show_agent_selection_window(agents: list[dict[str, str]]) -> dict[str, str] | None:
    """用 tkinter 弹出原生窗口让用户选择 Agent。"""
    try:
        import tkinter as tk
    except ImportError:
        # 无 tkinter,默认选第一个
        return agents[0]

    result: dict[str, str] | None = {"selected": None}
    root = tk.Tk()
    root.title("MemoryGuard - 选择 AI 引擎")
    root.geometry("480x320")
    root.attributes("-topmost", True)

    tk.Label(
        root,
        text="检测到多个 AI Agent，请选择用于记忆分类和翻译的引擎：",
        font=("Microsoft YaHei UI", 12),
        wraplength=440,
    ).pack(pady=20)

    selected_var = tk.StringVar(value=agents[0]["agent"])

    for agent in agents:
        tk.Radiobutton(
            root,
            text=f"{agent['label']}  ({agent['cli'][:50]}...)",
            value=agent["agent"],
            variable=selected_var,
            font=("Microsoft YaHei UI", 11),
        ).pack(anchor="w", padx=60, pady=5)

    def on_ok():
        for a in agents:
            if a["agent"] == selected_var.get():
                result["selected"] = a
                break
        root.destroy()

    btn_frame = tk.Frame(root)
    btn_frame.pack(fill="x", padx=20, pady=20)
    tk.Button(btn_frame, text="取消", width=10, command=root.destroy).pack(side="right", padx=(8, 0))
    tk.Button(btn_frame, text="确定", width=10, command=on_ok).pack(side="right")

    root.mainloop()
    return result["selected"]


# ---------------------------------------------------------------------------
# CLI 调用
# ---------------------------------------------------------------------------


def _call_cli(agent: str, cli_path: str, prompt: str, timeout: int = 60) -> str:
    """调用 Agent CLI,返回文本输出。

    agent: "codex" | "claude" | "trae" | "cursor"
    cli_path: CLI 可执行文件路径
    prompt: 输入提示词
    """
    hidden = _hidden_subprocess_kwargs()
    if agent == "codex":
        # codex exec --skip-git-repo-check -  (从 stdin 读 prompt)
        cmd = [cli_path, "exec", "--skip-git-repo-check", "-"]
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", **hidden,
        )
        # codex 输出有日志,JSON 在最后一行
        output = result.stdout.strip()
        return output

    elif agent == "claude":
        # claude --print "prompt"
        cmd = [cli_path, "--print", prompt]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", **hidden,
        )
        return result.stdout.strip()

    elif agent == "cursor":
        # Cursor Agent CLI: agent -p --force "prompt"
        cmd = [cli_path, "-p", "--force", "--output-format", "text", prompt]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=max(timeout, 120), encoding="utf-8", **hidden,
        )
        out = (result.stdout or "").strip()
        if not out and result.stderr:
            # 部分版本把结果打到 stderr；仍尽量返回
            err = result.stderr.strip()
            if err and not err.lower().startswith("error"):
                return err
        return out

    elif agent == "trae":
        # trae chat --print "prompt" (待验证)
        cmd = [cli_path, "chat", "--print", prompt]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", **hidden,
        )
        return result.stdout.strip()

    return ""


def _cli_command(agent: str, cli_path: str, prompt: str) -> tuple[list[str], str | None]:
    """Return ``(command_list, stdin_payload_or_None)`` for one agent CLI.

    ``codex`` reads the prompt from stdin; every other engine receives it as a
    positional argument.  Keeping this in one place lets both the blocking and
    cancellable callers share identical argv construction.
    """
    if agent == "codex":
        return [cli_path, "exec", "--skip-git-repo-check", "-"], prompt
    if agent == "claude":
        return [cli_path, "--print", prompt], None
    if agent == "cursor":
        return [cli_path, "-p", "--force", "--output-format", "text", prompt], None
    if agent == "trae":
        return [cli_path, "chat", "--print", prompt], None
    return [cli_path], None


def _terminate_proc(proc: subprocess.Popen) -> None:
    """Best-effort terminate, then bounded kill, of an owned subprocess."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=2.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_cli_cancellable(
    cmd: list[str],
    prompt: str | None,
    *,
    timeout: int = 60,
    execution: Any = None,
) -> str:
    """Run one CLI as an owned subprocess that observes TaskExecution cancellation.

    ``subprocess.communicate`` runs on a reader thread; the caller polls it and
    can terminate (then bounded-kill) the child the moment ``execution`` is
    cancelled.  The termination routine is also registered as owned cleanup so
    a cancellation observed elsewhere in the worker still releases the child.
    """
    hidden = _hidden_subprocess_kwargs()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        **hidden,
    )
    if execution is not None:
        execution.own_cleanup(lambda: _terminate_proc(proc))

    output: dict[str, str] = {}
    failure: dict[str, BaseException] = {}

    def _communicate() -> None:
        try:
            # One owner controls the deadline: the polling loop below.  Giving
            # communicate() its own equal timeout creates a race where its
            # TimeoutExpired can win and return while the child is still alive.
            out, err = proc.communicate(input=prompt or "")
            output["text"] = out or ""
            output["err"] = err or ""
        except BaseException as exc:  # noqa: BLE001 - reader must always finish
            failure["exc"] = exc

    reader = threading.Thread(target=_communicate, name="memoryguard-cli-reader", daemon=True)
    reader.start()
    deadline = time.monotonic() + max(float(timeout), 1.0)
    try:
        while reader.is_alive():
            if execution is not None and execution.cancelled:
                _terminate_proc(proc)
                reader.join(timeout=1.0)
                raise TaskCancelled("cli_subprocess_cancelled")
            if time.monotonic() > deadline:
                _terminate_proc(proc)
                reader.join(timeout=1.0)
                raise TimeoutError("cli_subprocess_timeout")
            time.sleep(0.03)
    except BaseException:
        _terminate_proc(proc)
        reader.join(timeout=1.0)
        raise
    if failure:
        raise failure["exc"]
    text = output.get("text", "")
    if not text.strip() and output.get("err"):
        # 部分版本把结果打到 stderr；仍尽量返回
        err = output["err"].strip()
        if err and not err.lower().startswith("error"):
            return err
    return text


def _call_cli_cancellable(
    agent: str,
    cli_path: str,
    prompt: str,
    timeout: int = 60,
    execution: Any = None,
) -> str:
    """Cancellable equivalent of :func:`_call_cli`."""
    cmd, stdin_payload = _cli_command(agent, cli_path, prompt)
    return _run_cli_cancellable(cmd, stdin_payload, timeout=timeout, execution=execution)


def _call_llm_json_cancellable(
    agent: str,
    cli_path: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 60,
    expect_array: bool = False,
    execution: Any = None,
) -> dict | list | None:
    """Cancellable JSON CLI call; same parsing contract as :func:`_call_llm_json`."""
    full_prompt = f"{system_prompt}\n\n{user_prompt}\n\n请只返回 JSON,不要其他内容。"
    output = _call_cli_cancellable(agent, cli_path, full_prompt, timeout, execution=execution)
    return _parse_llm_json(output or "", expect_array=expect_array)


def _parse_llm_json(output: str, expect_array: bool = False) -> dict | list | None:
    """Parse a raw CLI text output into JSON (array or object).

    P1-6: ``expect_array=True`` prefers a ``[``-led JSON array, scanning from
    the last line first so trailing logging does not defeat batch parsing.
    """
    if not output:
        return None

    # P1-6: 先找 JSON 块(数组或对象),从后往前扫
    lines = output.strip().splitlines()
    # 优先找数组(批量场景)
    if expect_array:
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("["):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    # 尝试找完整数组(可能跨行)
                    break
        # 尝试从输出中提取数组子串
        arr_start = output.find("[")
        arr_end = output.rfind("]")
        if arr_start >= 0 and arr_end > arr_start:
            try:
                return json.loads(output[arr_start:arr_end + 1])
            except json.JSONDecodeError:
                pass

    # 找对象(单条场景)
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    # 尝试整体解析
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _call_llm_json(agent: str, cli_path: str, system_prompt: str, user_prompt: str,
                   timeout: int = 60, expect_array: bool = False) -> dict | list | None:
    """调用 CLI 并解析 JSON 输出。

    P1-6: 增强数组解析 -- expect_array=True 时优先找 [ 开头的 JSON。
    """
    full_prompt = f"{system_prompt}\n\n{user_prompt}\n\n请只返回 JSON,不要其他内容。"
    output = _call_cli(agent, cli_path, full_prompt, timeout)
    return _parse_llm_json(output or "", expect_array=expect_array)


# ---------------------------------------------------------------------------
# HostAgentBackend (实现 ModelBackend Protocol)
# ---------------------------------------------------------------------------


class HostAgentBackend:
    """通过宿主 Agent CLI 实现 LLM 调用,满足 ModelBackend Protocol。

    不需要 API key,直接调用本机已安装的 Agent CLI。
    """

    provider_id = "host_agent"

    def __init__(self, agent: str = "", cli_path: str = ""):
        if not agent or not cli_path:
            selected = select_agent_for_llm()
            if selected is None:
                raise RuntimeError("没有可用的 Agent CLI")
            agent = selected["agent"]
            cli_path = selected["cli"]
        self.agent = agent
        self.cli_path = cli_path

    def classify(self, title: str, body: str, kind_hint: str = "") -> tuple[str, float]:
        """分类记忆,返回 (kind, confidence)。"""
        system = (
            "你是记忆分类器。根据记忆内容,返回记忆类型和置信度。\n"
            "类型只能是:preference|fact|project|procedure|episode|correction\n"
            '输出格式:严格 JSON {"kind":"...","confidence":0.0-1.0}'
        )
        user = f"title: {title}\nbody: {body}"
        if kind_hint:
            user += f"\nhint: {kind_hint}"
        data = _call_llm_json(self.agent, self.cli_path, system, user)
        if data and "kind" in data:
            kind = str(data["kind"])
            conf = float(data.get("confidence", 0.5))
            return (kind, conf)
        # 回退
        return ("fact", 0.5)

    def translate(self, text: str, target_lang: str = "zh") -> str:
        """翻译文本到目标语言。"""
        system = f"翻译到{target_lang}语言,只返回译文,不加解释。"
        output = _call_cli(self.agent, self.cli_path, f"{system}\n\n{text}")
        return output.strip() if output else text


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


def try_create_host_backend() -> HostAgentBackend | None:
    """尝试创建宿主 Agent 后端,失败返回 None。"""
    try:
        return HostAgentBackend()
    except RuntimeError:
        return None
    except Exception:
        return None


def batch_enrich_via_cli(
    tasks: list[dict],
    agent: str = "",
    cli_path: str = "",
    workspace: str | Path | None = None,
    execution: Any = None,
) -> list[dict]:
    """批量通过 CLI 处理 pending enrichment tasks。

    将多条 task 合并成一个 prompt,让 LLM 一次处理,减少 CLI 启动开销。
    返回 apply_results 所需的 results 列表。

    ``execution`` (TaskExecution) 传入时，CLI 子进程变为可取消的 owned 子进程：
    取消会终止/杀灭该进程，绝不留下孤儿命令行窗口或 CLI 进程。
    """
    if not tasks:
        return []

    if not agent or not cli_path:
        selected = select_agent_for_llm(workspace=workspace)
        if selected is None:
            return []
        agent = selected["agent"]
        cli_path = selected["cli"]

    # P0-3: 构造批量 prompt,index 用每批内局部下标
    batch_size = 20
    all_results = []

    system = (
        "你是记忆整理助手。对以下每条记忆:\n"
        "1. 分类:preference|fact|project|procedure|episode|correction\n"
        "2. 翻译标题和正文到中文(如果已是中文则保持)\n"
        "3. 给出置信度 0.0-1.0\n\n"
        "输出格式:JSON 数组,每个元素:\n"
        '{"index":0,"kind":"...","title":"中文标题","body":"中文正文","confidence":0.9}\n'
        "只返回 JSON 数组,不要其他内容。"
    )

    def invoke_batch(batch_tasks: list[dict]) -> dict[int, dict]:
        """Invoke one LLM batch and return validated results keyed by local index."""
        items = []
        for i, task in enumerate(batch_tasks):
            inp = task.get("input", {})
            items.append({
                "index": i,
                "task_id": task["task_id"],
                "title": inp.get("title", "")[:200],
                "body": inp.get("body", "")[:800],
                "kind_hint": inp.get("kind_hint", ""),
            })
        batch_user = json.dumps(items, ensure_ascii=False, indent=2)
        if execution is not None:
            data = _call_llm_json_cancellable(
                agent, cli_path, system, batch_user, timeout=120,
                expect_array=True, execution=execution,
            )
        else:
            data = _call_llm_json(
                agent, cli_path, system, batch_user, timeout=120, expect_array=True,
            )
        if not data or not isinstance(data, list):
            return {}
        normalized: dict[int, dict] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(batch_tasks) and idx not in normalized:
                normalized[idx] = item
        return normalized

    for start in range(0, len(tasks), batch_size):
        batch_tasks = tasks[start:start + batch_size]
        resolved = invoke_batch(batch_tasks)
        missing = [idx for idx in range(len(batch_tasks)) if idx not in resolved]
        if missing:
            # Model CLIs occasionally omit one element from an otherwise valid
            # JSON array.  Retry only the missing subset once.  We still return
            # partial results if the retry also omits an item; the native build
            # layer keeps the strict all-task completeness gate and will fail
            # closed instead of applying a partial enrichment batch.
            retry_tasks = [batch_tasks[idx] for idx in missing]
            retried = invoke_batch(retry_tasks)
            for retry_idx, item in retried.items():
                if 0 <= retry_idx < len(missing):
                    resolved[missing[retry_idx]] = item

        if len(resolved) != len(batch_tasks):
            import logging
            logging.getLogger("memoryguard").warning(
                "batch_enrich: batch %d-%d incomplete after retry (%d/%d)",
                start,
                start + len(batch_tasks),
                len(resolved),
                len(batch_tasks),
            )

        for idx in sorted(resolved):
            item = resolved[idx]
            task = batch_tasks[idx]
            all_results.append({
                "task_id": task["task_id"],
                "kind": item.get("kind", "fact"),
                "title": item.get("title", ""),
                "body": item.get("body", ""),
                "confidence": item.get("confidence", 0.7),
                "rationale": "host agent CLI batch enrich",
                "source": "host_cli",
            })

    return all_results


# ---------------------------------------------------------------------------
# Req3: scope-bundle 分桶计划 (batch_bundle_via_cli)
# ---------------------------------------------------------------------------


@dataclass
class _BindingInfo:
    """Normalized view of one full binding.

    ``rule_reconciliation`` helpers read bindings through ``getattr``; this
    dataclass is the single adapter for dict / dataclass / RuleBinding inputs
    so both the model prompt and the offline heuristic share one shape.
    """

    target_type: str = "agent"
    target_id: str = ""
    project_ref: str = ""
    provider: str = ""
    runtime_role: str = ""
    effect: str = "include"
    priority_override: int | None = None

    @classmethod
    def from_any(cls, raw: Any) -> "_BindingInfo":
        if isinstance(raw, dict):
            get = lambda key, default: raw.get(key, default)  # noqa: E731
        else:
            get = lambda key, default: getattr(raw, key, default)  # noqa: E731
        return cls(
            target_type=str(get("target_type", "agent") or "agent"),
            target_id=str(get("target_id", "") or ""),
            project_ref=str(get("project_ref", "") or ""),
            provider=str(get("provider", "") or ""),
            runtime_role=str(get("runtime_role", "") or ""),
            effect=str(get("effect", "include") or "include"),
            priority_override=get("priority_override", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
            "effect": self.effect,
            "priority_override": self.priority_override,
        }


class _LegacyFacade:
    """Minimal in-memory reader that satisfies ``rule_reconciliation``'s legacy
    interface (``list_records`` / ``list_rule_assignments``) so the offline
    heuristic fallback can run without a persisted SharedMemoryStore."""

    def __init__(
        self,
        records: list[Any],
        assignments_by_memory_id: dict[str, list[Any]] | None,
    ):
        self._records = list(records)
        self._assignments = {
            str(k): [_BindingInfo.from_any(b) for b in (v or [])]
            for k, v in (assignments_by_memory_id or {}).items()
        }

    def list_records(self) -> list[Any]:
        return list(self._records)

    def list_rule_assignments(self, memory_id: str) -> list[Any]:
        return list(self._assignments.get(str(memory_id), []))


def _extract_bundle_plan(data: Any) -> dict[str, Any] | None:
    """Normalize raw LLM output into the ``{"bundles", "kept_separate"}`` plan
    shape.  Returns None when the output cannot be read as a plan (fallback)."""
    if isinstance(data, dict):
        if "bundles" not in data:
            return None
        return {
            "bundles": data.get("bundles", []),
            "kept_separate": [str(x) for x in data.get("kept_separate", [])],
        }
    if isinstance(data, list):
        return {"bundles": data, "kept_separate": []}
    return None


def _to_bundle_dict(bundle: Any) -> dict[str, Any]:
    if isinstance(bundle, ScopeBundle):
        return bundle.to_dict()
    return ScopeBundle.from_dict(bundle).to_dict()


def _validate_model_scope_bundle(
    plan: dict[str, Any],
    source_index: dict[str, dict[str, Any]],
) -> None:
    """Strict model-plan checks beyond ``validate_bundles`` (Req3): no cross
    project_ref merge, no cross effect merge, no audience widening, and
    bundle priority == max of its sources.  Violations raise ValueError."""
    bundles = [
        bundle if isinstance(bundle, ScopeBundle)
        else ScopeBundle.from_dict(bundle)
        for bundle in plan.get("bundles", [])
    ]
    for bundle in bundles:
        source_ids = [str(x) for x in bundle.source_memory_ids]
        present = [sid for sid in source_ids if sid in source_index]
        project_refs: set[str] = set()
        providers: set[str] = set()
        effects: set[str] = set()
        agent_scoped = True
        for sid in present:
            src = source_index[sid]
            src_bindings = src.get("bindings") or []
            for binding in src_bindings:
                if binding.project_ref:
                    project_refs.add(binding.project_ref)
                if binding.provider:
                    providers.add(binding.provider)
                effects.add(binding.effect or "include")
            if not any(
                str(b.target_type) == "agent" and str(b.target_id or "")
                for b in src_bindings
            ):
                agent_scoped = False

        if len(project_refs) > 1:
            raise ValueError(
                f"cross_project_ref_merge: {sorted(project_refs)}"
            )
        if len(effects) > 1:
            raise ValueError(
                f"cross_effect_merge: {sorted(effects)}"
            )
        single_effect = next(iter(effects)) if effects else "include"
        if str(bundle.effect or "include") != single_effect:
            raise ValueError(f"bundle_effect_mismatch: {source_ids}")

        if bundle.bundle_kind == "project_overlay":
            if len(project_refs) != 1 or bundle.project_ref not in project_refs:
                raise ValueError(
                    f"project_overlay_scope_mismatch: {source_ids}"
                )
        elif bundle.bundle_kind == "agent_overlay":
            if project_refs:
                raise ValueError(
                    f"agent_overlay_widens_to_project: {source_ids}"
                )
            if providers:
                if len(providers) != 1 or bundle.provider not in providers:
                    raise ValueError(
                        f"agent_overlay_provider_mismatch: {source_ids}"
                    )
            elif not agent_scoped:
                raise ValueError(
                    f"agent_overlay_widens_audience: {source_ids}"
                )
        else:  # shared_baseline
            if project_refs or providers:
                raise ValueError(
                    f"shared_baseline_widens_audience: {source_ids}"
                )

        max_priority = max(
            (int(source_index[sid].get("priority") or 0) for sid in present),
            default=0,
        )
        if int(bundle.priority or 0) != max_priority:
            raise ValueError(
                f"bundle_priority_not_source_max: {source_ids}"
            )


def batch_bundle_via_cli(
    records: list[Any],
    assignments_by_memory_id: dict[str, list[Any]] | None = None,
    agent: str = "",
    cli_path: str = "",
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """为整组共享组生成 scope-bundle 分桶计划（Req3）。

    输入:
      records                -- 每个 active mandatory 的记录
                                (memory_id / body / priority / owner_agent_id)
      assignments_by_memory_id -- memory_id -> 该记录的完整 bindings；每条 binding
                                带 target_type/target_id/project_ref/provider/
                                runtime_role/effect（dict 或对象均可）。

    输出:
      {"bundles": [{"bundle_kind","source_memory_ids","priority","body",
                    "project_ref","provider","effect"}],
       "kept_separate": [...],
       "model_mode": "scope_bundle"}

    校验（启发式结果永远不被当作 scope_bundle 计划接受）:
      - 每个 source_id 恰好出现在一个 bundle 或 kept_separate
      - 不得跨 project_ref / effect 合并
      - 不得扩大受众（shared_baseline 无 project/provider；project_overlay 只能含
        同一 project_ref；agent_overlay 只能含同一 provider 或同一 agent 受众）
      - bundle priority == 各来源 priority 的最大值
      任一违反 -> ValueError("invalid_scope_bundle: ...")。

    LLM 返回为空 / 超时 / 不可解析时回退 :func:`build_bundles` 启发式计划，
    并把 model_mode 标为 "heuristic"。
    """
    records = list(records or [])
    assignments_by_memory_id = assignments_by_memory_id or {}
    source_ids = [str(record.memory_id) for record in records]
    if not source_ids:
        return {"bundles": [], "kept_separate": [], "model_mode": "heuristic"}

    sources: list[dict[str, Any]] = []
    for record in records:
        bindings = [
            _BindingInfo.from_any(b)
            for b in assignments_by_memory_id.get(record.memory_id, [])
        ]
        sources.append({
            "memory_id": str(record.memory_id),
            "body": str(getattr(record, "body", "") or "")[:2000],
            "priority": int(getattr(record, "priority", 0) or 0),
            "owner_agent_id": str(getattr(record, "agent_instance_id", "") or ""),
            "bindings": bindings,
        })
    source_index = {s["memory_id"]: s for s in sources}

    def fallback_heuristic() -> dict[str, Any]:
        facade = _LegacyFacade(records, assignments_by_memory_id)
        plan = build_bundles(
            None, facade, "", records, workspace=workspace or "",
        )
        validate_bundles(plan, source_ids)
        return {
            "bundles": [_to_bundle_dict(b) for b in plan.get("bundles", [])],
            "kept_separate": [
                str(x) for x in plan.get("kept_separate", [])
            ],
            "model_mode": "heuristic",
        }

    if not agent or not cli_path:
        selected = select_agent_for_llm(workspace=workspace)
        if selected is None:
            return fallback_heuristic()
        agent = selected["agent"]
        cli_path = selected["cli"]

    system = (
        "你是共享记忆规则分桶助手。给定一组 mandatory 规则的来源（source）及其"
        "绑定（bindings），把它们折叠为 scope-bundle 分桶计划。\n\n"
        "分桶语义：\n"
        "- shared_baseline：整组共享的基线规则，受众覆盖整个共享组；\n"
        "- agent_overlay：按 provider/agent 的覆盖规则，只属于特定 provider 或"
        "特定 agent；\n"
        "- project_overlay：按项目的覆盖规则，绑定一个 project_ref；\n"
        "- kept_separate：无法安全折叠、需单独保留 active 定义的来源。\n\n"
        "硬性约束：\n"
        "1. 每个 source_id 必须恰好出现在一个 bundle 的 source_memory_ids 或"
        "kept_separate 中；\n"
        "2. 不得把 project_ref 不同的来源合并进同一个 bundle；\n"
        "3. 不得把 effect 不同的来源合并进同一个 bundle；\n"
        "4. 不得扩大受众：shared_baseline 只能含无 project_ref、无 provider 的来源；"
        "agent_overlay 只能含同一 provider（或无 provider 但受众为特定 agent）且"
        "无 project_ref 的来源；project_overlay 只能含同一 project_ref 的来源；\n"
        "5. bundle 的 priority 必须等于其来源 priority 的最大值；\n"
        "6. bundle 的 body 由来源正文拼接（多条时用 [n] 前缀分行）。\n\n"
        '输出格式：严格 JSON 对象 {"bundles": [{"bundle_kind":"...",'
        '"source_memory_ids":[...],"priority":0,"body":"...","project_ref":"",'
        '"provider":"","effect":"include"}], "kept_separate":[...]}。'
        "只返回 JSON，不要其他内容。"
    )
    user = json.dumps(
        {
            "sources": [
                {
                    "memory_id": s["memory_id"],
                    "body": s["body"],
                    "priority": s["priority"],
                    "owner_agent_id": s["owner_agent_id"],
                    "bindings": [b.to_dict() for b in s["bindings"]],
                }
                for s in sources
            ]
        },
        ensure_ascii=False,
        indent=2,
    )

    data = _call_llm_json(agent, cli_path, system, user, timeout=120,
                          expect_array=True)
    if not data:
        return fallback_heuristic()
    plan = _extract_bundle_plan(data)
    if plan is None:
        return fallback_heuristic()

    # 无论 LLM 返回什么，返回前都强制校验；失败抛 invalid_scope_bundle。
    try:
        validate_bundles(plan, source_ids)
        _validate_model_scope_bundle(plan, source_index)
    except ValueError as exc:
        raise ValueError(f"invalid_scope_bundle: {exc}") from exc

    return {
        "bundles": [_to_bundle_dict(b) for b in plan.get("bundles", [])],
        "kept_separate": [
            str(x) for x in plan.get("kept_separate", [])
        ],
        "model_mode": "scope_bundle",
    }
