"""统一内容解析收口：Markdown / frontmatter / JSONL / topics / SQLite-meta。

所有进入 Memory IR 与 Import 的分段逻辑必须经此模块，禁止旁路手写分段。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 预算（确定性，不调模型）
MAX_SEGMENTS_PER_FILE = 50
MAX_BODY_CHARS = 4096

_PREFERENCE_RE = re.compile(
    r"(prefer|preference|喜欢|偏好|always|never|必须|不要|应|应该|"
    r"remember that|请记住|用户希望)",
    re.IGNORECASE,
)
_TOOL_NOISE_KEYS = frozenset({
    "tool_call", "tool_calls", "tool_result", "function_call",
    "toolUse", "tool_use", "functionCall",
})


@dataclass(frozen=True)
class ParsedSegment:
    """统一分段结果。"""
    locator: str
    title: str
    body: str
    kind_hint: str = ""       # preference/fact/project/episode/procedure/correction 或空
    signal_level: str = "full"  # full | high | meta | low
    truncated: bool = False


def parse_content(
    path: str | Path,
    content: str | bytes | None = None,
    *,
    media_type: str = "",
    surface_hint: str = "",
    verbatim: bool = False,
) -> list[ParsedSegment]:
    """按路径/媒体类型分发到具体解析器。

    verbatim=True 时不截断 body（用于 import_verbatim 原样导入）。
    """
    p = Path(path)
    name = p.name.lower()
    ext = p.suffix.lower()
    mt = (media_type or "").lower()
    body_limit = 10_000_000 if verbatim else MAX_BODY_CHARS

    if ext in {".sqlite", ".sqlite3", ".db", ".vscdb"} or "sqlite" in mt:
        return _parse_sqlite_meta(p)

    text: str
    if content is None:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
    elif isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return []
    else:
        text = content

    if not text.strip():
        return []

    if name == "topics.md" or surface_hint == "trae_topics":
        return _parse_trae_topics(text, body_limit=body_limit)

    if ext == ".jsonl" or mt.endswith("jsonl") or "jsonl" in mt:
        return _parse_jsonl_session(text, surface_hint=surface_hint, body_limit=body_limit)

    if ext in {".md", ".markdown"} or "markdown" in mt:
        if text.lstrip().startswith("---"):
            return _parse_frontmatter_markdown(text, body_limit=body_limit)
        return _parse_markdown(text, body_limit=body_limit)

    if ext in {".json"}:
        return _parse_json_blob(text, body_limit=body_limit)

    return _parse_markdown(text, body_limit=body_limit)


def parse_file(
    path: str | Path,
    *,
    media_type: str = "",
    surface_hint: str = "",
    content: str | bytes | None = None,
    verbatim: bool = False,
) -> list[ParsedSegment]:
    """文件入口；content 可选（已读文本可复用，避免二次 IO）。"""
    return parse_content(
        path, content, media_type=media_type, surface_hint=surface_hint,
        verbatim=verbatim,
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _parse_markdown(content: str, *, body_limit: int = MAX_BODY_CHARS) -> list[ParsedSegment]:
    segments: list[ParsedSegment] = []
    lines = content.split("\n")
    current_heading = ""
    current_start = 0
    current_lines: list[str] = []

    def flush() -> None:
        if not current_lines:
            return
        text = "\n".join(current_lines).strip()
        if not text:
            return
        locator = (
            f"heading:{current_heading}"
            if current_heading
            else f"line:{current_start + 1}-{current_start + len(current_lines)}"
        )
        title = _title_from_text(text)
        body, was_trunc = _truncate(text, body_limit)
        segments.append(ParsedSegment(
            locator=locator, title=title, body=body,
            kind_hint="", signal_level="full", truncated=was_trunc,
        ))

    for i, line in enumerate(lines):
        m = re.match(r"^#+\s+(.+)$", line)
        if m:
            flush()
            current_heading = m.group(1).strip()
            current_start = i
            current_lines = [line]
        else:
            if not current_lines:
                current_start = i
            current_lines.append(line)
    flush()
    return segments[:MAX_SEGMENTS_PER_FILE]


def _parse_frontmatter_markdown(content: str, *, body_limit: int = MAX_BODY_CHARS) -> list[ParsedSegment]:
    """Claude memory/*.md：YAML frontmatter + 正文。"""
    fm, body = _split_frontmatter(content)
    kind = _kind_from_frontmatter(fm)
    title = str(fm.get("title") or fm.get("name") or "").strip()
    if not title:
        title = _title_from_text(body) if body.strip() else "memory"
    body_text = body.strip() or content.strip()
    type_tag = str(fm.get("type") or fm.get("memory_type") or "").strip()
    locator = f"frontmatter:{type_tag or 'memory'}"
    body_out, was_trunc = _truncate(body_text, body_limit)
    return [ParsedSegment(
        locator=locator,
        title=title[:80],
        body=body_out,
        kind_hint=kind,
        signal_level="full",
        truncated=was_trunc,
    )]


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    text = content.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, content
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, content
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}, content
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:])
    return _parse_simple_yaml(raw), body


def _parse_simple_yaml(raw: str) -> dict[str, Any]:
    """极简 YAML：只支持顶层 key: value，不引入 PyYAML。"""
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        key, _, val = s.partition(":")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key:
            out[key] = val
    return out


_FRONTMATTER_KIND = {
    "user": "preference",
    "feedback": "preference",
    "preference": "preference",
    "project": "project",
    "reference": "fact",
    "fact": "fact",
    "procedure": "procedure",
    "episode": "episode",
    "correction": "correction",
}


def _kind_from_frontmatter(fm: dict[str, Any]) -> str:
    for key in ("type", "memory_type", "kind", "category"):
        raw = str(fm.get(key, "")).strip().lower()
        if raw in _FRONTMATTER_KIND:
            return _FRONTMATTER_KIND[raw]
    return ""


# ---------------------------------------------------------------------------
# JSONL sessions
# ---------------------------------------------------------------------------


def _parse_jsonl_session(content: str, *, surface_hint: str = "",
                         body_limit: int = MAX_BODY_CHARS) -> list[ParsedSegment]:
    segments: list[ParsedSegment] = []
    for i, line in enumerate(content.splitlines(), start=1):
        if len(segments) >= MAX_SEGMENTS_PER_FILE:
            break
        raw = line.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if _is_tool_noise(obj):
            continue
        if not _is_high_signal(obj, surface_hint=surface_hint):
            continue
        title, body, kind = _extract_jsonl_fields(obj)
        if not body.strip():
            continue
        body_out, was_trunc = _truncate(body, body_limit)
        segments.append(ParsedSegment(
            locator=f"jsonl:line:{i}",
            title=(title or body[:40].replace("\n", " "))[:80],
            body=body_out,
            kind_hint=kind,
            signal_level="high",
            truncated=was_trunc,
        ))
    return segments


def _is_tool_noise(obj: dict[str, Any]) -> bool:
    t = str(obj.get("type") or obj.get("event") or obj.get("kind") or "").lower()
    if any(k in t for k in ("tool", "function_call", "tool_result", "tool_use")):
        return True
    for k in _TOOL_NOISE_KEYS:
        if k in obj:
            return True
    msg = obj.get("message")
    if isinstance(msg, dict):
        for k in _TOOL_NOISE_KEYS:
            if k in msg:
                return True
        role = str(msg.get("role") or "").lower()
        if role == "tool":
            return True
    return False


def _is_high_signal(obj: dict[str, Any], *, surface_hint: str = "") -> bool:
    # TRAE: learned / outcome
    learned = obj.get("learned")
    if isinstance(learned, str) and learned.strip():
        return True
    if isinstance(learned, (list, dict)) and learned:
        return True
    outcome = obj.get("outcome")
    if isinstance(outcome, str) and outcome.strip():
        return True

    # explicit memory write
    t = str(obj.get("type") or obj.get("event") or "").lower()
    if "memory" in t and any(x in t for x in ("write", "store", "save", "update")):
        return True

    # nested payload
    payload = obj.get("payload")
    if isinstance(payload, dict):
        if _is_high_signal(payload, surface_hint=surface_hint):
            return True

    text = _flatten_text(obj)
    role = _role_of(obj)
    if role in {"user", "human"} and _PREFERENCE_RE.search(text):
        return True
    if role in {"user", "human"} and len(text) >= 40 and surface_hint.startswith("trae"):
        # TRAE session rows often omit role; already handled via learned/outcome
        return False
    # Codex/Claude: assistant summarizing preferences is weak; require preference cue
    if _PREFERENCE_RE.search(text) and role in {"user", "human", "system", ""}:
        return True
    return False


def _role_of(obj: dict[str, Any]) -> str:
    for key in ("role",):
        if key in obj:
            return str(obj[key]).lower()
    msg = obj.get("message")
    if isinstance(msg, dict) and "role" in msg:
        return str(msg["role"]).lower()
    return ""


def _extract_jsonl_fields(obj: dict[str, Any]) -> tuple[str, str, str]:
    kind = ""
    title = ""
    body = ""

    if isinstance(obj.get("learned"), str) and obj["learned"].strip():
        body = obj["learned"].strip()
        kind = "fact"
        title = str(obj.get("intent") or "learned")[:80]
    elif isinstance(obj.get("outcome"), str) and obj["outcome"].strip():
        body = obj["outcome"].strip()
        kind = "episode"
        title = str(obj.get("intent") or "outcome")[:80]
    else:
        body = _flatten_text(obj)
        title = str(obj.get("title") or obj.get("intent") or "")[:80]
        if _PREFERENCE_RE.search(body):
            kind = "preference"

    return title, body, kind


def _flatten_text(obj: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("text", "content", "body", "learned", "outcome", "summary", "message"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
        elif isinstance(val, dict):
            nested = val.get("content") or val.get("text") or val.get("body")
            if isinstance(nested, str) and nested.strip():
                parts.append(nested.strip())
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        t = item.get("text") or item.get("content") or ""
                        if isinstance(t, str) and t.strip():
                            parts.append(t.strip())
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    t = item.get("text") or item.get("content") or ""
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
    payload = obj.get("payload")
    if isinstance(payload, dict):
        nested = _flatten_text(payload)
        if nested:
            parts.append(nested)
    return "\n".join(parts).strip()


def _parse_json_blob(content: str, *, body_limit: int = MAX_BODY_CHARS) -> list[ParsedSegment]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return _parse_markdown(content, body_limit=body_limit)
    if isinstance(data, dict):
        body = json.dumps(data, ensure_ascii=False, indent=2)
        body, was_trunc = _truncate(body, body_limit)
        return [ParsedSegment(
            locator="json:root", title="json", body=body,
            signal_level="full", truncated=was_trunc,
        )]
    return _parse_markdown(content, body_limit=body_limit)


# ---------------------------------------------------------------------------
# TRAE topics.md
# ---------------------------------------------------------------------------


_TOPICS_HEADER = re.compile(r"^\[session_id:\s*([^\]]+)\]\s*$", re.MULTILINE)


def _parse_trae_topics(content: str, *, body_limit: int = MAX_BODY_CHARS) -> list[ParsedSegment]:
    matches = list(_TOPICS_HEADER.finditer(content))
    if not matches:
        return _parse_markdown(content, body_limit=body_limit)
    segments: list[ParsedSegment] = []
    for i, m in enumerate(matches):
        if len(segments) >= MAX_SEGMENTS_PER_FILE:
            break
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = content[start:end].strip()
        sid = m.group(1).strip()
        if not block:
            continue
        title = block.split("\n", 1)[0].strip()[:80] or sid
        body, was_trunc = _truncate(block, body_limit)
        segments.append(ParsedSegment(
            locator=f"topics:session:{sid}",
            title=title,
            body=body,
            kind_hint="episode",
            signal_level="high",
            truncated=was_trunc,
        ))
    return segments


# ---------------------------------------------------------------------------
# SQLite meta only
# ---------------------------------------------------------------------------


def _parse_sqlite_meta(path: Path) -> list[ParsedSegment]:
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        mtime = int(path.stat().st_mtime)
    except OSError:
        return []
    tables: list[str] = []
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 30"
            ).fetchall()
            tables = [str(r[0]) for r in rows]
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        tables = []
    body = (
        f"sqlite_index path={path.name} size={size} mtime={mtime}\n"
        f"tables={', '.join(tables) if tables else '(unreadable)'}"
    )
    return [ParsedSegment(
        locator="sqlite:meta",
        title=f"sqlite:{path.name}",
        body=body,
        kind_hint="",
        signal_level="meta",
    )]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = MAX_BODY_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n…[truncated]", True


def _title_from_text(segment_text: str) -> str:
    first_line = segment_text.split("\n", 1)[0]
    m = re.match(r"^#+\s+(.+)$", first_line)
    if m:
        return m.group(1).strip()[:80]
    return segment_text[:40].replace("\n", " ").strip()
