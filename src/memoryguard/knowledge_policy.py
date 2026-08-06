"""知识库读取与外发策略。

知识内容默认是只读参考资料。控制面文件和敏感片段不能进入普通检索、
Bootstrap、MCP 输出或远程 Provider；本地管理界面如需查看，必须显式传入
放行策略。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class KnowledgeAccessPolicy:
    """控制知识片段是否可被读取或发送给 Provider。"""

    allow_control_surface: bool = False
    allow_sensitive: bool = False

    def allows(self, row: Any) -> bool:
        """判断 sqlite.Row 或 dict 代表的片段是否可访问。"""
        role = str(_value(row, "content_role", "knowledge") or "knowledge")
        sensitivity = str(_value(row, "sensitivity", "normal") or "normal")
        if role == "control_surface" and not self.allow_control_surface:
            return False
        if sensitivity == "sensitive" and not self.allow_sensitive:
            return False
        return True


DEFAULT_KNOWLEDGE_POLICY = KnowledgeAccessPolicy()


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def policy_sql(
    policy: KnowledgeAccessPolicy | None,
    *,
    chunk_alias: str = "c",
    document_alias: str = "d",
) -> tuple[str, list[Any]]:
    """返回可拼接到 SQL WHERE 的安全过滤条件。"""
    policy = policy or DEFAULT_KNOWLEDGE_POLICY
    conditions: list[str] = []
    params: list[Any] = []
    if not policy.allow_sensitive:
        conditions.append(f"{chunk_alias}.sensitivity = ?")
        params.append("normal")
    if not policy.allow_control_surface:
        conditions.append(f"{document_alias}.content_role = ?")
        params.append("knowledge")
    return (" AND ".join(conditions), params)
