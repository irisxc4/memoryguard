# 实施交接

## 单一实施工作包

负责文件：

- `src/memoryguard/conversation_history.py`
- `src/memoryguard/history_api.py`
- `src/memoryguard/history_importers.py`
- `src/memoryguard/adapters.py`
- `src/memoryguard/gui.py`
- `src/memoryguard/interactive.py`
- 必要的 history / graph / MCP 测试

要求：

1. 实现服务端 `HistoryAccessResolver` 或等价边界，从活跃 binding 动态生成 authorized Agent IDs。
2. personal 精确隔离；shared 当前成员 list/search/timeline/read/extract/export 共享；跨 owner delete 拒绝。
3. 请求 group 不得扩大可信 binding；GUI/MCP 共用解析逻辑。
4. 项目 canonical helper 收口；parser 从结构化 metadata 逐会话提取 project_ref，缺失为 unknown。
5. 保留旧 session/turn/evidence ID。
6. 输出项目聚合安全元数据；图变为历史→项目→Agent→会话；点击不跳页。
7. 历史页按项目、Agent 分组；标题使用稳定回退。
8. 更新 README 中旧的“共享组不聚合历史”边界。
9. 添加动态入组/离组、跨组伪造、个人隔离、owner delete、project aliases、unknown/removed、图无 raw content 的测试。
10. 运行定向测试与全量测试；不得回退工作树中其他人的修改。

## 明确禁止

- 不把原始对话写入 SharedMemoryStore。
- 不把历史全文加入 bootstrap。
- 不根据聊天正文猜项目。
- 不信任前端传入的成员 ID 列表。
- 不用 basename 作为项目唯一键。
