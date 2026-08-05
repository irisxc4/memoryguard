"""data_home：知识书库统一存储目录解析。

知识书库数据库落 data_home（默认 MEMORYGUARD_HOME 或用户数据目录）下的
knowledge/knowledge.db。工作区（MEMORYGUARD_WORKSPACE）不是数据目录，
知识书库不落到任何目标项目目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 环境变量名
_ENV_DATA_HOME = "MEMORYGUARD_HOME"


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

    优先级：显式参数 > MEMORYGUARD_HOME 环境变量 > 用户数据目录。
    工作区（MEMORYGUARD_WORKSPACE）不是数据目录，知识书库不落到工作区。
    """
    if explicit and explicit.strip():
        return Path(explicit).expanduser().resolve()
    env_home = os.environ.get(_ENV_DATA_HOME, "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    return _user_data_dir().resolve()


def knowledge_db_path(data_home: Path | None = None) -> Path:
    """知识书库数据库路径。"""
    base = data_home or resolve_data_home()
    return base / "knowledge" / "knowledge.db"


def ensure_dirs(data_home: Path | None = None) -> Path:
    """确保 data_home 知识目录存在，返回 data_home。"""
    base = data_home or resolve_data_home()
    (base / "knowledge").mkdir(parents=True, exist_ok=True)
    return base