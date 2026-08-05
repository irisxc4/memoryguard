"""knowledge_parser：文档解析（KB1）。

将文件内容解析为结构化段落列表，保留行号。
支持 Markdown（标题/代码块/列表/表格）和纯文本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 支持的文件扩展名 → media_type
SUPPORTED_EXTENSIONS = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/x-rst",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".toml": "text/x-toml",
}

# 可选代码文件
CODE_EXTENSIONS = {
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/x-tsx",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".java": "text/x-java",
    ".cs": "text/x-csharp",
}


@dataclass
class ParsedBlock:
    """解析后的内容块。"""
    text: str
    line_start: int
    line_end: int
    block_type: str = "paragraph"  # heading, code, list, table, paragraph
    heading_level: int = 0  # 0=非标题, 1-6=Markdown 标题级别
    heading_text: str = ""


@dataclass
class ParsedDocument:
    """解析后的文档。"""
    relative_path: str
    media_type: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    # 从标题提取的章节路径
    chapter: str = ""
    section: str = ""


def parse_file(file_path: Path, root: Path) -> ParsedDocument | None:
    """解析文件，返回 ParsedDocument。不支持的类型返回 None。"""
    rel = str(file_path.relative_to(root)).replace("\\", "/")
    ext = file_path.suffix.lower()

    media_type = SUPPORTED_EXTENSIONS.get(ext) or CODE_EXTENSIONS.get(ext)
    if not media_type:
        return None

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if ext in (".md", ".markdown"):
        return _parse_markdown(rel, media_type, content)
    if ext in (".json", ".jsonl"):
        return _parse_structured(rel, media_type, content)
    if ext in (".yaml", ".yml"):
        return _parse_structured(rel, media_type, content)
    if ext in (".toml",):
        return _parse_structured(rel, media_type, content)
    if ext in CODE_EXTENSIONS:
        return _parse_code(rel, media_type, content)
    # 默认纯文本
    return _parse_text(rel, media_type, content)


def _parse_markdown(rel: str, media_type: str, content: str) -> ParsedDocument:
    """解析 Markdown：识别标题、代码块、列表、表格、段落。"""
    lines = content.split("\n")
    blocks: list[ParsedBlock] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 空行
        if not stripped:
            i += 1
            continue

        # 标题
        m = re.match(r'^(#{1,6})\s+(.+)', stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            blocks.append(ParsedBlock(
                text=line, line_start=i + 1, line_end=i + 1,
                block_type="heading", heading_level=level, heading_text=text,
            ))
            i += 1
            continue

        # 代码块
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            start = i
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                i += 1
            end = i + 1 if i < len(lines) else i
            code_text = "\n".join(lines[start:end])
            blocks.append(ParsedBlock(
                text=code_text, line_start=start + 1, line_end=end,
                block_type="code",
            ))
            i = end
            continue

        # 表格（连续 | 开头行）
        if "|" in stripped and i + 1 < len(lines) and "---" in lines[i + 1]:
            start = i
            while i < len(lines) and "|" in lines[i].strip():
                i += 1
            table_text = "\n".join(lines[start:i])
            blocks.append(ParsedBlock(
                text=table_text, line_start=start + 1, line_end=i,
                block_type="table",
            ))
            continue

        # 列表（- * + 或数字.）
        if re.match(r'^[-*+]\s|^(\d+\.)\s', stripped):
            start = i
            while i < len(lines) and (re.match(r'^[-*+]\s|^(\d+\.)\s', lines[i].strip()) or lines[i].strip().startswith("  ")):
                i += 1
            list_text = "\n".join(lines[start:i])
            blocks.append(ParsedBlock(
                text=list_text, line_start=start + 1, line_end=i,
                block_type="list",
            ))
            continue

        # 普通段落（连续非空行直到空行或特殊块）
        start = i
        while i < len(lines):
            s = lines[i].strip()
            if not s:
                break
            if s.startswith("#") or s.startswith("```") or s.startswith("~~~"):
                break
            if "|" in s and i + 1 < len(lines) and "---" in lines[i + 1]:
                break
            if re.match(r'^[-*+]\s|^(\d+\.)\s', s):
                break
            i += 1
        para_text = "\n".join(lines[start:i])
        blocks.append(ParsedBlock(
            text=para_text, line_start=start + 1, line_end=i,
            block_type="paragraph",
        ))

    return ParsedDocument(relative_path=rel, media_type=media_type, blocks=blocks)


def _parse_text(rel: str, media_type: str, content: str) -> ParsedDocument:
    """纯文本：按空行分段。"""
    lines = content.split("\n")
    blocks: list[ParsedBlock] = []
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        start = i
        while i < len(lines) and lines[i].strip():
            i += 1
        blocks.append(ParsedBlock(
            text="\n".join(lines[start:i]),
            line_start=start + 1, line_end=i,
            block_type="paragraph",
        ))
    return ParsedDocument(relative_path=rel, media_type=media_type, blocks=blocks)


def _parse_code(rel: str, media_type: str, content: str) -> ParsedDocument:
    """代码：按函数/类定义分块。"""
    lines = content.split("\n")
    blocks: list[ParsedBlock] = []
    # 简单识别 def/class/function/func 等定义行
    def_pattern = re.compile(
        r'^\s*(def |class |function |func |public |private |protected )',
        re.IGNORECASE,
    )
    i = 0
    current_start = 0
    while i < len(lines):
        if def_pattern.match(lines[i]):
            # 前面的内容作为一块
            if i > current_start:
                text = "\n".join(lines[current_start:i])
                if text.strip():
                    blocks.append(ParsedBlock(
                        text=text, line_start=current_start + 1, line_end=i,
                        block_type="code",
                    ))
            current_start = i
        i += 1
    # 最后一块
    if current_start < len(lines):
        text = "\n".join(lines[current_start:])
        if text.strip():
            blocks.append(ParsedBlock(
                text=text, line_start=current_start + 1, line_end=len(lines),
                block_type="code",
            ))
    if not blocks:
        blocks.append(ParsedBlock(
            text=content, line_start=1, line_end=len(lines),
            block_type="code",
        ))
    return ParsedDocument(relative_path=rel, media_type=media_type, blocks=blocks)


def _parse_structured(rel: str, media_type: str, content: str) -> ParsedDocument:
    """JSON/YAML/TOML：按顶层 key 分段。"""
    lines = content.split("\n")
    blocks: list[ParsedBlock] = []
    # 简单按缩进 0 的行分段（YAML/TOML 的 key，JSON 的顶层逗号项）
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        # 顶层 key（无缩进）
        if not lines[i].startswith(" ") and not lines[i].startswith("\t"):
            start = i
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or lines[i].startswith("\t") or not lines[i].strip()):
                i += 1
            blocks.append(ParsedBlock(
                text="\n".join(lines[start:i]),
                line_start=start + 1, line_end=i,
                block_type="paragraph",
                heading_text=stripped.rstrip(":").rstrip("="),
            ))
        else:
            i += 1
    if not blocks:
        blocks.append(ParsedBlock(
            text=content, line_start=1, line_end=len(lines),
            block_type="paragraph",
        ))
    return ParsedDocument(relative_path=rel, media_type=media_type, blocks=blocks)
