# MemoryGuard V2 实现状态（2026-08-10 发布候选快照）

本文记录真实 workspace 已验证状态，不替代 manifest、validator、Reference Audit 或
readiness receipt。当前真实 workspace 已完成 V2 迁移和最终激活：

```text
state = V2_ACTIVE
generation = 11
migration_id = prepare-1d6384875b804527bb286d0563f736cf
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

## Native coverage

当前 registry 总计 **233** 个 surface：

- implemented: **138**
- retired: **95**
- neutral-read: **0**
- blocker: **0**
- `production_complete=true`

按 surface（implemented / retired / blocker）：

- MCP：`51 / 0 / 0`
- GUI：`73 / 89 / 0`
- CLI：`13 / 6 / 0`
- Hook：`1 / 0 / 0`

registry digest：

`2badd22a8fe3ef48a8be5c1a589f92caa75dc80c430dc51a7e1f0dc76cd8f65f`

`retired` 是正式安全终态，不等同于 implemented。V2 已明确替代的 V1 report
patch/release、旧 import/GC、SharedMemory group migrate、原生记忆发布、旧
KnowledgeStore 写入口、request queue/IR/quarantine 等不会静默 fallback 到 V1。

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

## 回归与构建基线

发布候选最新测试：

- `pytest -q tests/test_v2_*.py` → **589 passed**
- `pytest -q tests --ignore-glob='tests/test_v2_*.py'` → **1087 passed**
- 拆分全仓合计：**1676 passed / 0 failed**
- packaged `memoryguard-v2` + CLI doctor/mcp-status/cutover 专项：**80 passed**
- `python -m compileall -q src scripts` → PASS
- `git diff --check` → 仅既有 LF/CRLF warning，无 whitespace error

water 单次 shell 上限为 300 秒，因此全仓测试按 V2 / 非 V2 两批完整执行；两批覆盖
全部 `tests/test_*.py` 文件。

## v0.6.0 发布状态

V2 已完成 production activation，本文件对应 v0.6.0 发布候选。发布前不得：

- 删除 `.memoryguard/migration-backups`；
- 清理 legacy V1 数据；
- 手工修改 manifest；
- 通过旧 V1 store 绕过 native V2 路由。

允许的后续工作是正常 V2 运行、只读健康检查，以及在完整回归通过后发布 v0.6.0。
