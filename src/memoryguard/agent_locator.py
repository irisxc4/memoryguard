"""v3.1 §3.2 AgentLocator 有限候选发现。

四层职责：
- AgentLocator：发现本机已知 Agent 候选，只做有限元数据探测
- AgentProfile：声明该 Agent 的已知本地表面、分类、加载关系和版本证据
- SourceRegistry：保存用户已经授权读取的 SourceRoot
- SourceAdapter：对授权根执行 inventory/read，不负责猜 Agent

安全边界（v3.1 §3.3）：
允许：
1. 当前工作区及其 Profile 明确声明的祖先规则文件；
2. Skill/CLI 宿主显式传入的配置根；
3. Profile 声明的少量固定路径模板；
4. 用户点击"检测本机 Agent"后，对固定路径执行 exists/lstat 和有限数量统计；
5. 用户手工选择的其他目录。

禁止：
- 从用户主目录递归搜索所有名为 memory/context/data 的文件夹；
- 候选阶段读取正文；
- 探测 Cookie、凭据和浏览器数据；
- 通过未公开路径猜测厂商私有数据库；
- 把云端或 GUI 内存声称为已追溯。
"""
from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_profiles import AgentProfileRegistry, detect_surface
from .agent_mapping import (
    AGENT_PRODUCT_MAP, IGNORED_DIRS,
    product_for_dot_dir, is_known_product, detect_stale_status,
)
from .schema_v3 import (
    AgentInstance, AgentProfile, DiscoveryEntry, DiscoveryLedger,
    SurfaceStatus, TakeoverState, TargetCapability, stable_hash,
)


@dataclass
class AgentCandidate:
    """discover_candidates() 返回的候选条目。"""
    dir_path: str
    dir_name: str
    product: str            # "unknown" 表示未映射
    has_profile: bool       # 是否有完整 AgentProfile
    stale_status: str       # "active" / "stale" / "likely_uninstalled"
    marked_uninstalled: bool = False
    mtime_iso: str = ""
    size_bytes: int = 0
    file_count: int = 0
    days_since_modified: float = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir_path": self.dir_path,
            "dir_name": self.dir_name,
            "product": self.product,
            "has_profile": self.has_profile,
            "stale_status": self.stale_status,
            "marked_uninstalled": self.marked_uninstalled,
            "mtime_iso": self.mtime_iso,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "days_since_modified": self.days_since_modified,
        }



@dataclass
class DetectionContext:
    """宿主显式传入的检测上下文（v3.1 §3.2）。"""

    workspace: str = ""
    host_id: str = ""
    config_root: str = ""
    platform: str = field(default_factory=lambda: platform.system().lower())

    @classmethod
    def from_workspace(cls, workspace: str | Path) -> "DetectionContext":
        return cls(
            workspace=str(workspace),
            host_id=os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "localhost",
            platform=platform.system().lower(),
        )


class AgentLocator:
    """v3.1 §3.2 有限候选发现器。

    只做：
    1. 加载所有 Profile
    2. 对每个 Profile 的每个 surface 执行 exists/lstat
    3. 生成 DiscoveryLedger
    4. 返回 AgentInstance 列表

    不做：
    - 不读正文
    - 不递归扫描
    - 不猜测未公开路径
    """

    def __init__(self, workspace: str | Path, context: DetectionContext | None = None):
        self.workspace = Path(workspace).resolve()
        self.context = context or DetectionContext.from_workspace(self.workspace)
        self.registry = AgentProfileRegistry(self.workspace)

    def discover_candidates(self, *, include_uninstalled: bool = False,
                            include_stale: bool = True,
                            include_unknown: bool = True) -> list[AgentCandidate]:
        """扫描当前系统 HOME 下所有 .<dir>，返回 Agent 候选列表。

        只扫当前系统 HOME（Path.home()），不做跨系统探测。
        每个候选取 stale_status，并查 uninstalled.json 判断是否已标记卸载。

        参数:
            include_uninstalled: 是否包含已标记为 uninstalled 的候选
            include_stale: 是否包含 stale 候选
            include_unknown: 是否包含未映射的 unknown 目录
        """
        home = Path.home()
        uninstalled = self._load_uninstalled_set()
        candidates: list[AgentCandidate] = []

        try:
            entries = list(home.iterdir())
        except (OSError, PermissionError):
            return []

        for entry in entries:
            if not entry.is_dir():
                continue
            if not entry.name.startswith("."):
                continue
            if entry.name in IGNORED_DIRS:
                continue

            product = product_for_dot_dir(entry.name) or "unknown"
            if product == "unknown" and not include_unknown:
                continue

            marked = product in uninstalled
            if marked and not include_uninstalled:
                continue

            stale_info = detect_stale_status(entry)
            if stale_info["stale_status"] == "stale" and not include_stale:
                continue

            has_profile = product != "unknown" and is_known_product(product)

            candidate = AgentCandidate(
                dir_path=str(entry),
                dir_name=entry.name,
                product=product,
                has_profile=has_profile,
                stale_status=stale_info["stale_status"],
                marked_uninstalled=marked,
                mtime_iso=stale_info["mtime_iso"],
                size_bytes=stale_info["size_bytes"],
                file_count=stale_info["file_count"],
                days_since_modified=stale_info["days_since_modified"],
            )
            candidates.append(candidate)

        return candidates

    def _load_uninstalled_set(self) -> set[str]:
        """读取 .memoryguard/cleanup/uninstalled.json，返回已标记卸载的产品名集合。"""
        path = self.workspace / ".memoryguard" / "cleanup" / "uninstalled.json"
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("products", []))
        except (ValueError, OSError):
            return set()

    def detect_instances(self) -> tuple[list[AgentInstance], dict[str, DiscoveryLedger]]:
        """检测所有 Profile 的实例。

        返回 (instances, ledgers_by_instance_id)。
        一个 Profile 只要有一个 surface FOUND 就算作 discovered。
        """
        instances: list[AgentInstance] = []
        ledgers: dict[str, DiscoveryLedger] = {}
        for profile in self.registry.list_profiles():
            instance, ledger = self._detect_one(profile)
            # v3.1 §3.4：只有至少一个 FOUND 才算 discovered
            found_count = sum(1 for e in ledger.entries if e.status == SurfaceStatus.FOUND)
            if found_count > 0:
                instances.append(instance)
                ledgers[instance.instance_id] = ledger
        return instances, ledgers

    def _detect_one(self, profile: AgentProfile) -> tuple[AgentInstance, DiscoveryLedger]:
        """探测单个 Profile 的所有 surface。"""
        instance_id = stable_hash(
            profile.profile_id,
            self.context.host_id,
            str(self.workspace),
        )
        surfaces_results: list[dict[str, Any]] = []
        entries: list[DiscoveryEntry] = []
        for surface in profile.surfaces:
            status, resolved = detect_surface(
                surface,
                home=Path.home(),
                workspace=self.workspace,
                appdata=os.environ.get("APPDATA", str(Path.home())),
            )
            entries.append(DiscoveryEntry(
                profile_id=profile.profile_id,
                surface_id=surface.surface_id,
                status=status,
                resolved_path=resolved,
                reason="" if status == SurfaceStatus.FOUND else f"surface {surface.surface_id} {status.value}",
            ))
            surfaces_results.append({
                "surface_id": surface.surface_id,
                "path_template": surface.path_template,
                "resolved_path": resolved,
                "status": status.value,
                "surface_role": surface.surface_role,
                "scope": surface.scope,
                "category": surface.category.value,
                "ingestion_policy": surface.ingestion_policy.value,
                "ownership": surface.ownership.value,
                "target_role": surface.target_role.value,
                "classification_confidence": surface.classification_confidence,
                "evidence_role": surface.evidence_role,
            })
        instance = AgentInstance(
            instance_id=instance_id,
            profile_id=profile.profile_id,
            product=profile.product,
            profile_version=profile.profile_version,
            platform=self.context.platform,
            host_id=self.context.host_id,
            workspace=str(self.workspace),
            config_root=self.context.config_root,
            surfaces=surfaces_results,
            target_capability=profile.target_capability,
        )
        ledger = DiscoveryLedger(instance_id=instance_id, entries=entries)
        return instance, ledger

    def get_selection_tree(self, instance_id: str) -> dict[str, Any]:
        """v3.2 改动包1：返回作用域优先的分类勾选树。

        结构从按 category 分组改为按 scope -> project_ref -> category 三层分组：
        {
          "instance_id": ...,
          "profile_id": ...,
          "product": ...,
          "scopes": [
            {
              "scope": "user",
              "scope_source": "profile_declared",
              "categories": [
                {
                  "category": "native_memory",
                  "files": [
                    {
                      "path": "/abs/path",
                      "surface_id": "...",
                      "scope": "user",
                      "scope_source": "profile_declared",
                      "project_ref": "",
                      "discovery_object_id": "...",
                      "ingestion_policy": "import_verbatim",
                      "ownership": "agent_managed",
                      "target_role": "takeover_input",
                      "default_selected": True,
                      "default_reason": "原生记忆默认选中",
                      "status": "found",
                      "confidence": 0.95,
                      "last_modified": "..."
                    }
                  ]
                }
              ]
            },
            {
              "scope": "project",
              "projects": [
                {
                  "project_ref": "MemoryGuard",
                  "scope_source": "project_resolver",
                  "categories": [...]
                }
              ]
            },
            {
              "scope": "unknown",
              "categories": [...]
            }
          ]
        }
        """
        instances, ledgers = self.detect_instances()
        instance = next((i for i in instances if i.instance_id == instance_id), None)
        if instance is None:
            return {"error": f"instance not found: {instance_id}"}

        # 按 scope -> project_ref -> category 三层分组
        scope_map: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
        # scope_map[scope][project_ref][category] = [files]

        for s in instance.surfaces:
            if s.get("status") != "found":
                continue
            scope = s.get("scope", "user")
            cat = s.get("category", "unknown")
            resolved = s.get("resolved_path", "")

            # 项目根目录展开：如果该 surface 是项目根目录，展开子目录
            effective_surfaces = [s]
            if self._is_project_root_surface(resolved):
                expanded = self._expand_project_root(resolved, s)
                if expanded:
                    effective_surfaces = expanded

            for es in effective_surfaces:
                es_resolved = es.get("resolved_path", "")
                # 项目归属：scope=project 时尝试从路径解析项目名
                project_ref = ""
                scope_source = "profile_declared"
                if es.get("_expanded_project_ref"):
                    project_ref = es["_expanded_project_ref"]
                    scope_source = "project_resolver"
                elif scope == "project":
                    project_ref = self._resolve_project_ref(es_resolved)
                    scope_source = "project_resolver" if project_ref else "fallback"
                elif scope == "user":
                    project_ref = ""
                    scope_source = "profile_declared"
                else:
                    scope_source = "fallback"

                es_scope = es.get("scope", scope)

                # 生成稳定的 discovery_object_id
                canonical_path = es_resolved.replace("\\", "/")
                surface_id = es.get("surface_id", "")
                dobj_id = stable_hash(instance_id, surface_id, canonical_path, "v1")

                # 默认选中策略
                ing = es.get("ingestion_policy", "extract_candidates")
                default_selected = ing == "import_verbatim"
                default_reason = self._default_selection_reason(ing, cat)

                file_info = {
                    "path": es_resolved,
                    "surface_id": surface_id,
                    "scope": es_scope,
                    "scope_source": scope_source,
                    "project_ref": project_ref,
                    "discovery_object_id": dobj_id,
                    "ingestion_policy": ing,
                    "ownership": es.get("ownership", "external_read_only"),
                    "target_role": es.get("target_role", "none"),
                    "default_selected": default_selected,
                    "default_reason": default_reason,
                    "status": es.get("status", "found"),
                    "confidence": es.get("classification_confidence", 0.5),
                    "last_modified": "",
                }

                scope_map.setdefault(es_scope, {})
                pr_key = project_ref or "_no_project"
                scope_map[es_scope].setdefault(pr_key, {})
                scope_map[es_scope][pr_key].setdefault(cat, [])
                scope_map[es_scope][pr_key][cat].append(file_info)

        # 构建返回结构
        scopes_output = []
        for scope in ["user", "project", "unknown"]:
            if scope not in scope_map:
                continue
            projects = scope_map[scope]
            if scope == "project":
                # 按项目分组
                project_list = []
                for pr_key, cat_map in projects.items():
                    project_ref = pr_key if pr_key != "_no_project" else ""
                    categories = [
                        {"category": cat, "files": files}
                        for cat, files in cat_map.items()
                    ]
                    project_list.append({
                        "project_ref": project_ref or "(未归属)",
                        "scope_source": "project_resolver" if project_ref else "fallback",
                        "categories": categories,
                    })
                scopes_output.append({
                    "scope": scope,
                    "scope_source": "project_resolver",
                    "projects": project_list,
                })
            else:
                # user / unknown 直接按 category 分组
                cat_map = projects.get("_no_project", {})
                categories = [
                    {"category": cat, "files": files}
                    for cat, files in cat_map.items()
                ]
                scopes_output.append({
                    "scope": scope,
                    "scope_source": "profile_declared" if scope == "user" else "fallback",
                    "categories": categories,
                })

        return {
            "instance_id": instance_id,
            "profile_id": instance.profile_id,
            "product": instance.product,
            "scopes": scopes_output,
        }

    def _resolve_project_ref(self, path: str) -> str:
        """从路径尝试解析项目名。

        规则：
        - workspace 下的固定 Surface -> 项目名为 workspace 本身的名称
        - ~/.claude/projects/<project_dir> -> 取 <project_dir>
        - ~/.codex/sessions/<project_dir> -> 取 <project_dir>
        - ~/.cursor/projects/<project_dir> -> 取 <project_dir>
        - ~/.codeium/windsurf/memories/<project_dir> -> 取 <project_dir>
        - ~/.trae-cn/memory/projects/<project_dir> -> 取 <project_dir>
        - ~/.zcode/cli/agents/<project_dir> -> 取 <project_dir>
        - 无法确认时返回空（进入 unknown）
        """
        if not path:
            return ""
        p = Path(path)
        try:
            ws = self.workspace.resolve()
            pr = p.resolve()
            # 真实父子路径判断，避免 C:\project 和 C:\project-old 误判
            if pr == ws or ws in pr.parents:
                return ws.name
        except (ValueError, OSError):
            pass
        normalized = path.replace("\\", "/")
        # 各 Agent 的项目目录模式
        project_patterns = [
            "/.claude/projects/",
            "/.codex/sessions/",
            "/.cursor/projects/",
            "/.codeium/windsurf/memories/",
            "/.trae-cn/memory/projects/",
            "/.zcode/cli/agents/",
            "/.trae-cn/work/",
        ]
        for pattern in project_patterns:
            if pattern in normalized:
                parts = normalized.split(pattern)
                if len(parts) > 1 and parts[1]:
                    # 取第一级目录名（可能后面还有子路径）
                    segments = parts[1].split("/")
                    if segments and segments[0]:
                        return segments[0]
        return ""

    # 需要展开子目录的项目根目录模式
    _PROJECT_ROOT_PATTERNS = [
        "/.claude/projects",
        "/.codex/sessions",
        "/.cursor/projects",
        "/.codeium/windsurf/memories",
        "/.trae-cn/memory/projects",
        "/.zcode/cli/agents",
    ]

    def _is_project_root_surface(self, resolved_path: str) -> bool:
        """判断该 surface 路径是否是项目根目录（含多个项目子目录）。"""
        if not resolved_path:
            return False
        normalized = resolved_path.replace("\\", "/")
        for pattern in self._PROJECT_ROOT_PATTERNS:
            if normalized.rstrip("/").endswith(pattern):
                return True
        return False

    def _expand_project_root(self, resolved_path: str, surface: dict) -> list[dict]:
        """展开项目根目录，为每个子目录生成一个 DiscoveryObject。

        返回展开后的 surface 列表，每个带独立 project_ref 和 resolved_path。
        """
        root = Path(resolved_path)
        if not root.exists() or not root.is_dir():
            return []
        expanded = []
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            # 跳过隐藏目录和缓存目录
            if sub.name.startswith(".") or sub.name.startswith("_"):
                continue
            expanded_surface = dict(surface)
            expanded_surface["resolved_path"] = str(sub)
            expanded_surface["surface_id"] = surface.get("surface_id", "") + ":" + sub.name
            expanded_surface["_expanded_project_ref"] = sub.name
            expanded.append(expanded_surface)
        return expanded

    @staticmethod
    def _default_selection_reason(ingestion_policy: str, category: str) -> str:
        """默认选中/不选中的原因。"""
        reasons = {
            "import_verbatim": "原生记忆默认选中，读取后仍需确认治理",
            "extract_candidates": "普通文档需手动勾选",
            "govern_only": "规则/指令文件默认治理，不自动萃取为事实",
            "evidence_only": "运行证据仅用于评估，默认不选",
            "ignore": "密钥/认证/缓存始终排除",
        }
        return reasons.get(ingestion_policy, "按策略处理")

    def save_discovery(self, instances: list[AgentInstance],
                       ledgers: dict[str, DiscoveryLedger]) -> Path:
        """持久化 DiscoveryLedger 到 .memoryguard/discovery/。"""
        out_dir = self.workspace / ".memoryguard" / "discovery"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "latest.json"
        data = {
            "detected_at": ledgers and list(ledgers.values())[0].entries[0].reason or "",
            "context": {
                "workspace": str(self.workspace),
                "host_id": self.context.host_id,
                "platform": self.context.platform,
            },
            "instances": [i.to_dict() for i in instances],
            "ledgers": {k: v.to_dict() for k, v in ledgers.items()},
        }
        out_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out_path

    def validate_discovery_objects(self, instance_id: str,
                                   discovery_object_ids: list[str]) -> dict[str, dict]:
        """验证 discovery_object_id 是否属于当前 Agent 实例。

        返回 {discovery_object_id: {valid: bool, file_info: dict|None, reason: str}}
        """
        tree = self.get_selection_tree(instance_id)
        if "error" in tree:
            return {dobj_id: {"valid": False, "file_info": None, "reason": "instance not found"} for dobj_id in discovery_object_ids}

        server_index: dict[str, dict] = {}
        for scope_obj in tree.get("scopes", []):
            for proj in scope_obj.get("projects", []):
                for cat in proj.get("categories", []):
                    for f in cat.get("files", []):
                        dobj_id = f.get("discovery_object_id", "")
                        if dobj_id:
                            info = dict(f)
                            info["category"] = cat.get("category", "unknown")
                            info["scope"] = scope_obj.get("scope", info.get("scope", "unknown"))
                            info["scope_source"] = proj.get("scope_source", scope_obj.get("scope_source", info.get("scope_source", "fallback")))
                            info["project_ref"] = proj.get("project_ref", info.get("project_ref", ""))
                            server_index[dobj_id] = info
            for cat in scope_obj.get("categories", []):
                for f in cat.get("files", []):
                    dobj_id = f.get("discovery_object_id", "")
                    if dobj_id:
                        info = dict(f)
                        info["category"] = cat.get("category", "unknown")
                        info["scope"] = scope_obj.get("scope", info.get("scope", "unknown"))
                        info["scope_source"] = scope_obj.get("scope_source", info.get("scope_source", "fallback"))
                        server_index[dobj_id] = info

        result = {}
        for dobj_id in discovery_object_ids:
            if dobj_id not in server_index:
                result[dobj_id] = {"valid": False, "file_info": None, "reason": "discovery_object_id not found in server snapshot"}
            else:
                f = server_index[dobj_id]
                if f.get("status") != "found":
                    result[dobj_id] = {"valid": False, "file_info": None, "reason": f"surface status is {f.get('status')}, not found"}
                else:
                    result[dobj_id] = {"valid": True, "file_info": f, "reason": ""}
        return result


def compute_takeover_state(instance: AgentInstance,
                           ledger: DiscoveryLedger | None,
                           selection_committed: bool,
                           canonicalized: bool,
                           release_planned: bool,
                           published: bool,
                           runtime_verified: bool,
                           drifted: bool = False) -> TakeoverState:
    """v3.1 §2.3 接管状态机。

    优先级（从高到低）：
    DRIFTED > OPERATIONAL > PUBLISHED > RELEASE_PLANNED > CANONICALIZED >
    SELECTED > PARTIAL（覆盖缺口） > DISCOVERED > NOT_DETECTED
    """
    if drifted:
        return TakeoverState.DRIFTED
    if runtime_verified:
        return TakeoverState.OPERATIONAL
    if published:
        return TakeoverState.PUBLISHED
    if release_planned:
        return TakeoverState.RELEASE_PLANNED
    if canonicalized:
        return TakeoverState.CANONICALIZED
    if selection_committed:
        return TakeoverState.SELECTED
    # 覆盖缺口优先于 DISCOVERED（v3.1 §2.3：PARTIAL 表示部分覆盖）
    if ledger is not None:
        cnt = ledger.counts()
        if cnt.get("unaccounted_count", 0) > 0 or cnt.get("missing", 0) > 0:
            return TakeoverState.PARTIAL
    if instance is not None:
        return TakeoverState.DISCOVERED
    return TakeoverState.NOT_DETECTED
