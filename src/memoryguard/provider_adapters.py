"""Provider adapters: 把宿主 Agent 的原生记忆机制重定向到 MemoryGuard MCP。

每个 adapter 做三件事：
1. 写入宿主特定的指令文件，明确告诉 Agent 调用 memoryguard_memory_write MCP 工具记录记忆，
   不要用原生记忆机制（编辑 CLAUDE.md/AGENTS.md/.cursorrules 等）。
2. 生成/更新宿主的 MCP 配置，让它能启动 MemoryGuard MCP 服务器。
3. 明确请求全局接管时，通过 HostHookManager 安装该宿主已验证的用户级 Hook。

设计：
- install() 幂等：重复调用不产生重复配置（指令用标记段落替换，MCP 配置用 JSON key 覆盖）
- uninstall() 干净移除 install() 写入的内容（只动 memoryguard 段落/key，不碰用户其他内容）
- status() 区分 configured 与 runtime_verified；写入配置不等于宿主已经连接
- Hook 配置由 host_hooks 深模块统一拥有；Provider adapter 不复制宿主 Hook 细节
- 纯标准库，不引入新依赖
- 有 workspace 时优先项目级配置，避免不同项目的 Agent 身份互相覆盖
- 无 workspace 时才使用 Path.home() / 环境变量指向的用户级配置
- MCP 启动命令用 python（要求 memoryguard 已 pip install），跨机器可移植
"""
from __future__ import annotations

import json
import os
import tempfile
from . import toml_compat as tomllib
from pathlib import Path
from typing import Any

from .runtime_v2.public_safety import v2_upgrade_payload


# ===========================================================================
# 常量
# ===========================================================================

MCP_SERVER_NAME = "memoryguard"
MCP_MODULE = "memoryguard.mcp_server"


def _binding_plane_for_workspace(workspace: str | Path) -> str:
    """Require the V2 system control plane for production provider setup."""
    from .system.manifest import ManifestManager

    try:
        current = ManifestManager(Path(workspace)).current()
        state = current.get("state", current.get("status", "")) if isinstance(current, dict) else current.state
    except Exception as exc:
        raise ValueError("v2_manifest_state_unavailable") from exc
    marker = str(getattr(state, "value", state) or "").strip().upper()
    if marker in {"V2_READY", "V2_ACTIVE"}:
        return "v2"
    if marker in {"V1_ACTIVE", "V2_BUILDING"}:
        raise ValueError(v2_upgrade_payload(marker, surface="Provider")["code"])
    raise ValueError("v2_manifest_state_unavailable")


def _require_provider_state(workspace: str | Path, *, mutation: bool) -> None:
    """Apply the provider operation's V2 read/write gate."""
    plane = _binding_plane_for_workspace(workspace)
    if not mutation:
        return
    from .system.manifest import ManifestManager

    try:
        current = ManifestManager(Path(workspace)).current()
        raw_state = current.get("state", current.get("status", "")) if isinstance(current, dict) else current.state
        marker = str(getattr(raw_state, "value", raw_state) or "").strip().upper()
    except Exception as exc:
        # Test/control-plane shims may provide the already-validated V2 plane
        # without a persisted manifest.  The normal helper above still fails
        # closed for real unknown/unreadable manifests.
        if plane == "v2":
            return
        raise ValueError("v2_manifest_state_unavailable") from exc
    if marker == "V2_READY":
        raise ValueError("v2_not_active")
    if marker == "V2_ACTIVE":
        return
    if marker == "V1_ACTIVE" and plane == "v2":
        return
    raise ValueError("v2_manifest_state_unavailable")

# 指令文件中使用的标记（HTML 注释，Markdown/MDC 都安全）
_BEGIN_MARKER = "<!-- BEGIN memoryguard:provider-redirect -->"
_END_MARKER = "<!-- END memoryguard:provider-redirect -->"

# Codex TOML 配置使用的标记（# 注释）
_TOML_BEGIN = "# BEGIN memoryguard:provider-redirect"
_TOML_END = "# END memoryguard:provider-redirect"


# 共用指令正文：告诉 Agent 所有记忆操作走 MCP 工具
def _instruction_body(share_group_id: str = "default") -> str:
    gid = share_group_id or "default"
    return f"""\
## MemoryGuard 记忆重定向（正式接管）

长期记忆的**唯一真相源**是 MemoryGuard MCP 共享库（share_group_id=`{gid}`）。
不要使用各 Agent 原生记忆文件或 GUI 内置记忆；所有读写必须走 MCP。
宿主支持时，用户级 MemoryGuard Hook 会自动注入限额上下文、拦截可见的原生
记忆写入，并在明确的长期候选遗漏时续跑一次；不得绕过或删除该 Hook。

### 记录记忆
调用 `memoryguard_memory_write` 工具：
- `body`（必填）：记忆内容
- `kind`（可选）：preference|fact|project|procedure|episode|correction，留空则自动分类
- `injection_policy`（可选）：默认 `relevant`。仅当用户明确要求“规则/必须/默认长期遵循/强制”时写 `always`；普通事实、偏好和 procedure 仍写 `relevant`，不得把所有 procedure 自动设为强制。
- `priority`（可选）：`always` 规则的有界排序整数，默认 0。
- 不要传 `agent_instance_id` 或覆盖 `share_group_id`；MCP 连接已可信绑定到当前 Agent 和共享组

### 搜索 / 读取
- `memoryguard_context_bootstrap`：新任务开始时一次性加载有预算的长期记忆上下文
- `memoryguard_memory_search`：按 query / kind / status 搜索
- `memoryguard_memory_read`：按 memory_id 读取单条
- `memoryguard_memory_status`：查看共享组状态

### 选择性回忆与对话注入
- 每个新任务优先调用一次 `memoryguard_context_bootstrap`，传入当前 `task`；同一任务不得重复调用
- Claude/Codex Hook 已提供本轮 bootstrap 上下文时不要重复调用；Cursor 以 Hook 的首次工具门控为准
- bootstrap 只补充长期记忆/长期规则；宿主当前对话上下文保持原样，不替换、不重复注入
- bootstrap 先注入独立预算的强制规则包，再召回相关记忆；强制包敏感或超限会失败封闭，停止继续执行。
- 仅在 bootstrap 后仍需精确治理查询时调用 `memoryguard_memory_search`
- 历史对话文件只是可选来源，必须先萃取为长期记忆；禁止把历史对话全文注入当前任务
- 需要精确原文时再用 `memoryguard_memory_read` 读取命中的单条记录

### 更新 / 删除
- `memoryguard_memory_update`：更新 body / kind / status，也可在 `injection_policy` 与 `priority` 间切换策略
- `memoryguard_memory_delete`：软删除
- GUI 可将强制规则改回按需，或删除/恢复；不要绕过治理路径。

### 规则
- 不要为了"记住"而编辑 CLAUDE.md / AGENTS.md / .cursorrules 等指令文件
- 不要把记忆写入 ~/.codex/memories、~/.claude/projects/*/memory 等本地文件
- 所有记忆操作都走 MCP，由 MemoryGuard 面板治理（去重、冲突、隔离、版本、supersede）
"""


_INSTRUCTION_BODY = _instruction_body()


# ===========================================================================
# 工具函数
# ===========================================================================


def _mcp_command() -> list[str]:
    """返回启动 MemoryGuard MCP 服务器的 command + args。

    用 "python"（要求 memoryguard 已 pip install），跨机器可移植，
    避免 sys.executable 把机器特定路径写入 .mcp.json。
    """
    return ["python", "-m", MCP_MODULE]


def _mcp_server_config(
    agent_instance_id: str = "",
    memoryguard_workspace: str | Path = "",
    provider: str = "",
    control_scope: str = "project",
) -> dict[str, Any]:
    """返回 MemoryGuard MCP server 的配置片段（JSON 格式，Claude/Cursor 通用）。"""
    cmd = _mcp_command()
    config: dict[str, Any] = {
        "command": cmd[0],
        "args": cmd[1:],
    }
    env: dict[str, str] = {}
    if agent_instance_id:
        env["MEMORYGUARD_AGENT_ID"] = agent_instance_id
    if provider:
        env["MEMORYGUARD_PROVIDER"] = provider
    env["MEMORYGUARD_CONTROL_SCOPE"] = (
        "global" if str(control_scope).strip().lower() == "global" else "project"
    )
    if memoryguard_workspace:
        env["MEMORYGUARD_WORKSPACE"] = str(
            Path(memoryguard_workspace).expanduser().resolve()
        )
    if env:
        config["env"] = env
    return config


def _mcp_toml_section(
    agent_instance_id: str = "",
    memoryguard_workspace: str | Path = "",
    provider: str = "codex",
    control_scope: str = "project",
) -> str:
    """返回 MemoryGuard MCP server 的 TOML 配置段落（Codex 用）。

    用 json.dumps 产出合法的 TOML basic string（自动转义反斜杠）。
    """
    cmd = _mcp_command()
    command_str = json.dumps(cmd[0])  # TOML basic string 与 JSON string 语法兼容
    args_items = ", ".join(json.dumps(a) for a in cmd[1:])
    lines = [
        f"[mcp_servers.{MCP_SERVER_NAME}]\n"
        f"command = {command_str}\n"
        f"args = [{args_items}]"
    ]
    env_items: list[str] = []
    if agent_instance_id:
        env_items.append(
            f"MEMORYGUARD_AGENT_ID = {json.dumps(agent_instance_id)}"
        )
    if provider:
        env_items.append(
            f"MEMORYGUARD_PROVIDER = {json.dumps(provider)}"
        )
    env_items.append(
        "MEMORYGUARD_CONTROL_SCOPE = "
        + json.dumps(
            "global" if str(control_scope).strip().lower() == "global" else "project"
        )
    )
    if memoryguard_workspace:
        resolved = str(Path(memoryguard_workspace).expanduser().resolve())
        env_items.append(
            f"MEMORYGUARD_WORKSPACE = {json.dumps(resolved)}"
        )
    if env_items:
        lines.append(f"\nenv = {{ {', '.join(env_items)} }}")
    return "".join(lines)


def _replace_section(text: str, begin_marker: str, end_marker: str,
                     section_content: str) -> str:
    """幂等替换标记之间的内容。标记不存在则追加新段落。"""
    begin_idx = text.find(begin_marker)
    end_idx = text.find(end_marker)
    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        before = text[:begin_idx]
        after = text[end_idx + len(end_marker):]
        before = before.rstrip("\n")
        after = after.lstrip("\n")
        parts = []
        if before:
            parts.append(before + "\n\n")
        parts.append(f"{begin_marker}\n{section_content}\n{end_marker}")
        if after:
            parts.append("\n" + after)
        return "".join(parts).rstrip("\n") + "\n"
    # 追加新段落
    stripped = text.rstrip()
    if stripped:
        return f"{stripped}\n\n{begin_marker}\n{section_content}\n{end_marker}\n"
    return f"{begin_marker}\n{section_content}\n{end_marker}\n"


def _remove_unmanaged_toml_table(text: str, table_name: str) -> str:
    """移除标记段外的旧 TOML table，保留其他 table 与独立注释。

    0.3.0 之前的 Codex 配置直接写 ``[mcp_servers.memoryguard]``，没有
    MemoryGuard 标记。升级时必须先迁移旧 table，否则追加新版标记段会形成
    重复 table，导致整个 Codex config.toml 无法解析。
    """
    target_header = f"[{table_name}]"
    result: list[str] = []
    managed = False
    removing = False

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == _TOML_BEGIN:
            managed = True

        header = stripped.split("#", 1)[0].strip()
        is_table_header = (
            header.startswith("[")
            and header.endswith("]")
            and len(header) >= 3
        )
        if is_table_header:
            removing = False
            if header == target_header and not managed:
                removing = True
                continue

        if removing:
            # 独立注释可能描述后续 table，保留；旧 table 的键值全部移除。
            if not stripped or stripped.startswith("#"):
                result.append(line)
            continue

        result.append(line)
        if stripped == _TOML_END:
            managed = False

    return "".join(result)


def _reconcile_memoryguard_toml_tables(text: str) -> str:
    """Remove every safely-identifiable legacy/owned MemoryGuard table.

    TOML parsers reject duplicate table declarations before normal upsert can
    run.  Only exact `mcp_servers.memoryguard` sections that identify this
    module (or live in our marked block) are removed; an unknown same-named
    section fails closed instead of silently deleting user configuration.
    """
    target = f"[mcp_servers.{MCP_SERVER_NAME}]"
    # Markers are exclusively ours.  Strip every orphan/duplicate marker first
    # and append one canonical block later via _replace_section; this prevents
    # BEGIN/END accumulation when a previously interrupted install left only
    # one side behind.
    lines = [
        line for line in text.splitlines(keepends=True)
        if line.strip() not in {_TOML_BEGIN, _TOML_END}
    ]
    result: list[str] = []
    index = 0
    while index < len(lines):
        header = lines[index].strip().split("#", 1)[0].strip()
        if header != target:
            result.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines):
            candidate = lines[end].strip().split("#", 1)[0].strip()
            if candidate.startswith("[") and candidate.endswith("]"):
                break
            end += 1
        section = lines[index:end]
        section_text = "".join(section)
        if MCP_MODULE not in section_text:
            raise ValueError(
                "cannot safely reconcile duplicate "
                "[mcp_servers.memoryguard]: section is not MemoryGuard-owned"
            )
        # Keep comments/blank lines: they can belong to the next user table;
        # remove only the owned table header and its TOML assignments.
        for line in section:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                result.append(line)
        index = end
    return "".join(result)


def _remove_section(text: str, begin_marker: str, end_marker: str) -> str:
    """移除标记之间的内容，返回剩余文本。"""
    begin_idx = text.find(begin_marker)
    end_idx = text.find(end_marker)
    if begin_idx == -1 or end_idx == -1 or end_idx <= begin_idx:
        return text
    before = text[:begin_idx]
    after = text[end_idx + len(end_marker):]
    result = before + after
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    if not result.strip():
        return ""
    return result.rstrip("\n") + "\n"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_text_for_update(path: Path) -> str:
    """更新配置时严格读取；已有文件损坏不能被当成空文件覆盖。"""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read existing config as UTF-8: {path}: {exc}") from exc


def _load_json_for_update(path: Path) -> dict[str, Any]:
    text = _read_text_for_update(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON config: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid JSON config root (expected object): {path}")
    return data


def _validate_toml(text: str, path: Path) -> None:
    if not text.strip():
        return
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML config: {path}: {exc}") from exc


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace，避免中途写坏配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.memoryguard-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _apply_file_transaction(updates: list[tuple[Path, str | None]]) -> None:
    """原子应用一组文本更新；任一步失败则恢复所有原文件。"""
    merged: dict[Path, str | None] = {}
    for path, content in updates:
        merged[path] = content
    ordered = list(merged.items())
    snapshots = {
        path: path.read_bytes() if path.exists() else None
        for path, _ in ordered
    }
    try:
        for path, content in ordered:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, content.encode("utf-8"))
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, _ in reversed(ordered):
            original = snapshots[path]
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write_bytes(path, original)
            except Exception as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                f"provider config update failed: {exc}; rollback failed: "
                + "; ".join(rollback_errors)
            ) from exc
        raise


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _set_mcp_server(data: dict[str, Any], server_name: str, config: dict[str, Any]) -> None:
    data.setdefault("mcpServers", {})[server_name] = config


def _remove_mcp_server(data: dict[str, Any], server_name: str) -> bool:
    servers = data.get("mcpServers", {})
    if server_name in servers:
        del servers[server_name]
        if not servers:
            del data["mcpServers"]
        return True
    return False


def _has_mcp_server(data: dict[str, Any], server_name: str) -> bool:
    return server_name in data.get("mcpServers", {})


def _other_adapter_uses_project_agents_md(
    workspace: Path, *, excluding: str,
) -> bool:
    """AGENTS.md 是 Codex/TRAE 共用面；卸载单方时不得破坏另一方。"""
    if excluding != "codex":
        codex_text = _read_text(workspace / ".codex" / "config.toml")
        if (
            f"[mcp_servers.{MCP_SERVER_NAME}]" in codex_text
            or _TOML_BEGIN in codex_text
        ):
            return True
    if excluding != "trae":
        trae_data = _load_json(workspace / ".trae" / "mcp.json")
        if _has_mcp_server(trae_data, MCP_SERVER_NAME):
            return True
    return False


# ===========================================================================
# 基类
# ===========================================================================


class ProviderAdapter:
    """Provider adapter 基类契约。

    子类实现 install / uninstall / status，把宿主原生记忆重定向到 MemoryGuard MCP。
    遵循现有 adapters.py 的契约风格（raise NotImplementedError，不用 ABC）。
    """

    provider_name: str = "base"

    def _select_install_workspace(
        self, workspace: str | Path = "", *, global_scope: bool = False,
    ) -> str:
        """Select the control plane before validating bindings or writing config.

        Global host integrations must always bind to the stable per-user
        MemoryGuard data home.  A source checkout/project path is valid only
        for an explicit project-scoped integration.  This prevents an upgrade
        or repair launched from a repository checkout from pinning a global MCP
        back to that checkout again.
        """
        self._superseded_project_workspace: Path | None = None
        if global_scope:
            from .data_home import resolve_data_home

            data_home = resolve_data_home()
            if workspace:
                requested = Path(workspace).expanduser().resolve()
                if requested != data_home:
                    self._superseded_project_workspace = requested
            self.workspace = data_home
            self._has_workspace = False
            control_workspace = getattr(self, "_superseded_project_workspace", None) or self.workspace
            _require_provider_state(control_workspace, mutation=True)
            return "global"
        if workspace:
            self.workspace = Path(workspace).expanduser().resolve()
            self._has_workspace = True
        control_workspace = getattr(self, "_superseded_project_workspace", None) or self.workspace
        _require_provider_state(control_workspace, mutation=True)
        return "project"

    def _cleanup_superseded_project_override(self) -> list[str]:
        """Remove only MemoryGuard-owned project config after global takeover."""
        project = getattr(self, "_superseded_project_workspace", None)
        if not isinstance(project, Path):
            return []
        try:
            project_adapter = type(self)(project)
            project_adapter.uninstall()
            return [
                f"已移除被全局配置取代的项目级 MemoryGuard 覆盖：{project}"
            ]
        except Exception as exc:  # global config is already valid; report cleanup debt
            return [
                "全局 MemoryGuard 配置已修复，但项目级旧覆盖清理失败："
                f"{project}: {type(exc).__name__}: {exc}"
            ]

    def _find_binding(self, agent_instance_id: str = "",
                      share_group_id: str = "") -> tuple[str | None, str | None]:
        """Read the authoritative binding plane selected by cutover state."""
        try:
            # Global install rewrites ``self.workspace`` to the stable user
            # data home, but authorization still belongs to the explicit
            # control workspace that initiated the takeover.
            control_workspace = getattr(self, "_superseded_project_workspace", None) or self.workspace
            plane = _binding_plane_for_workspace(control_workspace)
            if plane != "v2":
                return None, None
            if not agent_instance_id:
                return None, None
            from .runtime_v2.group_native import GroupControlService

            binding = GroupControlService(control_workspace, write=False).active_binding_for_agent(
                agent_instance_id
            )
            if binding is None:
                return None, None
            if share_group_id and str(binding.get("share_group_id") or "") != str(share_group_id):
                return None, None
            return str(binding.get("binding_id") or "") or None, str(binding.get("status") or "") or None
        except ValueError as exc:
            if str(exc) in {"v2_upgrade_required", "v2_manifest_state_unavailable"}:
                raise
            return None, None
        except Exception:
            return None, None

    def _require_active_binding(self, agent_instance_id: str,
                                share_group_id: str) -> str:
        """安装前先验证真实身份和授权，禁止生成不可用的匿名 MCP 配置。"""
        if not agent_instance_id:
            raise ValueError("agent_instance_id is required for MCP installation")
        binding_id, binding_status = self._find_binding(
            agent_instance_id, share_group_id
        )
        if not binding_id or binding_status != "active":
            raise ValueError(
                f"active binding not found for agent_instance_id="
                f"{agent_instance_id!r}, share_group_id={share_group_id!r}"
            )
        return binding_id

    def _install_host_hook(
        self,
        *,
        enabled: bool,
        agent_instance_id: str,
        share_group_id: str,
    ) -> dict[str, Any]:
        """Install the user-level hook only for an explicit global takeover."""
        from .host_hooks import HostHookManager

        if not enabled:
            return {
                "provider": self.provider_name,
                "supported": True,
                "configured": False,
                "status": "not_requested",
                "runtime_verified": False,
            }
        manager = HostHookManager(self.workspace)
        try:
            return manager.install(
                self.provider_name,
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
                mode="enforce",
            )
        except Exception as exc:
            return {
                "provider": self.provider_name,
                "supported": True,
                "configured": False,
                "status": "error",
                "runtime_verified": False,
                "error": str(exc),
            }

    def _configured_result(self, *, instruction_path: Path, mcp_path: Path,
                           binding_id: str,
                           warnings: list[str] | None = None,
                           hook: dict[str, Any] | None = None) -> dict[str, Any]:
        hook_result = dict(hook or {})
        result_warnings = list(warnings or [])
        if hook_result.get("status") == "error":
            result_warnings.append(
                f"Hook 安装失败：{hook_result.get('error', 'unknown error')}"
            )
        elif hook_result and not hook_result.get("supported", True):
            result_warnings.append(
                "该宿主没有已验证的用户级 Hook seam；当前仅安装 MCP + 规则重定向"
            )
        return {
            "provider": self.provider_name,
            "configured": True,
            "installed": True,  # 兼容旧 API：仅表示配置文件已安装
            "status": "configured",
            "restart_required": True,
            "runtime_verified": False,
            "instruction_file": str(instruction_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": binding_id,
            "hook": hook_result,
            "hook_configured": bool(hook_result.get("configured")),
            "hook_runtime_verified": bool(hook_result.get("runtime_verified")),
            "warnings": result_warnings,
        }

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    def uninstall(self) -> dict[str, Any]:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        raise NotImplementedError


# ===========================================================================
# ClaudeAdapter
# ===========================================================================


class ClaudeAdapter(ProviderAdapter):
    """Claude Code adapter。

    - 指令文件：<workspace>/CLAUDE.md（项目级）；无 workspace 时 ~/.claude/CLAUDE.md（用户级）
    - MCP 配置：<workspace>/.mcp.json（项目级）；全局用户级为 ~/.claude.json
    - 支持环境变量 CLAUDE_CONFIG_DIR 覆盖 ~/.claude/
    """

    provider_name = "claude"

    def __init__(self, workspace: str | Path = ""):
        self._has_workspace = bool(workspace)
        self.workspace = Path(workspace).resolve() if workspace else Path.home()

    def _config_dir(self) -> Path:
        configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        return Path(configured).expanduser() if configured else Path.home() / ".claude"

    def _instruction_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / "CLAUDE.md"
        return self._config_dir() / "CLAUDE.md"

    def _mcp_config_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".mcp.json"
        return Path.home() / ".claude.json"

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        control_scope = self._select_install_workspace(
            workspace, global_scope=global_scope,
        )
        binding_id = self._require_active_binding(
            agent_instance_id, share_group_id
        )

        # 先生成并验证全部内容，再事务写入。
        instr_path = self._instruction_path()
        content = _read_text_for_update(instr_path)
        body = _instruction_body(share_group_id)
        new_content = _replace_section(content, _BEGIN_MARKER, _END_MARKER, body)

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _set_mcp_server(
            data,
            MCP_SERVER_NAME,
            _mcp_server_config(
                agent_instance_id, self.workspace, "claude", control_scope,
            ),
        )
        new_mcp_content = json.dumps(data, ensure_ascii=False, indent=2)
        _apply_file_transaction([
            (instr_path, new_content),
            (mcp_path, new_mcp_content),
        ])
        hook = self._install_host_hook(
            enabled=global_scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
        )
        warnings = self._cleanup_superseded_project_override()
        return self._configured_result(
            instruction_path=instr_path,
            mcp_path=mcp_path,
            binding_id=binding_id,
            warnings=warnings,
            hook=hook,
        )

    def uninstall(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=True)
        instr_path = self._instruction_path()

        content = _read_text_for_update(instr_path)
        instruction_update: str | None = content if instr_path.exists() else None
        if _BEGIN_MARKER in content:
            new_content = _remove_section(content, _BEGIN_MARKER, _END_MARKER)
            instruction_update = new_content if new_content.strip() else None

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _remove_mcp_server(data, MCP_SERVER_NAME)
        mcp_update = (
            json.dumps(data, ensure_ascii=False, indent=2) if data else None
        )
        _apply_file_transaction([
            (instr_path, instruction_update),
            (mcp_path, mcp_update),
        ])

        return {
            "provider": self.provider_name,
            "installed": False,
            "configured": False,
            "status": "not_configured",
            "instruction_file": str(instr_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": None,
        }

    def status(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=False)
        instr_path = self._instruction_path()
        mcp_path = self._mcp_config_path()

        content = _read_text(instr_path)
        instruction_installed = _BEGIN_MARKER in content and _END_MARKER in content

        data = _load_json(mcp_path)
        mcp_configured = _has_mcp_server(data, MCP_SERVER_NAME)

        agent_id = str(
            data.get("mcpServers", {})
            .get(MCP_SERVER_NAME, {})
            .get("env", {})
            .get("MEMORYGUARD_AGENT_ID", "")
            or ""
        )
        binding_id, binding_status = self._find_binding(agent_id)
        configured = instruction_installed and mcp_configured and bool(agent_id)
        return {
            "installed": configured,
            "configured": configured,
            "status": "configured" if configured else "not_configured",
            "restart_required": configured,
            "runtime_verified": False,
            "instruction_file": str(instr_path),
            "mcp_configured": mcp_configured,
            "configured_agent_instance_id": agent_id,
            "binding_id": binding_id,
            "binding_status": binding_status,
        }


# ===========================================================================
# CodexAdapter
# ===========================================================================


class CodexAdapter(ProviderAdapter):
    """Codex CLI adapter。

    - 指令文件：<workspace>/AGENTS.md（项目级）；无 workspace 时 ~/.codex/AGENTS.md
    - MCP 配置：<workspace>/.codex/config.toml（项目级）；无 workspace 时 ~/.codex/config.toml
      段落：[mcp_servers.memoryguard]
    """

    provider_name = "codex"

    def __init__(self, workspace: str | Path = ""):
        self._has_workspace = bool(workspace)
        self.workspace = Path(workspace).resolve() if workspace else Path.home()

    def _instruction_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / "AGENTS.md"
        return Path.home() / ".codex" / "AGENTS.md"

    def _mcp_config_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".codex" / "config.toml"
        return Path.home() / ".codex" / "config.toml"

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        control_scope = self._select_install_workspace(
            workspace, global_scope=global_scope,
        )
        binding_id = self._require_active_binding(
            agent_instance_id, share_group_id
        )

        instr_path = self._instruction_path()
        content = _read_text_for_update(instr_path)
        body = _instruction_body(share_group_id)
        new_content = _replace_section(content, _BEGIN_MARKER, _END_MARKER, body)

        mcp_path = self._mcp_config_path()
        toml_content = _read_text_for_update(mcp_path)
        toml_content = _reconcile_memoryguard_toml_tables(toml_content)
        section = _mcp_toml_section(
            agent_instance_id, self.workspace, control_scope=control_scope,
        )
        new_toml = _replace_section(toml_content, _TOML_BEGIN, _TOML_END, section)
        _validate_toml(new_toml, mcp_path)
        _apply_file_transaction([
            (instr_path, new_content),
            (mcp_path, new_toml),
        ])
        hook = self._install_host_hook(
            enabled=global_scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
        )
        warnings = (
            ["Codex 仅在用户信任该项目后加载项目级 .codex/config.toml"]
            if self._has_workspace else []
        )
        warnings.extend(self._cleanup_superseded_project_override())
        global_path = Path.home() / ".codex" / "config.toml"
        if self._has_workspace and global_path != mcp_path:
            global_text = _read_text(global_path)
            if _TOML_BEGIN in global_text or (
                "[mcp_servers.memoryguard]" in global_text
            ):
                warnings.append(
                    "检测到旧用户级 MemoryGuard MCP 配置；项目配置优先，"
                    "验证项目连接后可移除旧全局条目"
                )
        return self._configured_result(
            instruction_path=instr_path,
            mcp_path=mcp_path,
            binding_id=binding_id,
            warnings=warnings,
            hook=hook,
        )

    def uninstall(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=True)
        instr_path = self._instruction_path()

        content = _read_text_for_update(instr_path)
        instruction_update: str | None = content if instr_path.exists() else None
        keep_shared_instruction = (
            self._has_workspace
            and _other_adapter_uses_project_agents_md(
                self.workspace, excluding=self.provider_name
            )
        )
        if _BEGIN_MARKER in content and not keep_shared_instruction:
            new_content = _remove_section(content, _BEGIN_MARKER, _END_MARKER)
            instruction_update = new_content if new_content.strip() else None

        mcp_path = self._mcp_config_path()
        toml_content = _read_text_for_update(mcp_path)
        mcp_update: str | None = toml_content if mcp_path.exists() else None
        if _TOML_BEGIN in toml_content:
            new_toml = _remove_section(toml_content, _TOML_BEGIN, _TOML_END)
            _validate_toml(new_toml, mcp_path)
            mcp_update = new_toml if new_toml.strip() else None
        _apply_file_transaction([
            (instr_path, instruction_update),
            (mcp_path, mcp_update),
        ])

        return {
            "provider": self.provider_name,
            "installed": False,
            "configured": False,
            "status": "not_configured",
            "instruction_file": str(instr_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": None,
        }

    def status(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=False)
        instr_path = self._instruction_path()
        mcp_path = self._mcp_config_path()

        content = _read_text(instr_path)
        instruction_installed = _BEGIN_MARKER in content and _END_MARKER in content

        toml_content = _read_text(mcp_path)
        mcp_configured = _TOML_BEGIN in toml_content and _TOML_END in toml_content

        agent_id = ""
        try:
            data = tomllib.loads(toml_content) if toml_content.strip() else {}
            agent_id = str(
                data.get("mcp_servers", {})
                .get(MCP_SERVER_NAME, {})
                .get("env", {})
                .get("MEMORYGUARD_AGENT_ID", "")
                or ""
            )
        except tomllib.TOMLDecodeError:
            mcp_configured = False
        binding_id, binding_status = self._find_binding(agent_id)
        configured = instruction_installed and mcp_configured and bool(agent_id)
        return {
            "installed": configured,
            "configured": configured,
            "status": "configured" if configured else "not_configured",
            "restart_required": configured,
            "runtime_verified": False,
            "instruction_file": str(instr_path),
            "mcp_configured": mcp_configured,
            "configured_agent_instance_id": agent_id,
            "binding_id": binding_id,
            "binding_status": binding_status,
        }


# ===========================================================================
# CursorAdapter
# ===========================================================================


class CursorAdapter(ProviderAdapter):
    """Cursor adapter。

    - 指令文件：<workspace>/.cursor/rules/memoryguard.mdc（新格式，MemoryGuard 专属文件）
      无 workspace 时 ~/.cursor/rules/memoryguard.mdc
    - MCP 配置：<workspace>/.cursor/mcp.json（项目级）；无 workspace 时 ~/.cursor/mcp.json
    """

    provider_name = "cursor"

    def __init__(self, workspace: str | Path = ""):
        self._has_workspace = bool(workspace)
        self.workspace = Path(workspace).resolve() if workspace else Path.home()

    def _instruction_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".cursor" / "rules" / "memoryguard.mdc"
        return Path.home() / ".cursor" / "rules" / "memoryguard.mdc"

    def _mcp_config_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".cursor" / "mcp.json"
        return Path.home() / ".cursor" / "mcp.json"

    def _instruction_content(self, share_group_id: str = "default") -> str:
        """生成 MDC 文件完整内容（含 frontmatter + 标记段落）。"""
        frontmatter = (
            "---\n"
            "description: MemoryGuard memory redirect\n"
            "alwaysApply: true\n"
            "globs: []\n"
            "---\n"
        )
        body = _instruction_body(share_group_id)
        return (
            frontmatter
            + _BEGIN_MARKER + "\n"
            + body + "\n"
            + _END_MARKER + "\n"
        )

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        control_scope = self._select_install_workspace(
            workspace, global_scope=global_scope,
        )
        binding_id = self._require_active_binding(
            agent_instance_id, share_group_id
        )

        instr_path = self._instruction_path()
        new_instruction = self._instruction_content(share_group_id)

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _set_mcp_server(
            data,
            MCP_SERVER_NAME,
            _mcp_server_config(
                agent_instance_id, self.workspace, "cursor", control_scope,
            ),
        )
        new_mcp_content = json.dumps(data, ensure_ascii=False, indent=2)
        _apply_file_transaction([
            (instr_path, new_instruction),
            (mcp_path, new_mcp_content),
        ])
        hook = self._install_host_hook(
            enabled=global_scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
        )
        warnings: list[str] = self._cleanup_superseded_project_override()
        global_path = Path.home() / ".cursor" / "mcp.json"
        if self._has_workspace and global_path != mcp_path:
            global_data = _load_json(global_path)
            if _has_mcp_server(global_data, MCP_SERVER_NAME):
                warnings.append(
                    "检测到旧用户级 MemoryGuard MCP 配置；请先验证项目级连接，"
                    "再从 Cursor 全局 MCP 设置移除旧条目"
                )
        return self._configured_result(
            instruction_path=instr_path,
            mcp_path=mcp_path,
            binding_id=binding_id,
            warnings=warnings,
            hook=hook,
        )

    def uninstall(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=True)
        instr_path = self._instruction_path()

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _remove_mcp_server(data, MCP_SERVER_NAME)
        mcp_update = (
            json.dumps(data, ensure_ascii=False, indent=2) if data else None
        )
        _apply_file_transaction([
            (instr_path, None),
            (mcp_path, mcp_update),
        ])

        return {
            "provider": self.provider_name,
            "installed": False,
            "configured": False,
            "status": "not_configured",
            "instruction_file": str(instr_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": None,
        }

    def status(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=False)
        instr_path = self._instruction_path()
        mcp_path = self._mcp_config_path()

        instruction_installed = (
            instr_path.exists()
            and _BEGIN_MARKER in _read_text(instr_path)
        )

        data = _load_json(mcp_path)
        mcp_configured = _has_mcp_server(data, MCP_SERVER_NAME)

        agent_id = str(
            data.get("mcpServers", {})
            .get(MCP_SERVER_NAME, {})
            .get("env", {})
            .get("MEMORYGUARD_AGENT_ID", "")
            or ""
        )
        binding_id, binding_status = self._find_binding(agent_id)
        configured = instruction_installed and mcp_configured and bool(agent_id)
        return {
            "installed": configured,
            "configured": configured,
            "status": "configured" if configured else "not_configured",
            "restart_required": configured,
            "runtime_verified": False,
            "instruction_file": str(instr_path),
            "mcp_configured": mcp_configured,
            "configured_agent_instance_id": agent_id,
            "binding_id": binding_id,
            "binding_status": binding_status,
        }


# ===========================================================================
# TRAE Adapter
# ===========================================================================


class TraeAdapter(ProviderAdapter):
    """TRAE IDE / TRAE CN adapter。

    - 指令文件：<workspace>/AGENTS.md（TRAE 支持项目/子仓库 AGENTS.md）
    - MCP 配置：<workspace>/.trae/mcp.json（TRAE 项目级 MCP）
    - 无 workspace 时回退到 TRAE 用户级 mcp.json 与 user_rules/

    `.trae-cn/mcps/` 是 TRAE 的运行时工具元数据缓存，不是可写安装入口。
    """

    provider_name = "trae"

    def __init__(self, workspace: str | Path = ""):
        self._has_workspace = bool(workspace)
        self.workspace = Path(workspace).resolve() if workspace else Path.home()

    @staticmethod
    def _user_mcp_config_path() -> Path:
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            candidates = [
                Path(appdata) / "TRAE SOLO CN" / "User" / "mcp.json",
                Path(appdata) / "TRAE" / "User" / "mcp.json",
            ]
            for candidate in candidates:
                if candidate.exists():
                    return candidate
            return candidates[0]
        return Path.home() / ".trae" / "mcp.json"

    def _instruction_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / "AGENTS.md"
        return Path.home() / ".trae-cn" / "user_rules" / "memoryguard.md"

    def _mcp_config_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".trae" / "mcp.json"
        return self._user_mcp_config_path()

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        control_scope = self._select_install_workspace(
            workspace, global_scope=global_scope,
        )
        binding_id = self._require_active_binding(
            agent_instance_id, share_group_id
        )

        instr_path = self._instruction_path()
        content = _read_text_for_update(instr_path)
        body = _instruction_body(share_group_id)
        new_content = _replace_section(content, _BEGIN_MARKER, _END_MARKER, body)

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _set_mcp_server(
            data,
            MCP_SERVER_NAME,
            _mcp_server_config(
                agent_instance_id, self.workspace, "trae", control_scope,
            ),
        )
        new_mcp_content = json.dumps(data, ensure_ascii=False, indent=2)
        _apply_file_transaction([
            (instr_path, new_content),
            (mcp_path, new_mcp_content),
        ])
        hook = self._install_host_hook(
            enabled=global_scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
        )

        warnings: list[str] = self._cleanup_superseded_project_override()
        global_path = self._user_mcp_config_path()
        if self._has_workspace and global_path != mcp_path:
            global_data = _load_json(global_path)
            if _has_mcp_server(global_data, MCP_SERVER_NAME):
                warnings.append(
                    "检测到旧用户级 MemoryGuard MCP 配置；请先验证项目级连接，"
                    "再从 TRAE 全局 MCP 设置移除旧条目"
                )
        return self._configured_result(
            instruction_path=instr_path,
            mcp_path=mcp_path,
            binding_id=binding_id,
            warnings=warnings,
            hook=hook,
        )

    def uninstall(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=True)
        instr_path = self._instruction_path()
        content = _read_text_for_update(instr_path)
        instruction_update: str | None = content if instr_path.exists() else None
        keep_shared_instruction = (
            self._has_workspace
            and _other_adapter_uses_project_agents_md(
                self.workspace, excluding=self.provider_name
            )
        )
        if _BEGIN_MARKER in content and not keep_shared_instruction:
            new_content = _remove_section(content, _BEGIN_MARKER, _END_MARKER)
            instruction_update = new_content if new_content.strip() else None

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _remove_mcp_server(data, MCP_SERVER_NAME)
        mcp_update = (
            json.dumps(data, ensure_ascii=False, indent=2) if data else None
        )
        _apply_file_transaction([
            (instr_path, instruction_update),
            (mcp_path, mcp_update),
        ])

        return {
            "provider": self.provider_name,
            "installed": False,
            "configured": False,
            "status": "not_configured",
            "instruction_file": str(instr_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": None,
        }

    def status(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=False)
        instr_path = self._instruction_path()
        mcp_path = self._mcp_config_path()

        content = _read_text(instr_path)
        instruction_installed = (
            _BEGIN_MARKER in content and _END_MARKER in content
        )
        data = _load_json(mcp_path)
        mcp_configured = _has_mcp_server(data, MCP_SERVER_NAME)
        agent_id = str(
            data.get("mcpServers", {})
            .get(MCP_SERVER_NAME, {})
            .get("env", {})
            .get("MEMORYGUARD_AGENT_ID", "")
            or ""
        )
        binding_id, binding_status = self._find_binding(agent_id)
        configured = instruction_installed and mcp_configured and bool(agent_id)
        return {
            "installed": configured,
            "configured": configured,
            "status": "configured" if configured else "not_configured",
            "restart_required": configured,
            "runtime_verified": False,
            "instruction_file": str(instr_path),
            "mcp_configured": mcp_configured,
            "configured_agent_instance_id": agent_id,
            "binding_id": binding_id,
            "binding_status": binding_status,
        }


PROVIDER_ADAPTERS: dict[str, type[ProviderAdapter]] = {
    "claude-code": ClaudeAdapter,
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "cursor": CursorAdapter,
    "trae": TraeAdapter,
}


def get_provider_adapter_class(product: str) -> type[ProviderAdapter] | None:
    """按规范化 product ID 返回自动安装适配器。"""
    return PROVIDER_ADAPTERS.get((product or "").strip().lower())


def repair_global_provider_configs(
    providers: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Rebuild global provider integrations from canonical data-home bindings.

    This is the upgrade/repair entry point.  It never trusts a provider's
    existing AgentInstance id, share-group id, or MEMORYGUARD_WORKSPACE.  The
    current instances are rediscovered, then each provider is installed from
    the one active binding stored in the canonical user data home.
    """
    from .agent_locator import AgentLocator
    from .data_home import resolve_data_home

    data_home = resolve_data_home()
    _require_provider_state(data_home, mutation=True)
    data_home.mkdir(parents=True, exist_ok=True)
    instances, _ = AgentLocator(data_home).detect_instances()
    from .runtime_v2.group_native import GroupControlService

    binding_store: Any = GroupControlService(data_home, write=False)

    requested: set[str] = set()
    for raw in providers or ():
        value = str(raw or "").strip().lower()
        if value in {"", "all", "*"}:
            continue
        cls = get_provider_adapter_class(value)
        if cls is None:
            raise ValueError(f"unsupported provider: {value}")
        requested.add(cls.provider_name)
    if not requested:
        requested = {"claude", "codex", "cursor", "trae"}

    by_provider: dict[str, list[Any]] = {}
    for instance in instances:
        cls = get_provider_adapter_class(instance.product)
        if cls is None or cls.provider_name not in requested:
            continue
        by_provider.setdefault(cls.provider_name, []).append(instance)

    repaired: list[dict[str, Any]] = []
    for provider in sorted(requested):
        matches = by_provider.get(provider, [])
        if not matches:
            repaired.append({
                "provider": provider,
                "status": "skipped",
                "reason": "provider_instance_not_detected",
            })
            continue
        if len(matches) != 1:
            repaired.append({
                "provider": provider,
                "status": "error",
                "reason": "multiple_provider_instances_detected",
                "agent_instance_ids": sorted(item.instance_id for item in matches),
            })
            continue
        instance = matches[0]
        binding = binding_store.active_binding_for_agent(instance.instance_id)
        binding_data = dict(binding) if isinstance(binding, dict) else None
        if binding_data is None:
            repaired.append({
                "provider": provider,
                "status": "error",
                "reason": "active_binding_not_found",
                "agent_instance_id": instance.instance_id,
            })
            continue
        cls = get_provider_adapter_class(instance.product)
        if cls is None:  # guarded above; keep the write path explicit
            continue
        try:
            group_id = str(binding_data.get("share_group_id") or "")
            result = cls(data_home).install(
                data_home,
                share_group_id=group_id,
                agent_instance_id=instance.instance_id,
                global_scope=True,
            )
            repaired.append({
                "provider": provider,
                "status": "configured",
                "agent_instance_id": instance.instance_id,
                "share_group_id": group_id,
                "result": result,
            })
        except Exception as exc:
            repaired.append({
                "provider": provider,
                "status": "error",
                "agent_instance_id": instance.instance_id,
                "share_group_id": str(binding_data.get("share_group_id") or ""),
                "reason": f"{type(exc).__name__}: {exc}",
            })

    configured = sum(item["status"] == "configured" for item in repaired)
    errors = sum(item["status"] == "error" for item in repaired)
    skipped = sum(item["status"] == "skipped" for item in repaired)
    return {
        "ok": errors == 0,
        "data_home": str(data_home),
        "configured": configured,
        "errors": errors,
        "skipped": skipped,
        "providers": repaired,
        "restart_required": configured > 0,
    }


def _main(argv: list[str] | None = None) -> int:
    """Small maintenance CLI used after upgrades and control-plane migration."""
    import argparse

    parser = argparse.ArgumentParser(description="Repair MemoryGuard provider integrations")
    sub = parser.add_subparsers(dest="command", required=True)
    repair = sub.add_parser(
        "repair", help="rebuild global provider configs from canonical bindings",
    )
    repair.add_argument(
        "providers", nargs="*", default=["all"],
        help="claude codex cursor trae, or all",
    )
    opts = parser.parse_args(argv)
    if opts.command == "repair":
        try:
            result = repair_global_provider_configs(opts.providers)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
