# Phase 7：安全 GC、压缩与可观测性契约

## 结论与边界

Phase 7 已包含维护控制面与受控执行面：12 域 Reference Audit、两 Epoch mark/sweep、维护租约、generation CAS、幂等请求、存储报告、安全 Blob Sweep、增量 vacuum 与深度 compact。`MaintenanceStore` 将账本放在固定路径 `.memoryguard/system/maintenance.db`，不改写 Phase 1 的 `manifest.db`，也不在业务库新增维护表。

默认路径仍为零物理副作用。只有 `V2_ACTIVE`、ACTIVE 非 dry-run Job、精确 lease/generation、可信 writer-quiescence 与 outbox-drained 证明同时成立，执行器才可删除稳定孤儿 Blob 或运行 SQLite 物理维护；任一证明缺失即失败封闭。

所有请求默认 `dry_run=true`。报告和审计可在 `V2_BUILDING`、`V2_READY` 阶段运行；真实 sweep/compact 必须同时满足：

1. manifest 为 `V2_ACTIVE`；
2. 维护 Job 为 `ACTIVE` 且 `dry_run=false`；
3. 调用方持有未过期 maintenance lease，且 owner/scope 完全匹配；
4. `expected_generation` 与本次 `RuntimeSnapshot` generation 相等；
5. 所有引用库、Workspace 清单和 outbox 审计成功，否则整轮失败封闭。

## 状态机

```text
PLANNED → AUDITING → READY
    │         │         ├─→ ACTIVE → SUCCEEDED/FAILED
    │         ├─→ FAILED └─→ SUCCEEDED/FAILED/CANCELLED
    └─→ CANCELLED
```

`ACTIVE` 只允许 `sweep` 和 `compact`。`audit`/`report` 不需要激活，也不得产生物理副作用。Reference Epoch 为 `OPEN → COMPLETE|FAILED`；同一 Job 的 Epoch N+1 只有在 Epoch N 完整成功后才能打开。Candidate 正常路径为 `MARKED → CONFIRMED → DELETING → SWEPT`；`DELETING` 是跨数据库删除的持久恢复点，内容事务失败时补偿回 `CONFIRMED`，并发引用则转为阻断而非删除。

## 权限与可信上下文

`MaintenanceScope` 是精确的 workspace/agent/project/provider/share-group/runtime ACL 元组；空字符串是明确值，不是 wildcard，`__UNKNOWN__` 和未知字段均拒绝。写操作必须使用不可变、显式 `trusted_context=true` 的 `MaintenanceContext`，并带非空 actor。Lease 绑定 scope digest 与 actor；不能用另一个 workspace、owner 或过期 lease 重放。

控制面只保存 digest、ID、状态、计数和结构化原因。禁止正文、Blob、向量、secret、token、ACL 任意 JSON 或命令字段进入维护模型/报告。

## P7 契约加固（maintenance ledger）

- workspace、`.memoryguard/system` 和数据库路径在任何 `exists()`/resolve 前先检查目录项；包括 dangling symlink/reparse point，均拒绝打开。
- 传入已构造的 `WorkspaceV2Layout` 时，调用方还必须提供原始 `source_workspace`；缺失或与 layout 不一致即拒绝，防止 layout 预先 `resolve()` 后丢失 symlink 证据。
- 维护数据库执行 exact-schema preflight：必需表和列必须完整，unknown/partial/future schema 一律 `MaintenanceSchemaError`，不迁移、不猜测。所有读入口（含坏 JSON、坏行数据）都稳定失败为该错误。
- Job 持久化精确 scope、actor 和 context digest。`transition`、epoch、candidate、report 的写操作必须由同一 trusted context 所有者发起；没有隐式 admin 绕过。Lease owner 固定等于 `context.actor`。
- Job 的 `FAILED`、`SUCCEEDED`、`CANCELLED` 与 Epoch 的 terminal 状态不可重开；任何 same-state replay 也先做 owner 校验并通过 CAS 语义。Epoch replay 必须匹配原 `reference_digest`；只有 `OPEN` epoch 可 mark/confirm/complete。
- counts/safety 仅接受有限深度/节点数/字符串和 JSON 大小的结构化元数据；`body`、`secret`、`control`、`control_payload`、`authority`、`admin` 等字段及其变体拒绝写入。

## 依赖矩阵

| 依赖 | 用途 | 失败处理 |
| --- | --- | --- |
| `WorkspaceV2Layout` | 固定 system 路径与 reparse containment | 拒绝写入 |
| `connect_database` / `transaction` | FK、WAL、显式事务 | 回滚并失败封闭 |
| Manifest `RuntimeSnapshot` / generation | Active 与 CAS 门禁 | 不可读或 generation 冲突即拒绝 |
| 全部注册引用库/Workspace 清单 | Reference Audit | 任一缺失或不可读则零 Sweep |
| Holds / Outbox | Candidate 与执行之间的竞态保护 | CAS 失败，不删除 |
| SQLite `integrity_check`、`wal_checkpoint`、`incremental_vacuum` | 安全点维护 | 默认报告；显式 apply 仍需完整门禁 |

注意：当前 `WorkspaceV2Layout` 的固定数据库列表不含 `skills/skills.db`。Reference Audit 执行器必须维护显式注册表；注册表不完整时必须 fail-closed，而不是猜测路径。

## 边界矩阵

| 场景 | 结果 |
| --- | --- |
| 空正文、正文、向量或 secret 进入控制字段 | 拒绝；本库无正文列 |
| readonly 且 maintenance.db 不存在 | `FileNotFoundError`，不创建目录/文件 |
| schema marker 缺失、未知或 future version | `MaintenanceSchemaError`，不迁移 |
| reparse/symlink workspace、system 或 db | `LayoutError`，不打开写连接 |
| 幂等 key 同请求重放 | 返回原 Job/Receipt |
| 幂等 key 不同 operation/context/generation | `MaintenanceConflictError` |
| Job/Candidate/Epoch 并发更新 | SQL CAS 或唯一约束失败，事务回滚 |
| Lease 过期、owner/scope 不同 | `MaintenanceLeaseError` |
| Partial/failed source 或未消费 outbox | 只报告，不 Sweep |
| Candidate 与 Sweep 间新增 Hold | 执行器 CAS 失败，不删除 |

## 停止条件

任一条件成立，停止当前阶段并回到旧读路径：未授权内容或计数泄漏；Golden Query Recall@5/中文短查询回归；迁移或业务摘要不一致；Partial Scan 使来源失活；Evidence/Revision/Occurrence 引用的 Blob 被清理；任一 Version 无法重建相同 State Digest；Delta 与当前状态无法同事务提交；Python 3.10/3.12 CI 失败；项目移动、恢复或回滚需要人工改库。

## SQLite 维护顺序

安全点可报告或显式执行 `PRAGMA wal_checkpoint(PASSIVE)`、`PRAGMA optimize` 和 `PRAGMA incremental_vacuum`（参见 [SQLite PRAGMA](https://www.sqlite.org/pragma.html)）。深度压缩取得全局维护锁、拒绝新写入、Drain Outbox、执行 `integrity_check`、`VACUUM INTO` 临时文件、校验 schema/行数/摘要/权限样本后原子替换（参见 [SQLite VACUUM](https://www.sqlite.org/lang_vacuum.html)）；Windows 下所有 SQLite 句柄均在替换前关闭，失败保留可恢复副本。

## API、CLI 与验收

- Python API：`MaintenanceV2Api.audit_references()`、`storage_report()`、`sweep()`、`compact()`；写操作只接受真正的 `MaintenanceContext`，不接受 Mapping 伪造权限。
- CLI：`memoryguard storage audit|report|lease-acquire|lease-release|sweep|compact`。READY 只允许读；所有会写维护账本或业务库的动作均由 Phase 6 门禁判定为 mutation。
- `sweep --apply` 和 `compact --apply` 在生产 verifier 未注入时返回稳定错误，绝不把调用方布尔值当成停写/出站清空证明。
- `scripts/accept_v2_phase7.py` 默认只读且单 JSON；`--self-test` 只在临时工作区验证 12 域、Hold 优先、双 Epoch Sweep、跨库恢复和 `VACUUM INTO`。
