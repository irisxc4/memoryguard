# GUI V2 与 CodeGraph 完整迁移方案

状态：待实施  
目标版本：V2 hotfix 后续重大版本  
范围：桌面主 GUI、Knowledge GUI、SafeBridge/HTTP transport、V2 native runtime、Graphify 抽取器、MemoryGuard CodeGraph 投影与验收

## 1. 最终目标

本轮不是隐藏、禁用或删除失效按钮，而是完整迁移现有产品能力：

1. 所有可见 GUI 操作均执行 V2 原生能力。
2. 现有 89 个 retired GUI method 全部获得 V2 canonical operation；重复名称可映射同一 canonical operation，但调用结果必须可执行，不能返回 `v2_operation_retired`。
3. V2 运行路径不得导入、实例化或回退到 V1 Store。
4. GUI、localhost HTTP、pywebview、compat facade 使用同一 operation registry、mutation 分类、参数转换和返回 envelope。
5. 所有后台操作支持真实进度、真实取消、失败恢复和关闭窗口后的资源回收。
6. Graphify 能识别 Python 字符串中的 HTML/JavaScript，并建立“控件、处理器、API method、V2 surface、native handler”完整调用链。
7. MemoryGuard CodeGraph 接收 Graphify 的安全元数据投影，提供可信 scope 下的 query/path/explain/affected 能力，不保存源码正文。

## 2. 硬验收标准

```text
GUI registry: 162 implemented / 0 retired / 0 blocker / 0 unknown
visible controls mapped: 100%
visible controls calling retired/blocker/unknown: 0
mutation registry diff: 0
localhost vs pywebview envelope diff: 0
V2 runtime legacy Store imports: 0
cancelled jobs still running after timeout: 0
GUI close leaves owned worker/thread/process: 0
Graphify embedded-GUI controls and callApi edges found: 100%
production-only path crosses tests/fixtures: 0
full regression failures: 0
```

任何一项未满足，不得宣称 `production_complete=true`，不得发布。

## 3. 已确认架构边界

### 3.1 必须复用

- V2 surface 权威入口：`src/memoryguard/cutover_v2/surfaces.py`。
- V2 native dispatch：`src/memoryguard/runtime_v2/native_ports.py`。
- 可信任务状态机：`src/memoryguard/runtime_v2/working_memory.py`，已有 `queued/running/succeeded/failed/cancelled`。
- 正文唯一入口：`ContentStore.upsert_source_connector`、`put_blob`、`upsert_occurrence`。
- Knowledge 读取：`KnowledgeV2ReadonlyService`；写入必须另建 command service，不能破坏 read-only 边界。
- Projection：`ProjectionStore.put_projection`、`tombstone`、`rollback` 与 `ScenarioProjector/ProfileProjector`。
- Rule：`RuleCreationService` 已具备 receipt-driven rule/exception 能力。
- Agent 发现：`AgentLocator.discover_candidates`、`get_selection_tree`。
- CodeGraph：`CodeGraphStore.put_edges`、`affected_query`、revision、outbox、ACL scope。
- V2 Manifest 四态和 no-dual-read/write 约束继续有效。

### 3.2 禁止模式

- 不得重新开放 legacy fallback。
- 不得让 Knowledge 页面继续“V1 读取、V2 写入拦截”。
- 不得在前端、`security.py`、surface、native port 各维护一份 method 清单。
- 不得用内存字典保存发布级 job 状态。
- `cancel_requested=true` 不等于取消成功；worker 必须终止并释放锁。
- 不得依赖或修改本机 `site-packages/graphify`；Graphify Core 已吸收到 `memoryguard.graphify_core`，后续改动、测试和打包均由 MemoryGuard 仓库负责。
- MemoryGuard CodeGraph 不得保存 source body、对话正文或不可信 authority。
- 不得用删除按钮或弱化文案规避迁移。

## 4. 统一 GUI operation contract

### 4.1 单一注册表

在 `cutover_v2/surfaces.py` 将散落集合收口为结构化 `GuiOperationSpec`。每项至少包含：

```text
public_name
canonical_name
domain
kind = read | mutation
execution = sync | task
native_handler
cancel_operation
idempotency
confirmation
```

由注册表生成：

- `GUI_METHOD_NAMES`
- `GUI_MUTATION_NAMES`
- `security.py` allowlist 兼容导出
- SafeBridge registry payload
- native handler coverage
- HTTP allowlist
- 文档清单与测试参数

重复旧名称使用 `canonical_name` 指向同一 V2 operation。public method 仍返回成功能力，不进入 retired 分支。

### 4.2 统一返回 envelope

同步操作：

```json
{
  "ok": true,
  "status": "succeeded",
  "operation": "knowledge_source_add",
  "data": {},
  "receipt": {}
}
```

后台操作：

```json
{
  "ok": true,
  "status": "accepted",
  "operation": "projection_build",
  "task": {
    "run_id": "...",
    "state": "queued",
    "progress": 0,
    "cancellable": true
  }
}
```

失败统一返回 `error.code/message/details`。HTTP 状态、pywebview 返回和 compat facade 必须语义一致。

## 5. retired 能力迁移矩阵

| 能力域 | 现有 retired 名称 | V2 canonical 落点 | 必须保留的用户能力 |
|---|---|---|---|
| Knowledge | `knowledge_add/reingest/rebuild_smart/remove/restore/purge_deleted/update_settings/candidate_review/candidate_targets/job_status/deleted_list` | 新 `KnowledgeV2CommandService` + `ContentStore` + Knowledge metadata projection + `TaskRun` | 文件/文件夹导入、重扫、候选治理、回收站、设置、进度、取消 |
| Projection/build | `build_projection/start_build_projection/get_build_progress/cancel_build_projection/delete_projection/get_projection_source_map/set_projection_source_enabled` | `ProjectionBuildService` + `ProjectionStore` + `TaskRun` + outbox | 构建、LLM 整理、进度、真实取消、删除、来源开关 |
| Build plan/release | `create_build_plan/apply_build/publish_reconstructed_memory/rollback_native_memory_release/list_native_memory_releases/verify_release/choose_publish_target_path/list_publish_targets` | V2 plan record + immutable release receipt + Projection/Memory transaction coordinator | 预览、确认、发布、验证、回滚 |
| Agent discovery | `discover_agents/get_selection_tree/get_agent_data/list_agents/list_agent_candidates` | `AgentLocator` 只读 adapter + V2 scoped DTO | 展示全部可用 Agent、名称、来源、能力、选择树 |
| Agent lifecycle | `archive_agent_dir/delete_archived_agent/open_agent_folder/restore_archived_agent/mark_agent_uninstalled/unmark_agent_uninstalled/list_archived_agents` | V2 system operation service + desktop executor capability | 归档、恢复、打开目录、卸载标记 |
| Shared groups/binding | `bind_agent/unbind_agent/bind_agents_to_shared_group/ensure_personal_memory_group/leave_shared_group_to_personal/dissolve_shared_group/check_binding_drift/get_shared_group_preview/set_governance_scope/commit_shared_memory_governance/install_shared_group_mcp_redirects` | V2 binding/scope tables + manifest/hook installer + transactional receipts | 成员增删、重建组、个人组、治理范围、MCP 重定向 |
| Governance | `get_conflicts/get_quarantine/resolve_conflict/release_quarantine/delete_quarantine/get_auto_actions/get_memory_ir/neuron_decide/rollback_memory/get_recent_events/get_supersede_decisions` | V2 Rules/Evidence/Binding/Memory governance service | 冲突、隔离、自动动作、神经图决策、回滚、审计事件 |
| Rule exceptions | `create_child_exception/create_rule_exception/revoke_rule_exception` | `RuleCreationService` receipt-driven exception API | 新建、撤销、作用域校验、证据链 |
| Audit/plan | `generate_plan/apply_plan/undo_change` | V2 audit result + persisted plan/task + inverse receipt | 生成方案、确认应用、可验证撤销 |
| History/backfill | `discover_local_history_sources/backfill_local_history` | provider adapter + `ConversationSync/ContentStore` + task/outbox | 来源发现、增量回填、断点恢复、取消 |
| Import/maintenance | `create_import/preview_source/get_source_file_content/plan_memoryguard_gc/apply_memoryguard_gc/list_cleanup_history/get_residual_cleanup` | V2 import service + bounded safe preview + maintenance service | 预览、导入、GC 计划、执行、历史、残留检查 |
| Hook/mode | `set_host_hook_mode/get_host_hook_status/uninstall_host_hook/enter_multi_agent_mode/exit_multi_agent_mode` | V2 system manifest + host-hook installer receipts | 模式切换、状态、安装/卸载、失败恢复 |
| Request queue compatibility | `submit_request/get_request_status/list_pending_requests` | 直接映射 `request_mutation` 与 `TaskRun` | 旧客户端兼容，不保留第二套 RequestQueue |

## 6. 分阶段实施

### Phase 0：契约与失败门禁

实施：

1. 新增 GUI operation ADR，冻结上节字段和 envelope。
2. 从渲染后的主 GUI、Knowledge GUI 提取所有可见控件、handler、`callApi/api/detailApi` method。
3. 新增失败测试：可见 method 必须存在于 registry，且 native status 为 implemented。
4. 修改 readiness：`visible ∩ {retired, blocker, unknown}` 非空即失败。
5. 生成 162 项 migration ledger；每项记录 public name、canonical name、实现阶段、测试 ID。

验证：测试必须先稳定复现当前 48 个主 GUI retired method、9 个 Knowledge retired method，然后随迁移逐项清零。

### Phase 1：注册表、bridge、transport 收口

实施：

1. 建立 `GuiOperationSpec` 单一来源。
2. `security.py` 改为兼容导出，不再拥有独立 truth source。
3. SafeBridge、HTTP handler、facade、native dispatch 全部从同一 spec 做方法校验、mutation 分类和参数绑定。
4. 删除 HTTP 对旧 RequestQueue 的旁路；兼容 method 转入 `TaskRun`。
5. `_phase9_gui_payload` 改为按 spec 参数 schema 绑定，覆盖 Knowledge 查询、书库、候选和 task 参数。

验证：162 项 transport 参数化测试；同输入的 HTTP/WebView/compat/native envelope 深度相等。

### Phase 2：统一后台任务与取消

实施：

1. 用 `runtime_v2.working_memory.TaskRun` 替换 GUI 内存 `_build_jobs` 和 Knowledge 私有 job 状态。
2. 新建 task coordinator，负责状态转换、progress event、cancel token、异常 receipt、进程/线程 ownership。
3. 取消流程必须检查 token、停止下一阶段、等待 worker 退出、关闭 SQLite connection、释放文件锁，再返回 `cancelled`。
4. GUI 关闭执行有界 shutdown；未完成任务可恢复，不留后台扫描。

验证：构建、Knowledge 导入、history backfill、import 四类任务覆盖 queued/running/succeeded/failed/cancelled/restart recovery。取消后 5 秒内无 owned worker、无 DB 锁。

### Phase 3：Knowledge 完整迁移

实施：

1. 保持 `KnowledgeV2ReadonlyService` 只读；新增独立 command service。
2. “添加文件夹或文件”流程：路径选择、安全校验、source connector、scan、blob/occurrence 写入、Knowledge reference projection、task receipt。
3. `reingest/rebuild_smart` 只重建可派生索引，不复制正文进 knowledge.db。
4. `remove/restore/purge_deleted` 使用 tombstone、hold/reference audit，避免正文孤儿。
5. candidates review/targets 使用可信 scope；书库 DTO 同时满足列表、详情、搜索。
6. Knowledge GUI 全部切到 SafeBridge；检查 HTTP 状态、显示结构化错误、轮询 task、支持取消。

验证：文件、嵌套文件夹、空目录、大目录、重复内容、删除恢复、重建、取消、重启恢复、非法路径、scope 越权。

### Phase 4：Projection、构建、发布与回滚

实施：

1. 新建 `ProjectionBuildService`，组合 `ScenarioProjector/ProfileProjector` 与 `ProjectionStore`。
2. build plan 保存输入 digest、scope、Agent、LLM provider、来源水位和预期输出，不再写临时 legacy plan 文件。
3. apply/build 使用 task coordinator；每阶段提交 checkpoint 和 outbox。
4. delete/toggle source/rollback 使用 generation 与 immutable receipt。
5. cancel 必须中断扫描、LLM 调用后的后续提交和投影写入；已提交阶段按 receipt 回滚或标记可恢复。

验证：成功、LLM 失败、取消、重复点击、并发构建、关闭窗口、重启恢复、发布验证、回滚一致性。

### Phase 5：Agent、共享组与治理范围

实施：

1. `AgentLocator` 提供发现和选择树，只输出安全 DTO；GUI 始终显示 Agent 名称，ID 仅作次级标识。
2. 将 binding、group、governance scope 写入 V2 权威表；变更与 outbox/receipt 同事务。
3. 添加/删除成员、解散、离组、个人组、MCP redirect 安装使用同一 group command service。
4. 所有组操作幂等；重复创建返回现有组和 changed=false，不返回笼统失败。

验证：已有组成员增删、重复创建、Agent 重命名、卸载/恢复、binding drift、部分 hook 安装失败回滚。

### Phase 6：Governance 与规则例外

实施：

1. 新建 V2 governance query/command service，直接组合 Rules、Evidence、Binding、Memory V2 stores。
2. conflict/quarantine/supersede/rollback/neuron decision 使用 canonical rule ID 和完整 evidence/binding receipt。
3. 将 `create_child_exception/create_rule_exception/revoke_rule_exception` 接到既有 `RuleCreationService`，不复制算法。
4. 所有决策更新生命周期 outbox，同事务驱动神经图投影。

验证：重复规则自动合并、独立约束覆盖、负证据、作用域冲突、撤销、回滚、神经图实时一致。

### Phase 7：History、Import、Maintenance、Hook

实施：

1. History backfill 使用 provider adapter、`ConversationSync` 和 Content ingestion。
2. Import 使用 preview/confirm/task/receipt 四段式；读取正文只允许显式、安全、有限预览。
3. GC 使用 V2 maintenance plan/apply，不跨域直接删除。
4. Agent archive/open/restore 和 Hook 安装卸载进入 desktop executor capability，保留平台差异和错误详情。
5. mode 切换写 manifest receipt，并验证实际 hook/MCP 状态后才显示成功。

验证：跨平台路径、权限失败、部分成功回滚、重复执行、关闭窗口、重启恢复。

### Phase 8：GUI 状态与交互闭环

实施：

1. 主 GUI 和 Knowledge GUI 使用统一 API client、task poller、error renderer、confirmation modal。
2. 每个按钮明确定义 idle/loading/accepted/running/cancelling/succeeded/failed/cancelled。
3. 页面切换不丢失 active task；重新进入页面从 `TaskRun` 恢复。
4. “全部丢弃”等文案必须执行真实动作；禁止只导航却提示成功。
5. 建议将 JS/CSS 移出 Python 巨型字符串，构建时打包回桌面资源；即使完成拆分，Graphify 仍须支持嵌入式语言。

验证：逐按钮点击测试；失败状态不得卡 loading；取消不得先报成功；刷新/切页/重启状态一致。

### Phase 9：Graphify 嵌入式 GUI 抽取

Graphify Core 以 Graphify 0.9.19（MIT）为来源基线，现已作为 MemoryGuard 内置代码图引擎维护在 `src/memoryguard/graphify_core/`。运行时不得依赖外部 `graphifyy` distribution、PATH 中的 `graphify` CLI 或本机私有 wheel；上游项目仅作为许可证归属和未来参考来源。

实施：

1. 新增 `graphify/extractors/embedded.py`；从 `extract_python()` 的确定性 AST pass 识别 HTML/JS 字符串区域，生成带 source-map 的 virtual document。
2. 复用现有 Svelte/Vue/Astro 的“markup 内再解析 JavaScript”模式；提取 `button/a/[role=button]/onclick/data-*` 控件和 `<script>`，JavaScript 片段继续复用现有 Tree-sitter JavaScript extractor。
3. 第一版沿用现有 `references` relation，通过 `context=control_handler|handler_api|api_surface` 和 `metadata.semantic_kind` 表达逻辑语义，避免破坏旧图消费者：
   - `control --references[control_handler]--> handler`
   - `handler --references[handler_api]--> api_method`
   - `api_method --references[api_surface]--> surface_spec`
   - `surface_spec --references[api_surface]--> native_handler`
4. virtual node ID 必须稳定，包含宿主文件、宿主 symbol、区域序号和内容 hash；行号映射回宿主 Python 文件。
5. 为 node/edge 增加 `provenance=production|test|fixture|generated|vendor|unknown`。复用 `paths.py` 现有 test-path 判定；embedded 节点继承宿主文件 provenance。
6. `query/path/explain/affected` 增加 provenance filter；过滤必须发生在 seed scoring 前。默认行为兼容旧图，`--provenance production` 必须完全排除 test/fixture/unknown。
7. 保持普通 Python/JS 图结果兼容；嵌入抽取失败只记录诊断，不能破坏整个仓库构图。
8. 修正 edge dedup key；当前仅按 `(source, target, relation)` 去重，必须纳入 `context/provenance/source_location`，否则不同 GUI 语义边会被吞掉。

MemoryGuard Graphify Core 文件边界：

```text
src/memoryguard/graphify_core/
├── engine.py      # source discovery + deterministic structural extraction
├── embedded.py    # Python-hosted HTML/JavaScript + GUI semantic chain
├── export.py      # body-free MemoryGuard CodeGraph metadata envelope
├── LICENSE.graphify.txt
└── NOTICE.md
```

Graphify 原项目的 CLI/wiki/viz/MCP/report/transcribe 等外围产品能力不进入 MemoryGuard 运行时；CodeGraph query/path/explain/affected 继续由 `CodeGraphStore` / native runtime 提供。

验收 fixture：`interactive.py`、`knowledge_gui.py`、`gui.py` 的真实嵌入式页面。必须能查询 `addBook`、中文按钮文本、`knowledge_add`，并得到只经过生产节点的完整路径。压测约 218,908 字符 inline JavaScript；设置每文件 fragment 数和单 fragment 字节上限，超限输出诊断，不能拖垮全仓构图。

### Phase 10：MemoryGuard CodeGraph 完整投影与查询

实施：

1. 新增 Graphify export adapter，只接收 path/hash/language/symbol/edge/source-role/source-map 元数据。
2. 投影进 `CodeGraphStore`，保留 ACL、revision、outbox、tombstone、幂等 hash；继续拒绝 source body。
3. CodeGraph schema 增加 source role 与必要 edge metadata；提供向前迁移和旧 DB preflight。
4. native runtime 增加受可信 scope 保护的 query/path/explain/affected/update/status canonical operations。
5. GUI 验收器读取 CodeGraph semantic edges，并和 `GuiOperationSpec` 做交叉检查。
6. 内置 Graphify Core 导入/解析异常时返回明确 capability error；不得回退到外部 Graphify、不得伪造空图或 `production_complete`。

验证：权限隔离、revision 幂等、增量更新、删除 tombstone、source-map、production filter、bounded path/affected、旧 schema 升级、内置 Graphify Core 故障闭合、干净 wheel 安装后无 `graphifyy` distribution 仍可构图。

### Phase 11：总验收、文档与发布

1. 执行 GUI capability ledger，要求 162/162 implemented。
2. 执行主 GUI 与 Knowledge GUI 全按钮点击测试。
3. 执行 transport parity、task lifecycle、Reference Audit、SQLite integrity/FK/outbox 检查。
4. 执行所有 V2 与非 V2 回归、打包安装测试、桌面真实启动测试。
5. 使用 `memoryguard.graphify_core.export_repository()` 对真实仓库构建，再经 MemoryGuard query/path/explain/affected 验证新语义链；发布环境不得调用外部 `graphify` CLI。
6. 更新 README、README.zh-CN、CHANGELOG、release notes 和 V2 implementation status；不得继续报告“89 retired + production_complete”。

## 7. 测试门禁

### 7.1 静态契约

- 渲染 HTML 后提取所有可见 controls。
- JavaScript AST 提取 handler 与 API method。
- registry 验证 method 存在、implemented、mutation 分类正确。
- native handler 必须可解析，不得落入 retired/unsupported。
- 无未使用 compatibility alias；无 GUI 直接导入 legacy Store。

### 7.2 transport 参数化

每个 public method 至少覆盖：

```text
SafeBridge direct
pywebview bridge
localhost HTTP
compat facade
native dispatch
```

检查参数、确认门、身份/scope、status、error code、receipt 完全一致。

### 7.3 用户旅程

- Knowledge：添加文件、添加文件夹、进度、取消、删除、恢复、重建。
- Build：选择 Agent、开始、切页恢复、取消、失败、重试、发布、回滚。
- Group：创建、重复创建、添加成员、删除成员、解散、个人组恢复。
- Governance：冲突、隔离、合并、例外、撤销、回滚、神经图同步。
- History/Import/Hook：发现、预览、执行、失败恢复、卸载。

### 7.4 性能与资源

- 大目录导入不能阻塞 GUI event loop。
- CodeGraph 增量更新只重建内容 hash 变化文件。
- path/affected 有 depth、limit 和确定排序。
- 取消与关闭窗口后进程、线程、SQLite connection、文件锁全部回收。

## 8. 并行派工建议

Phase 0–2 必须串行完成，先冻结契约与 task 基础设施。之后可并行：

| Worker | 文件/职责边界 | 依赖 |
|---|---|---|
| Luna A | Knowledge V2 command service、Content ingestion、Knowledge tests | Phase 0–2 |
| Luna B | Projection/build/release/task integration | Phase 0–2 |
| Luna C | Agent/group/binding/scope | Phase 0–2 |
| Luna D | Governance/rule exceptions/outbox | Phase 0–2 |
| Luna E | History/import/maintenance/hook | Phase 0–2 |
| Luna F | Graphify upstream embedded extractor、provenance、query filters | Phase 0 contract |
| Luna G | MemoryGuard CodeGraph adapter/schema/native queries | Graphify export contract |
| Luna H | GUI client、状态机、逐按钮 wiring | 各 domain API 稳定后分批接入 |
| Sol | 架构决策、冲突收口、每阶段审查、最终验收 | 全程 |

所有 worker 使用独立文件边界，不得修改他人职责文件；先补测试，再实现。每阶段由主线程审核，未通过不得进入下一阶段。

## 9. 发布与回滚

1. 本地先迁移 schema 与 capability ledger，不直接覆盖用户 DB。
2. 对真实 workspace 做 online backup、preflight、dry-run、fresh shadow 验证。
3. 新 Graphify 版本先独立发布并固定最低兼容版本；MemoryGuard 不依赖本机临时补丁。
4. 激活前保存 manifest、schema、registry、Graphify export digest。
5. 回滚恢复上一 generation 和 manifest，但不恢复 V1 runtime 路径。
6. 发布后监控 task backlog、cancel latency、outbox、SQLite lock、GUI operation failure code。

## 10. 停止条件

遇到以下任一情况立即停止发布并修复：

- 任一可见控件命中 retired/blocker/unknown。
- 任一 transport 绕过统一 registry。
- 任一 V2 handler 导入或实例化 V1 Store。
- Knowledge 正文进入 knowledge.db 或 CodeGraph DB。
- mutation 与规则/投影 outbox 非同事务，或出现孤儿引用。
- 取消后 worker 仍运行、数据库仍锁定。
- Graphify production path 穿过 tests/fixtures。
- readiness 仍可在 GUI retired 非零时返回 production complete。
- 全量回归、Reference Audit、SQLite integrity、真实桌面点击任一失败。
