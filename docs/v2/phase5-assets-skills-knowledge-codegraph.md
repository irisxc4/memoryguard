# V2 Phase 5-C：Assets、Skills、Knowledge Reference 与 CodeGraph

Phase 5-C 扩展 V2 的只读资产索引和代码关系投影。它仍是 shadow build：
manifest 状态固定为 `V2_BUILDING`，`ready=false`、`can_promote=false`，不接入
Hook、MCP、GUI，也不改变运行时读写路径。

## 边界与复用

- `ContentStore` 是唯一正文入口。`KnowledgeV2Adapter` 只读
  `content.db` 的 `ContentReadScope` 命中行，不导入或实例化 V1
  `KnowledgeStore`。
- Knowledge adapter 的输出白名单只有 `summary`、`ref`、`hash` 和
  `trust=reference_only`。正文、authority、ownership、ACL、namespace 和
  “存在/不存在”状态不会进入结果。
- CodeGraph 复用 `WorkspaceV2Layout`、`open_database`、
  `execute_sql_script` 和 `transaction`。数据库固定为
  `.memoryguard/codegraph/codegraph.db`；没有 `ATTACH`、`executescript` 或
  第三方依赖。

## CodeGraph 数据模型

`source_files` 只保存 workspace-relative path、内容 hash、语言、source
revision 和 ACL。`symbols` 只保存 name/kind/signature/hash/line range；
`edges` 连接稳定 symbol ID。`revisions` 是追加式不可变版本，重复提交相同
hash/revision 是幂等 no-op，变更生成新 revision。删除生成 tombstone，旧版本
和证据不被覆盖。

每个读写都要求可信的完整 ACL：workspace、agent、project、provider、share
group、runtime role。未知 ACL 或未授权 scope 直接拒绝；路径必须相对且所有
已存在的父级都不能是 symlink/reparse point。

`outbox`、`checkpoints`、`affected_queries`、`migration_map` 和
`unknown_ledger` 记录跨库 saga 的 hash/ID 证据。affected 查询使用固定深度、
limit 和排序，结果确定且有界。数据库不保存源码正文。

## Legacy migration

`V1CodeGraphMigrator` 只用 SQLite `mode=ro` 打开显式指定的旧库，并先做完整性
检查和 source hash。仅选择安全元数据列；body/text/content/payload/vector 等
列不会进入内存映射。authority 或 ownership 缺失/未知的行写入
`unknown_ledger(status=BLOCKED)`，不生成可信节点/边。`migration_map` 的
source hash 不可变，源 hash 变化会 fail closed。

## Acceptance

```text
python scripts/accept_v2_phase5.py --workspace <workspace>
```

默认 dry-run 只读，不创建 `.memoryguard`。隔离 shadow 构建需要显式提供旧库：

```text
python scripts/accept_v2_phase5.py \
  --workspace <fixture> --source <legacy.sqlite> --write-shadow
```

脚本只输出一个机器 JSON，汇总 storage/content/assets/skills/knowledge/
codegraph 的 counts、orphan、loss、outbox、ACL anomaly、unknown ledger 和
迁移状态；无论数据是否完整都不会把 V2 标记为 ready。

## 停止条件

出现正文泄漏、scope 旁路、绝对/越界/reparse 路径、未知 authority/ownership
被提升、source hash 改写、非幂等 revision、无界 affected 查询、FK/orphan 或
outbox 故障时，Phase 5-C 必须停止在 `V2_BUILDING`，修复后使用新的 shadow
`migration_id` 重跑。
