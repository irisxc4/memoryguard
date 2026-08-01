"""Merge policy: the safety rules for Definition merging (P3).

Merging Definitions must never expand permission.  This module encodes:

1. Three-layer duplicate detection (exact hash / intent fingerprint / semantic
   n-gram similarity) composed into one ``duplicate_score``.
2. The five auto-merge conditions (similarity, polarity, parameters,
   independent evidence, zero contradiction).
3. The scope-invariance contract that a merge transaction must honour:
   ``before_bindings == after_bindings``.

Everything here is a pure function; storage lives in ``RuleMergeStore``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .rule_definition import (
    POLARITY_POSITIVE,
    RuleDefinition,
    RuleIntent,
    semantic_surface,
)
from .rule_evidence import dedupe_evidence

# Auto-merge thresholds (P3 §5).
AUTO_MERGE_SCORE = 0.95
AUTO_MERGE_MIN_EVIDENCE = 3
AUTO_MERGE_MIN_AGENTS = 2
AUTO_MERGE_MIN_PROJECTS = 2
AUTO_MERGE_ZERO_CONTRADICTION = 0

# Layer weights (P3 §4): 0.3 exact + 0.4 intent + 0.3 semantic.
W_HASH = 0.3
W_INTENT = 0.4
W_SEMANTIC = 0.3


def _chars(value: str) -> list[str]:
    return list(str(value or "").casefold())


def char_bigram_set(value: str) -> set[str]:
    """Character bigrams for semantic similarity (no external embeddings)."""
    chars = _chars(value)
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)} if len(chars) >= 2 else set(chars)


def dice_coefficient(a: str, b: str) -> float:
    """Dice coefficient over character bigrams, the semantic layer's score."""
    set_a = char_bigram_set(a)
    set_b = char_bigram_set(b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return 2.0 * len(set_a & set_b) / (len(set_a) + len(set_b))


@dataclass(frozen=True)
class LayerScores:
    """The three independent similarity layers and their composition."""
    hash_score: float
    intent_score: float
    semantic_score: float
    duplicate_score: float


def intent_similarity(a: RuleIntent, b: RuleIntent) -> float:
    """Deterministic intent overlap: action/object/trigger/parameters."""
    def _field(x: str, y: str) -> int:
        return 1 if x == y else 0

    shared = sum((
        _field(a.action, b.action),
        _field(a.object, b.object),
        _field(a.trigger, b.trigger),
    ))
    params_a = set(a.parameters)
    params_b = set(b.parameters)
    param_jaccard = (
        len(params_a & params_b) / len(params_a | params_b)
        if params_a or params_b else 1.0
    )
    # Fields matter more than the optional parameter bag.
    return 0.75 * (shared / 3.0) + 0.25 * param_jaccard


def compute_layers(a: RuleDefinition, b: RuleDefinition) -> LayerScores:
    """Compose the three layers for a Definition pair."""
    # Layer 1: exact semantic hash (normalized intent + polarity + params).
    hash_score = 1.0 if a.semantic_hash and a.semantic_hash == b.semantic_hash else 0.0

    # Layer 2: structured intent fingerprint.
    intent_a = RuleIntent.from_dict(json.loads(a.normalized_intent))
    intent_b = RuleIntent.from_dict(json.loads(b.normalized_intent))
    intent_score = intent_similarity(intent_a, intent_b)

    # Layer 3: character-bigram semantic similarity over the synonym-collapsed
    # surface projection.  Raw canonical text would treat "运行测试" vs "执行测试"
    # as unrelated; the projection makes near-synonyms score high.
    semantic_score = dice_coefficient(
        semantic_surface(a.canonical_text),
        semantic_surface(b.canonical_text),
    )

    duplicate_score = (
        W_HASH * hash_score
        + W_INTENT * intent_score
        + W_SEMANTIC * semantic_score
    )
    return LayerScores(
        hash_score=hash_score,
        intent_score=intent_score,
        semantic_score=semantic_score,
        duplicate_score=duplicate_score,
    )


def parameters_of(definition: RuleDefinition) -> set[str]:
    try:
        schema = json.loads(definition.parameter_schema or "{}")
    except (TypeError, ValueError):
        schema = {}
    return set(schema.get("parameters", []))


def parameter_conflict(a: RuleDefinition, b: RuleDefinition) -> bool:
    """Two definitions conflict on parameters when both specify different ones.

    pytest vs unittest is a conflict; pytest vs pytest (or empty vs one) is not.
    """
    pa = parameters_of(a)
    pb = parameters_of(b)
    if not pa and not pb:
        return False
    return pa != pb


def contradiction_score(a: RuleDefinition, b: RuleDefinition) -> float:
    """0.0 unless the pair is a polarity contradiction with identical intent.

    "必须运行测试" vs "不要运行测试" contradicts; "使用 pnpm" vs "使用 npm"
    are different rules but not a polarity contradiction (parameters differ).
    """
    if a.polarity != b.polarity:
        intent_a = RuleIntent.from_dict(json.loads(a.normalized_intent))
        intent_b = RuleIntent.from_dict(json.loads(b.normalized_intent))
        same_core = (
            intent_a.action == intent_b.action
            and intent_a.object == intent_b.object
        )
        if same_core and parameters_of(a) == parameters_of(b):
            return 1.0
    return 0.0


@dataclass(frozen=True)
class MergeAssessment:
    """Safety evaluation of one merge candidate."""
    duplicate_score: float
    polarity_ok: bool
    parameters_ok: bool
    evidence_ok: bool
    contradiction_ok: bool
    can_auto_merge: bool
    reasons: tuple[str, ...]


def evaluate_candidate(
    a: RuleDefinition,
    b: RuleDefinition,
    *,
    evidence: list[Any] | tuple[Any, ...] | None = None,
    min_score: float = AUTO_MERGE_SCORE,
    min_evidence: int = AUTO_MERGE_MIN_EVIDENCE,
    min_agents: int = AUTO_MERGE_MIN_AGENTS,
    min_projects: int = AUTO_MERGE_MIN_PROJECTS,
) -> MergeAssessment:
    """Evaluate the five auto-merge conditions (P3 §5)."""
    layers = compute_layers(a, b)
    reasons: list[str] = []

    if layers.duplicate_score < min_score:
        reasons.append("insufficient_similarity")

    polarity_ok = a.polarity == b.polarity
    if not polarity_ok:
        reasons.append("polarity_conflict")

    params_ok = not parameter_conflict(a, b)
    if not params_ok:
        reasons.append("parameter_conflict")

    evidence_list = list(evidence or [])
    evidence_ok = True
    if evidence_list:
        unique_ev = dedupe_evidence(evidence_list)
        if len(unique_ev) < min_evidence:
            reasons.append("insufficient_evidence")
            evidence_ok = False
        if min_agents > 1:
            agents = {ev.agent_instance_id for ev in unique_ev if ev.agent_instance_id}
            if len(agents) < min_agents:
                reasons.append("insufficient_agent_diversity")
                evidence_ok = False
        if min_projects > 1:
            projects = {
                (ev.project_ref or "").strip()
                for ev in unique_ev if (ev.project_ref or "").strip()
            }
            if len(projects) < min_projects:
                reasons.append("insufficient_project_diversity")
                evidence_ok = False
    elif min_evidence > 0:
        # Evidence thresholds are mandatory for automatic merge.
        reasons.append("missing_evidence")
        evidence_ok = False

    contradiction_ok = contradiction_score(a, b) <= AUTO_MERGE_ZERO_CONTRADICTION
    if not contradiction_ok:
        reasons.append("contradiction")

    can_auto_merge = (
        layers.duplicate_score >= min_score
        and polarity_ok
        and params_ok
        and evidence_ok
        and contradiction_ok
    )
    return MergeAssessment(
        duplicate_score=layers.duplicate_score,
        polarity_ok=polarity_ok,
        parameters_ok=params_ok,
        evidence_ok=evidence_ok,
        contradiction_ok=contradiction_ok,
        can_auto_merge=can_auto_merge,
        reasons=tuple(reasons),
    )
