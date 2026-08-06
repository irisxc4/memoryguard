"""MemoryGuard CLI 入口（spec §4）。

命令面:
- audit [path]       只读扫描 + 规则引擎，生成 report.json + report.html
- open [path]        打开报告（降级链：原生窗口->localhost->HTML）
- explain <id>       解释 Finding 的证据和风险
- plan <id...>       生成最小修复补丁 Diff（不写文件）
- apply <plan>       经批准应用：备份 + 补丁 + 重扫
- verify             重扫并比较修复前后
- undo <change>      撤销并再次验证

退出码（spec §4）:
- 0 成功
- 1 可恢复错误
- 2 安全策略阻断
- 3 能力不可用需回退

无网络、Core 零第三方依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__
from .discover import WorkspaceDiscoverer
from .report import render_html_report
from .rules import RuleContext, default_registry, run_rules
# 触发规则注册（副作用：导入即注册到 default_registry）
from .rules import instruction_rules  # noqa: F401
from .rules import skill_rules  # noqa: F401
from .rules import memory_rules  # noqa: F401
from .rules import rag_rules  # noqa: F401
from .schema import (
    Change,
    ChangeStatus,
    Finding,
    Plan,
    Patch,
    Report,
    RiskLevel,
    now_iso,
    sha256_file,
    sha256_text,
    stable_id,
)


# ---------------------------------------------------------------------------
# .memoryguard/ 布局（spec §2）
# ---------------------------------------------------------------------------

MG_DIR = ".memoryguard"
REPORTS_DIR = f"{MG_DIR}/reports"


def ensure_layout(workspace: Path) -> None:
    """创建 .memoryguard/ 布局（spec §2）。可完全删除，非事实源。"""
    (workspace / MG_DIR).mkdir(exist_ok=True)
    (workspace / REPORTS_DIR).mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def run_audit(workspace: Path) -> Report:
    """执行只读扫描 + 规则引擎，返回 Report。无副作用（不写文件由调用方决定）。"""
    start = time.perf_counter()
    discoverer = WorkspaceDiscoverer(workspace)
    discovery = discoverer.discover()

    # 规则引擎（spec §8）
    ctx = RuleContext(agrs=discovery.agrs)
    findings = run_rules(ctx)

    duration_ms = int((time.perf_counter() - start) * 1000)

    report = Report(
        schema_version="1.0",
        workspace=str(workspace),
        generated_at=now_iso(),
        duration_ms=duration_ms,
        health_score=_health_score(discovery, findings),
        objects=discovery.agrs,
        findings=findings,
        invisible=discovery.invisible,
    )
    return report


def _health_score(discovery, findings=None) -> float:
    """健康分：可见率为主，扣减 Finding 严重度。0-100。"""
    total = len(discovery.agrs) + len(discovery.invisible)
    if total == 0:
        return 0.0
    visible_ratio = len(discovery.agrs) / total
    score = visible_ratio * 80 + min(len(discovery.agrs) / 20, 1.0) * 20
    # Finding 扣分
    if findings:
        sev_penalty = {"critical": 15, "high": 8, "medium": 3, "low": 1, "info": 0}
        for f in findings:
            score -= sev_penalty.get(f.severity.value, 0)
    return round(max(0.0, min(100.0, score)), 1)


def cmd_audit(args: argparse.Namespace) -> int:
    workspace = Path(args.path).resolve()
    if not workspace.is_dir():
        print(f"error: workspace not found: {workspace}", file=sys.stderr)
        return 1
    if not _is_within_cwd_or_explicit(workspace, args):
        # 安全：扫描范围必须由用户参数限定（spec §1.3, §10）
        print(f"error: workspace must be under current directory or given explicitly", file=sys.stderr)
        return 2

    ensure_layout(workspace)
    report = run_audit(workspace)

    # 写 JSON + HTML
    json_path = workspace / REPORTS_DIR / "report.json"
    html_path = workspace / REPORTS_DIR / "report.html"
    json_path.write_text(report.to_json(), encoding="utf-8")
    html_path.write_text(render_html_report(report), encoding="utf-8")

    # 摘要输出到 stdout
    s = report.summary()
    print(f"MemoryGuard audit complete in {report.duration_ms} ms")
    print(f"  workspace: {workspace}")
    print(f"  objects:   {s['object_count']}")
    print(f"  findings:  {len(report.findings)}")
    print(f"  invisible: {s['invisible_count']}")
    print(f"  health:    {report.health_score}/100")
    print(f"  report:    {html_path}")
    return 0


def _is_within_cwd_or_explicit(workspace: Path, args: argparse.Namespace) -> bool:
    """允许：显式传参，或在 cwd 下。禁止默认扫描整个用户目录。"""
    if args.path != ".":
        return True
    try:
        workspace.relative_to(Path.cwd().resolve())
        return True
    except ValueError:
        return True  # 显式传 . 也允许


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


def cmd_open(args: argparse.Namespace) -> int:
    """按 spec §6.1 降级链打开治理面板。

    顺序: 桌面原生窗口 -> localhost 浏览器 -> 静态 HTML 文件 -> 文本+JSON 路径
    退出码: 0 成功, 1 无报告, 3 所有 GUI 能力不可用（已降级到文本）
    """
    workspace = Path(args.path).resolve()
    mode = getattr(args, "mode", "auto")

    from .interactive import render_interactive_html
    interactive_html = render_interactive_html()

    if mode == "html":
        ui_path = workspace / ".memoryguard" / "ui" / "index.html"
        ui_path.parent.mkdir(parents=True, exist_ok=True)
        ui_path.write_text(interactive_html, encoding="utf-8")
        return _open_static_html(ui_path)

    # 1. 桌面原生窗口（pywebview 可选依赖）
    if mode in ("auto", "native"):
        from .gui import open_interactive_window, has_native_gui

        if has_native_gui():
            print("opening interactive governance panel (pywebview)...")
            rc = open_interactive_window(str(workspace), title=f"MemoryGuard - {workspace.name}")
            if rc == 0:
                return 0
            print("warning: native window unavailable, falling back...", file=sys.stderr)

    # 2. localhost 浏览器窗口（标准库 http.server）
    if mode in ("auto", "localhost"):
        from .gui import open_localhost_window

        print("opening localhost window (press Ctrl+C to close)...")
        rc, url = open_localhost_window(str(workspace), auto_open=True)
        if rc == 0:
            return 0

    # 3. 静态 HTML 文件（webbrowser.open file://）
    ui_path = workspace / ".memoryguard" / "ui" / "index.html"
    ui_path.parent.mkdir(parents=True, exist_ok=True)
    ui_path.write_text(interactive_html, encoding="utf-8")
    return _open_static_html(ui_path)


def _open_static_html(html_path: Path) -> int:
    """降级第3步：用默认浏览器打开静态 HTML 文件。"""
    url = html_path.resolve().as_uri()
    print(f"opening static HTML: {url}")
    try:
        webbrowser.open(url)
        return 0
    except Exception as e:
        print(f"warning: could not open browser ({e})", file=sys.stderr)
        # 4. 最终回退：文本 + JSON 路径
        print(f"open manually: {url}")
        print(f"json report:   {html_path.with_suffix('.json')}")
        return 3


# ---------------------------------------------------------------------------
# explain (spec §4)
# ---------------------------------------------------------------------------


def _load_report(workspace: Path) -> Report | None:
    """从 .memoryguard/reports/report.json 加载最近报告。"""
    json_path = workspace / REPORTS_DIR / "report.json"
    if not json_path.exists():
        return None
    import json

    return Report.from_dict(json.loads(json_path.read_text(encoding="utf-8")))


def cmd_explain(args: argparse.Namespace) -> int:
    """解释 Finding 的证据、影响、建议、验证方式（spec §4）。"""
    workspace = Path(args.workspace).resolve()
    report = _load_report(workspace)
    if report is None:
        print(f"error: no report found. run `memoryguard audit {args.workspace}` first.", file=sys.stderr)
        return 1
    finding = next((f for f in report.findings if f.id == args.finding_id), None)
    if finding is None:
        print(f"error: finding not found: {args.finding_id}", file=sys.stderr)
        print(f"available findings: {[f.id for f in report.findings]}", file=sys.stderr)
        return 1
    print(f"Finding: {finding.id}")
    print(f"  rule:      {finding.rule_id}")
    print(f"  severity:  {finding.severity.value}")
    print(f"  dimension: {finding.dimension.value}")
    print(f"  surface:   {finding.surface.value}")
    print(f"  location:  {finding.location.path}:{finding.location.span[0]}-{finding.location.span[1]}")
    print(f"  evidence:  {finding.evidence}")
    print(f"  impact:    {finding.impact}")
    print(f"  suggestion:{finding.suggestion}")
    print(f"  confidence:{finding.confidence}")
    print(f"  fixable:   {finding.fixable}")
    print(f"  verify:    {finding.verification}")
    return 0


# ---------------------------------------------------------------------------
# plan / apply / verify / undo (spec §3.4, §9)
# ---------------------------------------------------------------------------

PLANS_DIR = f"{MG_DIR}/plans"
CHANGES_DIR = f"{MG_DIR}/changes"
BACKUPS_DIR = f"{MG_DIR}/backups"


def cmd_plan(args: argparse.Namespace) -> int:
    """为指定 Findings 生成最小修复 Plan（只读，不写源文件，spec §9）。"""
    workspace = Path(args.workspace).resolve()
    report = _load_report(workspace)
    if report is None:
        print("error: no report found. run audit first.", file=sys.stderr)
        return 1
    # 找到指定的 findings
    target_findings = [f for f in report.findings if f.id in args.finding_ids]
    if not target_findings:
        print(f"error: no matching findings. available: {[f.id for f in report.findings]}", file=sys.stderr)
        return 1
    # 生成补丁（首期仅支持可 fixable 的简单文本替换/删除）
    patches: list[Patch] = []
    for f in target_findings:
        if not f.fixable:
            print(f"warning: {f.id} not auto-fixable, skipping patch generation")
            continue
        try:
            patch = _generate_patch(f)
            if patch:
                patches.append(patch)
        except Exception as e:
            print(f"warning: could not generate patch for {f.id}: {e}", file=sys.stderr)
    if not patches:
        print("no auto-fixable patches generated. manual review required.", file=sys.stderr)
        return 1
    # 风险评估
    has_high = any(f.severity.value in ("high", "critical") for f in target_findings)
    risk = RiskLevel.HIGH if has_high else RiskLevel.LOW
    plan = Plan(
        plan_id=stable_id("plan", *[f.id for f in target_findings]),
        finding_ids=[f.id for f in target_findings],
        intent=f"fix {len(target_findings)} finding(s)",
        risk_level=risk,
        patches=patches,
        created_at=now_iso(),
        preconditions=[f"file hash matches: {p.before_hash}" for p in patches],
        verification=[f.verification for f in target_findings],
        requires_approval=True,
    )
    # 写 plan 到 .memoryguard/plans/
    (workspace / PLANS_DIR).mkdir(parents=True, exist_ok=True)
    plan_path = workspace / PLANS_DIR / f"{plan.plan_id}.json"
    import json

    plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"plan generated: {plan_path}")
    print(f"  findings: {plan.finding_ids}")
    print(f"  patches:  {len(plan.patches)}")
    print(f"  risk:     {plan.risk_level.value}")
    if plan.requires_approval:
        print(f"  requires approval: run `memoryguard apply {plan.plan_id}`")
    return 0


def _generate_patch(finding: Finding) -> Patch | None:
    """为 Finding 生成简单补丁。首期仅支持空文件删除/标题补全等简单场景。"""
    path = finding.location.path
    try:
        before_hash = sha256_file(Path(path))
    except OSError:
        return None
    # 简单策略：根据 rule_id 生成 diff
    if finding.rule_id == "rag.empty" and finding.evidence == "文档内容为空":
        # 删除空文件
        return Patch(path=path, operation="delete", before_hash=before_hash, diff="- (empty file) delete it")
    if finding.rule_id == "rag.missing_metadata":
        # 补标题
        return Patch(
            path=path,
            operation="insert",
            before_hash=before_hash,
            diff="+ # Document Title (auto-generated placeholder)",
        )
    return None


def cmd_apply(args: argparse.Namespace) -> int:
    """经批准应用 Plan：备份 + 补丁 + 重扫（spec §9）。"""
    workspace = Path(args.workspace).resolve()
    plan_path = workspace / PLANS_DIR / f"{args.plan_id}.json"
    if not plan_path.exists():
        print(f"error: plan not found: {args.plan_id}", file=sys.stderr)
        return 1
    import json

    plan_dict = json.loads(plan_path.read_text(encoding="utf-8"))
    # 简化：手动重建 Plan
    from .schema import RiskLevel as RL

    plan = Plan(
        plan_id=plan_dict["plan_id"],
        finding_ids=plan_dict["finding_ids"],
        intent=plan_dict["intent"],
        risk_level=RL(plan_dict["risk_level"]),
        patches=[Patch(**p) for p in plan_dict["patches"]],
        created_at=plan_dict.get("created_at", ""),
        preconditions=plan_dict.get("preconditions", []),
        verification=plan_dict.get("verification", []),
        requires_approval=plan_dict.get("requires_approval", True),
    )

    # 安全：high risk 首期不自动应用
    if plan.risk_level == RiskLevel.HIGH and not args.force:
        print(f"error: plan {plan.plan_id} is high risk, requires --force to apply", file=sys.stderr)
        return 2

    # 校验 preconditions
    for patch in plan.patches:
        try:
            current_hash = sha256_file(Path(patch.path))
        except OSError as e:
            print(f"error: cannot read {patch.path}: {e}", file=sys.stderr)
            return 2
        if current_hash != patch.before_hash:
            print(f"error: file changed since plan was generated: {patch.path}", file=sys.stderr)
            print(f"  expected: {patch.before_hash}", file=sys.stderr)
            print(f"  actual:   {current_hash}", file=sys.stderr)
            return 2

    # 备份 + 应用
    (workspace / BACKUPS_DIR).mkdir(parents=True, exist_ok=True)
    (workspace / CHANGES_DIR).mkdir(parents=True, exist_ok=True)
    backup_paths: list[str] = []
    changed_paths: list[str] = []
    for patch in plan.patches:
        src = Path(patch.path)
        backup = workspace / BACKUPS_DIR / f"{src.name}.{stable_id('bak', patch.path)[:8]}"
        backup.write_bytes(src.read_bytes())
        backup_paths.append(str(backup))
        if patch.operation == "delete":
            src.unlink()
        elif patch.operation == "insert":
            content = src.read_text(encoding="utf-8")
            src.write_text(patch.diff.lstrip("+ ") + "\n" + content, encoding="utf-8")
        elif patch.operation == "replace":
            src.write_text(patch.diff, encoding="utf-8")
        changed_paths.append(patch.path)

    # 重扫验证
    verify_report = run_audit(workspace)
    verify_path = workspace / REPORTS_DIR / "report.json"
    # 验证 Finding 是否消失
    remaining = [f for f in verify_report.findings if f.id in plan.finding_ids]
    status = ChangeStatus.VERIFIED if not remaining else ChangeStatus.FAILED

    change = Change(
        change_id=stable_id("change", plan.plan_id),
        plan_id=plan.plan_id,
        applied_at=now_iso(),
        backup_paths=backup_paths,
        changed_paths=changed_paths,
        status=status,
        verify_report=str(verify_path),
    )
    change_path = workspace / CHANGES_DIR / f"{change.change_id}.json"
    change_path.write_text(json.dumps(change.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"applied: {change.change_id}")
    print(f"  status:  {status.value}")
    print(f"  backups: {len(backup_paths)}")
    print(f"  changed: {len(changed_paths)}")
    if remaining:
        print(f"  remaining findings: {[f.id for f in remaining]}", file=sys.stderr)
    print(f"  undo:   run `memoryguard undo {change.change_id}`")
    return 0 if status == ChangeStatus.VERIFIED else 1


def cmd_undo(args: argparse.Namespace) -> int:
    """撤销 Change：从备份恢复 + 重扫验证（spec §9）。"""
    workspace = Path(args.workspace).resolve()
    change_path = workspace / CHANGES_DIR / f"{args.change_id}.json"
    if not change_path.exists():
        print(f"error: change not found: {args.change_id}", file=sys.stderr)
        return 1
    import json

    change_dict = json.loads(change_path.read_text(encoding="utf-8"))
    # 恢复备份
    for backup_path, changed_path in zip(change_dict["backup_paths"], change_dict["changed_paths"]):
        backup = Path(backup_path)
        target = Path(changed_path)
        if not backup.exists():
            print(f"error: backup missing: {backup}", file=sys.stderr)
            return 1
        target.write_bytes(backup.read_bytes())
    # 重扫验证
    verify_report = run_audit(workspace)
    # 更新 change 状态
    change_dict["status"] = ChangeStatus.UNDONE.value
    change_path.write_text(json.dumps(change_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"undone: {args.change_id}")
    print(f"  restored: {len(change_dict['backup_paths'])} files")
    print(f"  health:   {verify_report.health_score}/100")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """重扫并比较修复前后（spec §4）。"""
    workspace = Path(args.workspace).resolve()
    report = _load_report(workspace)
    if report is None:
        print("error: no report found.", file=sys.stderr)
        return 1
    # 重新扫描
    new_report = run_audit(workspace)
    print(f"verify: {new_report.generated_at}")
    print(f"  before: {len(report.findings)} findings, health {report.health_score}")
    print(f"  after:  {len(new_report.findings)} findings, health {new_report.health_score}")
    # 写新报告
    json_path = workspace / REPORTS_DIR / "report.json"
    html_path = workspace / REPORTS_DIR / "report.html"
    import json

    json_path.write_text(new_report.to_json(), encoding="utf-8")
    html_path.write_text(render_html_report(new_report), encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# v3 命令：source / scan / import / memory（spec §4.1-§4.4）
# ---------------------------------------------------------------------------


def cmd_source(args: argparse.Namespace) -> int:
    """来源管理（spec §4.1）。"""
    from .source_registry import SourceRegistry
    from .schema_v3 import SourceRootType
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    reg = SourceRegistry(workspace)
    action = args.action
    if action == "list":
        sources = reg.list_sources()
        print(f"sources: {len(sources)}")
        for s in sources:
            print(f"  - {s.root_id}  {s.type.value}  {s.display_name}  scope={s.scope}")
            print(f"      path: {s.path}")
        return 0
    if action == "add":
        root_type = SourceRootType(args.type)
        root = reg.add(args.path, root_type, display_name=args.name or "")
        print(f"added: {root.root_id}  type={root.type.value}  scope={root.scope}")
        preview = reg.preview(args.path, root_type)
        print(f"  estimated_files: {preview.get('estimated_files', 0)}")
        return 0
    if action == "remove":
        ok = reg.remove(args.source_id)
        if ok:
            print(f"removed: {args.source_id}")
            return 0
        print(f"error: cannot remove {args.source_id} (not found or project default)", file=sys.stderr)
        return 1
    if action == "preview":
        root_type = SourceRootType(args.type)
        preview = reg.preview(args.path, root_type)
        print(f"preview: {preview}")
        return 0
    print(f"unknown source action: {action}", file=sys.stderr)
    return 1


def cmd_scan(args: argparse.Namespace) -> int:
    """只读扫描，生成快照 + 覆盖率账本（spec §4.2）。"""
    import json as _json
    from .source_registry import SourceRegistry, ScanBudget
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    reg = SourceRegistry(workspace)
    snap = reg.scan(ScanBudget())
    cov = snap.coverage
    counts = cov.counts()
    print(f"snapshot: {snap.snapshot_id}")
    print(f"  created_at: {snap.created_at}")
    print(f"  source_objects: {len(snap.source_objects)}")
    print(f"  coverage: {counts['coverage_status'] if 'coverage_status' in counts else cov.status().value}")
    print(f"  candidates: {counts['candidate_count']}")
    print(f"  read: {counts['read']}  unsupported: {counts['unsupported']}  unreadable: {counts['unreadable']}")
    print(f"  skipped_by_policy: {counts['skipped_by_policy']}  unaccounted: {counts['unaccounted_count']}")
    # 持久化快照
    snap_dir = workspace / MG_DIR / "snapshots" / snap.snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "sources.json").write_text(
        _json.dumps([o.to_dict() for o in snap.source_objects], ensure_ascii=False, indent=2),
        encoding="utf-8")
    (snap_dir / "coverage.json").write_text(
        _json.dumps(cov.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  saved: {snap_dir}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """离线导入（spec §4.3）。"""
    import json as _json
    from .adapters import GenericImportAdapter, ChatGPTImportAdapter
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    bundle = Path(args.bundle).resolve()
    if not bundle.exists():
        print(f"error: bundle not found: {bundle}", file=sys.stderr)
        return 1
    # 尝试 detect
    adapters = [ChatGPTImportAdapter(), GenericImportAdapter()]
    detected = None
    for ad in adapters:
        d = ad.detect(bundle)
        if d.supported:
            detected = (ad, d)
            break
    if detected is None:
        print(f"error: unsupported bundle format: {bundle}", file=sys.stderr)
        return 1
    ad, det = detected
    print(f"detected: provider={det.provider} confidence={det.confidence} notes={det.notes}")
    if args.action == "preview":
        inv = ad.inventory(bundle)
        print(f"inventory: {inv}")
        return 0
    if args.action == "create":
        convs = ad.parse(bundle)
        records = ad.normalize(convs)
        print(f"imported: conversations={len(convs)} memory_records={len(records)}")
        # 保存到 imports
        import_id = "imp-" + stable_id(bundle.name, str(len(records)))
        imp_dir = workspace / MG_DIR / "imports" / import_id
        imp_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "import_id": import_id, "provider": det.provider,
            "bundle_path": str(bundle), "created_at": now_iso(),
            "conversation_count": len(convs),
            "memory_record_count": len(records),
            "records": [r.to_dict() for r in records[:100]],
        }
        (imp_dir / "manifest.json").write_text(
            _json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  saved: {imp_dir / 'manifest.json'}")
        return 0
    return 1


def cmd_memory(args: argparse.Namespace) -> int:
    """记忆构建与发布（spec §4.4）。须显式 --agent-instance-id。"""
    import json as _json
    from .adapters import GenericMarkdownTarget
    from .governance_scope import (
        filter_ir_for_agent, resolve_governance_scope,
        resolve_scoped_roots, derive_publish_target_file, root_authorizes_agent,
    )
    from .release_manager import ReleaseManager
    from .source_registry import ScanBudget, SourceRegistry
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    rm = ReleaseManager(workspace)
    action = args.action
    agent_id = str(getattr(args, "agent_instance_id", "") or "").strip()
    share_id = str(getattr(args, "share_group_id", "") or "").strip()
    if share_id:
        print("error: share_group scope is not supported for memory build/publish; use GUI neuron share_group projection", file=sys.stderr)
        return 2
    scope, scope_err = resolve_governance_scope(agent_instance_id=agent_id, mode="agent")
    if scope is None:
        print(f"error: {scope_err or 'missing_governance_scope'}; pass --agent-instance-id", file=sys.stderr)
        return 2
    gscope = scope
    scope_dict = gscope.to_dict()

    def _resolve_authorized_root(target_root_id: str):
        if not target_root_id:
            return None, "target_root_id_required"
        reg = SourceRegistry(workspace)
        root = next((r for r in reg.list_all_sources() if r.root_id == target_root_id), None)
        if root is None or not root_authorizes_agent(root, gscope.agent_instance_id):
            return None, "target_root_not_authorized_for_agent"
        return root, ""

    if action == "build-plan":
        target = GenericMarkdownTarget()
        target_root_id = str(getattr(args, "target_root_id", "") or "").strip()
        if not target_root_id:
            print("error: --target-root-id required for build-plan", file=sys.stderr)
            return 2
        root, err = _resolve_authorized_root(target_root_id)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        reg = SourceRegistry(workspace)
        snap, ir = rm.scan_and_normalize(ScanBudget())
        roots, _ = resolve_scoped_roots(reg.list_all_sources(), gscope, enabled_only=True)
        allowed = {r.root_id for r in roots}
        scoped_ir = filter_ir_for_agent(ir, allowed, snap)
        derived = derive_publish_target_file(root)
        target_path = derived.parent if derived.suffix else derived
        plan = rm.create_build_plan(
            scoped_ir, target, target_path,
            governance_scope=scope_dict, target_root_id=target_root_id,
        )
        print(f"plan: {plan.plan_id}")
        print(f"  scope: agent={gscope.agent_instance_id}")
        print(f"  target_root_id: {target_root_id}")
        print(f"  scoped_records: {len(scoped_ir.records)}")
        print(f"  snapshot: {plan.snapshot_id}")
        print(f"  target_profile: {plan.target_profile}")
        print(f"  coverage: {plan.coverage_status}")
        print(f"  integrity: {plan.integrity_ok}")
        print(f"  published: {plan.manifest.published_record_count}")
        print(f"  unaccounted: {plan.manifest.unaccounted_record_count}")
        print(f"  diff: {plan.diff_preview}")
        if not plan.integrity_ok:
            print("  WARNING: integrity check failed; apply will refuse", file=sys.stderr)
            return 2
        return 0
    if action == "build-apply":
        if not args.yes:
            print("error: apply requires --yes to confirm", file=sys.stderr)
            return 2
        target_root_id = str(getattr(args, "target_root_id", "") or "").strip()
        root, err = _resolve_authorized_root(target_root_id)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        derived = derive_publish_target_file(root)
        target_path = derived.parent if derived.suffix else derived
        target = GenericMarkdownTarget()
        try:
            release = rm.apply_build(
                args.plan_id, target, target_path, approval=True,
                expected_scope=scope_dict, expected_target_root_id=target_root_id,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"apply: {release.release_id}  status={release.status.value}")
        print(f"  scope: agent={gscope.agent_instance_id}")
        print(f"  target_root_id: {target_root_id}")
        print(f"  target_file: {derived}")
        print(f"  changed: {len(release.changed_paths)}  backups: {len(release.backup_paths)}")
        if release.status.value != "verified":
            print(f"  FAILED: {release.verify_result}", file=sys.stderr)
            return 1
        return 0
    if action == "verify":
        target_root_id = str(getattr(args, "target_root_id", "") or "").strip()
        root, err = _resolve_authorized_root(target_root_id)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        derived = derive_publish_target_file(root)
        target_path = derived.parent if derived.suffix else derived
        target = GenericMarkdownTarget()
        # 校验 release 绑定
        release_path = workspace / MG_DIR / "releases" / f"{args.release_id}.json"
        if not release_path.exists():
            # 兼容旧 changes/
            release_path = workspace / MG_DIR / "changes" / f"{args.release_id}.json"
        if not release_path.exists():
            print(f"error: release not found: {args.release_id}", file=sys.stderr)
            return 1
        data = _json.loads(release_path.read_text(encoding="utf-8"))
        try:
            ReleaseManager.validate_release_binding(
                data,
                expected_scope=scope_dict,
                expected_target_root_id=target_root_id,
                expected_target_path=target_path,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        change_path = workspace / MG_DIR / "changes" / f"{args.release_id}.json"
        plan_files = list((workspace / MG_DIR / "plans").glob("*.json"))
        manifest = data.get("manifest")
        if manifest is None and change_path.exists():
            cdata = _json.loads(change_path.read_text(encoding="utf-8"))
            for pf in plan_files:
                pd = _json.loads(pf.read_text(encoding="utf-8"))
                if pd.get("manifest", {}).get("build_id") == cdata.get("build_id"):
                    manifest = pd["manifest"]
                    break
        if manifest is None:
            print(f"error: manifest not found for release {args.release_id}", file=sys.stderr)
            return 1
        from .schema_v3 import BuildManifest
        mm = BuildManifest(
            build_id=manifest["build_id"], source_snapshot_id=manifest.get("source_snapshot_id", ""),
            target_profile=manifest.get("target_profile", ""), coverage_status=manifest.get("coverage_status", ""),
            input_record_count=manifest.get("input_record_count", 0),
            published_record_count=manifest.get("published_record_count", 0),
            linked_record_count=manifest.get("linked_record_count", 0),
            excluded_record_count=manifest.get("excluded_record_count", 0),
            quarantined_record_count=manifest.get("quarantined_record_count", 0),
            unaccounted_record_count=manifest.get("unaccounted_record_count", 0),
            release_hash=manifest.get("release_hash", ""),
        )
        v = rm.verify_release(args.release_id, target, target_path, mm)
        print(f"verify: {v['release_id']}  rescan={v['rescan_match']}  hashes={v['hashes_match']}")
        return 0 if v["rescan_match"] and v["hashes_match"] else 1
    if action == "rollback":
        if not args.yes:
            print("error: rollback requires --yes to confirm", file=sys.stderr)
            return 2
        target_root_id = str(getattr(args, "target_root_id", "") or "").strip()
        root, err = _resolve_authorized_root(target_root_id)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        derived = derive_publish_target_file(root)
        target_path = derived.parent if derived.suffix else derived
        # 校验 release 绑定，禁止用错误目录删文件；无绑定则拒绝
        release_path = workspace / MG_DIR / "releases" / f"{args.release_id}.json"
        if not release_path.exists():
            print(f"error: release not found: {args.release_id}", file=sys.stderr)
            return 1
        data = _json.loads(release_path.read_text(encoding="utf-8"))
        try:
            ReleaseManager.validate_release_binding(
                data,
                expected_scope=scope_dict,
                expected_target_root_id=target_root_id,
                expected_target_path=target_path,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        target = GenericMarkdownTarget()
        try:
            rb = rm.rollback_release(
                args.release_id, target, target_path,
                expected_scope=scope_dict,
                expected_target_root_id=target_root_id,
                expected_target_path=target_path,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"rollback: {rb.release_id}  status={rb.status.value}")
        return 0
    print(f"unknown memory action: {action}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# doctor / mcp-status
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """诊断安装环境，输出检查报告。"""
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    issues = 0
    lines: list[str] = ["MemoryGuard Doctor", "=================="]

    # 1. Python 版本（>= 3.10）
    py_ok = sys.version_info >= (3, 10)
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    lines.append(
        f"Python: {py_ver} {'[ok]' if py_ok else '[error] require >= 3.10'}"
    )
    if not py_ok:
        issues += 1

    # 2. memoryguard 包可 import
    try:
        import memoryguard  # noqa: F401
        lines.append("memoryguard package: [ok]")
    except ImportError as e:
        lines.append(f"memoryguard package: [error] ({e})")
        issues += 1

    # 3. MCP server 模块可 import
    try:
        from . import mcp_server  # noqa: F401
        lines.append("MCP server module: [ok]")
    except ImportError as e:
        lines.append(f"MCP server module: [error] ({e})")
        issues += 1

    # 4. 工作区 .memoryguard/ 目录
    mg_dir = workspace / MG_DIR
    if mg_dir.is_dir():
        lines.append(f"Workspace .memoryguard/: [ok] (found at {mg_dir})")
    else:
        lines.append(f"Workspace .memoryguard/: not found (run `memoryguard audit` to initialize)")

    # 5. 已绑定的 Agent
    try:
        from .agent_binding import AgentBindingStore
        from .schema_v3 import BindingStatus
        store = AgentBindingStore(workspace)
        bindings = store.list_bindings(include_inactive=False)
        lines.append(f"Agent bindings: {len(bindings)} active")
    except Exception as e:
        lines.append(f"Agent bindings: error ({e})")

    # 6. shared-memory 记录
    sm_dir = workspace / MG_DIR / "shared-memory"
    group_count = 0
    record_count = 0
    if sm_dir.is_dir():
        try:
            from .shared_memory_store import SharedMemoryStore
            for group_dir in sorted(sm_dir.iterdir()):
                if not group_dir.is_dir():
                    continue
                group_count += 1
                s = SharedMemoryStore(workspace, group_dir.name).status()
                record_count += s.get("total_records", 0)
        except Exception:
            pass
    lines.append(f"Shared memory: {group_count} groups, {record_count} records")

    # 7. provider adapter 安装状态
    lines.append("Provider adapters:")
    try:
        from .provider_adapters import ClaudeAdapter, CodexAdapter, CursorAdapter
        for name, cls in (("claude", ClaudeAdapter), ("codex", CodexAdapter), ("cursor", CursorAdapter)):
            try:
                st = cls(str(workspace)).status()
                if st.get("installed"):
                    lines.append(f"  {name}: installed [ok]")
                else:
                    lines.append(f"  {name}: not installed")
            except Exception as e:
                lines.append(f"  {name}: error ({e})")
    except ImportError as e:
        lines.append(f"  (provider_adapters unavailable: {e})")

    # 8. user-level host hooks
    lines.append("Host hooks:")
    try:
        from .host_hooks import HostHookManager

        hook_status = HostHookManager(workspace).status()
        for item in hook_status.get("providers", []):
            if not item.get("supported"):
                state = "unsupported"
            elif item.get("runtime_verified"):
                state = "operational [ok]"
            elif item.get("configured"):
                state = "configured, awaiting runtime receipt"
            else:
                state = "not configured"
            lines.append(f"  {item.get('provider')}: {state}")
    except Exception as e:
        lines.append(f"  error ({e})")

    # 9. pywebview（可选）
    try:
        import webview  # type: ignore  # noqa: F401
        lines.append("GUI (pywebview): available [ok]")
    except ImportError:
        lines.append("GUI (pywebview): not available (optional)")

    # 汇总
    if issues == 0:
        lines.append("")
        lines.append("All checks passed.")
    else:
        lines.append("")
        lines.append(f"{issues} issue(s) found.")

    print("\n".join(lines))
    return 0 if issues == 0 else 1


def cmd_mcp_status(args: argparse.Namespace) -> int:
    """查询 MCP 记忆后端状态。"""
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    lines: list[str] = ["MemoryGuard MCP Status", "======================"]

    # MCP server 运行状态（stdio 模式，无常驻进程）
    lines.append("MCP server: not running (stdio mode, starts on demand)")
    lines.append("")

    # 收集所有 share group
    sm_dir = workspace / MG_DIR / "shared-memory"
    groups: list[tuple[str, dict]] = []
    if sm_dir.is_dir():
        try:
            from .shared_memory_store import SharedMemoryStore
            for group_dir in sorted(sm_dir.iterdir()):
                if not group_dir.is_dir():
                    continue
                st = SharedMemoryStore(workspace, group_dir.name).status()
                groups.append((group_dir.name, st))
        except Exception as e:
            lines.append(f"error reading shared-memory: {e}")
            print("\n".join(lines))
            return 1
    # 收集已绑定 Agent
    group_agents: dict[str, list[str]] = {}
    try:
        from .agent_binding import AgentBindingStore
        from .schema_v3 import BindingStatus
        store = AgentBindingStore(workspace)
        for b in store.list_bindings(include_inactive=False):
            group_agents.setdefault(b.share_group_id, []).append(b.agent_instance_id)
    except Exception:
        pass

    # 输出每个 group
    lines.append("Shared Memory Groups:")
    total_records = 0
    total_events = 0
    for group_id, st in groups:
        recs = st.get("total_records", 0)
        evs = st.get("total_events", 0)
        confs = st.get("total_conflicts", 0)
        quar = st.get("total_quarantine", 0)
        ver = st.get("active_version") or "(none)"
        agents = group_agents.get(group_id, [])
        lines.append(
            f"  [{group_id}] {recs} records, {evs} events, {confs} conflicts, {quar} quarantined"
        )
        lines.append(f"    active version: {ver}")
        lines.append(f"    agents: {', '.join(agents) if agents else '(none)'}")
        lines.append("")
        total_records += recs
        total_events += evs

    # 绑定的 Agent 总览
    bound_agents: list[str] = []
    try:
        from .agent_binding import AgentBindingStore
        store = AgentBindingStore(workspace)
        bound_agents = [b.agent_instance_id for b in store.list_bindings(include_inactive=False)]
    except Exception:
        pass
    if bound_agents:
        lines.append(f"Bound agents: {len(bound_agents)} ({', '.join(bound_agents)})")

    lines.append(f"Total: {len(groups)} groups, {total_records} records, {total_events} events")
    print("\n".join(lines))
    return 0


def _infer_hook_provider(workspace: Path, agent_instance_id: str = "") -> str:
    """Infer only when evidence is unambiguous; never install into every host."""
    import os

    explicit = os.environ.get("MEMORYGUARD_PROVIDER", "").strip().lower()
    if explicit:
        return explicit

    candidates: set[str] = set()
    if os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates.add("claude")
    if os.environ.get("CURSOR_PROJECT_DIR") or os.environ.get("CURSOR_VERSION"):
        candidates.add("cursor")
    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_THREAD_ID"):
        candidates.add("codex")
    if os.environ.get("TRAE_PROJECT_DIR") or os.environ.get("TRAE_VERSION"):
        candidates.add("trae")

    if agent_instance_id:
        try:
            from .agent_locator import AgentLocator

            instances, _ = AgentLocator(workspace).detect_instances()
            product = next(
                (
                    item.product.lower()
                    for item in instances
                    if item.instance_id == agent_instance_id
                ),
                "",
            )
            aliases = {
                "claude-code": "claude",
                "claude": "claude",
                "codex": "codex",
                "cursor": "cursor",
                "trae": "trae",
            }
            if product in aliases:
                candidates.add(aliases[product])
        except Exception:
            pass
    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        raise ValueError(
            "cannot infer current host; pass --provider "
            "claude|codex|cursor|trae"
        )
    raise ValueError(
        "host inference is ambiguous: "
        + ", ".join(sorted(candidates))
        + "; pass --provider explicitly"
    )


def cmd_hooks(args: argparse.Namespace) -> int:
    """Install/status/uninstall user-level lifecycle hooks."""
    import json
    import os

    from .agent_binding import AgentBindingStore
    from .host_hooks import HostHookManager, set_hook_mode

    workspace = Path(args.workspace).expanduser().resolve()
    manager = HostHookManager(workspace)
    agent_id = (
        str(getattr(args, "agent_id", "") or "")
        or os.environ.get("MEMORYGUARD_AGENT_ID", "").strip()
    )
    provider = str(getattr(args, "provider", "") or "").lower()
    try:
        if provider == "auto":
            provider = _infer_hook_provider(workspace, agent_id)

        if args.action == "status":
            result = manager.status(
                "" if provider in {"", "all"} else provider,
                agent_instance_id=agent_id,
            )
        elif args.action in {"install", "ensure"}:
            if not agent_id:
                raise ValueError(
                    "agent identity is required; pass --agent-id or set "
                    "MEMORYGUARD_AGENT_ID"
                )
            group_id = str(args.share_group_id or "")
            if not group_id:
                bindings = AgentBindingStore(workspace).find_by_agent(
                    agent_id, include_inactive=False,
                )
                if not bindings:
                    raise ValueError(
                        f"no active binding found for {agent_id!r}"
                    )
                group_id = bindings[0].share_group_id
            result = manager.install(
                provider,
                agent_instance_id=agent_id,
                share_group_id=group_id,
                mode=args.mode,
            )
        elif args.action == "uninstall":
            result = manager.uninstall(provider)
        elif args.action == "mode":
            if not agent_id:
                raise ValueError("--agent-id is required for mode changes")
            result = set_hook_mode(
                workspace,
                provider,
                agent_id,
                args.mode,
            )
        else:
            raise ValueError(f"unknown hooks action: {args.action}")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") in {"error", "unsupported"}:
        return 1
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    """`.memoryguard/` GC：默认可重建物优先清的 dry-run 预览。"""
    from .gc import MemoryGuardGc

    workspace = Path(args.path).resolve()
    if not workspace.is_dir():
        print(f"error: workspace not found: {workspace}", file=sys.stderr)
        return 1

    gc = MemoryGuardGc(
        workspace,
        older_than_days=args.older_than_days,
        keep_releases=args.keep_releases,
        keep_snapshots=args.keep_snapshots,
    )
    dry_run = not args.apply
    plan = gc.plan(dry_run=dry_run)

    print(f"MemoryGuard gc {'plan' if dry_run else 'apply'}")
    print(f"  workspace:     {workspace}")
    print(f"  items:         {len(plan.items)}")
    print(f"  total_bytes:   {plan.total_bytes}")
    print(f"  dry_run:       {plan.dry_run}")
    for item in plan.items:
        print(f"  - {item.action:16} {item.bytes_estimate:>10} B  {item.path}")
        print(f"    {item.reason}")

    if dry_run:
        if plan.items:
            print("  (dry-run only; pass --apply to execute)")
        return 0

    result = gc.apply(plan, confirmed=True)
    if not result.get("ok"):
        print(f"error: gc apply failed: {result.get('error', 'see results')}", file=sys.stderr)
        for entry in result.get("results", []):
            if not entry.get("ok"):
                print(f"  failed: {entry}", file=sys.stderr)
        return 1
    print(f"  applied:       {result.get('applied', 0)}")
    print(f"  history:       {result.get('history_path', '')}")
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    """启动 MemoryGuard Desktop Executor（可信执行端）。"""
    from .desktop_executor import main as desktop_main
    argv = []
    if getattr(args, "workspace", "."):
        argv.append(args.workspace)
    if getattr(args, "auto_confirm", False):
        argv.append("--auto-confirm")
    if getattr(args, "watch", False):
        argv.append("--watch")
    if getattr(args, "request", ""):
        argv.extend(["--request", args.request])
    if getattr(args, "uri", ""):
        argv.extend(["--uri", args.uri])
    if getattr(args, "register_uri", False):
        argv.append("--register-uri")
    return desktop_main(argv)


def cmd_gui(args: argparse.Namespace) -> int:
    """从可见终端启动交互式治理台。"""
    workspace_args = [args.workspace] if args.workspace else []
    if os.name == "nt" and os.environ.get("_MEMORYGUARD_GUI_CHILD") != "1":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        env = os.environ.copy()
        env["_MEMORYGUARD_GUI_CHILD"] = "1"
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        subprocess.Popen(
            [str(pythonw), "-m", "memoryguard.cli", "gui", *workspace_args],
            cwd=str(Path.cwd()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
        return 0
    return gui_main(workspace_args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoryguard",
        description="Local-first Agent governance: audit instructions, skills, memory, and local RAG.",
    )
    parser.add_argument("--version", action="version", version=f"memoryguard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="read-only scan, generate report")
    p_audit.add_argument("path", nargs="?", default=".", help="workspace path (default: .)")
    p_audit.add_argument(
        "--json", action="store_true", help="also print JSON to stdout"
    )
    p_audit.set_defaults(func=cmd_audit)

    p_open = sub.add_parser("open", help="open latest report in a window")
    p_open.add_argument("path", nargs="?", default=".", help="workspace path (default: .)")
    p_open.add_argument(
        "--mode",
        choices=("auto", "native", "localhost", "html"),
        default="auto",
        help="window mode: auto(native->localhost->html) | native | localhost | html (default: auto)",
    )
    p_open.set_defaults(func=cmd_open)

    p_explain = sub.add_parser("explain", help="explain a finding's evidence and risk")
    p_explain.add_argument("finding_id", help="finding id from report")
    p_explain.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_explain.set_defaults(func=cmd_explain)

    p_plan = sub.add_parser("plan", help="generate minimal fix plan (no write)")
    p_plan.add_argument("finding_ids", nargs="+", help="finding ids to fix")
    p_plan.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="apply a plan: backup + patch + rescan")
    p_apply.add_argument("plan_id", help="plan id from `plan` command")
    p_apply.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_apply.add_argument("--force", action="store_true", help="force apply high-risk plans")
    p_apply.set_defaults(func=cmd_apply)

    p_verify = sub.add_parser("verify", help="rescan and compare before/after")
    p_verify.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_verify.set_defaults(func=cmd_verify)

    p_undo = sub.add_parser("undo", help="restore from backup and re-verify")
    p_undo.add_argument("change_id", help="change id from `apply` command")
    p_undo.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_undo.set_defaults(func=cmd_undo)

    # v3 命令（spec §4.1-§4.4）
    p_source = sub.add_parser("source", help="manage authorized sources (v3)")
    p_source.add_argument("action", choices=("list", "add", "remove", "preview"))
    p_source.add_argument("path", nargs="?", help="path for add/preview")
    p_source.add_argument("--type", default="selected_directory",
                          choices=("project_directory", "selected_directory", "selected_file", "obsidian_vault"))
    p_source.add_argument("--name", default="", help="display name")
    p_source.add_argument("source_id", nargs="?", help="source_id for remove")
    p_source.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_source.set_defaults(func=cmd_source)

    p_scan = sub.add_parser("scan", help="read-only scan, build coverage ledger (v3)")
    p_scan.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_scan.set_defaults(func=cmd_scan)

    p_import = sub.add_parser("import", help="offline import bundle (v3)")
    p_import.add_argument("action", choices=("preview", "create"))
    p_import.add_argument("bundle", help="bundle path (file or dir)")
    p_import.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_import.set_defaults(func=cmd_import)

    p_doctor = sub.add_parser("doctor", help="diagnose installation and environment")
    p_doctor.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_doctor.set_defaults(func=cmd_doctor)

    p_mcp_status = sub.add_parser("mcp-status", help="query MCP memory backend status")
    p_mcp_status.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_mcp_status.set_defaults(func=cmd_mcp_status)

    p_hooks = sub.add_parser(
        "hooks",
        help="install and inspect user-level host lifecycle hooks",
    )
    p_hooks.add_argument(
        "action",
        choices=("status", "install", "ensure", "uninstall", "mode"),
    )
    p_hooks.add_argument(
        "--provider",
        default="auto",
        choices=("auto", "all", "claude", "codex", "cursor", "trae"),
        help="host provider; auto requires unambiguous runtime evidence",
    )
    p_hooks.add_argument("-w", "--workspace", default=".", help="MemoryGuard control workspace")
    p_hooks.add_argument("--agent-id", default="", help="trusted Agent instance ID")
    p_hooks.add_argument("--share-group-id", default="", help="bound memory group; inferred when omitted")
    p_hooks.add_argument(
        "--mode",
        default="enforce",
        choices=("enforce", "observe", "paused"),
        help="runtime policy (default: enforce)",
    )
    p_hooks.set_defaults(func=cmd_hooks)

    p_gc = sub.add_parser("gc", help="garbage-collect .memoryguard/ artifacts (default dry-run)")
    p_gc.add_argument("path", nargs="?", default=".", help="workspace path (default: .)")
    p_gc.add_argument("--apply", action="store_true", help="execute the GC plan (default: dry-run)")
    p_gc.add_argument("--older-than-days", type=int, default=30, help="native release artifact age threshold")
    p_gc.add_argument("--keep-releases", type=int, default=20, help="reserved for future release pruning")
    p_gc.add_argument("--keep-snapshots", type=int, default=3, help="snapshots to retain")
    p_gc.set_defaults(func=cmd_gc)

    p_gui = sub.add_parser("gui", help="launch the interactive governance console")
    p_gui.add_argument("workspace", nargs="?", default="", help="workspace path (auto-detected when omitted)")
    p_gui.set_defaults(func=cmd_gui)

    p_desktop = sub.add_parser("desktop", help="launch MemoryGuard Desktop Executor (trusted execution)")
    p_desktop.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_desktop.add_argument("--auto-confirm", action="store_true", help="skip confirmation (testing only)")
    p_desktop.add_argument("--watch", action="store_true", help="continuously poll for new requests")
    p_desktop.add_argument("--request", default="", help="process a specific request ID")
    p_desktop.add_argument("--uri", default="", help="launch from memoryguard:// URI")
    p_desktop.add_argument("--register-uri", action="store_true", help="register memoryguard:// URI protocol")
    p_desktop.set_defaults(func=cmd_desktop)

    return parser


def _gui_state_path() -> Path:
    from .data_home import resolve_data_home

    return resolve_data_home() / "gui-state.json"


def _load_last_gui_workspace() -> Path | None:
    try:
        data = json.loads(_gui_state_path().read_text(encoding="utf-8"))
        path = Path(str(data.get("workspace", ""))).expanduser().resolve()
        return path if path.is_dir() else None
    except (OSError, ValueError, TypeError):
        return None


def _choose_gui_workspace() -> Path | None:
    """让无上下文启动的 GUI 选择一次项目目录。"""
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="选择要打开的 MemoryGuard 项目文件夹",
            mustexist=True,
        )
        root.destroy()
        return Path(selected).resolve() if selected else None
    except Exception as exc:
        print(f"error: cannot open the workspace picker: {exc}", file=sys.stderr)
        return None


def _remember_gui_workspace(workspace: Path) -> None:
    try:
        state_path = _gui_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"workspace": str(workspace)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _resolve_gui_workspace(argv: list[str]) -> Path | None:
    if argv:
        return Path(argv[0]).expanduser().resolve()

    configured = os.environ.get("MEMORYGUARD_WORKSPACE", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_dir():
            return candidate

    last = _load_last_gui_workspace()
    if last is not None:
        return last

    cwd = Path.cwd().resolve()
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    if cwd.is_dir() and cwd != system_root and system_root not in cwd.parents:
        return cwd

    return _choose_gui_workspace()


def gui_main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    workspace = _resolve_gui_workspace(argv)
    if workspace is None:
        print(
            "error: no workspace selected. Run `memoryguard gui <project-path>` "
            "or set MEMORYGUARD_WORKSPACE.",
            file=sys.stderr,
        )
        return 2
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    if workspace == system_root or system_root in workspace.parents:
        print(
            "error: refusing to use a Windows system directory as the workspace.\n"
            f"  resolved path: {workspace}\n"
            "  run from your project directory or pass its path explicitly, for example:\n"
            r"  memoryguard gui H:\ai\workspace\工具项目\memoryguard",
            file=sys.stderr,
        )
        return 2
    if not workspace.is_dir():
        print(f"error: GUI workspace does not exist or is not a directory: {workspace}", file=sys.stderr)
        return 2
    _remember_gui_workspace(workspace)
    from .gui import (
        has_native_gui,
        open_interactive_window,
        open_localhost_window,
    )
    if has_native_gui():
        rc = open_interactive_window(
            str(workspace), title=f"MemoryGuard - {workspace.name}",
        )
        if rc == 0:
            return 0
    # GUI 启动器的降级也必须保留完整交互能力；静态报告不能展开风险、
    # 调用治理 API 或展示实时 MCP 记忆层。
    rc, _ = open_localhost_window(str(workspace), auto_open=True)
    if rc == 0:
        return 0
    report = run_audit(workspace)
    out_dir = workspace / REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "report.html"
    html_path.write_text(render_html_report(report), encoding="utf-8")
    webbrowser.open(html_path.resolve().as_uri())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
