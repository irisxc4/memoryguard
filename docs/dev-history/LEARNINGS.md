# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260730-002] correction

**Logged**: 2026-07-30T17:35:00+08:00
**Priority**: high
**Status**: resolved
**Area**: frontend

### Summary
神经图中的一级分类应是可展开、可治理的图内节点，不能只是跳转到独立页面的导航按钮。

### Details
此前把“规则与习惯”和“对话历史”映射为虚拟分类后，点击节点会调用 `switchTab` 跳转。用户明确要求分类、分支和具体对象都保留在神经图语境中：分类点击只聚焦/展开，具体规则可在右侧治理，会话可在图内查看摘要；独立页面只作为可选深度详情。

### Suggested Action
虚拟节点保持图内选择状态；复用现有治理 API 和确认路径，在详情轨提供规则编辑、范围、策略、删除/恢复操作。跳转只放在显式“打开详情页”按钮上，不绑定到节点主点击。

### Metadata
- Source: user_feedback
- Related Files: src/memoryguard/interactive.py, src/memoryguard/gui.py
- Tags: neuron-graph, governance, interaction-model

### Resolution
- **Resolved**: 2026-07-30T18:10:00+08:00
- **Notes**: 所有虚拟节点主点击统一为图内选择/聚焦；规则正文、范围、策略、删除/恢复进入详情轨治理；会话仅以显式按钮打开原文页。

---

## [LRN-20260729-001] correction

**Logged**: 2026-07-29T22:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: config

### Summary
已注入的协作规则必须转化为实际工具选择，不能只在回答中体现部分偏好。

### Details
本次已收到“代码检索与证据收集优先 luna_high”和“Shell 搜索显式通过 RTK”的长期规则，但首次排查由主 Agent 独自完成，且 RTK 失败后退回了裸 `rg`。用户指出执行未遵守记忆规则。

### Suggested Action
开始代码任务时把已注入规则转换成检查表：先 graphify；检索类工作优先派 luna_high；搜索和测试使用 `rtk rg` / `rtk pytest`；主 Agent 负责判断、修改与验收。

### Metadata
- Source: user_feedback
- Related Files: AGENTS.md
- Tags: memoryguard, delegation, rtk, workflow

### Resolution
- **Resolved**: 2026-07-29T22:00:00+08:00
- **Notes**: 已公开纠正，并重新派发 luna_high 只读审查。

---
## [LRN-20260730-003] correction

**Logged**: 2026-07-30T18:20:00+08:00
**Priority**: critical
**Status**: resolved
**Area**: backend

### Summary
GUI 内已由用户明确确认的“正式接管”属于受信本地治理链，不能再次要求用户手工设置管理员环境变量。

### Details
安全边界应区分调用来源：GUI 的确认桥可为单次已确认请求传递受控 `_admin_override`，CLI/MCP/脚本直接调用仍须经过管理员能力检查。把两者统一落到 `MEMORYGUARD_ADMIN` 环境变量会让正常桌面流程不可用；直接移除管理员检查又会扩大权限。

### Suggested Action
追踪正式接管从前端 `callApi`、确认队列、SafeBridge 到 GovernanceApi 的完整参数链；只修复可信确认上下文的传递，并添加未设置环境变量时的 GUI 回归及外部调用仍被拒绝的安全回归。

### Metadata
- Source: user_feedback
- Related Files: src/memoryguard/interactive.py, src/memoryguard/gui.py, src/memoryguard/security.py
- Tags: takeover, admin-capability, trusted-gui, authorization

### Resolution
- **Resolved**: 2026-07-30T18:35:00+08:00
- **Notes**: SafeBridge 仅在原生 direct mutation 且目标签名含 `_admin_override` 时内部注入；桌面确认执行器同样在确认后注入。直接 GovernanceApi/CLI/MCP 无环境变量仍被拒绝。

---
