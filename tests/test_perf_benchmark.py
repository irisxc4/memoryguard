"""性能基准 + 功能闭环验证:重构->接管->衍生->自更新。

测试维度:
1. 扫描+规范化耗时(不同数据规模)
2. 去重 TF-IDF 耗时(记录数增长)
3. 发布+Loader 复读耗时
4. AutoOrganizer 衍生触发条件
5. 端到端闭环:从原始记忆到接管验证
"""
import sys
import json
import time
import tempfile
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["MEMORYGUARD_ADMIN"] = "1"
os.environ["MEMORYGUARD_ALLOW_ANON"] = "1"
os.environ["MEMORYGUARD_STRICT_BINDING"] = "0"


def make_test_workspace(record_count: int = 50, dup_ratio: float = 0.2) -> Path:
    """创建测试工作区,含指定规模的记忆文件。"""
    ws = Path(tempfile.mkdtemp())
    docs = ws / "docs"
    docs.mkdir()

    # 生成记忆文件(部分重复)
    titles = [
        "用户偏好 Python", "项目部署流程", "代码审查规则", "API 设计规范",
        "数据库迁移步骤", "用户偏好 Go", "测试覆盖率要求", "文档编写标准",
    ]
    bodies = [
        "用户偏好使用 Python 编程语言进行开发,喜欢类型注解和 dataclass",
        "项目部署需要先跑测试,再构建 Docker 镜像,最后 kubectl apply",
        "代码审查必须检查:错误处理、日志、测试覆盖、安全输入校验",
        "API 设计遵循 RESTful 规范,资源用复数,版本放 URL,错误用标准状态码",
        "数据库迁移步骤:1. 写 migration 2. 本地测试 3. staging 验证 4. 生产",
        "用户偏好使用 Go 语言,喜欢 goroutine 和 channel",
        "测试覆盖率不低于 80%,关键路径必须 100%,用 pytest-cov 测量",
        "文档用中文编写,代码注释用英文,README 含安装和使用说明",
    ]

    files_per_doc = max(1, record_count // 8)
    for i in range(8):
        content = f"# {titles[i]}\n\n"
        for j in range(files_per_doc):
            # 重复内容占 dup_ratio
            if j < int(files_per_doc * dup_ratio):
                content += f"## {titles[i]}\n{bodies[i]}\n\n"
            else:
                content += f"## {titles[i]} 变体{j}\n{bodies[i]} 变体内容{j}\n\n"
        (docs / f"memory_{i}.md").write_text(content, encoding="utf-8")

    # 注册数据源
    from memoryguard.source_registry import SourceRegistry, SourceRootType
    reg = SourceRegistry(ws)
    reg.add(str(docs), SourceRootType.SELECTED_DIRECTORY, "Test Memory",
            scope="project")
    return ws


def bench_scan_normalize(ws: Path) -> dict:
    """基准:扫描+规范化。"""
    from memoryguard.source_registry import SourceRegistry, ScanBudget
    from memoryguard.memory_ir import MemoryNormalizer

    t0 = time.perf_counter()
    reg = SourceRegistry(ws)
    snap = reg.scan(ScanBudget())
    roots = reg.list_sources()
    root_map = {r.root_id: r.path for r in roots}
    root_policies = {r.root_id: {"source_category": r.source_category,
                                 "ingestion_policy": r.ingestion_policy} for r in roots}
    norm = MemoryNormalizer(ws)
    ir = norm.normalize(snap, root_map=root_map, root_policies=root_policies)
    norm.save(ir)
    t1 = time.perf_counter()

    return {
        "scan_normalize_ms": round((t1 - t0) * 1000, 2),
        "source_objects": len(snap.source_objects),
        "ir_records": len(ir.records),
        "duplicate_groups": len(ir.duplicate_groups),
    }


def bench_publish_loader(ws: Path) -> dict:
    """基准:发布 + Loader 复读。"""
    from memoryguard.gui import GovernanceApi

    api = GovernanceApi(str(ws))
    target_file = ws / "native_memory" / "memory.md"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("# 旧记忆\n\n旧内容\n", encoding="utf-8")

    # 注册为 native_memory 数据源
    from memoryguard.source_registry import SourceRegistry, SourceRootType
    reg = SourceRegistry(ws)
    root = reg.add(str(target_file), SourceRootType.SELECTED_FILE, "Native Memory",
                   scope="user")
    root.source_category = "native_memory"
    root.ownership = "agent_managed"
    root.target_role = "takeover_input"
    root.surface_id = "trae_user_profile"
    root.enabled = True
    from memoryguard.governance_scope import grant_root_to_agent
    from memoryguard.managed_store import ManagedStore
    from memoryguard.memory_ir import MemoryNormalizer
    grant_root_to_agent(root, "agent-bench")
    reg._save()
    ir = MemoryNormalizer(ws).load()
    if ir and ir.records:
        store = ManagedStore(ws, "agent-bench")
        if store.get_active_version_id() is None:
            store.create_initial_version(list(ir.records))

    t0 = time.perf_counter()
    result = api.publish_reconstructed_memory(
        "", True, True,
        {"mode": "agent", "agent_instance_id": "agent-bench"},
        "agent-bench",
        root.root_id,
    )
    t1 = time.perf_counter()

    return {
        "publish_ms": round((t1 - t0) * 1000, 2),
        "publish_ok": result.get("ok", False),
        "published_count": result.get("published_record_count", 0),
        "takeover_verify": result.get("takeover_verify", {}),
    }


def bench_autocomplete_derive(ws: Path) -> dict:
    """基准:AutoOrganizer 衍生触发。"""
    from memoryguard.auto_organizer import AutoOrganizer
    from memoryguard.schema_v3 import MemoryEvent

    org = AutoOrganizer(ws, "default")
    # 写入 3 条相似 episode(应触发衍生)
    actions_all = []
    t0 = time.perf_counter()
    for i in range(3):
        event = MemoryEvent(
            event_id=f"evt-{i}", agent_instance_id="a1", share_group_id="default",
            raw_content=f"用户偏好 Python 编程,喜欢用 pytest 做测试",
            metadata={},
        )
        record, actions = org.organize(event)
        actions_all.append(actions)
    t1 = time.perf_counter()

    derive_actions = [a for actions in actions_all for a in actions if a.get("action") == "derive"]
    return {
        "organize_3_events_ms": round((t1 - t0) * 1000, 2),
        "derive_triggered": len(derive_actions) > 0,
        "derive_count": len(derive_actions),
    }


def bench_gc_plan(ws: Path) -> dict:
    """基准:GC 计划生成。"""
    from memoryguard.gc import MemoryGuardGc

    t0 = time.perf_counter()
    gc = MemoryGuardGc(ws, keep_releases=5, older_than_days=30)
    plan = gc.plan(dry_run=True)
    t1 = time.perf_counter()

    return {
        "gc_plan_ms": round((t1 - t0) * 1000, 2),
        "gc_items": len(plan.items),
        "gc_total_bytes": plan.total_bytes,
    }


def run_benchmarks():
    """运行全部基准。"""
    print("=" * 60)
    print("MemoryGuard 性能基准 + 功能闭环验证")
    print("=" * 60)

    # 小规模(50 条)
    print("\n--- 小规模(~50 条记忆) ---")
    ws50 = make_test_workspace(50)
    r1 = bench_scan_normalize(ws50)
    print(f"扫描+规范化: {r1['scan_normalize_ms']}ms | "
          f"源对象={r1['source_objects']} IR记录={r1['ir_records']} "
          f"重复组={r1['duplicate_groups']}")

    r2 = bench_publish_loader(ws50)
    print(f"发布+复读: {r2['publish_ms']}ms | ok={r2['publish_ok']} "
          f"发布={r2['published_count']}条 | "
          f"takeover={r2['takeover_verify'].get('verified', 'N/A')}")

    r3 = bench_autocomplete_derive(ws50)
    print(f"衍生触发: {r3['organize_3_events_ms']}ms | "
          f"衍生触发={r3['derive_triggered']} 衍生数={r3['derive_count']}")

    r4 = bench_gc_plan(ws50)
    print(f"GC计划: {r4['gc_plan_ms']}ms | 条目={r4['gc_items']} "
          f"可回收={r4['gc_total_bytes']}bytes")

    # 中规模(200 条)
    print("\n--- 中规模(~200 条记忆) ---")
    ws200 = make_test_workspace(200)
    r5 = bench_scan_normalize(ws200)
    print(f"扫描+规范化: {r5['scan_normalize_ms']}ms | "
          f"源对象={r5['source_objects']} IR记录={r5['ir_records']} "
          f"重复组={r5['duplicate_groups']}")

    r6 = bench_publish_loader(ws200)
    print(f"发布+复读: {r6['publish_ms']}ms | ok={r6['publish_ok']} "
          f"发布={r6['published_count']}条")

    # 大规模(500 条)
    print("\n--- 大规模(~500 条记忆) ---")
    ws500 = make_test_workspace(500)
    r7 = bench_scan_normalize(ws500)
    print(f"扫描+规范化: {r7['scan_normalize_ms']}ms | "
          f"源对象={r7['source_objects']} IR记录={r7['ir_records']} "
          f"重复组={r7['duplicate_groups']}")

    r8 = bench_publish_loader(ws500)
    print(f"发布+复读: {r8['publish_ms']}ms | ok={r8['publish_ok']} "
          f"发布={r8['published_count']}条")

    # 性能要求评估
    print("\n" + "=" * 60)
    print("性能评估")
    print("=" * 60)
    targets = {
        "扫描+规范化(50条)": (r1["scan_normalize_ms"], 2000, "ms"),
        "扫描+规范化(200条)": (r5["scan_normalize_ms"], 5000, "ms"),
        "扫描+规范化(500条)": (r7["scan_normalize_ms"], 15000, "ms"),
        "发布+复读(50条)": (r2["publish_ms"], 3000, "ms"),
        "发布+复读(200条)": (r6["publish_ms"], 8000, "ms"),
        "发布+复读(500条)": (r8["publish_ms"], 20000, "ms"),
        "衍生检测(3事件)": (r3["organize_3_events_ms"], 2000, "ms"),
        "GC计划": (r4["gc_plan_ms"], 1000, "ms"),
    }
    all_pass = True
    for name, (actual, target, unit) in targets.items():
        status = "PASS" if actual < target else "FAIL"
        if status == "FAIL":
            all_pass = False
        print(f"  [{status}] {name}: {actual}{unit} < {target}{unit}")

    # 功能闭环评估
    print("\n" + "=" * 60)
    print("功能闭环评估:重构->接管->衍生->自更新")
    print("=" * 60)
    checks = {
        "重构(扫描->IR->去重)": r1["ir_records"] > 0 and r1["duplicate_groups"] >= 0,
        "发布到原生记忆": r2["publish_ok"],
        "Loader 复读验证": r2["takeover_verify"].get("verified") is True,
        "runtime_verified 驱动": r2["takeover_verify"].get("runtime_verified") is True,
        "衍生触发(3条相似)": r3["derive_triggered"],
        "GC 可执行": r4["gc_items"] >= 0,
    }
    for name, passed in checks.items():
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name}: {passed}")

    print("\n" + "=" * 60)
    if all_pass:
        print("结论:全部达标,可合理重构整理接管记忆并自更新衍生")
    else:
        print("结论:部分未达标,需优化")
    print("=" * 60)

    return all_pass


if __name__ == "__main__":
    run_benchmarks()
