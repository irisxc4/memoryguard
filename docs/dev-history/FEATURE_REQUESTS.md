# Feature Requests

Capabilities requested by the user.

---

## [FEAT-20260730-003] in_graph_governance_and_history_titles

**Logged**: 2026-07-30T17:35:00+08:00
**Priority**: high
**Status**: resolved
**Area**: frontend

### Requested Capability
规则与历史分类在神经图内展开并治理；每个会话索引自动生成可读的总结标题，不显示“未命名会话”。

### User Context
分类节点当前会直接跳转到独立页面，破坏图谱上下文；规则页治理能力不直观。旧会话多数没有宿主提供的标题，图和历史页只能展示占位文本。

### Complexity Estimate
medium

### Suggested Implementation
节点点击只选择/聚焦，图内详情复用现有记忆编辑、受众、删除和恢复 API。历史标题优先使用宿主标题，否则从首条可见用户消息生成短标题；无可见消息时使用 Provider 与会话时间作为稳定 fallback，并对旧空标题执行幂等回填及 FTS 同步。

### Metadata
- Frequency: recurring
- Related Features: neuron_graph, rules_habits, conversation_history

### Resolution
- **Resolved**: 2026-07-30T18:10:00+08:00
- **Notes**: 图内规则治理与会话元数据详情已落地；会话标题按宿主标题、首条用户文本、Provider+时间三级生成，并幂等回填旧库和同步 FTS。真实 Codex 作用域 48 条会话已无空/占位标题。

---

## [FEAT-20260730-002] history_backfill_and_graph_categories

**Logged**: 2026-07-30T16:20:00+08:00
**Priority**: high
**Status**: resolved
**Area**: frontend

### Requested Capability
扫描并索引安装前可读取的本地 Agent 会话历史；在记忆神经图中增加“规则与习惯”和“对话历史”两个一级分类，同时保留各自的详细治理页面。

### User Context
当前历史库主要从 Hook 安装后开始记录，旧会话没有自动进入历史索引；规则和历史也没有作为记忆架构中的明确大分类呈现在神经图里。

### Complexity Estimate
complex

### Suggested Implementation
为 Codex、Claude、Cursor 的稳定 JSONL 格式提供流式、增量、幂等导入；不解析不稳定的专有数据库。历史原文继续保存在独立历史库，不进入长期记忆或 bootstrap。神经图通过虚拟索引节点展示分类、会话元数据和计数，不复制规则正文或会话全文。

### Metadata
- Frequency: recurring
- Related Features: conversation_history, neuron_graph, rules_habits

### Resolution
- **Resolved**: 2026-07-30T17:20:00+08:00
- **Notes**: 已增加 Codex/Claude/Cursor 旧会话发现与有界分批回填、TRAE 专有库显式不支持状态，以及规则/历史神经图虚拟一级分类。全量 477 项测试通过；真实首批处理 25 个 Codex 日志，0 错误。

---

## [FEAT-20260729-001] local_knowledge_source_cards

**Logged**: 2026-07-29T22:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: frontend

### Requested Capability
数据页添加文件夹后立即显示为本地知识库卡片，并提供删除映射按钮。

### User Context
当前后端已保存来源，但页面没有任何可见反馈，用户无法确认知识库是否连接，也无法从页面解除映射。

### Complexity Estimate
simple

### Suggested Implementation
复用现有 `SourceRegistry`、`list_sources`、`get_raw_memory` 和 `remove_source`；前端渲染非项目目录的知识来源卡片，显示路径、类型、文件数、连接状态和文件列表。删除只移除注册表映射，不删除磁盘内容。

### Metadata
- Frequency: first_time
- Related Features: source_registry, data_page

### Resolution
- **Resolved**: 2026-07-29T22:15:00+08:00
- **Notes**: 已增加本地知识库卡片、路径状态、文件展开与仅删除映射的安全操作；全量测试 389 项通过。

---
