"""v3.2 改动包2 新增模块：拆分程序安装检测和数据残留检测。

核心变化：
- 不再用目录修改时间判断程序是否安装（旧方法见 agent_mapping.detect_stale_status）
- 安装检测独立为声明式探针：PATH / 注册表 / 已知目录 / 包管理器 / 进程证据
- 数据残留检测只做 stat，不读正文
- 生命周期评估按状态矩阵输出：installed / installed_no_data / data_only / uncertain / not_detected / ignored

安全边界：
- 只做 stat / which / 注册表读取 / 有限进程查询，不读正文
- 不做递归扫描
- 配置目录只能算 data_evidence，不能算 install_evidence
- detect_stale_status() 从 agent_mapping.py 导入但降级为数据活跃度计算，不作为安装判据

生命周期状态矩阵：
| 安装证据     | 本地数据 | 正确状态           |
|-------------|---------|-------------------|
| 强证据存在   | 有      | installed          |
| 强证据存在   | 无      | installed_no_data  |
| 无安装证据   | 有      | data_only          |
| 证据不完整   | 有      | uncertain          |
| 无          | 无      | not_detected       |
| 用户标记忽略 | 任意    | ignored            |
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema_v3 import stable_hash
from .agent_mapping import detect_stale_status  # 降级使用：仅取数据活跃度，不作为安装判据
from .agent_profiles import expand_path

try:
    import winreg
except ImportError:
    winreg = None


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 强证据探针类型（path_executable / windows_registry / known_install_dir / package_manager）
# process_evidence 为弱证据，不单独构成安装判据
STRONG_PROBE_TYPES: frozenset[str] = frozenset({
    "path_executable",
    "windows_registry",
    "known_install_dir",
    "package_manager",
    "vscode_extension",
})

# 常见文件扩展名（用于判断 surface 路径是文件还是目录）
_FILE_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".txt",
    ".db", ".sqlite", ".sqlite3", ".log", ".ini", ".cfg",
})


# ---------------------------------------------------------------------------
# 内置安装探针配置
# ---------------------------------------------------------------------------

DEFAULT_INSTALL_PROBES: dict[str, list[dict[str, Any]]] = {
    "claude-code": [
        {"probe_type": "path_executable", "command": "claude"},
        {"probe_type": "package_manager", "manager": "npm",
         "package": "@anthropic-ai/claude-code"},
        {"probe_type": "vscode_extension", "extension_prefix": "anthropic.claude-code"},
        {"probe_type": "known_install_dir",
         "path_template": "%LOCALAPPDATA%/claude-cli-nodejs"},
        {"probe_type": "known_install_dir",
         "path_template": "%LOCALAPPDATA%/Claude"},
    ],
    "codex": [
        {"probe_type": "path_executable", "command": "codex"},
        {"probe_type": "vscode_extension", "extension_prefix": "openai.chatgpt"},
        {"probe_type": "vscode_extension", "extension_prefix": "openai.codex"},
        {"probe_type": "known_install_dir",
         "path_template": "%HOME%/.codex"},
    ],
    "cursor": [
        {"probe_type": "windows_registry", "key_name": "Cursor",
         "search": "uninstall"},
        {"probe_type": "known_install_dir",
         "path_template": "%LOCALAPPDATA%/Programs/cursor"},
    ],
    "windsurf": [
        {"probe_type": "windows_registry", "key_name": "Windsurf",
         "search": "uninstall"},
        {"probe_type": "known_install_dir",
         "path_template": "%LOCALAPPDATA%/Programs/windsurf"},
    ],
    "trae": [
        {"probe_type": "windows_registry", "key_name": "TRAE Work CN",
         "search": "uninstall"},
        {"probe_type": "known_install_dir",
         "path_template": "%LOCALAPPDATA%/Trae"},
    ],
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class InstallEvidence:
    """单条安装探针检测结果。"""
    probe_type: str   # path_executable / windows_registry / known_install_dir / package_manager / process_evidence
    target: str       # 检查的目标路径/键名/包名
    found: bool       # 是否找到
    detail: str       # 详情

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_type": self.probe_type,
            "target": self.target,
            "found": self.found,
            "detail": self.detail,
        }


@dataclass
class DataEvidence:
    """单条数据残留检测结果。"""
    dir_path: str
    exists: bool
    file_count: int
    size_bytes: int
    last_activity_at: str   # ISO 时间
    days_since_activity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir_path": self.dir_path,
            "exists": self.exists,
            "file_count": self.file_count,
            "size_bytes": self.size_bytes,
            "last_activity_at": self.last_activity_at,
            "days_since_activity": self.days_since_activity,
        }


@dataclass
class LifecycleAssessment:
    """Agent 生命周期综合评估结果。"""
    lifecycle_state: str    # installed / installed_no_data / data_only / uncertain / not_detected / ignored
    install_confidence: float  # 0.0 ~ 1.0
    install_evidence: list[InstallEvidence]
    data_evidence: list[DataEvidence]
    last_activity_at: str
    reason_codes: list[str]
    candidate_id: str       # 稳定 ID: hash(product, host_id, config_root)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_state": self.lifecycle_state,
            "install_confidence": self.install_confidence,
            "install_evidence": [e.to_dict() for e in self.install_evidence],
            "data_evidence": [e.to_dict() for e in self.data_evidence],
            "last_activity_at": self.last_activity_at,
            "reason_codes": list(self.reason_codes),
            "candidate_id": self.candidate_id,
        }


# ---------------------------------------------------------------------------
# AgentInstallDetector
# ---------------------------------------------------------------------------


class AgentInstallDetector:
    """Agent 安装检测与生命周期评估器。

    职责拆分：
    - detect_install(): 只检测程序是否安装（PATH / 注册表 / 已知目录 / 包管理器 / 进程）
    - detect_data_residue(): 只检测数据残留（stat，不读正文）
    - assess_lifecycle(): 综合两者，按状态矩阵输出生命周期状态

    安全边界：
    - 只做 stat / which / 注册表读取 / 有限进程查询
    - 不读正文，不递归扫描
    - 配置目录只算 data_evidence，不算 install_evidence
    """

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self._host_id = (
            os.environ.get("COMPUTERNAME")
            or os.environ.get("HOSTNAME")
            or "localhost"
        )
        self._platform = platform.system().lower()
        self._home = Path.home()
        self._localappdata = os.environ.get(
            "LOCALAPPDATA", str(self._home / "AppData" / "Local")
        )
        self._appdata = os.environ.get(
            "APPDATA", str(self._home / "AppData" / "Roaming")
        )

    # -----------------------------------------------------------------------
    # 公开 API
    # -----------------------------------------------------------------------

    def detect_install(
        self, product: str, install_probes: list[dict]
    ) -> list[InstallEvidence]:
        """执行安装探针检测。"""
        results: list[InstallEvidence] = []
        for probe in install_probes:
            probe_type = probe.get("probe_type", "")
            handler = {
                "path_executable": self._probe_path_executable,
                "windows_registry": self._probe_windows_registry,
                "known_install_dir": self._probe_known_install_dir,
                "package_manager": self._probe_package_manager,
                "process_evidence": self._probe_process_evidence,
                "vscode_extension": self._probe_vscode_extension,
            }.get(probe_type)
            if handler:
                results.append(handler(probe))
            else:
                results.append(InstallEvidence(
                    probe_type=probe_type or "unknown",
                    target=str(probe),
                    found=False,
                    detail=f"unknown probe type: {probe_type}",
                ))
        return results

    def detect_data_residue(
        self, product: str, data_paths: list[str]
    ) -> list[DataEvidence]:
        """检测数据残留（只做 stat，不读正文）。

        detect_stale_status() 降级使用：仅取 file_count / size_bytes / mtime_iso /
        days_since_modified，不使用 stale_status 作为安装判据。
        """
        results: list[DataEvidence] = []
        for path_str in data_paths:
            path = Path(path_str)
            if not path.exists():
                results.append(DataEvidence(
                    dir_path=path_str,
                    exists=False,
                    file_count=0,
                    size_bytes=0,
                    last_activity_at="",
                    days_since_activity=-1.0,
                ))
                continue
            if path.is_file():
                st = path.stat()
                mtime_iso = datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).isoformat()
                days = (time.time() - st.st_mtime) / (24 * 3600)
                results.append(DataEvidence(
                    dir_path=path_str,
                    exists=True,
                    file_count=1,
                    size_bytes=st.st_size,
                    last_activity_at=mtime_iso,
                    days_since_activity=round(days, 1),
                ))
            else:
                # 目录 -> detect_stale_status 降级为数据活跃度计算
                stale_info = detect_stale_status(path)
                results.append(DataEvidence(
                    dir_path=path_str,
                    exists=True,
                    file_count=stale_info.get("file_count", 0),
                    size_bytes=stale_info.get("size_bytes", 0),
                    last_activity_at=stale_info.get("mtime_iso", ""),
                    days_since_activity=stale_info.get(
                        "days_since_modified", -1.0
                    ),
                ))
        return results

    def assess_lifecycle(
        self,
        product: str,
        install_probes: list[dict],
        data_paths: list[str],
        marked_ignored: bool = False,
        profile_id: str = "",
    ) -> LifecycleAssessment:
        """综合评估生命周期状态。

        candidate_id 基于 product + host_id + profile_id，不含 data_paths 和 workspace，
        因此清掉残留目录或换工作区不会改变 candidate_id。
        """
        candidate_id = stable_hash(
            product, self._host_id, profile_id or product,
        )

        install_ev = self.detect_install(product, install_probes)
        data_ev = self.detect_data_residue(product, data_paths)

        if marked_ignored:
            return LifecycleAssessment(
                lifecycle_state="ignored",
                install_confidence=0.0,
                install_evidence=install_ev,
                data_evidence=data_ev,
                last_activity_at=self._latest_activity(data_ev),
                reason_codes=["marked_ignored"],
                candidate_id=candidate_id,
            )

        strong_found = [
            e for e in install_ev
            if e.found and e.probe_type in STRONG_PROBE_TYPES
        ]
        weak_found = [
            e for e in install_ev
            if e.found and e.probe_type == "process_evidence"
        ]
        has_strong = len(strong_found) > 0
        has_weak = len(weak_found) > 0
        has_data = any(e.exists for e in data_ev)
        last_activity = self._latest_activity(data_ev)

        # 安装置信度
        if has_strong:
            confidence = min(1.0, 0.5 + 0.25 * len(strong_found))
        elif has_weak:
            confidence = 0.3
        else:
            confidence = 0.0

        # reason_codes
        reason_codes: list[str] = []
        if has_strong:
            reason_codes.append(
                f"strong_install_evidence:"
                f"{','.join(e.probe_type for e in strong_found)}"
            )
        if has_weak:
            reason_codes.append("weak_install_evidence:process_evidence")
        if has_data:
            data_dirs = sum(1 for e in data_ev if e.exists)
            reason_codes.append(f"data_residue:{data_dirs}_dirs")
        if not install_ev:
            reason_codes.append("no_install_probes")
        if not data_ev:
            reason_codes.append("no_data_paths")

        # 状态矩阵
        if has_strong and has_data:
            state = "installed"
        elif has_strong and not has_data:
            state = "installed_no_data"
        elif not has_strong and has_weak and has_data:
            state = "uncertain"
        elif not has_strong and has_weak and not has_data:
            state = "uncertain"
            reason_codes.append("weak_install_no_data")
        elif not has_strong and not has_weak and has_data:
            state = "data_only"
        else:
            state = "not_detected"

        return LifecycleAssessment(
            lifecycle_state=state,
            install_confidence=confidence,
            install_evidence=install_ev,
            data_evidence=data_ev,
            last_activity_at=last_activity,
            reason_codes=reason_codes,
            candidate_id=candidate_id,
        )

    def detect_all(
        self,
        profiles: list,
        marked_ignored_products: set[str] | None = None,
    ) -> list[LifecycleAssessment]:
        """检测所有 Profile 的生命周期。"""
        marked = marked_ignored_products or set()
        results: list[LifecycleAssessment] = []
        for profile in profiles:
            product = self._get_product_name(profile)
            install_probes = self._get_install_probes(profile, product)
            data_paths = self._get_data_paths(profile)
            results.append(self.assess_lifecycle(
                product=product,
                install_probes=install_probes,
                data_paths=data_paths,
                marked_ignored=product in marked,
            ))
        return results

    # -----------------------------------------------------------------------
    # 探针实现
    # -----------------------------------------------------------------------

    def _probe_path_executable(self, probe: dict) -> InstallEvidence:
        """PATH 可执行文件探针。"""
        command = probe.get("command", "")
        found_path = shutil.which(command) if command else None
        return InstallEvidence(
            probe_type="path_executable",
            target=command,
            found=found_path is not None,
            detail=found_path or f"not in PATH: {command}",
        )

    def _probe_windows_registry(self, probe: dict) -> InstallEvidence:
        """Windows 注册表探针（卸载表 / App Paths）。"""
        key_name = probe.get("key_name", "")
        search = probe.get("search", "uninstall")
        if winreg is None or self._platform != "windows":
            return InstallEvidence(
                probe_type="windows_registry",
                target=key_name,
                found=False,
                detail="not windows platform",
            )
        try:
            found, detail = self._search_registry(key_name, search)
            return InstallEvidence(
                probe_type="windows_registry",
                target=key_name,
                found=found,
                detail=detail,
            )
        except OSError as e:
            return InstallEvidence(
                probe_type="windows_registry",
                target=key_name,
                found=False,
                detail=f"registry read error: {e}",
            )

    def _probe_known_install_dir(self, probe: dict) -> InstallEvidence:
        """已知安装目录探针。"""
        path_template = probe.get("path_template", "")
        resolved = self._expand_probe_path(path_template)
        try:
            path = Path(resolved)
            exists = path.exists() and path.is_dir()
            return InstallEvidence(
                probe_type="known_install_dir",
                target=path_template,
                found=exists,
                detail=(
                    f"exists at {resolved}" if exists
                    else f"not found: {resolved}"
                ),
            )
        except OSError as e:
            return InstallEvidence(
                probe_type="known_install_dir",
                target=path_template,
                found=False,
                detail=f"stat error: {e}",
            )

    def _probe_package_manager(self, probe: dict) -> InstallEvidence:
        """包管理器探针（npm global / pip）。"""
        manager = probe.get("manager", "")
        package = probe.get("package", "")
        if manager == "npm":
            return self._probe_npm_global(package)
        if manager == "pip":
            return self._probe_pip_package(package)
        return InstallEvidence(
            probe_type="package_manager",
            target=f"{manager}:{package}",
            found=False,
            detail=f"unsupported manager: {manager}",
        )

    def _probe_process_evidence(self, probe: dict) -> InstallEvidence:
        """进程证据探针（可选，弱证据）。"""
        process_name = probe.get("process_name", "")
        if not process_name:
            return InstallEvidence(
                probe_type="process_evidence",
                target="",
                found=False,
                detail="no process_name specified",
            )
        try:
            if self._platform == "windows":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                result = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {process_name}",
                     "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=creationflags,
                )
                found = process_name.lower() in result.stdout.lower()
            else:
                result = subprocess.run(
                    ["pgrep", "-x", process_name],
                    capture_output=True, text=True, timeout=5,
                )
                found = result.returncode == 0
            return InstallEvidence(
                probe_type="process_evidence",
                target=process_name,
                found=found,
                detail="running" if found else "not running",
            )
        except Exception as e:
            return InstallEvidence(
                probe_type="process_evidence",
                target=process_name,
                found=False,
                detail=f"process check error: {e}",
            )

    # -----------------------------------------------------------------------
    # 探针辅助
    # -----------------------------------------------------------------------

    def _search_registry(
        self, key_name: str, search: str
    ) -> tuple[bool, str]:
        """搜索 Windows 注册表（只读，不写）。"""
        if search == "uninstall":
            roots = [
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_CURRENT_USER,
                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ]
            for root, base_path in roots:
                try:
                    with winreg.OpenKey(root, base_path) as parent_key:
                        subkey_count = winreg.QueryInfoKey(parent_key)[0]
                        for i in range(subkey_count):
                            try:
                                subkey_name = winreg.EnumKey(parent_key, i)
                                with winreg.OpenKey(
                                    parent_key, subkey_name
                                ) as sk:
                                    try:
                                        display_name, _ = (
                                            winreg.QueryValueEx(
                                                sk, "DisplayName"
                                            )
                                        )
                                        if key_name.lower() in (
                                            display_name.lower()
                                        ):
                                            return True, (
                                                f"found: {display_name} "
                                                f"({subkey_name})"
                                            )
                                    except FileNotFoundError:
                                        continue
                            except OSError:
                                continue
                except OSError:
                    continue
            return False, f"DisplayName not matching: {key_name}"

        if search == "app_paths":
            app_paths = (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
            )
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, app_paths
                ) as parent_key:
                    subkey_count = winreg.QueryInfoKey(parent_key)[0]
                    for i in range(subkey_count):
                        try:
                            subkey_name = winreg.EnumKey(parent_key, i)
                            if key_name.lower() in subkey_name.lower():
                                return True, (
                                    f"found app path: {subkey_name}"
                                )
                        except OSError:
                            continue
            except OSError:
                pass
            return False, f"not found in App Paths: {key_name}"

        return False, f"unknown search type: {search}"

    def _probe_npm_global(self, package: str) -> InstallEvidence:
        """检查 npm 全局包安装（只检查目录存在性，不运行 npm）。"""
        npm_path = shutil.which("npm")
        if not npm_path:
            return InstallEvidence(
                probe_type="package_manager",
                target=f"npm:{package}",
                found=False,
                detail="npm not in PATH",
            )
        candidates: list[Path] = []
        if self._platform == "windows":
            candidates.append(
                Path(self._appdata) / "npm" / "node_modules" / package
            )
        else:
            candidates.append(Path("/usr/lib/node_modules") / package)
            candidates.append(Path("/usr/local/lib/node_modules") / package)
            candidates.append(
                self._home / ".npm-global" / "lib" / "node_modules" / package
            )
        for candidate in candidates:
            if candidate.exists() and candidate.is_dir():
                return InstallEvidence(
                    probe_type="package_manager",
                    target=f"npm:{package}",
                    found=True,
                    detail=f"found at {candidate}",
                )
        return InstallEvidence(
            probe_type="package_manager",
            target=f"npm:{package}",
            found=False,
            detail=(
                f"package dir not found in "
                f"{len(candidates)} candidate locations"
            ),
        )

    def _probe_pip_package(self, package: str) -> InstallEvidence:
        """检查 pip 包安装（importlib.metadata，只读元数据）。"""
        try:
            from importlib.metadata import distribution, PackageNotFoundError
        except ImportError:
            return InstallEvidence(
                probe_type="package_manager",
                target=f"pip:{package}",
                found=False,
                detail="importlib.metadata not available",
            )
        try:
            dist = distribution(package)
            return InstallEvidence(
                probe_type="package_manager",
                target=f"pip:{package}",
                found=True,
                detail=f"found: version {dist.version}",
            )
        except PackageNotFoundError:
            return InstallEvidence(
                probe_type="package_manager",
                target=f"pip:{package}",
                found=False,
                detail=f"package not installed: {package}",
            )
        except Exception as e:
            return InstallEvidence(
                probe_type="package_manager",
                target=f"pip:{package}",
                found=False,
                detail=f"metadata read error: {e}",
            )

    def _probe_vscode_extension(self, probe: dict) -> InstallEvidence:
        """VSCode/Cursor 扩展探针：检查 ~/.vscode/extensions 或 ~/.cursor/extensions。"""
        extension_prefix = probe.get("extension_prefix", "")
        editor_dirs = [
            self._home / ".vscode" / "extensions",
            self._home / ".cursor" / "extensions",
            self._home / ".windsurf" / "extensions",
            self._home / ".trae-cn" / "extensions",
        ]
        for ext_dir in editor_dirs:
            if not ext_dir.exists() or not ext_dir.is_dir():
                continue
            for sub in ext_dir.iterdir():
                if sub.is_dir() and sub.name.lower().startswith(extension_prefix.lower()):
                    return InstallEvidence(
                        probe_type="vscode_extension",
                        target=f"{ext_dir.name}/{extension_prefix}",
                        found=True,
                        detail=f"found: {sub.name}",
                    )
        return InstallEvidence(
            probe_type="vscode_extension",
            target=extension_prefix,
            found=False,
            detail=f"extension not found in {len(editor_dirs)} editor dirs",
        )

    # -----------------------------------------------------------------------
    # Profile 辅助
    # -----------------------------------------------------------------------

    def _get_product_name(self, profile: Any) -> str:
        return getattr(profile, "product", "") or ""

    def _get_install_probes(
        self, profile: Any, product: str
    ) -> list[dict]:
        """获取 Profile 的安装探针配置。

        优先级：
        1. profile 自带 install_probes 属性（未来扩展）
        2. detection_rules 中含 probe_type 的规则
        3. 内置默认探针 DEFAULT_INSTALL_PROBES
        """
        probes = getattr(profile, "install_probes", None)
        if probes:
            return list(probes)
        detection_rules = getattr(profile, "detection_rules", [])
        probe_rules = [
            r for r in detection_rules
            if isinstance(r, dict) and "probe_type" in r
        ]
        if probe_rules:
            return probe_rules
        return list(DEFAULT_INSTALL_PROBES.get(product, []))

    def _get_data_paths(self, profile: Any) -> list[str]:
        """从 Profile 的 surfaces 提取数据路径。

        配置目录只能算 data_evidence，不能算 install_evidence。
        文件路径取父目录，目录路径保持原样，去重。
        """
        surfaces = getattr(profile, "surfaces", [])
        seen: set[str] = set()
        paths: list[str] = []
        for surface in surfaces:
            path_template = getattr(surface, "path_template", "")
            if not path_template or path_template.startswith("gui-only://"):
                continue
            try:
                expanded = expand_path(
                    path_template,
                    home=self._home,
                    workspace=self.workspace,
                    appdata=self._appdata,
                )
            except (OSError, ValueError):
                continue
            # 文件路径 -> 取父目录
            if expanded.suffix.lower() in _FILE_EXTENSIONS:
                target = str(expanded.parent)
            else:
                target = str(expanded)
            if target not in seen:
                seen.add(target)
                paths.append(target)
        return paths

    # -----------------------------------------------------------------------
    # 工具
    # -----------------------------------------------------------------------

    def _expand_probe_path(self, template: str) -> str:
        """展开探针路径模板中的环境变量占位符。"""
        result = template
        result = result.replace("%LOCALAPPDATA%", self._localappdata)
        result = result.replace("%APPDATA%", self._appdata)
        result = result.replace("%USERPROFILE%", str(self._home))
        result = result.replace("%HOME%", str(self._home))
        return result

    @staticmethod
    def _latest_activity(data_evidence: list[DataEvidence]) -> str:
        """返回最近的数据活跃时间（ISO 字符串字典序即时间序）。"""
        latest = ""
        for ev in data_evidence:
            if ev.last_activity_at and ev.last_activity_at > latest:
                latest = ev.last_activity_at
        return latest


__all__ = [
    "InstallEvidence",
    "DataEvidence",
    "LifecycleAssessment",
    "AgentInstallDetector",
    "DEFAULT_INSTALL_PROBES",
    "STRONG_PROBE_TYPES",
]
