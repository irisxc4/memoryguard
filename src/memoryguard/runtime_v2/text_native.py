"""Small, dependency-free text predicates used by the native runtime.

These helpers deliberately stay independent from the legacy memory model and
organizer modules.  Keeping the heuristics here lets a native service import
without pulling the legacy module graph into the process.
"""

from __future__ import annotations

import re
from typing import Any


# Keep this ordering in sync with the established heuristic: the first
# matching kind wins when a body contains terms from multiple categories.
_CLASSIFY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("preference", ("偏好", "喜欢", "prefer", "like", "习惯")),
    ("procedure", ("步骤", "流程", "procedure", "step", "how to")),
    ("project", ("项目", "project", "仓库", "repo")),
    ("episode", ("事件", "episode", "发生", "happened")),
    ("correction", ("纠正", "更正", "correction", "actually", "不对", "错误", "应该是")),
)
VALID_KINDS = frozenset(kind for kind, _ in _CLASSIFY_KEYWORDS) | {"fact"}
_KIND_LABELS = {
    "fact": "事实",
    "preference": "偏好",
    "project": "项目",
    "episode": "事件",
    "procedure": "流程",
    "correction": "纠错",
}
_FAKE_ZH_PREFIXES = ("中文整理：", "中文辅助摘要：")


def classify_kind(content: str, metadata: Any = "") -> str:
    """Return the established native heuristic kind for ``content``."""
    del metadata
    text = content.lower()
    for kind, keywords in _CLASSIFY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return kind
    return "fact"


def looks_english_text(text: str) -> bool:
    """Return whether text contains a substantial English-language signal."""
    if not text:
        return False
    latin = sum(1 for char in text if "a" <= char.lower() <= "z")
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return latin >= 12 and latin > cjk * 2


def _compact_english_snippet(text: str, limit: int) -> str:
    replacements = {
        "memory": "记忆", "project": "项目", "preference": "偏好", "rule": "规则",
        "workflow": "流程", "procedure": "流程", "constraint": "约束", "fact": "事实",
        "use": "使用", "should": "应", "must": "必须", "avoid": "避免",
        "file": "文件", "files": "文件", "folder": "文件夹", "source": "来源",
        "truth": "事实依据", "agent": "智能体", "global": "全局", "local": "本地",
        "compact": "简洁",
    }
    words = " ".join(str(text or "").replace("\n", " ").split())[:limit].split()
    mapped = [replacements.get(word.strip(".,:;()[]{}\"'").casefold(), word) for word in words[:36]]
    return " ".join(mapped).strip()


def _strip_fake_zh_prefix(text: str) -> str:
    value = str(text or "")
    for prefix in _FAKE_ZH_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix):].lstrip()
    return value


def localize_native_text(title: str, body: str, kind: str = "fact") -> dict[str, str]:
    """Return bounded display fields with explicit native localization metadata.

    This is a pure V2 text contract.  It never writes a record or stores source
    bodies in projection metadata; callers decide how the returned display
    fields are presented or attached to a governed atom.
    """
    original_title = str(title or "")
    original_body = _strip_fake_zh_prefix(body)
    if not looks_english_text(original_title + " " + original_body):
        return {
            "title": original_title,
            "body": original_body,
            "original_title": original_title,
            "original_body": original_body,
            "display_language": "zh",
            "localization_mode": "none",
        }
    label = _KIND_LABELS.get(str(kind or "fact").casefold(), "记忆")
    return {
        "title": f"{label}：{_compact_english_snippet(original_title or original_body, 72)}",
        "body": _compact_english_snippet(original_body or original_title, 420),
        "original_title": original_title,
        "original_body": original_body,
        "display_language": "mixed",
        "localization_mode": "heuristic",
    }


__all__ = [
    "VALID_KINDS", "classify_kind", "looks_english_text", "localize_native_text",
]
