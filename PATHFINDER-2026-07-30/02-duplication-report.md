# 重复与割裂报告

| 关注点 | 当前重复/割裂 | 风险 | 统一 owner |
|---|---|---|---|
| 项目路径 | `rule_scope.canonical_project_ref` 与 `conversation_history._normalize_project_ref` 规范不同 | Windows 大小写/斜杠别名拆分项目 | `rule_scope.canonical_project_ref` |
| 导入项目身份 | `ImportedConversation` 无 project；batch scope 只有一个 project | 多项目历史落入 unknown 或错挂 | provider parser → per-conversation metadata |
| 历史授权 | GUI 直接构造 scope；MCP `_resolve_access`、`HistoryScope.trusted`、`history_api` 各做一层 | 共享 fan-out 后可能跨组伪造 | 新的 `HistoryAccessResolver` |
| 项目分组 | 神经图与历史页各自可自行按字符串分组 | count、标签、同名路径不一致 | store 返回统一 safe projection |
| UI scope | `scopeApiArgs()` 与 `historyScope()` 两套构造 | agent/group 同时传，语义含混 | mode-aware scope builder |
| MCP 工具声明 | `history_api.py` 与 `mcp_server.py` 分散注册 | schema/权限漂移 | 单一 history tool registry |

## 应统一

- project identity canonicalization。
- 当前共享组成员的服务端授权解析。
- safe session/project projection contract。
- personal/shared scope 的互斥表达。
- 标题回退算法和 project path status。

## 不应统一

- 原始历史 SQLite 与长期记忆 SharedMemoryStore 的物理存储。
- 原始全文读取与 bootstrap 注入。
- 查询权限与删除权限。
- GUI 展示偏好与后端授权。

## 关键漏洞

1. 当前 `_scope_where` 永远锁定单 Agent，共享组不能互查。
2. 若只把 SQL 改成 group fan-out，nested `scope.share_group_id` 和 GUI 自报 scope 会成为越权入口。
3. 若共享查询 scope 直接复用于 delete，成员可删除同组其他 Agent 的原始历史。
4. canonical 规则变化若参与重算 session ID，会导致旧会话和 evidence 漂移。
5. importer 缺少逐会话 project_ref，不能靠 UI 补救。
