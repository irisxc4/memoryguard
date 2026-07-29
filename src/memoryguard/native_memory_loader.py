"""NativeMemoryLoader:Profile 专用原生记忆复读验证(spec §2.3, LRN-007)。

真实"已接管"证明要求:用 Agent Profile 声明的 Loader 重新解析目标文件,
断言 IR record 出现。不是简单搜索标题(无效二进制含标题会假通过)。

能力分级(TargetCapability):
- EXPORT_ONLY: 无 Loader,只能导出,publish 后无法复读验证
- SKILL_GATEWAY: 通过 Skill 间接,复读用 markdown parser
- NATIVE_TAKEOVER: 有真实 Loader fixture,用 fixture 解析

无 loader 时:publish 只能到 export_only,验证结果为 {verified: False, reason: "no loader"}。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .schema_v3 import MemoryKind, TargetCapability


@dataclass
class LoaderVerifyResult:
    """Loader 复读验证结果。"""
    verified: bool
    reason: str
    capability: str = "export_only"
    checked: int = 0
    matched: int = 0
    missing_titles: list[str] = field(default_factory=list)
    # 驱动 TakeoverState:verified=True 才能进 RUNTIME_VERIFIED
    runtime_verified: bool = False


class NativeMemoryLoader(Protocol):
    """Profile 专用 Loader 接口(duck typing)。

    实现者必须用真实 fixture/parser 解析目标文件,不能只做字符串搜索。
    """

    def parse_memory_file(self, path: Path) -> list[dict[str, str]]:
        """解析目标记忆文件,返回 [{title, body}, ...]。

        必须用 Profile 声明的格式(JSON/markdown/YAML)解析,
        无效格式返回空列表,不抛异常。
        """
        ...


# ---------------------------------------------------------------------------
# MarkdownMemoryLoader:通用 markdown ## 标题解析(Skill/Claude/Cursor)
# ---------------------------------------------------------------------------


class MarkdownMemoryLoader:
    """通用 markdown 记忆 Loader。

    按 ## 标题段解析,每段 title=标题 body=正文。
    无效 markdown(无标题/二进制)返回空列表。
    """

    def parse_memory_file(self, path: Path) -> list[dict[str, str]]:
        if not path.exists() or not path.is_file():
            return []
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 二进制或编码错误:返回空,不假通过
            return []
        import re
        records: list[dict[str, str]] = []
        current_title = ""
        current_body: list[str] = []
        for line in content.splitlines():
            m = re.match(r"^(#{1,4})\s+(.+)$", line)
            if m:
                # flush 前一个段(允许 title-only,body 用空字符串)
                if current_title:
                    body = "\n".join(current_body).strip()
                    records.append({"title": current_title, "body": body})
                current_title = m.group(2).strip()
                current_body = []
            else:
                if current_title:
                    current_body.append(line)
        if current_title:
            body = "\n".join(current_body).strip()
            records.append({"title": current_title, "body": body})
        return records


# ---------------------------------------------------------------------------
# LoaderRegistry:按 surface_id / profile_id 注册 Loader
# ---------------------------------------------------------------------------


_LOADERS: dict[str, NativeMemoryLoader] = {}


def register_loader(surface_id: str, loader: NativeMemoryLoader) -> None:
    """注册 Profile 专用 Loader。"""
    _LOADERS[surface_id] = loader


def get_loader(surface_id: str) -> NativeMemoryLoader | None:
    """获取 surface_id 对应的 Loader。"""
    return _LOADERS.get(surface_id)


def clear_loaders() -> None:
    """清空注册表(测试用)。"""
    _LOADERS.clear()


# 默认注册 markdown loader(通用 Agent)
_DEFAULT_MARKDOWN_LOADER = MarkdownMemoryLoader()
for _sid in ("claude_code_memory", "cursor_memory", "windsurf_memory",
             "trae_user_profile", "trae_project_memory", "generic_markdown"):
    register_loader(_sid, _DEFAULT_MARKDOWN_LOADER)


# ---------------------------------------------------------------------------
# verify_takeover:用真实 Loader 复读验证
# ---------------------------------------------------------------------------


def verify_takeover(
    target_path: Path,
    ir_records: list[Any],
    surface_id: str = "",
    capability: TargetCapability = TargetCapability.EXPORT_ONLY,
) -> LoaderVerifyResult:
    """用 Profile 专用 Loader 复读目标文件,验证 IR record 出现。

    - EXPORT_ONLY: 无 Loader,返回 verified=False
    - SKILL_GATEWAY / NATIVE_TAKEOVER: 用注册的 Loader 解析目标文件,
      断言每条非 rejected/quarantined 的 IR record 的 title 在解析结果中

    无效格式(二进制/无标题)Loader 返回空列表,验证失败。
    """
    if capability == TargetCapability.EXPORT_ONLY:
        return LoaderVerifyResult(
            verified=False,
            reason="export_only: no loader, cannot verify takeover",
            capability=capability.value,
            runtime_verified=False,
        )

    loader = get_loader(surface_id) if surface_id else None
    if loader is None:
        return LoaderVerifyResult(
            verified=False,
            reason=f"no loader registered for surface_id={surface_id}",
            capability=capability.value,
            runtime_verified=False,
        )

    parsed = loader.parse_memory_file(target_path)
    if not parsed:
        return LoaderVerifyResult(
            verified=False,
            reason="loader returned empty (invalid format or no records)",
            capability=capability.value,
            runtime_verified=False,
        )

    parsed_titles = {r.get("title", "").strip() for r in parsed if r.get("title")}
    parsed_titles_lower = {t.lower() for t in parsed_titles}

    checked = 0
    matched = 0
    missing: list[str] = []
    for rec in ir_records:
        status_val = rec.status.value if hasattr(rec.status, "value") else str(rec.status)
        if status_val in {"rejected", "quarantined"}:
            continue
        checked += 1
        title = (rec.title or "").strip()
        if not title:
            continue
        # 精确匹配或大小写不敏感匹配
        if title in parsed_titles or title.lower() in parsed_titles_lower:
            matched += 1
        else:
            missing.append(title[:40])

    verified = checked > 0 and matched == checked and not missing
    return LoaderVerifyResult(
        verified=verified,
        reason=(
            f"{matched}/{checked} records verified"
            if verified
            else f"{len(missing)}/{checked} records missing in parsed target"
        ),
        capability=capability.value,
        checked=checked,
        matched=matched,
        missing_titles=missing[:5],
        # 只有 verified=True 才能驱动 runtime_verified
        runtime_verified=verified,
    )
