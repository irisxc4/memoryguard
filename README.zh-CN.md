# MemoryGuard

> 本地优先的 MCP 记忆后端与治理台，面向编程 Agent。
>
> English: [README.md](README.md)

MemoryGuard 让多个编程 Agent 写入同一套共享记忆，自动整理、隔离、覆盖和回滚——不让 GUI 变成审批队列。

## 它做什么

MemoryGuard 提供本地 MCP stdio 记忆后端，Agent（Claude Code、Codex、Cursor 等）按原有记忆机制写入即可。每次写入自动整理——分类、去重、覆盖、隔离、压缩——保持共享记忆干净。治理台让你事后观察、编辑、合并、回滚、解决冲突，而不是逐条批准写入。

```text
Agent 写入记忆
  -> memoryguard_memory_write（MCP 工具）
  -> 原始事件
  -> 自动整理（分类 / 去重 / 覆盖 / 冲突 / 隔离 / 衍生 / 压缩）
  -> 活跃共享记忆
  -> GUI 治理（观察 / 纠错 / 覆盖 / 回滚）
```

## 功能

- **MCP 记忆后端** — 6 个核心工具：read / search / write / update / delete / status
- **自动整理** — 分类 / 去重 / 覆盖 / 冲突 / 隔离 / 衍生 / 压缩
- **多 Agent 共享记忆组** — 多个 Agent 写入同一套受治理记忆
- **GUI 治理台** — 观察、编辑、合并、锁定、恢复、回滚（不是审批队列）
- **Provider 适配器** — 一键配置 Claude Code / Codex / Cursor
- **版本回滚** — 所有治理动作版本化，可回滚
- **无账号、无服务器、无遥测** — 一切在本地

## 快速开始

### 安装

**一行安装（推荐）：**

```bash
pip install git+https://github.com/irisxc4/memoryguard.git
```

带 GUI 桌面窗口（可选）：

```bash
pip install "git+https://github.com/irisxc4/memoryguard.git#egg=memoryguard[gui]"
```

**从源码安装：**

```bash
git clone https://github.com/irisxc4/memoryguard.git
cd memoryguard
pip install -e .
```

验证安装：

```bash
memoryguard doctor
```

### 配置你的 Agent

MemoryGuard 自带 provider 适配器，自动写入 MCP 配置和指令文件。也可手动配置——见安装指南：

- [Claude Code](docs/install-claude-code.md)
- [Codex](docs/install-codex.md)
- [Cursor](docs/install-cursor.md)

<details>
<summary>手动 MCP 配置（所有 Agent 用同一个 server）</summary>

**Claude Code** — `.mcp.json`（项目根目录）：

```json
{
  "mcpServers": {
    "memoryguard": {
      "command": "python",
      "args": ["-m", "memoryguard.mcp_server"]
    }
  }
}
```

**Codex** — `~/.codex/config.toml`：

```toml
[mcp_servers.memoryguard]
command = "python"
args = ["-m", "memoryguard.mcp_server"]
```

**Cursor** — `~/.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "memoryguard": {
      "command": "python",
      "args": ["-m", "memoryguard.mcp_server"]
    }
  }
}
```

</details>

### 使用

```bash
memoryguard doctor            # 诊断安装环境
memoryguard mcp-status        # 查询 MCP 记忆后端状态
python -m memoryguard.mcp_server  # 启动 MCP stdio 服务（由你的 Agent 调用）
memoryguard audit .           # 只读扫描工作区，生成报告
memoryguard open .            # 打开最新报告
```

Agent 配置好后，通过 `memoryguard_memory_write` MCP 工具写入记忆，MemoryGuard 自动整理每次写入。打开 GUI 观察和治理结果。

## 工作原理

三层架构：

| 层 | 职责 |
|---|---|
| **数据层** | Agent 原生记忆、文件、外部 MCP、文档——提供原始证据，不参与治理 |
| **记忆层** | MemoryGuard MCP 共享记忆后端——唯一共享事实源；Agent 写入；每次写入自动整理 |
| **治理层** | GUI + CLI——事后观察、编辑、合并、锁定、恢复、回滚 |

## 命令

| 命令 | 说明 |
|---|---|
| `audit [path]` | 只读扫描，生成报告 |
| `open [path]` | 打开最新报告 |
| `explain <finding_id>` | 解释发现项的证据和风险 |
| `plan <finding_ids...>` | 生成最小修复计划（不写入） |
| `apply <plan_id>` | 应用计划：备份 + 修补 + 重扫 |
| `verify` | 重扫并对比前后 |
| `undo <change_id>` | 从备份恢复并重新验证 |
| `source <action>` | 管理授权来源（list / add / remove / preview） |
| `scan` | 只读扫描，构建覆盖率账本 |
| `import <action> <bundle>` | 离线导入包（preview / create） |
| `memory <action>` | 记忆构建与发布（build-plan / build-apply / verify / rollback） |
| `doctor` | 诊断安装环境 |
| `mcp-status` | 查询 MCP 记忆后端状态 |

## MCP 工具

### 记忆后端（6 个核心工具）

| 工具 | 说明 |
|---|---|
| `memoryguard_memory_read` | 按 ID 读取单条记忆 |
| `memoryguard_memory_search` | 按查询、类别、状态搜索记忆 |
| `memoryguard_memory_write` | 写入新记忆；写入时自动整理 |
| `memoryguard_memory_update` | 更新记忆（正文 / 类别 / 状态） |
| `memoryguard_memory_delete` | 软删除记忆 |
| `memoryguard_memory_status` | 查询共享记忆组状态 |

### 审计与扫描

| 工具 | 说明 |
|---|---|
| `memoryguard_audit` | 只读工作区扫描 |
| `memoryguard_explain` | 解释发现项的证据和风险 |
| `memoryguard_list_sources` | 列出授权来源 |
| `memoryguard_scan_summary` | 扫描 + 覆盖率账本 |
| `memoryguard_neuron_graph` | 读取神经图投影 |
| `memoryguard_import_preview` | 预览导入包 |
| `memoryguard_build_plan` | 生成记忆构建计划（不写入） |

### Agent 绑定

| 工具 | 说明 |
|---|---|
| `memoryguard_binding_create` | 绑定 Agent 到共享组 |
| `memoryguard_binding_list` | 列出 Agent 绑定 |
| `memoryguard_resolve_group` | 查询 Agent 对应的共享组 |

### 外部 MCP

| 工具 | 说明 |
|---|---|
| `memoryguard_external_mcp_list` | 列出已导入的外部 MCP 描述符 |
| `memoryguard_external_mcp_import` | 导入外部 MCP 描述符（L0–L4 分级） |

### 文档萃取

| 工具 | 说明 |
|---|---|
| `memoryguard_extract_memories` | 从源文件萃取记忆片段（只读预览） |
| `memoryguard_accept_candidates` | 确认接受候选记忆，写入共享记忆 |

### 语义与 Provider

| 工具 | 说明 |
|---|---|
| `memoryguard_semantic_check` | 检查文本的语义重复 / 冲突 |
| `memoryguard_provider_install` | 安装 provider 适配器（Claude / Codex / Cursor） |

## 治理 GUI

GUI 是**治理台，不是审批队列**。Agent 通过 MCP 写入记忆，MemoryGuard 自动整理。GUI 展示：

- 最近自动写入
- 自动整理结果（分类 / 去重 / 压缩）
- 覆盖链（什么被覆盖了、为什么）
- 冲突队列（需要人工仲裁的记忆）
- 隔离队列（自动隔离的密钥 / Token / 凭证）
- 衍生记忆（从重复行为生成的 procedure / preference）
- 版本回滚（恢复到任意历史版本）

治理动作：**编辑、合并、锁定、恢复、删除、回滚**。所有动作版本化，可逆。

## FAQ

**MemoryGuard 需要服务器或账号吗？**
不需要。它以本地 MCP stdio 服务运行。无账号、无云端、无遥测。

**它会完全替代 Agent 的原生记忆吗？**
MemoryGuard 在 Agent 支持时将写入重定向到 MCP 后端。部分 Agent 无法完全关闭原生记忆——MemoryGuard 用 redirected / observed / unsupported 分级处理，不假装所有 Agent 都能停用原生记忆。

**GUI 会批准每次写入吗？**
不会。MCP 后端接收写入并自动整理。GUI 只在事后治理结果。

**我的记忆会被上传到任何地方吗？**
不会。所有数据存储在本地 `.memoryguard/` 目录下的 SQLite 数据库中。

**可以回滚更改吗？**
可以。所有治理动作版本化。可以恢复、取消覆盖、回滚到任意历史版本。回滚完整恢复全部 5 类数据（records / events / decisions / conflicts / quarantine）。

## 路线图

- **现在（开源核心）：** 本地 MCP 记忆后端 + 自动整理 + GUI 治理台 + provider 适配器 + 版本回滚。即本仓库。
- **之后：** 增强治理功能（自动衰减、衍生记忆、治理报告）。不承诺时间。
- **之后：** 团队和企业功能。不承诺时间。

我们不承诺未来功能的具体日期。开源核心本身已完全可用。

## 许可证

MIT — 见 [LICENSE](LICENSE)。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。提交 PR 即表示同意 [CLA](CLA.md)。
