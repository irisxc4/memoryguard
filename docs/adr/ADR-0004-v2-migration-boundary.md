# ADR-0004: V1 仅迁移读取器与无来源域标记

- 状态：Accepted for V2 Phase 1
- 日期：2026-08-08

## 决策

V1 仅能由 migration reader 读取。Phase 6 门禁要求运行时代码中不再有
legacy imports、legacy active database references 或 legacy schema
references。没有对应 V1 来源的旧 CodeGraph、Asset、TaskCanvas 域写入
`NO_SOURCE`，而不是生成空对象并声称 lossless conversion。

## 原因

迁移读取器的临时依赖与运行时主路径必须可区分。把没有来源的域伪装成已
转换会误导审计、覆盖率和用户恢复决策；显式 NO_SOURCE 保留事实边界。

## 验收

验收脚本扫描新 V2 包中的 legacy Store/ConversationHistory 直接导入，检查
契约列出的 NO_SOURCE 域，并在 JSON 结果中区分 source、dependency 和 static
contract failures。
