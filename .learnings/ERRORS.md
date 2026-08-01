# Errors

Command failures and integration errors.

---

## [ERR-20260730-004] src_layout_runtime_import

**Logged**: 2026-07-30T16:55:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
直接运行诊断脚本时加载了全局已安装包，未加载当前 `src/` 工作树的新模块。

### Error
```
ModuleNotFoundError: No module named 'memoryguard.history_importers'
```

### Context
- 项目使用 `src` layout。
- `pytest` 会配置源码路径，但独立 `python -c` 不会自动加入 `src/`。

### Suggested Fix
诊断当前工作树时显式把仓库 `src` 目录加入 `sys.path`，或在安全的项目运行入口中使用开发安装。

### Metadata
- Reproducible: yes
- Related Files: pyproject.toml

### Resolution
- **Resolved**: 2026-07-30T16:56:00+08:00
- **Notes**: 后续诊断显式加载当前工作树 `src`，真实发现结果正常。

---

## [ERR-20260730-003] windows_sqlite_wal_probe_race

**Logged**: 2026-07-30T16:20:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
规则页首次读取偶发失败，因为 Windows 上 SQLite WAL 在 `exists()` 与 `stat()` 之间被并发连接正常清理。

### Error
```
[WinError 2] 系统找不到指定的文件: memory.db-wal
```

### Context
- 只读共享记忆连接先检查 `memory.db-wal` 是否存在，再读取文件大小。
- Hook/MCP 写事务可在两个文件操作之间 checkpoint 并删除 WAL。
- 第二次点击时 WAL 已消失，读取通常恢复正常。

### Suggested Fix
使用一次 `stat()` 探测并捕获 `FileNotFoundError`，必要时对可恢复的 sidecar 竞态做一次有界重试；增加确定性竞态测试和并发读写回归测试。

### Metadata
- Reproducible: yes
- Related Files: src/memoryguard/shared_memory_store.py, tests/test_personal_memory_group.py
- Recurrence-Count: 1
- Last-Seen: 2026-07-30

### Resolution
- **Resolved**: 2026-07-30T17:20:00+08:00
- **Notes**: WAL 探测改为单次 `stat()` 并捕获 sidecar 消失；加入确定性竞态与并发读写回归。真实共享组连续读取规则 200 次，0 错误。

---

## [ERR-20260730-002] legacy_shared_memory_schema

**Logged**: 2026-07-30T03:50:00+08:00
**Priority**: high
**Status**: resolved
**Area**: database

### Summary
新字段只在新库/写模式初始化，真实旧库被只读 MCP 先打开时读取失败。

### Error
```
IndexError: No item with that key
```

### Context
- 旧 `records` 表没有 `injection_policy` / `priority`
- `read_only=True` 跳过 `_init_db`
- `_row_to_record` 直接按新列读取

### Suggested Fix
所有生产打开路径在查询前执行事务性、幂等、无损 schema migration；回归测试必须从真实旧表结构和已有数据开始，覆盖只读读取、MCP 更新与 bootstrap。

### Metadata
- Reproducible: yes
- Related Files: src/memoryguard/shared_memory_store.py, tests/test_mandatory_rules.py
- Recurrence-Count: 1
- Last-Seen: 2026-07-30

### Resolution
- **Resolved**: 2026-07-30T04:10:00+08:00
- **Notes**: 旧库先补列，再创建依赖新列的 index/FTS；`_row_to_record` 保留缺列回退。

---

## [ERR-20260729-001] windows_rtk_invocation

**Logged**: 2026-07-29T22:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
RTK 不能包装 PowerShell 内建命令，Windows 环境也没有 Unix `head`。

### Error
```
rtk: Failed to resolve 'ls' via PATH
The term 'head' is not recognized
```

### Context
- 尝试执行 `rtk ls`
- 随后把 `rtk rg` 输出管给 `head`
- 2026-07-30 再次把 `rtk rg` 管给不存在的 `rtk pipe grep` 组合
- 2026-07-30 又尝试通过 RTK 调用 Windows 中不存在的 `sed`，随后首次嵌套 PowerShell 时 `$` 被外层提前展开
- 2026-07-30 再次在双引号嵌套 PowerShell 中让 `$` 被提前展开，并误以为当前 RTK 提供 `read` 子命令
- 2026-07-30 使用 `$env:USERPROFILE` 的嵌套命令时再次被外层展开，改为经上下文确认的绝对路径
- 2026-07-30 误把 `--level` 传给不支持该参数的 `rtk pipe`，改为直接使用单条 `rtk rg`
- 2026-07-30 在 PowerShell 双引号中嵌套含单引号的 Python/JavaScript 片段导致解析失败，改为检查无嵌套引号的稳定 UI 标记
- 2026-07-30 对 Windows 不支持的 `tests/test_*takeover*` 路径 glob、未闭合搜索正则，以及预期“无匹配”的 `rtk rg` 与其他检查串联，分别造成非零退出；改为显式文件列表、多个 `-e` 模式，并将允许无匹配的审计单独执行
- Windows PowerShell 环境

### Suggested Fix
RTK 只包装真实可执行程序；搜索直接用 `rtk rg -m <count>` 控制输出，读文件用 `rtk read`。需要行区间时用 `rtk proxy powershell -NoProfile -Command '...'`，并用外层单引号保护 `$`。不要假设 Unix 工具存在。

### Metadata
- Reproducible: yes
- Related Files: none
- Recurrence-Count: 11
- Last-Seen: 2026-07-30

### Resolution
- **Resolved**: 2026-07-29T22:00:00+08:00
- **Notes**: 后续检索改用单条 `rtk rg -m` 或 `rtk read`，不再拼接二次过滤。

---

## [ERR-20260730-006] windows_python_pipe_gbk

**Logged**: 2026-07-30T18:05:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
Windows 默认 GBK 输出无法把包含 `•` 的完整前端脚本管给 Node，第一次语法检查实际收到空输入。

### Error
```
UnicodeEncodeError: 'gbk' codec can't encode character '\u2022'
```

### Context
- 从 `render_interactive_html()` 提取内联 JavaScript，经标准输出交给 Node 解析。
- Node 第一版命令没有拒绝空输入，造成“parse OK”假阳性。

### Suggested Fix
跨进程输出 Unicode 前设置任务专用 `PYTHONIOENCODING=utf-8`；消费端必须先断言输入非空，再执行解析。

### Metadata
- Reproducible: yes
- Related Files: src/memoryguard/interactive.py
- Recurrence-Count: 1
- Last-Seen: 2026-07-30

### Resolution
- **Resolved**: 2026-07-30T18:07:00+08:00
- **Notes**: 以 UTF-8 重跑并加入非空断言；218908 字符的内联 JavaScript 通过 Node 语法解析。

---
## [ERR-20260730-005] luna_subagent_stream_disconnect

**Logged**: 2026-07-30T17:45:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
Luna 只读代码映射任务因远端响应流断开而未返回结果。

### Error
```
stream disconnected before completion
```

### Context
- 子代理仅承担图内治理与会话标题数据链的只读检索。
- 主线程已取得关键调用链证据，后续由两个边界独立的 Terra high 实现任务继续，不依赖该响应。

### Suggested Fix
服务瞬时断流时最多重试一次；若主线程已有充分证据，则记录失败并继续，避免重复阻塞实现。

### Metadata
- Reproducible: no
- Related Files: none
- Recurrence-Count: 1
- Last-Seen: 2026-07-30

### Resolution
- **Resolved**: 2026-07-30T17:47:00+08:00
- **Notes**: 主线程完成调用链定位，任务拆分为图内治理与标题回填两个 Terra high 工作单元。

---
## [ERR-20260730-007] takeover_admin_capability_leak

**Logged**: 2026-07-30T18:20:00+08:00
**Priority**: critical
**Status**: resolved
**Area**: backend

### Summary
GUI 正式接管确认后仍返回管理员能力缺失错误，核心接管流程被阻断。

### Error
```
admin capability required (set MEMORYGUARD_ADMIN=1)
```

### Context
- 用户已从桌面 GUI 进入正式接管操作。
- 预期由本地确认桥授予单次受控权限，不依赖持久环境变量。
- 必须保持 CLI/MCP 直接写操作的管理员防线。

### Suggested Fix
构造无 `MEMORYGUARD_ADMIN` 的确定性 GUI 调用链回归，定位 `_admin_override` 丢失位置并仅修复该可信路径。

### Metadata
- Reproducible: unknown
- Related Files: src/memoryguard/gui.py, src/memoryguard/interactive.py, src/memoryguard/security.py
- Recurrence-Count: 1
- Last-Seen: 2026-07-30

### Resolution
- **Resolved**: 2026-07-30T18:35:00+08:00
- **Notes**: 原始无环境变量复现从 trusted bridge 失败变为成功并生成版本；反向验证 direct API 仍返回 admin capability error。定向 77 项、全量 487 项通过。

---
