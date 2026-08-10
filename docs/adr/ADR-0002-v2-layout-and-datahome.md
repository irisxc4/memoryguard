# ADR-0002: 固定 V2 数据库布局与显式 DataHome 指针

- 状态：Accepted for V2 Phase 1
- 日期：2026-08-08

## 决策

每个 workspace 的 V2 数据库路径固定为：

```text
.memoryguard/runtime/runtime.db
.memoryguard/memory/memory.db
.memoryguard/rules/rules.db
.memoryguard/evidence/evidence.db
.memoryguard/content/content.db
.memoryguard/knowledge/knowledge.db
.memoryguard/codegraph/codegraph.db
.memoryguard/assets/assets.db
.memoryguard/projection/scenario.db
.memoryguard/projection/profile.db
.memoryguard/system/manifest.db
```

manifest 同时保存 workspace source pointer、global source pointer 和解析后
的 DataHome。路径必须显式发现、规范化并通过 containment 校验。缺少指针时
拒绝操作；禁止根据目录名称猜测旧数据，禁止静默移动数据。原有全局
`${MEMORYGUARD_HOME}/knowledge/knowledge.db` 只登记为 V1 migration source。

## 原因

固定布局让部署、备份和审计拥有稳定边界；显式指针避免工作区复制或移动后
意外连接到另一用户的数据。knowledge 的旧全局路径仍需可迁移，但不能成为
V2 运行时的隐式依赖。

## 验收

契约 JSON 是路径的单一清单；验收脚本对布局做精确集合比较，并对模块的
read-only 打开行为验证“缺失文件不创建目录或数据库”。
