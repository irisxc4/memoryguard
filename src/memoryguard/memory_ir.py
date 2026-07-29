"""Memory IR：跨来源规范化记忆层（spec §3.4, §10）。

v3 核心纠偏：
- 保留全部 provenance，不静默丢弃重复项
- TF-IDF 只生成 DuplicateGroup 候选，不自动合并
- Instruction/Skill 默认不进入 Memory IR（spec §1.1）
- 稳定 ID：hash(source_object_id + stable_locator + normalized_content_fingerprint)

与 v2.1 extractor.py 的区别：
- extractor.py 直接去重丢弃；本模块只生成候选组
- extractor.py 无 provenance；本模块强制每条 MemoryRecord 至少一个 Provenance
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .schema_v3 import (
    Completeness, DecisionEvent, DuplicateDecision, DuplicateGroup,
    MemoryKind, MemoryRecord, MemoryStatus, Provenance, SourceObject,
    SourceSnapshot, stable_hash, _now_iso,
)


# ---------------------------------------------------------------------------
# TF-IDF 工具（纯标准库，零依赖）
# ---------------------------------------------------------------------------


class TfidfVectorizer:
    """极简 TF-IDF：中文按字符切，英文按词切。"""

    def __init__(self):
        self.vocabulary: dict[str, int] = {}
        self.idf: dict[int, float] = {}

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        # 中英文混合：英文按 word，中文按单字
        tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*|[\u4e00-\u9fff]", text.lower())
        return [t for t in tokens if len(t) >= 1]

    def fit(self, docs: list[str]) -> None:
        df: dict[str, int] = {}
        total = len(docs)
        for doc in docs:
            seen = set()
            for tok in self._tokenize(doc):
                if tok not in seen:
                    df[tok] = df.get(tok, 0) + 1
                    seen.add(tok)
        self.vocabulary = {t: i for i, t in enumerate(sorted(df.keys()))}
        self.idf = {
            self.vocabulary[t]: math.log((1 + total) / (1 + df[t])) + 1.0
            for t in df
        }

    def transform(self, text: str) -> list[float]:
        vec = [0.0] * len(self.vocabulary)
        toks = self._tokenize(text)
        tf: dict[int, int] = {}
        for t in toks:
            idx = self.vocabulary.get(t)
            if idx is not None:
                tf[idx] = tf.get(idx, 0) + 1
        for idx, count in tf.items():
            vec[idx] = count * self.idf.get(idx, 1.0)
        return vec

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)


# ---------------------------------------------------------------------------
# MemoryNormalizer：从 SourceObject 提取 MemoryRecord
# ---------------------------------------------------------------------------


# 判断文件是否属于 Instruction/Skill（不进入 Memory IR）
INSTRUCTION_FILENAMES = {"AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "GEMINI.md",
                         "CODEBUDDY.md", ".cursorrules", ".windsurfrules",
                         "copilot-instructions.md"}
SKILL_MARKERS = ("SKILL.md", "skills/")


def _is_instruction_or_skill(rel_path: str) -> bool:
    """Instruction/Skill 默认不进入 Memory IR（spec §1.1）。"""
    name = rel_path.rsplit("/", 1)[-1]
    if name in INSTRUCTION_FILENAMES:
        return True
    if "SKILL.md" in rel_path or "skills/" in rel_path:
        return True
    return False


def _is_plan_or_ops_doc(rel_path: str) -> bool:
    """实施计划 / 任务台账等作业文档，不应自动变成长期记忆。"""
    p = rel_path.replace("\\", "/").lower().lstrip("./")
    if p.endswith(".plan.md") or p.endswith(".plan.json"):
        return True
    markers = (
        ".cursor/plans/",
        ".trellis/tasks/",
        ".trellis/workspace/",
        "agent-transcripts/",
        ".codex/sessions/",
    )
    return any(m in p or p.startswith(m.lstrip("/")) for m in markers)


def _should_skip_auto_ingest(cat: str, ing: str) -> bool:
    """构建 normalize 时是否跳过自动写入 IR。

    - knowledge_source + extract_candidates：只允许面板「萃取」，不整仓灌进记忆
    - conversation_history：仅 extract_candidates 保留（既有语义）
    """
    if cat in {"runtime_evidence", "ignored_runtime_data"}:
        return True
    if cat == "conversation_history" and ing != "extract_candidates":
        return True
    if cat in {"knowledge_source", "control_surface", "skill_surface"} and ing != "import_verbatim":
        return True
    if ing in {"evidence_only", "govern_only", "ignore"}:
        return True
    return False


# P1.2: secret 检测 + 脱敏(与 auto_organizer.SECRET_PATTERNS 一致)
import re as _re_mod
_SECRET_PATTERNS: list[_re_mod.Pattern] = [
    _re_mod.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd)\s*[=:]\s*\S+"),
    _re_mod.compile(r"AKIA[0-9A-Z]{16}"),
    _re_mod.compile(r"ghp_[A-Za-z0-9]{36}"),
    _re_mod.compile(r"gho_[A-Za-z0-9]{36}"),
    _re_mod.compile(r"sk-[A-Za-z0-9]{20,}"),
    _re_mod.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
    _re_mod.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
]


def _detect_secret_in_text(text: str) -> str:
    """检测 secret,返回匹配模式描述(空串表示无匹配)。"""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return pattern.pattern[:50]
    return ""


def _redact_for_enricher(content: str) -> str:
    """脱敏后送 enricher,防止残留 secret 进入模型 backend。"""
    redacted = content
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _safe_confidence(confidence: Any) -> float:
    """校验模型返回的 confidence,非法值回退 0.5。"""
    try:
        val = float(confidence)
        if not (0.0 <= val <= 1.0):
            return 0.5
        return val
    except (ValueError, TypeError):
        return 0.5


def _enum_or_default(enum_cls, value, default):
    """P2.2: 统一 enum 防御性解析,非法值用默认值。

    覆盖 MemoryKind / MemoryStatus / Completeness / DuplicateDecision 等所有 enum。
    """
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def _split_into_segments(content: str, *, path: str = "", media_type: str = "") -> list[tuple[str, str]]:
    """薄包装：统一收口到 content_parsers（兼容旧 (locator, text) 元组）。"""
    from .content_parsers import parse_content
    segs = parse_content(path or "inline.md", content, media_type=media_type)
    return [(s.locator, s.body if not s.title else (
        f"# {s.title}\n{s.body}" if not s.body.lstrip().startswith("#") else s.body
    )) for s in segs]


def _infer_kind(title: str, body: str) -> MemoryKind:
    """启发式推断 MemoryKind（委托 policies.classify_kind）。"""
    from .policies import classify_kind

    return MemoryKind(classify_kind(f"{title} {body}"))


def _extract_title(segment_text: str) -> str:
    """从段提取标题：第一行若有 # 取其内容，否则取前 40 字符。"""
    first_line = segment_text.split("\n", 1)[0]
    m = re.match(r"^#+\s+(.+)$", first_line)
    if m:
        return m.group(1).strip()[:80]
    return segment_text[:40].replace("\n", " ").strip()


def looks_english_text(text: str) -> bool:
    if not text:
        return False
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return latin >= 12 and latin > cjk * 2


def compact_english_snippet(text: str, limit: int = 160) -> str:
    text = " ".join(str(text or "").replace("\n", " ").split())
    replacements = {
        "memory": "记忆", "project": "项目", "preference": "偏好", "rule": "规则",
        "workflow": "流程", "procedure": "流程", "constraint": "约束", "fact": "事实",
        "use": "使用", "should": "应", "must": "必须", "avoid": "避免",
        "file": "文件", "files": "文件", "folder": "文件夹", "source": "来源", "truth": "事实依据",
        "agent": "智能体", "global": "全局", "local": "本地", "compact": "简洁",
    }
    words = text[:limit].split()
    mapped = [replacements.get(w.strip(".,:;()[]{}\"'").lower(), w) for w in words[:36]]
    return " ".join(mapped).strip()


_KIND_LABELS = {
    MemoryKind.FACT: "事实", MemoryKind.PREFERENCE: "偏好", MemoryKind.PROJECT: "项目",
    MemoryKind.EPISODE: "事件", MemoryKind.PROCEDURE: "流程", MemoryKind.CORRECTION: "纠错",
}


_FAKE_ZH_PREFIXES = ("中文整理：", "中文辅助摘要：")


def _strip_fake_zh_prefix(text: str) -> str:
    for prefix in _FAKE_ZH_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].lstrip()
    return text


def _has_fake_zh_prefix(text: str) -> bool:
    return any(text.startswith(prefix) for prefix in _FAKE_ZH_PREFIXES)


def localize_memory_text(
    title: str, body: str, kind: MemoryKind,
) -> tuple[str, str, str, str, str, str]:
    """Return display fields plus honest localization metadata.

    Returns:
        title, body, original_title, original_body, display_language, localization_mode
    """
    if not looks_english_text(title + " " + body):
        return title, body, title, body, "zh", "none"
    kind_label = _KIND_LABELS.get(kind, "记忆")
    zh_title = f"{kind_label}：{compact_english_snippet(title or body, 72)}"
    zh_body = compact_english_snippet(body or title, 420)
    return zh_title, zh_body, title, body, "mixed", "heuristic"


def localized_record_fields(rec: dict[str, Any]) -> dict[str, Any]:
    """Build GUI display fields without implying full translation."""
    title = rec.get("title") or rec.get("memory_id", "")[:8]
    body = rec.get("body") or ""
    kind = rec.get("kind", "")
    original_title = rec.get("original_title") or title
    original_body = rec.get("original_body") or body
    mode = rec.get("localization_mode", "none")
    display_language = rec.get("display_language", "zh")

    if mode == "heuristic" or display_language == "mixed":
        return {
            "original_title": original_title,
            "original_body": original_body,
            "title_zh": title,
            "body_zh": body,
            "localization_mode": "heuristic",
            "display_language": display_language,
        }

    if mode == "none" and looks_english_text(title + " " + body):
        kind_enum = MemoryKind(kind) if kind in {k.value for k in MemoryKind} else MemoryKind.FACT
        zh_title, zh_body, orig_title, orig_body, disp_lang, loc_mode = localize_memory_text(
            title, body, kind_enum,
        )
        return {
            "original_title": orig_title,
            "original_body": orig_body,
            "title_zh": zh_title,
            "body_zh": zh_body,
            "localization_mode": loc_mode,
            "display_language": disp_lang,
        }

    return {
        "original_title": original_title,
        "original_body": original_body,
        "title_zh": title,
        "body_zh": body,
        "localization_mode": mode,
        "display_language": display_language,
    }


# Backward-compatible aliases for internal callers
_looks_english_text = looks_english_text
_compact_english_snippet = compact_english_snippet
_localize_memory_text = localize_memory_text


@dataclass
class MemoryIR:
    """Memory IR 容器：所有 MemoryRecord + DuplicateGroup + DecisionEvent。"""

    records: list[MemoryRecord] = field(default_factory=list)
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    decisions: list[DecisionEvent] = field(default_factory=list)
    snapshot_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "duplicate_groups": [g.to_dict() for g in self.duplicate_groups],
            "decisions": [d.to_dict() for d in self.decisions],
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryIR":
        records = []
        for r in data.get("records", []):
            provs = [Provenance(**p) for p in r.get("provenance", [])]
            # P2.2: 统一 _enum_or_default 覆盖所有 enum
            kind = _enum_or_default(MemoryKind, r.get("kind", "fact"), MemoryKind.FACT)
            status = _enum_or_default(MemoryStatus, r.get("status", "candidate"), MemoryStatus.CANDIDATE)
            completeness = _enum_or_default(Completeness, r.get("completeness", "verifiable"), Completeness.VERIFIABLE)
            records.append(MemoryRecord(
                memory_id=r["memory_id"], kind=kind,
                title=r["title"], body=r["body"], scope=r.get("scope", "project"),
                original_title=r.get("original_title", ""),
                original_body=r.get("original_body", ""),
                display_language=r.get("display_language", "zh"),
                localization_mode=r.get("localization_mode", "none"),
                confidence=r.get("confidence", 0.5), provenance=provs,
                status=status,
                completeness=completeness,
                created_at=r.get("created_at", ""),
            ))
        # P2.2: DuplicateDecision 也用 _enum_or_default 防护
        groups = [DuplicateGroup(
            group_id=g["group_id"], member_ids=g["member_ids"],
            similarity_method=g.get("similarity_method", "tfidf_cosine"),
            scores=g.get("scores", []),
            decision=_enum_or_default(DuplicateDecision, g.get("decision", "unresolved"),
                                      DuplicateDecision.UNRESOLVED),
        ) for g in data.get("duplicate_groups", [])]
        decisions = [DecisionEvent(
            event_id=d["event_id"], actor=d["actor"], action=d["action"],
            target_ids=d["target_ids"], before_hash=d.get("before_hash", ""),
            after_hash=d.get("after_hash", ""), reason=d.get("reason", ""),
            created_at=d.get("created_at", ""),
        ) for d in data.get("decisions", [])]
        return cls(records=records, duplicate_groups=groups, decisions=decisions,
                   snapshot_id=data.get("snapshot_id", ""),
                   created_at=data.get("created_at", ""))


class MemoryNormalizer:
    """从 SourceSnapshot 规范化为 Memory IR。"""

    def __init__(self, workspace: str | Path, enricher_mode: str | None = None):
        self.workspace = Path(workspace).resolve()
        self.mg_dir = self.workspace / ".memoryguard"
        self.ir_path = self.mg_dir / "ir" / "current.json"
        self.decisions_path = self.mg_dir / "ir" / "decisions.jsonl"
        self._enricher_mode = enricher_mode

    def _get_enricher(self):
        from .semantic_enricher import get_enricher

        return get_enricher(self._enricher_mode)

    def ensure_localized(self, ir: MemoryIR) -> bool:
        changed = False
        for rec in ir.records:
            needs_migration = _has_fake_zh_prefix(rec.title) or _has_fake_zh_prefix(rec.body)
            if rec.original_body and not needs_migration:
                continue
            if needs_migration:
                src_title = _strip_fake_zh_prefix(rec.original_title or rec.title)
                src_body = _strip_fake_zh_prefix(rec.original_body or rec.body)
            else:
                src_title = rec.title
                src_body = rec.body
            title, body, original_title, original_body, display_language, localization_mode = localize_memory_text(
                src_title, src_body, rec.kind,
            )
            if (
                title != rec.title or body != rec.body or original_body != rec.body
                or rec.localization_mode != localization_mode
                or needs_migration
            ):
                rec.title = title
                rec.body = body
                rec.original_title = original_title
                rec.original_body = original_body
                rec.display_language = display_language
                rec.localization_mode = localization_mode
                changed = True
        return changed

    def filter_by_source_policies(self, ir: MemoryIR, snapshot: SourceSnapshot,
                                  root_policies: dict[str, dict[str, str]] | None = None) -> bool:
        object_to_root = {obj.source_object_id: obj.source_root_id for obj in snapshot.source_objects}
        def allowed(rec: MemoryRecord) -> bool:
            for prov in rec.provenance:
                root_id = object_to_root.get(prov.source_object_id, "")
                policy = (root_policies or {}).get(root_id, {})
                cat = policy.get("source_category", "")
                ing = policy.get("ingestion_policy", "")
                if _should_skip_auto_ingest(cat, ing):
                    return False
            return True
        before = len(ir.records)
        ir.records = [rec for rec in ir.records if allowed(rec)]
        valid_ids = {rec.memory_id for rec in ir.records}
        ir.duplicate_groups = [g for g in ir.duplicate_groups if all(mid in valid_ids for mid in g.member_ids)]
        return len(ir.records) != before

    def normalize(self, snapshot: SourceSnapshot,
                  duplicate_threshold: float = 0.80,
                  root_map: dict[str, str] | None = None,
                  root_policies: dict[str, dict[str, str]] | None = None) -> MemoryIR:
        """从快照生成 Memory IR。

        v3.1 §1.3 P0：必须传入 root_map（root_id -> root.path），
        不能再用 workspace / relative_path 猜路径，否则外部来源会静默丢失。
        """
        from .content_parsers import parse_file
        from pathlib import Path

        records: list[MemoryRecord] = []
        for obj in snapshot.source_objects:
            policy = (root_policies or {}).get(obj.source_root_id, {})
            if _is_instruction_or_skill(obj.relative_path):
                continue
            if _is_plan_or_ops_doc(obj.relative_path):
                continue
            cat = policy.get("source_category", "")
            ing = policy.get("ingestion_policy", "")
            if _should_skip_auto_ingest(cat, ing):
                continue

            full_path = self._resolve_source_path(obj, root_map)
            if full_path is None:
                continue

            media = getattr(obj, "media_type", "") or ""
            is_sqlite = full_path.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".vscdb"} or "sqlite" in media
            content: str | None = None
            if not is_sqlite:
                content = self._read_source_content(obj, root_map)
                if content is None:
                    continue
                # 哈希一致性检查（v3.1 §1.3）— 仅文本路径
                if obj.read_status != "meta":
                    current_hash = stable_hash(content)
                    if current_hash != obj.content_hash:
                        continue

            surface_hint = policy.get("surface_hint", "")
            verbatim = ing == "import_verbatim"
            parsed = parse_file(
                full_path,
                media_type=media,
                surface_hint=surface_hint,
                content=content,
                verbatim=verbatim,
            )
            # meta 段不进长期记忆 IR
            parsed = [s for s in parsed if s.signal_level != "meta"]
            enricher = self._get_enricher()
            for seg in parsed:
                locator = seg.locator
                original_title = seg.title or _extract_title(seg.body)
                original_body = seg.body
                text = seg.body
                secret_hit = _detect_secret_in_text(text)
                if secret_hit:
                    kind = MemoryKind.FACT
                    safe_title = original_title
                    safe_body = f"[QUARANTINED: {secret_hit}]"
                    disp_lang = "zh"
                    loc_mode = "none"
                    conf = 0.1
                    status = MemoryStatus.QUARANTINED
                    completeness = Completeness.UNVERIFIABLE
                elif verbatim:
                    # import_verbatim：原样保留 title/body，不用 enricher 改写
                    if seg.kind_hint:
                        try:
                            kind = MemoryKind(seg.kind_hint)
                        except (ValueError, TypeError):
                            kind = _infer_kind(original_title, text)
                    else:
                        kind = _infer_kind(original_title, text)
                    safe_title = original_title
                    safe_body = original_body
                    disp_lang = "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in text[:80]) else "en"
                    loc_mode = "none"
                    conf = 0.9
                    status = MemoryStatus.CANDIDATE
                    completeness = (
                        Completeness.UNVERIFIABLE if seg.truncated else Completeness.VERIFIABLE
                    )
                else:
                    safe_text = _redact_for_enricher(text)
                    kind_hint = seg.kind_hint or ""
                    enriched = enricher.enrich(
                        title=original_title,
                        body=safe_text,
                        kind_hint=kind_hint,
                        metadata={"locator": locator, "source_object_id": obj.source_object_id},
                    )
                    try:
                        kind = MemoryKind(kind_hint) if kind_hint else MemoryKind(enriched.kind)
                    except (ValueError, TypeError):
                        try:
                            kind = MemoryKind(enriched.kind)
                        except (ValueError, TypeError):
                            kind = MemoryKind.FACT
                    safe_title = enriched.title
                    safe_body = enriched.body
                    disp_lang = enriched.display_language
                    loc_mode = enriched.localization_mode
                    conf = _safe_confidence(enriched.confidence)
                    status = MemoryStatus.CANDIDATE
                    completeness = (
                        Completeness.UNVERIFIABLE if seg.truncated else Completeness.VERIFIABLE
                    )
                excerpt_hash = stable_hash(text)
                memory_id = stable_hash(obj.source_object_id, locator, excerpt_hash)
                prov = Provenance(
                    source_object_id=obj.source_object_id, locator=locator,
                    excerpt_hash=excerpt_hash, source_revision=obj.content_hash,
                )
                rec = MemoryRecord(
                    memory_id=memory_id, kind=kind, title=safe_title, body=safe_body,
                    scope="project", original_title=original_title,
                    original_body=original_body, display_language=disp_lang,
                    localization_mode=loc_mode,
                    confidence=conf, provenance=[prov],
                    status=status,
                    completeness=completeness,
                    created_at=_now_iso(),
                )
                records.append(rec)
        groups = self._find_duplicates(records, duplicate_threshold)
        ir = MemoryIR(
            records=records, duplicate_groups=groups,
            snapshot_id=snapshot.snapshot_id, created_at=_now_iso(),
        )
        return ir

    def _resolve_source_path(self, obj: SourceObject,
                             root_map: dict[str, str] | None = None) -> Path | None:
        from pathlib import Path
        import os
        if root_map and obj.source_root_id in root_map:
            root_path = Path(root_map[obj.source_root_id]).resolve()
            # 单文件 root：relative_path 可能是文件名；目录 root：拼接
            root_as_path = Path(root_map[obj.source_root_id])
            if root_as_path.is_file():
                full = root_as_path.resolve()
            else:
                full = (root_path / obj.relative_path).resolve()
            try:
                if root_as_path.is_file():
                    pass
                else:
                    full.relative_to(root_path)
            except ValueError:
                return None
            if full.is_symlink():
                try:
                    target = Path(os.readlink(full)).resolve()
                    if not root_as_path.is_file():
                        target.relative_to(root_path)
                except (ValueError, OSError):
                    return None
            if not full.is_file():
                return None
            return full
        p = self.workspace / obj.relative_path
        return p if p.is_file() else None

    def _read_source_content(self, obj: SourceObject,
                             root_map: dict[str, str] | None = None) -> str | None:
        """v3.1 §1.3 P0：使用 SourceRoot.path 定位，不再用 workspace/relative_path 猜路径。"""
        full = self._resolve_source_path(obj, root_map)
        if full is None:
            return None
        try:
            return full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _find_duplicates(self, records: list[MemoryRecord],
                         threshold: float) -> list[DuplicateGroup]:
        """TF-IDF 生成重复候选组，不自动删除。"""
        if len(records) < 2:
            return []
        vec = TfidfVectorizer()
        vec.fit([r.body for r in records])
        vectors = [vec.transform(r.body) for r in records]
        groups: list[DuplicateGroup] = []
        used: set[int] = set()
        for i in range(len(records)):
            if i in used:
                continue
            members = [i]
            scores = [1.0]
            for j in range(i + 1, len(records)):
                if j in used:
                    continue
                sim = TfidfVectorizer.cosine(vectors[i], vectors[j])
                if sim >= threshold:
                    members.append(j)
                    scores.append(round(sim, 3))
            if len(members) > 1:
                group_id = "dup-" + stable_hash(records[i].memory_id, str(len(members)))
                groups.append(DuplicateGroup(
                    group_id=group_id,
                    member_ids=[records[m].memory_id for m in members],
                    similarity_method="tfidf_cosine",
                    scores=scores,
                    decision=DuplicateDecision.UNRESOLVED,
                ))
                used.update(members)
        return groups

    def _redact_ir_for_persist(self, ir: MemoryIR) -> None:
        """Redact secrets from all persisted text fields before writing IR."""
        from .secrets import redact_secrets

        for rec in ir.records:
            rec.title, _ = redact_secrets(rec.title)
            rec.body, _ = redact_secrets(rec.body)
            if rec.original_title:
                rec.original_title, _ = redact_secrets(rec.original_title)
            if rec.original_body:
                rec.original_body, _ = redact_secrets(rec.original_body)

    def save(self, ir: MemoryIR) -> None:
        """持久化 IR 到 .memoryguard/ir/current.json。

        P2.1: 写入前备份上一版到 .prev.json(只保留一份,覆盖式)。
        P2.1: current 和 prev 均使用临时文件 + os.replace() 原子写。
        """
        import os
        import shutil
        self._redact_ir_for_persist(ir)
        self.ir_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(ir.to_dict(), ensure_ascii=False, indent=2)
        # P2.1: 备份上一版到 .prev.json(原子写)
        prev_path = self.ir_path.with_suffix(".prev.json")
        if self.ir_path.exists():
            try:
                prev_tmp = prev_path.with_suffix(".prev.json.tmp")
                shutil.copy2(self.ir_path, prev_tmp)
                os.replace(prev_tmp, prev_path)
            except OSError:
                # 备份失败不阻塞主写入,但记录到 stderr
                import sys
                print(f"memoryguard: prev backup failed for {prev_path}", file=sys.stderr)
        # P2.1: current.json 原子写(临时文件 + os.replace)
        tmp = self.ir_path.with_suffix(".current.json.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, self.ir_path)

    def load(self) -> MemoryIR | None:
        if not self.ir_path.exists():
            return None
        try:
            data = json.loads(self.ir_path.read_text(encoding="utf-8"))
            return MemoryIR.from_dict(data)
        except (OSError, json.JSONDecodeError):
            return None

    def append_decision(self, event: DecisionEvent) -> None:
        """追加决策到 decisions.jsonl。"""
        self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
        with self.decisions_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
