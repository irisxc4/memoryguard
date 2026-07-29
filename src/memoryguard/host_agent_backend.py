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
from pathlib import Path
from typing import Any


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


def _find_codex_cli() -> str | None:
    """查找 Codex CLI 路径。"""
    # 1. PATH 中查找
    p = shutil.which("codex")
    if p:
        return p
    # 2. 常见安装位置
    candidates = [
        Path.home() / ".codex" / ".sandbox-bin" / "codex.exe",
        Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe",
    ]
    # 3. 从 config.toml 读取 CODEX_CLI_PATH
    try:
        config_toml = Path.home() / ".codex" / "config.toml"
        if config_toml.exists():
            for line in config_toml.read_text(encoding="utf-8").splitlines():
                if "CODEX_CLI_PATH" in line and "=" in line:
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    candidates.insert(0, Path(val))
    except Exception:
        pass
    for c in candidates:
        if c.exists():
            return str(c)
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


def _call_llm_json(agent: str, cli_path: str, system_prompt: str, user_prompt: str,
                   timeout: int = 60, expect_array: bool = False) -> dict | list | None:
    """调用 CLI 并解析 JSON 输出。

    P1-6: 增强数组解析 -- expect_array=True 时优先找 [ 开头的 JSON。
    """
    full_prompt = f"{system_prompt}\n\n{user_prompt}\n\n请只返回 JSON,不要其他内容。"
    output = _call_cli(agent, cli_path, full_prompt, timeout)
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
) -> list[dict]:
    """批量通过 CLI 处理 pending enrichment tasks。

    将多条 task 合并成一个 prompt,让 LLM 一次处理,减少 CLI 启动开销。
    返回 apply_results 所需的 results 列表。
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

    for start in range(0, len(tasks), batch_size):
        batch_tasks = tasks[start:start + batch_size]
        # P0-3: index 用批内局部下标,回写时直接用 batch_tasks[idx]
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
        # P1-6: expect_array=True
        data = _call_llm_json(agent, cli_path, system, batch_user, timeout=120, expect_array=True)
        if not data or not isinstance(data, list):
            import logging
            logging.getLogger("memoryguard").warning(
                "batch_enrich: batch %d-%d JSON parse failed", start, start + len(batch_tasks)
            )
            continue

        for item in data:
            idx = item.get("index", -1)
            # P0-3: idx 是批内局部下标,直接索引 batch_tasks
            if idx < 0 or idx >= len(batch_tasks):
                continue
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
