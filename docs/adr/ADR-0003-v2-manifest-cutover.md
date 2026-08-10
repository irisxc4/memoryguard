# ADR-0003: 通过 manifest generation 的逻辑切换

- 状态：Accepted for V2 Phase 1
- 日期：2026-08-08

## 决策

manifest 只能处于 `V1_ACTIVE`、`V2_BUILDING`、`V2_READY`、`V2_ACTIVE`
四种状态。只有 manifest 为 `V2_ACTIVE` 时运行时代码才可读取 V2。构建和
ready 校验期不双读、不双写；构建失败必须记录原因并回到 `V1_ACTIVE`。

SQLite 跨库事务不被宣称为物理原子。提交依据是单调 generation、每个域的
immutable digest，以及可重建的 checkpoint。generation 与 digest 不匹配时
拒绝激活并回退 V1。

## 允许的切换

```text
V1_ACTIVE  -> V2_BUILDING -> V2_READY -> V2_ACTIVE
V1_ACTIVE  -> V2_BUILDING -> V1_ACTIVE             (失败/取消)
V2_READY   -> V1_ACTIVE                            (ready 校验失败)
V2_ACTIVE  -> V1_ACTIVE -> V2_BUILDING             (重建，先停用旧 V2)
V2_ACTIVE  -> V1_ACTIVE                             (显式回滚)
```

任意状态的自循环只允许作为幂等重放，不得改变 generation 的语义；不得从
`V2_BUILDING` 直接跳到 `V2_ACTIVE`，也不得从 `V2_ACTIVE` 直接进入构建。

## 验收

验收脚本校验状态和转换集合，并在可用的 `SystemManifestStore` 上执行一次
成功和一次失败转换；缺失实现时报告 dependency failure，而不是伪造通过。
