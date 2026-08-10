# Phase 6：GUI / CLI V2 受门控接线

Phase 6 将既有 GUI 与 CLI 名称接到统一的 V2 运行时 facade，同时保留
V1 的旧 envelope 和 18 个顶层命令。Phase 6-A facade 不存在时，入口只做
feature-detect；不会在 V2 路径导入或构造 `SharedMemoryStore`。

## 单次门禁与状态矩阵

每一次 GUI/CLI 调用只读取一次 manifest（原生 `V2RuntimeFacade` 自己持有
这次 snapshot），然后选择唯一的一条路径：

| manifest | readonly | mutation |
| --- | --- | --- |
| `V1_ACTIVE` | legacy | legacy |
| `V2_BUILDING` | legacy | legacy |
| `V2_READY` | V2 | `v2_not_active`，不 fallback |
| `V2_ACTIVE` | V2 | V2 |
| unknown / corrupt | fail-closed | fail-closed |

`SafeBridgeApi` 是 GUI 的唯一 readonly/mutation 入口；HTTP localhost handler
也只能经 bridge 调用。身份来自 trusted bridge context，浏览器传入的
`actor`、`preview` 或 scope 字段不构成授权。CLI 保持 `argparse.Namespace`
和子动作（`source/import/hooks/provider/groups/gc/desktop` 等），默认
dry-run 的子动作仍是只读。

## 验收

```text
python scripts/accept_v2_phase6.py
python -m pytest tests/test_v2_gui_cli_cutover.py -q
```

验收脚本只在临时 fixture 中覆盖四态矩阵，并检查每调用 manifest 计数、
legacy/V2 counters、unknown fail-closed、GUI/CLI 名称快照与可选 MCP/Hook
存在性。额外通过真实 `SafeBridgeApi` 验证沙箱变更先过 manifest gate 后才
进入 `RequestQueue`，通过 localhost `/api/submit_request` 验证 HTTP 写入口
不再直调 legacy handler，并统计 V2 路径的 `GovernanceApi`/legacy adapter
懒加载计数。真实工作区只读检查前后 state/generation 不变；脚本 stdout
始终为单个 JSON。

## 回滚与停止条件

回滚只允许通过 manifest 的受控 `V2_* -> V1_ACTIVE` 失败路径，并记录
reason、generation 与 digest 证据；不要在入口层添加 V2→V1 的隐式 fallback。
如果出现 unknown/corrupt manifest、generation 不匹配、facade context 能力
不足、跨路由 counters、V2_READY mutation 被执行、或任意 V2 路径触碰
`SharedMemoryStore`，立即停止 promotion，保持 `V1_ACTIVE` / `V2_BUILDING`
并修复证据后再重跑验收。只有 validator、readiness、digest/checkpoint 和
回滚审计全部通过，才可由独立 activation 流程推进 `V2_READY -> V2_ACTIVE`。
