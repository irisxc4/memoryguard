# MemoryGuard V2 文档索引

- [Phase 0：基线、测量与 Golden Query](phase0-baseline.md)
- [Phase 1：架构契约](phase1-architecture-contract.json)
- [实现状态矩阵](implementation-status.md)

# MemoryGuard V2 Phase 1 契约

本目录是 V2 第一阶段的机器可读边界。`phase1-architecture-contract.json`
是唯一的布局和状态机清单；ADR 解释为什么这样分层，验收脚本只验证
第一阶段已经承诺的能力，不把后续迁移能力伪装成已完成。

## 存储边界

V2 新增 `.memoryguard/content/content.db` 保存原始内容和会话正文。
`evidence.db` 只保存证据引用、摘要和哈希，因此全文不会因为证据链而
自动成为长期记忆。其他域各自持有独立 SQLite 数据库；projection 域有
`scenario.db` 与 `profile.db` 两个数据库，system 域的唯一数据库是
`manifest.db`。

DataHome 指针由 manifest 显式记录 workspace/global source pointer，并在
解析后做 containment 校验。指针丢失或越界时必须报告错误，不能猜测旧
路径，也不能静默搬运文件。旧的全局 knowledge 数据库只作为 V1 migration
reader 的来源。

## 切换语义

manifest 的状态是 `V1_ACTIVE`、`V2_BUILDING`、`V2_READY` 或 `V2_ACTIVE`。
只有 `V2_ACTIVE` 才能读 V2；构建和 ready 校验期不双读、不双写；任何构建
失败都回到 `V1_ACTIVE`。SQLite 跨库不宣称物理原子性，切换证据必须由
generation、immutable digests 与 checkpoints 组成。

## 迁移边界

V1 代码只允许作为 migration reader 依赖。Phase 6 的门禁是运行时代码中
legacy import、legacy active database reference 与 legacy schema reference
均为零。历史上没有旧 CodeGraph、Asset 或 TaskCanvas 来源的域必须记录为
`NO_SOURCE`，不得宣称 lossless converted。

运行：

```text
python scripts/accept_v2_phase1.py
python -m pytest tests/test_v2_architecture_contract.py -q
```

脚本默认只向 stdout 输出 JSON；依赖尚未落地时会给出 dependency failure，
并以非零退出码结束。
