"""data_home：统一存储目录解析（KB6）。

把 target_workspace（被扫描的项目，只读）和 data_home（统一存储目录）分开。
所有 MemoryGuard 工件集中到 data_home，不再散落在各目标项目的 .memoryguard/。
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

# 环境变量名
_ENV_DATA_HOME = "MEMORYGUARD_HOME"
_ENV_WORKSPACE = "MEMORYGUARD_WORKSPACE"


def _user_data_dir() -> Path:
    """跨平台用户数据目录。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "MemoryGuard"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MemoryGuard"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "MemoryGuard"


def resolve_data_home(explicit: str | None = None) -> Path:
    """解析统一存储目录。

    优先级：显式参数 > MEMORYGUARD_HOME 环境变量 > MEMORYGUARD_WORKSPACE > 用户数据目录。
    """
    if explicit and explicit.strip():
        return Path(explicit).expanduser().resolve()
    env_home = os.environ.get(_ENV_DATA_HOME, "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    env_ws = os.environ.get(_ENV_WORKSPACE, "").strip()
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    return _user_data_dir().resolve()


def resolve_target_workspace(explicit: str | None = None) -> Path:
    """解析被扫描的目标项目目录（只读）。"""
    if explicit and explicit.strip():
        return Path(explicit).expanduser().resolve()
    return Path.cwd().resolve()


def project_hash(target_workspace: Path) -> str:
    """对目标项目路径取哈希，用作 data_home 下的子目录名。"""
    raw = str(target_workspace).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def project_dir(data_home: Path, target_workspace: Path) -> Path:
    """目标项目在 data_home 下的集中目录。"""
    return data_home / "projects" / project_hash(target_workspace)


def knowledge_db_path(data_home: Path | None = None) -> Path:
    """知识书库数据库路径。"""
    base = data_home or resolve_data_home()
    return base / "knowledge" / "knowledge.db"


def ensure_dirs(data_home: Path | None = None) -> Path:
    """确保 data_home 基础目录存在，返回 data_home。"""
    base = data_home or resolve_data_home()
    (base / "knowledge").mkdir(parents=True, exist_ok=True)
    (base / "projects").mkdir(parents=True, exist_ok=True)
    (base / "shared-memory").mkdir(parents=True, exist_ok=True)
    return base


def get_artifacts_dir(target_workspace: Path,
                      data_home: Path | None = None,
                      subdir: str = "") -> Path:
    """目标项目在 data_home 下的工件目录（KB6）。

    替代旧的 target_workspace/.memoryguard/。所有 audit/plan/apply/snapshot
    工件集中到 data_home/projects/<hash>/<subdir>/。
    """
    base = data_home or resolve_data_home()
    pdir = project_dir(base, target_workspace)
    if subdir:
        pdir = pdir / subdir
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


def ensure_project_dirs(target_workspace: Path,
                        data_home: Path | None = None) -> Path:
    """确保项目工件子目录存在，返回项目目录。"""
    base = data_home or resolve_data_home()
    ensure_dirs(base)
    pdir = project_dir(base, target_workspace)
    for sub in ("reports", "plans", "changes", "backups", "snapshots", "imports", "releases"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    return pdir


def detect_legacy_memoryguard(target_workspace: Path) -> Path | None:
    """检测旧 target_workspace/.memoryguard/ 目录。

    返回旧目录路径（存在时）或 None。用于 KB6 迁移提示。
    """
    legacy = target_workspace / ".memoryguard"
    return legacy if legacy.is_dir() else None


def migrate_legacy_memoryguard(target_workspace: Path,
                               data_home: Path | None = None,
                               dry_run: bool = False) -> dict[str, object]:
    """迁移旧 .memoryguard/ 到 data_home/projects/<hash>/。

    迁移前可只读读取旧目录，不强制删除（dry_run=True 只预览）。
    不预创建标准子目录，避免与 legacy 同名目录冲突。
    """
    import shutil
    legacy = detect_legacy_memoryguard(target_workspace)
    if not legacy:
        return {"ok": True, "migrated": False, "reason": "no legacy directory"}

    base = data_home or resolve_data_home()
    ensure_dirs(base)  # 只创建 data_home 根目录，不预创建项目子目录
    dest = project_dir(base, target_workspace)
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)

    migrated: list[str] = []
    skipped: list[str] = []
    for item in legacy.iterdir():
        dest_item = dest / item.name
        if dest_item.exists():
            skipped.append(item.name)
            continue
        if not dry_run:
            if item.is_dir():
                shutil.copytree(item, dest_item)
            else:
                shutil.copy2(item, dest_item)
        migrated.append(item.name)

    return {
        "ok": True,
        "migrated": migrated,
        "skipped": skipped,
        "legacy_path": str(legacy),
        "dest_path": str(dest),
        "dry_run": dry_run,
        "cleanup_hint": "迁移完成后可手动删除旧目录" if migrated and not dry_run else "",
    }
