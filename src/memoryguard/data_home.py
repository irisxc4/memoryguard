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
