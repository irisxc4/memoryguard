# ADR-0001: V2 content plane 与 evidence 引用分离

- 状态：Accepted for V2 Phase 1
- 日期：2026-08-08

## 决策

新增 `.memoryguard/content/content.db`，由 content 域承载 raw content、
conversation turns、content blobs 和 occurrences。`evidence.db` 只承载
证据引用、摘要和内容哈希，不保存 raw content、完整会话正文或 transcript。

## 原因

MemoryGuard 需要保留来源正文以便审计和重建派生索引，但正文不应因为被
evidence 引用就晋升为长期记忆。单独的 content plane 还能让 evidence 的
权限和生命周期保持轻量，同时避免继续把全文写入 history.sqlite。

## 约束与后果

1. Evidence 行必须能指向 content occurrence/blob，并带摘要或 hash；缺少
   引用时不能把摘要当作正文。
2. 任何全文检索、embedding 或摘要都是 content 的派生数据，不是 canonical
   memory。
3. 迁移期间可读取 V1 history 作为输入，但不得让 V1 history 成为 V2 的
   active read/write store。

## 验收

`accept_v2_phase1.py` 检查 content/evidence 路径、schema marker、SQLite
完整性，并扫描新 V2 目录中是否直接导入 ConversationHistory 或使用
`executescript`。
