# MemoryGuard V2 实现状态（2026-08-12 v0.7.0 V2-only 发布候选）

本文记录真实 workspace 与当前迁移分支的已验证状态，不替代 manifest、validator、
Reference Audit 或 readiness receipt。下方 `V2_ACTIVE` 是已有 workspace 的历史激活
快照；v0.7.0 主验收仍在进行，不把本文件当作当前全仓全绿声明：

```text
state = V2_ACTIVE
generation = 11
migration_id = prepare-1d6384875b804527bb286d0563f736cf
main_acceptance = IN_PROGRESS
```

激活继承的不可变摘要：

- source digest: `d9bc6555716164caf49f67be9fd4cace2570828b3e1c3479ced737fb34936eba`
- target digest: `cdcb67ea8b4f0869558825f224007c62249db7df59472d811752b3ef644ffc09`
- manifest digest: `c54ec5aad6be9348ff160a77605ad7893b21450475eba50103ae4b83cc7a47b4`
- readiness evidence digest: `c7d6060d25fbbccf16d0e315aee491de04268cd23cdfb59ed173283d064c0549`

## 阶段状态

| 阶段 | 状态 | 生产验证 |
| --- | --- | --- |
| Phase 0/1 | PASS | 架构、四态 manifest、存储与权限契约通过 |
| Phase 2 | PASS | live V1 → online backup → frozen source → fresh V2 migration → validator |
| Phase 3 | PASS | 构建期 V1 primary / V2 shadow 隔离，失败不污染 V1 |
| Phase 4 | PASS | ContextEngine mandatory/recall/scope/token/determinism 验收通过 |
| Phase 5 | PASS | Content、Assets、Skills、Knowledge reference、CodeGraph、Projection production schema 就绪 |
| Phase 6 | PASS | MCP / CLI / GUI / Hook 全部通过四态 native cutover |
| Phase 7 | PASS | 12-domain Reference Audit、Maintenance、integrity/FK、双 Epoch 验收通过 |
| Phase 8 | PASS | activation/native smoke/rollback rehearsal 通过 |
| Phase 9 | PASS | Rule lifecycle、RuleMerge、Extraction/Enrichment、External MCP、Provider 与 GUI governance 原生化完成 |
| Production activation | **PASS** | `V2_READY → V2_ACTIVE` 前 fresh live-source verification PASS |

### 2026-08-11 GUI V2 / CodeGraph 完整迁移增量

| 计划阶段 | 状态 | 当前证据 |
| --- | --- | --- |
| Phase 6 Governance | PASS | Governance native 53 项专项通过；相关 GUI blocker 全清零 |
| Phase 7 History / Import / Maintenance / Hook | PASS | discover/backfill、import、GC、Hook mode/uninstall、raw/source preview、request compatibility 全部 native implemented |
| Phase 8 GUI 状态机 | PASS | 持久 `TaskRun`、轮询、取消、恢复、统一错误/envelope 专项与全量回归通过 |
| Phase 9 Graphify embedded GUI | **FUNCTIONAL PASS / PACKAGING BLOCKED** | embedded/provenance 测试 15 项通过；真实 GUI production-only semantic chain 完整；但当前 Graphify 目录仍是 sparse overlay，不是完整 upstream source checkout |
| Phase 10 CodeGraph | PASS | metadata-only adapter、schema v2、source role/provenance/source-map、query/path/explain/affected/update/status、GUI semantic cross-check 全部通过 |
| Phase 11 回归 / 打包 | **RECORDED EVIDENCE / MAIN ACCEPTANCE IN PROGRESS** | 迁移候选曾记录 1761 测试通过、专项 gate 与干净 sdist→wheel；这些是历史候选证据，不是当前全仓全绿声明；发布仍等待主验收与 Graphify upstream source-tree package gate |

## Native coverage

当前 registry 总计 **239** 个 surface：

- implemented: **233**
- retired: **6**
- neutral-read: **0**
- blocker: **0**

按 surface（implemented / retired / blocker）：

- MCP：`57 / 0 / 0`
- GUI：`162 / 0 / 0`
- CLI：`13 / 6 / 0`
- Hook：`1 / 0 / 0`

GUI 硬门禁已达到 `162 implemented / 0 retired / 0 blocker / 0 unknown`。
当前 6 个 retired 仅是旧 CLI `plan/apply/verify/undo/import/gc` compatibility 命令，
不会写回 legacy store；产品可见 GUI 不再存在 retired 能力。

registry / coverage digest：

`45d1b85b4353532a843baf5da2a5e0752d2e7d60b9455ede6e69c8e39ddc3ee1`

`NativeV2RuntimePort.coverage()` 的本地 coverage 布尔值只描述 native registry 本身，
**不等同于本轮迁移计划的发布完成声明**。当前 Graphify upstream source/package
硬门禁未通过，因此不得据此宣称 production complete。

## V2-only 控制面与治理边界

- **V1 runtime retirement：** 生产入口的导入闭包不再包含 V1 runtime/store；
  `V1_ACTIVE` 仅表示尚未完成显式迁移。旧格式 reader 只存在于
  `memoryguard.migration`，其他入口遇到非 V2 状态返回 `v2_upgrade_required`。
  V1 数据与 migration-backups 保留为迁移回滚/审计证据，不参与 V2 runtime 写入。
- **六个 V2 面：** Memory、Evidence、History、Source、Binding、Group。Memory
  atom、evidence/decision receipt、raw History、授权来源、Agent binding 与
  share-group membership 分域治理；Group 是跨 Agent 同组去重与治理边界。
- **Canonical reconciliation：** 规则按 `shared_baseline`、`agent_overlay`、
  `project_overlay` 形成 canonical bundle，保留 source link；完成 parity 与
  outbox/readiness 后才启用 canonical read，再可恢复地 shadow 重复源。
  V2 自动整理做同组 exact/semantic dedup；RuleMerge 自动扫描产生 merge
  proposal，merge、supersede、conflict、quarantine 均经 V2 evidence/decision
  receipt，不能静默改 audience 或覆盖旧值。
- **Knowledge：** 文件夹作为 book、文件作为 document；Content Plane 独占正文，
  Knowledge metadata/reference 不重复存正文，re-ingest、remove/restore/purge
  与 candidate review 走 V2 TaskRun/治理路径。
- **GUI：** Agent 名称/实例发现、来源选择、Binding 与 Group 成员管理、drift、
  personal/shared group、leave/dissolve 都由 system control 提供。构建、知识、
  import、history、maintenance、release 和兼容请求使用持久 `TaskRun`；取消、
  恢复与 shutdown 必须收尾 owned worker/process。
- **CodeGraph / Graphify：** 只接受可信、无正文 metadata export；保留 source role、
  provenance、source map、revision、tombstone、outbox，提供 bounded
  query/path/explain/affected 与 production-only 过滤。
- **安全：** unknown/corrupt state、缺失 scope、非法 provenance、reparse path、
  不安全 metadata 与幂等冲突 fail-closed；公共 receipt 脱敏正文/路径，审计与
  rollback 使用可追溯的 evidence、hold、occurrence 和稳定 digest。

## 真实迁移与激活证据

生产迁移实际执行：

```text
live V1 SQLite
  → coherent online backup（支持非空 WAL）
  → immutable frozen source set
  → archive pre-existing dirty V2 shadow
  → fresh V2 Content/Memory/Rules/Evidence migration
  → outbox drain
  → validator
  → Runtime/Assets/CodeGraph/Projection/Skills/Maintenance 初始化
  → live-source drift verification
  → production readiness
  → V2_READY
  → 用户批准
  → fresh activation drift verification
  → V2_ACTIVE
```

激活前 live-source verification：

- status: `PASS`
- checked sources: `6`
- changed: `[]`
- missing: `[]`
- snapshot digest: `1f2f13e3de77906bd6b5cba8f906df72fbb78c162e11bed547eed3096d2732a8`

migration backups 与 legacy V1 数据均保留为本地回滚/审计证据，未删除。

## Readiness / Reference Audit

激活后再次执行 `scripts/accept_v2_readiness.py --workspace .`：

- status: `READY`
- blockers: `[]`
- loss: `0`
- orphan: `0`
- outbox: `0`
- scope leak: `0`
- binding diff: `0`
- unknown authoritative: `0`
- validator passed: `true`
- Reference Audit: `PASS`
- audited domains: `12`
- reference count: `42541`
- native coverage: `PASS`

## SQLite 生产健康检查

激活后对以下 12 个 authoritative domain 逐个运行：

```text
memoryguard storage report --domain <domain>
```

域：Runtime、Memory、Rules、Evidence、Content、Knowledge、CodeGraph、Assets、
Scenario、Profile、System、Skills。

全部结果：

- readable: `true`
- integrity_check: `ok`
- foreign_key_errors: `0`
- journal_mode: `WAL`
- 当前检查时 WAL pending bytes: `0`

`memoryguard storage audit -w .` 同时返回：

- status: `PASS`
- blocked: `false`
- blockers: `[]`
- domain_count: `12`

## 激活后 CLI 健康修复

生产激活后发现无 Agent host binding 的普通终端中，`doctor` / `mcp-status` 曾返回
`context_scope_required`。现已修复为安全的 workspace 级诊断：

- `doctor`: `ok=true`, `state=V2_ACTIVE`, `scope_status=UNBOUND`
- `mcp-status`: `ok=true`, `memory_status=READY`, `scope_status=UNBOUND`
- 未绑定终端不会返回 share-group ID、memory record count 或其他跨租户存在性信息。

有 Agent trusted binding 时仍使用严格 scoped native read。

## 回归与构建证据（历史候选记录）

下列数字来自 2026-08-11 GUI V2 / CodeGraph 迁移候选记录，属于可复核的专项
证据，不是 v0.7.0 版本切换后的当前全仓结果；主验收仍须重跑：

- 四个确定性 shard 曾覆盖全部 186 个 `tests/test_*.py` 文件，记录为
  **1761 passed / 0 failed**。
- Reference Audit / SQLite / outbox / task lifecycle：**106 passed**；transport /
  GUI registry / readiness：**104 passed**；Graphify embedded/provenance：**15
  passed**；GUI launcher / acceptance smoke：**12 passed**。
- 真实 Graphify MemoryGuard export 曾产出 5 个生产文件、1570 nodes / 2078 edges、
  diagnostics `[]`；`加入书架 → addBook → knowledge_add →
  GuiOperationSpec:knowledge_add → gui_knowledge_command` 全路径只经过
  `production` 节点/边。
- 干净 sdist → wheel → isolated import/entry-point 曾通过；重建 wheel 内
  `__pycache__=0`、`.pyc=0`。
- AST 扫描 `runtime_v2` / `cutover_v2` 曾记录 legacy `SharedMemoryStore` /
  `ManagedStore` import = `0`。

版本从 0.6.2 改为 0.7.0 后，必须重新执行版本一致性、V1 retirement
AST/subprocess、升级 fixture、主验收和支持矩阵 CI；本文件不把历史候选数字包装成
当前全仓全绿。

## v0.6.2 → v0.7.0 升级合同

```bash
python -m pip install --upgrade agent-memguard
memoryguard upgrade --workspace .                 # PREVIEW; zero-write
memoryguard upgrade --workspace . --apply         # V2_READY
memoryguard upgrade --workspace . --apply --confirm V2_ACTIVE
memoryguard doctor
```

若旧工作区使用独立 user data home，所有 upgrade 调用传入同一个
`--data-home <path>`。预览必须证明 `writes_performed=false`；apply 只经
`memoryguard.migration` 读取 legacy input，完成 frozen-source/live-source drift、
Agent/Group control、V2 validator 与 readiness 后停在 `V2_READY`。只有精确确认
`V2_ACTIVE` 才可激活；失败保持非 active，不得 fallback。V1 数据、migration-backups、
receipt 和审计证据在发布门禁解除前不得清理。完整步骤见
[docs/releases/v0.7.0.md](../releases/v0.7.0.md)。

## 当前发布结论与 release gate

已有 V2 workspace 的 production activation 快照仍有效，本轮也没有重新开放 V1
runtime fallback；**local release acceptance passed; ready for commit/publish, not yet
published**。本地证据：`1761 / 1761`，无 skip/xfail；retirement+CodeGraph `15 / 15`；
Graphify 专项 `3 / 3`；canonical `ACCEPTED`；RuleMerge `46 / 46`；v3.2 `27 / 27`；
真实全仓 Graphify export/projection：`486 files / 11672 nodes / 38714 edges → 11667
canonical symbols / 38714 edges`，query/path/affected 通过，失败原子性全 `0`；clean
wheel `206 files`、legacy bad `0`，隔离包/CLI/MCP `0.7.0`，desktop help 通过。
Graphify 证据仅指专项与真实全仓导出/投影，不声称 upstream Graphify 全仓测试通过。

必须同时满足以下 release gate：

1. **版本/制品：** `pyproject.toml`、`memoryguard.__version__`、CLI/package metadata
   一致；干净 sdist → wheel → isolated install/entry-point 通过。
2. **V1 retirement：** production entrypoint AST 与 clean-subprocess import closure
   无 V1 runtime/store；旧格式读取只在 `memoryguard.migration`；`V1_ACTIVE`、未知或
   损坏状态均 fail-closed。
3. **治理与范围：** canonical reconciliation 完成 source-link/parity/readiness；
   同组跨 Agent dedup/merge/supersede/conflict/quarantine 的 evidence、scope、
   idempotency、undo receipt 通过；跨 Group 不可见。
4. **GUI/Knowledge/Runtime：** Agent 名称/实例、Binding/Group 成员管理、文件/文件夹
   Knowledge、TaskRun status/cancel/recovery，以及 cancel/shutdown 后无 owned worker/
   process 残留均有验收证据。
5. **CodeGraph/Graphify：** metadata-only、body-free、provenance/source-map/revision/
   tombstone/outbox、production filter 与 query/path/explain/affected 通过；在完整
   upstream Graphify source checkout 中完成同等改动、其测试、package/install 验证。
6. **安全与主验收：** fail-closed、scope isolation、audit、rollback/hold/occurrence、
   upgrade-from-0.6.2 与支持矩阵 CI 全部通过并有 receipt。

当前 `H:/ai/workspace/graphify` 仍是已验证功能的 sparse overlay，没有完整 upstream
source checkout / `.git` / `pyproject.toml`；不得改已安装 `site-packages/graphify`
代替该门禁。门禁通过前不得删除迁移备份、清理 legacy 审计证据、手工改 manifest，也
不得把局部 native coverage 包装成 `production_complete=true`，更不得声称当前全仓已全绿。
