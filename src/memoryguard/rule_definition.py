"""Rule Definition: the semantic core of a governed rule (P3).

A ``RuleDefinition`` answers "what the rule *is*" — its normalized intent,
polarity and parameter schema — and deliberately knows nothing about who uses
it or where.  Scope lives in ``RuleBinding``; provenance lives in
``RuleEvidence``.  Splitting those three concerns is what lets repeated rules
merge across Agents and projects without ever merging permission.

Nothing here touches a database.  The three-layer duplicate matcher and the
polarity/parameter extractors are pure functions so the merge pipeline can be
tested and shared by MCP, hooks and the GUI.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from .schema_v3 import MemoryKind, _now_iso, stable_hash

POLARITY_POSITIVE = "positive"
POLARITY_NEGATIVE = "negative"

# Negative-intent markers: "提交前必须运行测试" is positive; "不要提交未测试代码"
# is negative.  The list is deliberately conservative so positive text is never
# misread as a prohibition.
_NEGATIVE_MARKERS = (
    "不要", "禁止", "绝不", "不得", "避免", "严禁", "不要使用", "别", "别用",
    "不许", "不应当", "不得使用", "禁止使用",
)
# Normalization: things that never carry semantic weight.
_STRIP_TOKENS = (
    "所有", "全部", "任何", "的", "地", "得", "必须", "应该", "应当", "需要",
    "要", "请", "务必", "统一", "默认", "一律", "以及", "与", "和", "并",
)
# Common action/object vocabulary used by the intent fingerprint.  These make
# the intent dimension stable across surface wording while still being a pure
# deterministic token match (no LLM, no embeddings).
_ACTIONS = (
    "运行", "执行", "提交", "使用", "采用", "安装", "测试", "检查", "保持",
    "写入", "读取", "启动", "停止", "部署", "更新", "创建", "删除", "覆盖",
    "验证", "记录", "引用", "调用", "run", "test", "commit", "push", "install",
)
_OBJECTS = (
    "测试", "代码", "项目", "依赖", "规则", "配置", "日志", "文件", "数据",
    "测试用例", "pytest", "pnpm", "npm", "yarn", "git", "docker", "python",
    "utf-8", "rtk", "utf8", "caveman",
)
_TRIGGERS = (
    "提交前", "提交代码前", "之前", "前", "时", "时候", "每次", "定期",
    "before commit", "before_commit", "before", "when",
)

# Synonym normalisation: surface wording collapses into a canonical action /
# object / trigger so "运行测试" and "执行测试" hash identically.  This is the
# deterministic backbone of the "intent fingerprint" — no LLM, no embeddings.
_ACTION_SYNONYMS = {
    "运行": "run", "执行": "run", "跑": "run", "run": "run",
    "测试": "test", "检查": "test", "验证": "test", "test": "test",
    "提交": "commit", "推送": "commit", "push": "commit", "commit": "commit",
    "使用": "use", "采用": "use", "引用": "use", "调用": "use",
    "安装": "install", "install": "install",
    "创建": "create", "删除": "remove", "更新": "update",
    "启动": "start", "停止": "stop", "保持": "keep",
    "写入": "write", "读取": "read", "记录": "record", "部署": "deploy",
}
_OBJECT_SYNONYMS = {
    "测试用例": "test", "测试": "test",
    "代码": "code", "依赖": "dependency", "项目": "project",
    "配置": "config", "日志": "log", "文件": "file", "数据": "data",
    "规则": "rule",
    "pytest": "pytest", "pnpm": "pnpm", "npm": "npm", "yarn": "yarn",
    "git": "git", "docker": "docker", "python": "python",
    "utf-8": "utf8", "utf8": "utf8", "rtk": "rtk", "caveman": "caveman",
}
_TRIGGER_SYNONYMS = {
    "提交代码前": "before_commit", "提交前": "before_commit",
    "before commit": "before_commit", "before_commit": "before_commit",
    "before": "before_commit", "之前": "before_commit", "前": "before_commit",
    "每次": "periodic", "定期": "periodic",
    "时": "when", "时候": "when", "when": "when",
}

# Intent extraction is deliberately small and interpretable.  The fingerprint
# is a JSON document; semantic hashing operates on the canonical projection so
# surface differences collapse and real parameter differences survive.


@dataclass(frozen=True)
class RuleIntent:
    """Structured intent fingerprint for one rule definition."""
    action: str = ""
    object: str = ""
    trigger: str = ""
    parameters: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "object": self.object,
            "trigger": self.trigger,
            "parameters": sorted(self.parameters),
        }

    def canonical(self) -> str:
        """Deterministic canonical projection used for intent hashing."""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleIntent":
        return cls(
            action=str(data.get("action", "") or ""),
            object=str(data.get("object", "") or ""),
            trigger=str(data.get("trigger", "") or ""),
            parameters=tuple(str(x) for x in data.get("parameters", [])),
        )


def detect_polarity(text: str) -> str:
    """Return ``positive`` or ``negative`` from explicit prohibition markers."""
    lowered = str(text or "").casefold()
    for marker in _NEGATIVE_MARKERS:
        if marker.casefold() in lowered:
            return POLARITY_NEGATIVE
    return POLARITY_POSITIVE


def normalize_rule_text(text: str) -> str:
    """Strip weightless tokens and punctuation for canonical comparison."""
    value = str(text or "").strip()
    for token in _STRIP_TOKENS:
        value = value.replace(token, "")
    value = re.sub(r"[^\w一-鿿]+", "", value)
    return value.casefold().strip()


def semantic_surface(text: str) -> str:
    """Surface projection with synonyms collapsed (for the semantic layer).

    ``canonical_text`` keeps the exact stripped wording so exact duplicates
    anchor to one Definition id; the semantic layer instead compares this
    synonym-collapsed projection so "运行测试" and "执行测试" score near-identical
    even though their raw surface differs.
    """
    value = normalize_rule_text(text)
    tables = (
        (_TRIGGER_SYNONYMS, _TRIGGERS),
        (_ACTION_SYNONYMS, _ACTIONS),
        (_OBJECT_SYNONYMS, _OBJECTS),
    )
    for table, candidates in tables:
        # Longest-first so "提交代码前" wins over "提交前".
        for token in sorted(candidates, key=len, reverse=True):
            key = token.casefold()
            if key in value:
                value = value.replace(key, table.get(key, key))
    return value.strip()


def extract_parameters(text: str) -> tuple[str, ...]:
    """Pull explicit parameter tokens (pytest/unittest/npm/…).

    Only standalone ASCII word tokens and quoted fragments count; bare words
    embedded in Chinese text are treated as intent vocabulary, not parameters.
    """
    found: list[str] = []
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9_.-]{1,}", str(text or "")):
        token = match.group(0).casefold()
        if token in {"test", "run", "commit", "before", "when", "code"}:
            continue
        if token not in found:
            found.append(token)
    return tuple(sorted(found))


def _normalize_token(value: str, table: dict[str, str], candidates: tuple[str, ...]) -> str:
    lowered = str(value or "").casefold()
    for token in candidates:
        key = token.casefold()
        if key in lowered:
            return table.get(key, key)
    return ""


def extract_intent(text: str, kind: MemoryKind | str | None = None) -> RuleIntent:
    """Extract the structured intent fingerprint from a rule sentence.

    Surface wording is normalised through synonym tables, so "运行测试",
    "执行测试" and "run tests" all collapse to the same action/object pair.
    """
    value = str(text or "").strip()
    action = _normalize_token(value, _ACTION_SYNONYMS, _ACTIONS)
    obj = _normalize_token(value, _OBJECT_SYNONYMS, _OBJECTS)
    trigger = _normalize_token(value, _TRIGGER_SYNONYMS, _TRIGGERS)
    return RuleIntent(
        action=action, object=obj, trigger=trigger,
        parameters=extract_parameters(value),
    )


def semantic_hash(text: str, intent: RuleIntent | None = None, polarity: str = "") -> str:
    """Stable hash over the semantic projection of a rule.

    The hash deliberately covers intent + polarity + parameters, never the raw
    surface wording.  Two sentences that mean the same thing collapse to the
    same hash; a polarity flip or a parameter change does not.
    """
    intent = intent or extract_intent(text)
    return stable_hash(
        "rule-definition-v1",
        intent.canonical(),
        polarity or detect_polarity(text),
        json.dumps(
            {"parameters": sorted(intent.parameters)},
            ensure_ascii=False, sort_keys=True,
        ),
    )


@dataclass
class RuleDefinition:
    """Semantic core of a governed rule (P3).  No scope, no provenance."""
    definition_id: str
    canonical_text: str
    normalized_intent: str
    rule_kind: str = "workflow"
    polarity: str = POLARITY_POSITIVE
    semantic_hash: str = ""
    parameter_schema: str = "{}"
    status: str = "active"  # active | merged | alias
    confidence: float = 1.0
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    superseded_by: str = ""  # definition_id this alias/merged definition points at

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition_id": self.definition_id,
            "canonical_text": self.canonical_text,
            "normalized_intent": self.normalized_intent,
            "rule_kind": self.rule_kind,
            "polarity": self.polarity,
            "semantic_hash": self.semantic_hash,
            "parameter_schema": self.parameter_schema,
            "status": self.status,
            "confidence": self.confidence,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleDefinition":
        return cls(
            definition_id=data["definition_id"],
            canonical_text=data.get("canonical_text", ""),
            normalized_intent=data.get("normalized_intent", ""),
            rule_kind=data.get("rule_kind", "workflow"),
            polarity=data.get("polarity", POLARITY_POSITIVE),
            semantic_hash=data.get("semantic_hash", ""),
            parameter_schema=data.get("parameter_schema", "{}"),
            status=data.get("status", "active"),
            confidence=float(data.get("confidence", 1.0)),
            revision=int(data.get("revision", 1)),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            superseded_by=data.get("superseded_by", ""),
        )

    def with_revision(self, revision: int | None = None) -> "RuleDefinition":
        return replace(
            self,
            revision=self.revision + 1 if revision is None else revision,
            updated_at=_now_iso(),
        )


def build_definition(
    body: str,
    *,
    definition_id: str = "",
    kind: MemoryKind | str = "workflow",
    confidence: float = 1.0,
    created_at: str = "",
) -> RuleDefinition:
    """Build a Definition from raw rule text (used by backfill and dual-write).

    The definition id is anchored to the *canonical surface wording*: the exact
    same sentence collapses to one Definition on ingest, while a synonym
    rephrase becomes a distinct Definition that the merge layer can later
    evaluate.  ``semantic_hash`` is the similarity key for that evaluation and
    is intentionally a different (wider) projection than the id.
    """
    text = str(body or "").strip()
    intent = extract_intent(text, kind=kind)
    polarity = detect_polarity(text)
    parameters = sorted(intent.parameters)
    kind_value = kind.value if isinstance(kind, MemoryKind) else str(kind)
    normalized_intent = intent.canonical()
    canon = normalize_rule_text(text)
    definition_id = definition_id or stable_hash(
        "rule-definition", "canonical", canon,
    )
    return RuleDefinition(
        definition_id=definition_id,
        canonical_text=canon,
        normalized_intent=normalized_intent,
        rule_kind=kind_value,
        polarity=polarity,
        semantic_hash=semantic_hash(text, intent, polarity),
        parameter_schema=json.dumps(
            {"parameters": parameters}, ensure_ascii=False, sort_keys=True,
        ),
        confidence=confidence,
        created_at=created_at,
        updated_at=created_at,
    )
