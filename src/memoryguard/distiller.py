"""MemoryDistiller：IR 与发布之间的规则提炼层。

不修改 IR 事实源；产出可重建的 DistilledMemory 投影。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .policies import _jaccard, _tokenize
from .schema_v3 import (
    Completeness,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    Provenance,
    stable_hash,
)

MERGE_THRESHOLD = 0.85
DISTILLER_VERSION = "distiller-v1"
MAX_TITLE_LEN = 80
COMPRESS_BODY_THRESHOLD = 200
MAX_DISTILLED_BODY_LEN = 240


@dataclass
class DistilledGroup:
    group_id: str
    kind: str
    title: str
    body: str
    scope: str = "project"
    source_record_ids: list[str] = field(default_factory=list)
    completeness: str = Completeness.VERIFIABLE.value
    provenance: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.5
    rationale: str = ""
    enrichment_mode: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "scope": self.scope,
            "source_record_ids": list(self.source_record_ids),
            "completeness": self.completeness,
            "provenance": list(self.provenance),
            "confidence": self.confidence,
            "rationale": self.rationale,
            "enrichment_mode": self.enrichment_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DistilledGroup":
        return cls(
            group_id=data["group_id"],
            kind=data["kind"],
            title=data["title"],
            body=data["body"],
            scope=data.get("scope", "project"),
            source_record_ids=list(data.get("source_record_ids", [])),
            completeness=data.get("completeness", Completeness.VERIFIABLE.value),
            provenance=list(data.get("provenance", [])),
            confidence=float(data.get("confidence", 0.5)),
            rationale=data.get("rationale", ""),
            enrichment_mode=data.get("enrichment_mode", "rule"),
        )


@dataclass
class DistilledMemory:
    distilled_id: str
    source_snapshot_id: str
    groups: list[DistilledGroup] = field(default_factory=list)
    redundant_record_ids: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "distilled_id": self.distilled_id,
            "source_snapshot_id": self.source_snapshot_id,
            "groups": [g.to_dict() for g in self.groups],
            "redundant_record_ids": list(self.redundant_record_ids),
            "stats": dict(self.stats),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DistilledMemory":
        return cls(
            distilled_id=data["distilled_id"],
            source_snapshot_id=data.get("source_snapshot_id", ""),
            groups=[DistilledGroup.from_dict(g) for g in data.get("groups", [])],
            redundant_record_ids=list(data.get("redundant_record_ids", [])),
            stats=dict(data.get("stats", {})),
        )


def _record_status(rec: MemoryRecord) -> str:
    return rec.status.value if hasattr(rec.status, "value") else str(rec.status)


_NON_PUBLISHABLE_STATUSES = frozenset({
    "rejected", "quarantined", "superseded", "shadowed",
})


def _is_publishable(rec: MemoryRecord) -> bool:
    return _record_status(rec) not in _NON_PUBLISHABLE_STATUSES


def _pick_primary(records: list[MemoryRecord]) -> MemoryRecord:
    return max(records, key=lambda r: (len(r.body), r.confidence))


def _record_scope(rec: MemoryRecord) -> str:
    scope = str(getattr(rec, "scope", "project") or "project")
    return scope if scope else "project"


def _record_completeness(rec: MemoryRecord) -> str:
    completeness = getattr(rec, "completeness", Completeness.VERIFIABLE)
    if hasattr(completeness, "value"):
        return completeness.value
    return str(completeness)


def _combined_completeness(records: list[MemoryRecord]) -> str:
    values = {_record_completeness(r) for r in records}
    if len(values) == 1:
        return values.pop()
    return Completeness.UNVERIFIABLE.value


def _truncate_title(title: str) -> str:
    title = title.strip()
    if len(title) <= MAX_TITLE_LEN:
        return title
    return title[:MAX_TITLE_LEN].rstrip()


def _extract_core_body(body: str) -> str:
    text = body.strip()
    if len(text) <= COMPRESS_BODY_THRESHOLD:
        return text
    sentences = re.split(r"[。\n！？.!?]", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return text[:MAX_DISTILLED_BODY_LEN]
    count = 3 if len(text) > COMPRESS_BODY_THRESHOLD * 2 else 1
    key_sentences = sorted(sentences, key=len, reverse=True)[:count]
    distilled = "。".join(key_sentences)
    if len(distilled) > MAX_DISTILLED_BODY_LEN:
        distilled = distilled[:MAX_DISTILLED_BODY_LEN].rstrip()
    return distilled


def _merge_provenance(records: list[MemoryRecord]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, Any]] = []
    for rec in records:
        for prov in rec.provenance:
            key = (prov.source_object_id, prov.locator, prov.excerpt_hash)
            if key in seen:
                continue
            seen.add(key)
            merged.append(prov.to_dict())
    return merged


def _build_group(member_records: list[MemoryRecord], *, rationale: str) -> DistilledGroup:
    primary = _pick_primary(member_records)
    member_ids = [primary.memory_id] + [
        r.memory_id for r in member_records if r.memory_id != primary.memory_id
    ]
    group_id = stable_hash("distilled-group", *sorted(member_ids))
    avg_conf = sum(r.confidence for r in member_records) / len(member_records)
    scope = _record_scope(primary)
    if not all(_record_scope(r) == scope for r in member_records):
        scope = "ambiguous"
    return DistilledGroup(
        group_id=group_id,
        kind=primary.kind.value if hasattr(primary.kind, "value") else str(primary.kind),
        scope=scope,
        title=_truncate_title(primary.title),
        body=_extract_core_body(primary.body),
        source_record_ids=member_ids,
        provenance=_merge_provenance(member_records),
        completeness=_combined_completeness(member_records),
        confidence=avg_conf,
        rationale=rationale,
        enrichment_mode="rule",
    )


def _can_merge_records(a: MemoryRecord, b: MemoryRecord) -> bool:
    if a.kind != b.kind:
        return False
    return _record_scope(a) == _record_scope(b)


class MemoryDistiller:
    """规则版 MemoryDistiller：合并同义、抽核心、标记冗余。"""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self.mg_dir = self.workspace / ".memoryguard"
        self.distilled_path = self.mg_dir / "ir" / "distilled.json"

    def distill(self, ir: Any) -> DistilledMemory:
        record_map = {r.memory_id: r for r in ir.records}
        eligible = [r for r in ir.records if _is_publishable(r)]
        input_count = len(eligible)

        assigned: set[str] = set()
        redundant: list[str] = []
        groups: list[DistilledGroup] = []

        for dup in ir.duplicate_groups:
            members = [record_map[mid] for mid in dup.member_ids if mid in record_map and _is_publishable(record_map[mid])]
            if len(members) < 2:
                continue
            by_signature: dict[tuple[str, str], list[MemoryRecord]] = {}
            for rec in members:
                key = (_record_scope(rec), rec.kind.value if hasattr(rec.kind, "value") else str(rec.kind))
                by_signature.setdefault(key, []).append(rec)
            for bucket in by_signature.values():
                if len(bucket) >= 2:
                    for rec in bucket:
                        if rec.memory_id != _pick_primary(bucket).memory_id:
                            redundant.append(rec.memory_id)
                        assigned.add(rec.memory_id)
                    groups.append(_build_group(bucket, rationale="merged_from_duplicate_group"))
                else:
                    rec = bucket[0]
                    groups.append(_build_group([rec], rationale="single_record"))
                    assigned.add(rec.memory_id)

        remaining = [r for r in eligible if r.memory_id not in assigned]
        remaining.sort(key=lambda r: r.memory_id)
        while remaining:
            seed = remaining.pop(0)
            cluster = [seed]
            still_remaining: list[MemoryRecord] = []
            seed_tokens = _tokenize(f"{seed.title} {seed.body}")
            for other in remaining:
                if not _can_merge_records(seed, other):
                    still_remaining.append(other)
                    continue
                other_tokens = _tokenize(f"{other.title} {other.body}")
                if _jaccard(seed_tokens, other_tokens) >= MERGE_THRESHOLD:
                    cluster.append(other)
                else:
                    still_remaining.append(other)
            remaining = still_remaining
            if len(cluster) > 1:
                primary = _pick_primary(cluster)
                for rec in cluster:
                    if rec.memory_id != primary.memory_id:
                        redundant.append(rec.memory_id)
                    assigned.add(rec.memory_id)
                groups.append(_build_group(cluster, rationale="merged_by_jaccard"))
            else:
                assigned.add(seed.memory_id)
                groups.append(_build_group([seed], rationale="single_record"))

        # 兼容旧行为：若 cluster 组内剩余 seed 仍为空，不再添加重复记录。

        output_count = len(groups)
        ratio = round(output_count / input_count, 4) if input_count else 1.0
        group_ids = sorted(g.group_id for g in groups)
        distilled_id = stable_hash(ir.snapshot_id or "no-snapshot", DISTILLER_VERSION, *group_ids)

        return DistilledMemory(
            distilled_id=distilled_id,
            source_snapshot_id=ir.snapshot_id,
            groups=groups,
            redundant_record_ids=sorted(set(redundant)),
            stats={
                "input_count": input_count,
                "output_count": output_count,
                "compression_ratio": ratio,
            },
        )

    def save(self, distilled: DistilledMemory) -> None:
        self.distilled_path.parent.mkdir(parents=True, exist_ok=True)
        self.distilled_path.write_text(
            json.dumps(distilled.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self) -> DistilledMemory | None:
        if not self.distilled_path.exists():
            return None
        try:
            data = json.loads(self.distilled_path.read_text(encoding="utf-8"))
            return DistilledMemory.from_dict(data)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
