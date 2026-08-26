"""Deterministic claim-level composition for canonical memory bodies.

The composer deliberately has no model or store dependency.  It only parses
formatting boundaries, delegates semantic safety to the shared governance
classifier, and renders one stable multi-claim record.  Unrelated claims are
reported to the caller so a higher layer can retain them as separate records.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from ..rule_definition import normalize_rule_text
from .governance_semantics import (
    GovernanceRelation,
    classify_governance_relation,
    semantic_tokens,
)


_CLAIM_BREAK = re.compile(r"[\r\n]+|(?<=[。！？!?；;])\s*|(?<=[.!?])(?=\s|$)")
_FORMAT_PREFIX = re.compile(
    r"^\s*(?:(?:[-*+•◦▪▸‣])\s+|\[\d+\]\s+|\d+[.)]\s+)+"
)
_LATIN_ANCHOR = re.compile(r"[a-z][a-z0-9_.+-]{2,}", re.IGNORECASE)
_ASCII_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.+#/-]*", re.IGNORECASE)
_HAN_RUN = re.compile(r"[\u3400-\u9fff]+")
_GENERIC_LATIN = frozenset({
    "agent", "always", "and", "default", "defaults", "do", "for", "from", "general",
    "guideline", "guidelines", "instruction", "instructions", "keep", "mandatory", "must",
    "never", "note", "notes", "please", "policy", "procedure", "require", "required",
    "rule", "rules", "subagent", "task", "tasks", "the", "todo", "user", "users",
    "use", "using", "with",
})
_GENERIC_HAN = frozenset({
    "以及", "可以", "始终", "建议", "应该", "并且", "必须", "所有", "规则", "默认",
    "要求", "需要", "用户", "习惯", "不要", "不得", "禁止", "优先", "每次", "通用",
    "使用", "执行", "运行", "进行", "处理", "操作", "允许", "任务", "对象", "主题",
    "范围", "场景", "相关", "方面",
})
_GENERIC_HEADING = frozenset({
    "default", "general", "guideline", "guidelines", "instruction", "instructions",
    "note", "notes", "policy", "procedure", "rule", "rules", "todo", "通用", "规则",
    "默认", "要求", "说明", "提示", "注意", "用户", "习惯",
})
_NEGATION = re.compile(
    r"\b(?:never|don't|do not|must not|forbid|forbidden|without)\b|不[\u3400-\u9fff]?|禁止",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClaimComposition:
    """Result of composing the safe semantic cluster from body strings."""

    body: str
    claims: tuple[str, ...]
    rejected_conflicts: tuple[str, ...]
    changed: bool
    rejected_unrelated: tuple[str, ...] = ()


def _strip_format_prefix(value: str) -> str:
    """Remove bullets/indices only; preserve the claim's semantic wording."""

    current = str(value or "").strip()
    previous = None
    while current and current != previous:
        previous = current
        current = _FORMAT_PREFIX.sub("", current).strip()
    return current


def _split_claims(body: str) -> list[str]:
    claims: list[str] = []
    for piece in _CLAIM_BREAK.split(str(body or "")):
        claim = _strip_format_prefix(piece)
        if claim:
            claims.append(claim)
    return claims


def _claim_key(value: str) -> tuple[str, str, str]:
    surface = " ".join(str(value or "").split()).casefold()
    raw = " ".join(str(value or "").split())
    return normalize_rule_text(surface), surface, raw


def _specificity(value: str) -> tuple[int, int, int, str]:
    surface = " ".join(str(value or "").split())
    return (
        len(semantic_tokens(surface)),
        len(surface),
        len(str(value or "")),
        _claim_key(surface)[0],
    )


def _preferred_claim(left: str, right: str) -> str:
    """Select the more complete equivalent/update surface deterministically."""

    left_key = _specificity(left)
    right_key = _specificity(right)
    if right_key > left_key:
        return right
    if left_key > right_key:
        return left
    return min((left, right), key=_claim_key)


def _relation(left: str, right: str) -> GovernanceRelation:
    return classify_governance_relation(left, right)


def _is_conflict(left: str, right: str) -> bool:
    left_heading, right_heading = _heading(left), _heading(right)
    if left_heading and left_heading == right_heading:
        left_tail, right_tail = _heading_tail(left, left_heading), _heading_tail(right, right_heading)
        relation = _relation(left_tail, right_tail)
        return _conflict_confident(relation, left_tail, right_tail) or _direct_polarity_conflict(
            left_tail, right_tail
        )
    if left_heading or right_heading:
        left_tail = _heading_tail(left, left_heading)
        right_tail = _heading_tail(right, right_heading)
        relation = _relation(left_tail, right_tail)
        return _conflict_confident(relation, left_tail, right_tail) or _direct_polarity_conflict(
            left_tail, right_tail
        )

    relation = _relation(left, right)
    if relation.kind == "conflict":
        if _conflict_confident(relation, left, right):
            return True
    return _direct_polarity_conflict(left, right)


def _semantic_anchors(value: str) -> frozenset[str]:
    """Return conservative non-generic anchors for additive relatedness."""

    latin, cjk = _feature_sets(value)
    return frozenset(latin | cjk)


def _strip_generic_han(value: str) -> str:
    cleaned = str(value or "")
    for generic in sorted(_GENERIC_HAN, key=len, reverse=True):
        cleaned = cleaned.replace(generic, "")
    return cleaned


def _feature_sets(value: str) -> tuple[frozenset[str], frozenset[str]]:
    text = " ".join(str(value or "").casefold().split())
    latin = frozenset(
        token
        for token in _ASCII_IDENTIFIER.findall(text)
        if len(token) >= 3 and token not in _GENERIC_LATIN
    )
    han: set[str] = set()
    for run in _HAN_RUN.findall(text):
        cleaned = _strip_generic_han(run)
        han.update(cleaned[index:index + 2] for index in range(len(cleaned) - 1))
        han.update(cleaned[index:index + 3] for index in range(len(cleaned) - 2))
    return latin, frozenset(han)


def _heading(value: str) -> str:
    match = re.match(r"^\s*([^:：]{1,32})\s*[:：]", str(value or ""))
    if not match:
        return ""
    heading = " ".join(match.group(1).split()).casefold().strip(" -_#")
    if not heading or heading in _GENERIC_HEADING:
        return ""
    if not _semantic_anchors(heading):
        return ""
    return normalize_rule_text(heading)


def _heading_tail(value: str, heading: str = "") -> str:
    match = re.match(r"^\s*([^:：]{1,32})\s*[:：]\s*", str(value or ""))
    if not match or (heading and _heading(value) != heading):
        return str(value or "").strip()
    return str(value or "")[match.end():].strip()


def _compact_surface(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").casefold())


def _set_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    if not overlap:
        return 0.0
    containment = overlap / min(len(left), len(right))
    jaccard = overlap / len(left | right)
    return 0.65 * containment + 0.35 * jaccard


def _topic_pair_affinity(left: str, right: str) -> float:
    left_compact, right_compact = _compact_surface(left), _compact_surface(right)
    if not left_compact or not right_compact:
        return 0.0
    if left_compact == right_compact:
        return 1.0

    scores: list[float] = []
    if min(len(left_compact), len(right_compact)) >= 4:
        if left_compact in right_compact or right_compact in left_compact:
            scores.append(0.84 + 0.12 * (
                min(len(left_compact), len(right_compact))
                / max(len(left_compact), len(right_compact))
            ))
    left_ascii, left_cjk = _feature_sets(left)
    right_ascii, right_cjk = _feature_sets(right)
    scores.extend((
        _set_similarity(left_ascii, right_ascii),
        _set_similarity(left_cjk, right_cjk),
    ))
    left_chars = frozenset(_strip_generic_han("".join(_HAN_RUN.findall(left))))
    right_chars = frozenset(_strip_generic_han("".join(_HAN_RUN.findall(right))))
    scores.append(_set_similarity(left_chars, right_chars))
    return max(scores, default=0.0)


def topic_affinity(left: str, right: str) -> float:
    """Return deterministic structural topic affinity in the inclusive range 0..1."""

    left_heading, right_heading = _heading(left), _heading(right)
    score = _topic_pair_affinity(left, right)
    if left_heading and left_heading == right_heading:
        score = max(score, 0.55)
        score = max(
            score,
            _topic_pair_affinity(
                _heading_tail(left, left_heading),
                _heading_tail(right, right_heading),
            ),
        )
    elif left_heading or right_heading:
        score = max(
            score,
            _topic_pair_affinity(
                _heading_tail(left, left_heading),
                _heading_tail(right, right_heading),
            ),
        )
    return max(0.0, min(1.0, score))


def _direct_polarity_conflict(left: str, right: str) -> bool:
    left_negative = bool(_NEGATION.search(left))
    right_negative = bool(_NEGATION.search(right))
    if left_negative == right_negative:
        return False
    left_base = _NEGATION.sub("", left)
    right_base = _NEGATION.sub("", right)
    left_compact, right_compact = _compact_surface(left_base), _compact_surface(right_base)
    if min(len(left_compact), len(right_compact)) < 4:
        return False
    if left_compact in right_compact or right_compact in left_compact:
        return True
    left_chars = frozenset("".join(_HAN_RUN.findall(left_base)))
    right_chars = frozenset("".join(_HAN_RUN.findall(right_base)))
    overlap = len(left_chars & right_chars)
    if overlap >= 3 and overlap / min(len(left_chars), len(right_chars)) >= 0.55:
        return True
    left_ascii, left_cjk = _feature_sets(left_base)
    right_ascii, right_cjk = _feature_sets(right_base)
    return (
        len(left_ascii & right_ascii) >= 2
        and _set_similarity(left_ascii, right_ascii) >= 0.72
    ) or (
        len(left_cjk & right_cjk) >= 2
        and _set_similarity(left_cjk, right_cjk) >= 0.72
    )


def _conflict_confident(
    relation: GovernanceRelation,
    left: str,
    right: str,
) -> bool:
    if relation.kind != "conflict":
        return False
    shared = _semantic_anchors(left) & _semantic_anchors(right)
    # The broad relation classifier can label two compatible guardrails as
    # opposite merely because both mention the same action (for example,
    # "delegate only when necessary" and "do not repeatedly delegate").
    # A semantic-only conflict therefore needs more than one shared anchor;
    # exact one-anchor polarity reversals are still caught separately by
    # _direct_polarity_conflict().
    return len(shared) >= 2 and float(relation.score) >= 0.58


def _related(left: str, right: str) -> bool:
    """Return whether two non-conflicting claims may share one record."""

    return claims_related(left, right)


def claims_related(left: str, right: str) -> bool:
    """Return whether two claims are safe members of one canonical topic component."""

    if _is_conflict(left, right):
        return False
    left_heading, right_heading = _heading(left), _heading(right)
    same_heading = bool(left_heading and left_heading == right_heading)
    different_headings = bool(left_heading and right_heading and left_heading != right_heading)
    comparison_left = _heading_tail(left, left_heading) if same_heading else left
    comparison_right = _heading_tail(right, right_heading) if same_heading else right
    relation = _relation(comparison_left, comparison_right)
    if different_headings:
        return topic_affinity(comparison_left, comparison_right) >= 0.32 and bool(
            _semantic_anchors(comparison_left) & _semantic_anchors(comparison_right)
        )
    if bool(left_heading) != bool(right_heading):
        return topic_affinity(comparison_left, comparison_right) >= 0.32 and bool(
            _semantic_anchors(comparison_left) & _semantic_anchors(comparison_right)
        )
    affinity = topic_affinity(left, right)
    shared = _semantic_anchors(left) & _semantic_anchors(right)
    # The classifier already splits compound ASCII identifiers (foo/bar vs
    # foo + bar) and labels same-polarity paraphrases exact/equivalent/update.
    # Structural anchors do not; requiring them here would split one rule into
    # two canonical atoms after an incremental compose rewrote the surface.
    if relation.kind in {"exact", "equivalent", "update"}:
        return True
    if relation.kind == "additive":
        return affinity >= 0.28 and bool(shared)
    if left_heading and left_heading == right_heading:
        return True
    return affinity >= 0.32 and bool(shared)


def _deduplicate_claims(claims: Sequence[str]) -> list[str]:
    """Collapse exact/equivalent/update claims while retaining additive ones."""

    representatives: list[str] = []
    for claim in sorted(set(claims), key=_claim_key):
        merged = False
        for index, existing in enumerate(representatives):
            relation = _relation(existing, claim)
            if relation.kind not in {"exact", "equivalent", "update"}:
                continue
            representatives[index] = _preferred_claim(existing, claim)
            merged = True
            break
        if not merged:
            representatives.append(claim)
    return sorted(representatives, key=_claim_key)


def _render(claims: Sequence[str]) -> str:
    return "\n".join(f"- {claim}" for claim in claims)


def _conflict_rejections(claims: Sequence[str]) -> set[str]:
    """Reject both members of every classifier-confirmed conflict edge."""

    rejected: set[str] = set()
    for index, left in enumerate(claims):
        for right in claims[index + 1:]:
            if not _is_conflict(left, right):
                continue
            rejected.update((left, right))
    return rejected


def _components(claims: Sequence[str]) -> list[list[str]]:
    """Build deterministic connected components of related claims."""

    ordered = sorted(claims, key=_claim_key)
    remaining = set(ordered)
    components: list[list[str]] = []
    while remaining:
        start = min(remaining, key=_claim_key)
        remaining.remove(start)
        component = [start]
        frontier = [start]
        while frontier:
            current = frontier.pop()
            neighbors = [
                candidate for candidate in sorted(remaining, key=_claim_key)
                if _related(current, candidate)
            ]
            for candidate in neighbors:
                remaining.remove(candidate)
                frontier.append(candidate)
                component.append(candidate)
        components.append(sorted(component, key=_claim_key))
    return components


def compose_canonical_bodies(bodies: Sequence[str]) -> ClaimComposition:
    """Compose one safe canonical record from claim-bearing body strings.

    Only related claims share a record.  Distinct claims must provide a
    semantic anchor or explicit heading; otherwise their component is returned
    through ``rejected_unrelated`` for separate persistence.  Conflicting
    claims are both rejected so callers can abort rather than pick a winner.
    """

    parsed: list[str] = []
    source_members: dict[str, set[int]] = {}
    evidence_counts: dict[str, int] = {}
    for source_index, body in enumerate(bodies):
        for claim in _split_claims(str(body or "")):
            parsed.append(claim)
            source_members.setdefault(claim, set()).add(source_index)
            evidence_counts[claim] = evidence_counts.get(claim, 0) + 1
    if not parsed:
        return ClaimComposition("", (), (), bool(bodies), ())

    unique_claims = sorted(set(parsed), key=_claim_key)
    rejected_conflicts = _conflict_rejections(unique_claims)
    eligible = [claim for claim in unique_claims if claim not in rejected_conflicts]
    components = _components(eligible) if eligible else []

    def component_key(component: Sequence[str]) -> tuple[object, ...]:
        source_coverage = len({
            source_index
            for claim in component
            for source_index in source_members.get(claim, ())
        })
        evidence = sum(evidence_counts.get(claim, 0) for claim in component)
        return (
            -source_coverage,
            -evidence,
            -len(component),
            tuple(_claim_key(item) for item in component),
        )

    selected = min(
        components,
        key=component_key,
        default=[],
    )
    selected_set = set(selected)
    claims = tuple(_deduplicate_claims(selected))
    rendered = _render(claims)
    rejected_unrelated = tuple(
        sorted(
            (claim for claim in eligible if claim not in selected_set),
            key=_claim_key,
        )
    )

    source = str(bodies[0] or "").strip() if len(bodies) == 1 else ""
    changed = bool(
        rendered != source
        or len(bodies) != 1
        or rejected_conflicts
        or rejected_unrelated
    )
    return ClaimComposition(
        body=rendered,
        claims=claims,
        rejected_conflicts=tuple(sorted(rejected_conflicts, key=_claim_key)),
        changed=changed,
        rejected_unrelated=rejected_unrelated,
    )


__all__ = [
    "ClaimComposition",
    "claims_related",
    "compose_canonical_bodies",
    "topic_affinity",
]
