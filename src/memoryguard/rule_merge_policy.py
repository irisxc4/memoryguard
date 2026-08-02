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
import math
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Any

from .rule_definition import (
    POLARITY_POSITIVE,
    STRENGTH_UNKNOWN,
    RuleDefinition,
    RuleIntent,
    semantic_surface,
)
from .rule_evidence import dedupe_evidence
from .schema_v3 import _now_iso

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

# P3-001 / P3-002 / P3-003 governance constants.
# A merge is only ever *executed* when every hard gate passes:
#   strength equality (P3-002), negative evidence (P3-001), and the
#   five P3 §5 conditions.  The readiness score is the soft gate on top.
NEGATIVE_EVIDENCE_THRESHOLD = 0.05
WEIGHTED_EVIDENCE_MIN = 2.5
MAX_SINGLE_SOURCE_RATIO = 0.6
AUTO_READINESS_SCORE = 0.80
COOLDOWN_HOURS = 72

# Maturity stages (P3-001 §1).  observing < candidate < validated < trusted.
OBSERVING_DAYS = 7
TRUSTED_DAYS = 30
VALIDATED_SUCCESS_RATE = 0.95
TRUSTED_MIN_SUCCESS_SAMPLES = 20

# Proposal identity policy.  The proposal id is stable across scans (no
# timestamp) so cooldown and first-merge acknowledgment accumulate on one row;
# a policy change that must invalidate old proposals bumps this version.
MERGE_POLICY_VERSION = "rule-merge-policy-v3"

# PR7 bounded scan: only pairs whose similarity is high enough to be a real
# duplicate are persisted as ``rejected`` proposals for governance visibility;
# everything below this floor is counted in the scan summary instead of written
# to the proposal table (which used to grow without bound).
REJECTED_PERSIST_FLOOR = 0.7

# Merge Readiness Score (P3-001 §2).
W_READINESS_SEMANTIC = 0.25
W_READINESS_EVIDENCE = 0.20
W_READINESS_MATURITY = 0.20
W_READINESS_EXECUTION = 0.15
W_READINESS_DIVERSITY = 0.10
W_READINESS_STABILITY = 0.10

# Evidence weight (P3-003 §2, PR5 Bayesian).
# Unknown sources default to 0.5 (a neutral prior), never to full credit: a
# new Agent with no track record must not outweigh a verified production one.
W_AGENT_RELIABILITY = 0.30
W_PROJECT_IMPORTANCE = 0.20
W_RULE_SPECIFIC_SUCCESS = 0.20
W_FEEDBACK_AUTHORITY = 0.10
W_RECENCY = 0.10
W_EVIDENCE_CONFIDENCE = 0.10
# Bayesian prior: an unknown source starts at 0.5 and moves with feedback.
BAYESIAN_PRIOR = 4.0
BAYESIAN_PRIOR_ACCURACY = 0.5
# Minimum sample count before an agent reputation is treated as established.
MIN_REPUTATION_SAMPLES = 20
# Feedback producer -> authority score (user explicit > agent > hook > legacy).
_FEEDBACK_AUTHORITY_SCORES = {
    "user": 1.0,
    "agent": 0.75,
    "hook": 0.5,
    "legacy": 0.3,
    "unobserved": 0.3,
}

# Maturity score for readiness computation.
_MATURITY_SCORES = {
    "observing": 0.2,
    "candidate": 0.5,
    "validated": 0.8,
    "trusted": 1.0,
}


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


def days_between(created_at: str, now: str = "") -> float:
    """Fractional days between two ISO timestamps; 0.0 when unparseable."""
    if not created_at:
        return 0.0
    now = now or _now_iso()
    try:
        start = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (end - start).total_seconds() / 86400.0)


def maturity_score(state: str) -> float:
    """Readiness contribution of one maturity stage (P3-001 §1)."""
    return _MATURITY_SCORES.get(str(state or "").casefold(), 0.2)


def recency_factor(days_old: float) -> float:
    """Recency half-life for evidence weight (P3-003 §2.5)."""
    return math.exp(-max(0.0, days_old) / 90.0)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def bayesian_accuracy(
    followed: int, violated: int = 0, *,
    prior: float = BAYESIAN_PRIOR,
    prior_accuracy: float = BAYESIAN_PRIOR_ACCURACY,
) -> float:
    """Bayesian posterior accuracy with shrinkage toward a neutral prior.

    An unknown source with zero feedback is ``prior_accuracy`` (0.5); each
    followed/violated observation moves the estimate.  Small samples stay near
    the prior instead of claiming full credit or full blame.
    """
    followed = max(0, int(followed))
    violated = max(0, int(violated))
    if followed + violated == 0:
        return float(prior_accuracy)
    return (
        prior * prior_accuracy + followed
    ) / (prior + followed + violated)


def feedback_authority_score(
    source: str = "", authority: int = 0,
) -> float:
    """Map a feedback producer to an authority score in [0, 1].

    An unknown producer (no authority, no recognised source) is neutral 0.5,
    never full credit.
    """
    if authority:
        return _clamp(authority / 4.0)
    key = str(source or "").casefold()
    if key in _FEEDBACK_AUTHORITY_SCORES:
        return _FEEDBACK_AUTHORITY_SCORES[key]
    return 0.5


def project_importance_score(
    production_level: float = 0.0, criticality: float = 0.0,
    owner_verified: bool = False,
) -> float:
    """Project importance from production level, criticality and ownership.

    Unknown project -> 0.5 (neutral, never full credit); verified ownership
    adds a small bonus.
    """
    if not production_level and not criticality:
        return 0.5
    return _clamp(
        float(production_level or 0.0)
        * (0.5 + 0.5 * float(criticality or 0.0))
        + (0.1 if owner_verified else 0.0)
    )


def evidence_weight(
    *,
    agent_reliability: float = 0.5,
    project_importance: float = 0.5,
    rule_specific_success: float = 0.5,
    feedback_authority: float = 0.5,
    recency: float = 1.0,
    evidence_confidence: float = 0.5,
) -> float:
    """Evidence weight (P3-003 §2, PR5).

    The default for every *unknown* component is 0.5, never 1.0: an unvetted
    source must not outrank a verified production source.  The component mix is
    0.30 agent reliability + 0.20 project importance + 0.20 rule-specific
    success + 0.10 feedback authority + 0.10 recency + 0.10 evidence
    confidence.
    """
    return (
        W_AGENT_RELIABILITY * _clamp(agent_reliability)
        + W_PROJECT_IMPORTANCE * _clamp(project_importance)
        + W_RULE_SPECIFIC_SUCCESS * _clamp(rule_specific_success)
        + W_FEEDBACK_AUTHORITY * _clamp(feedback_authority)
        + W_RECENCY * _clamp(recency)
        + W_EVIDENCE_CONFIDENCE * _clamp(evidence_confidence)
    )


def weighted_evidence_score(weights: list[float] | tuple[float, ...]) -> float:
    """Aggregate evidence as weighted votes (P3-003 §3)."""
    return sum(float(w) for w in weights)


def largest_source_ratio(per_source_weights: dict[str, float]) -> float:
    """Fraction of total evidence weight held by the largest source (P3-003 §5).

    A value >= ``MAX_SINGLE_SOURCE_RATIO`` means one Agent monopolises the
    rule's evidence; such a rule must not auto-merge on its own say-so.
    """
    total = sum(float(v) for v in per_source_weights.values())
    if total <= 0.0:
        return 0.0
    return max(float(v) for v in per_source_weights.values()) / total


def negative_evidence_score(negative_weight: float, positive_weight: float) -> float:
    """Weighted fraction of contradicting observations (P3-001 §5).

    0.0 with no negative evidence; approaches 1.0 when most observations
    contradict the rule.  A merge requires ``score < NEGATIVE_EVIDENCE_THRESHOLD``.
    """
    total = negative_weight + positive_weight
    if total <= 0.0:
        return 0.0
    return max(0.0, min(1.0, negative_weight / total))


def merge_readiness_score(
    *,
    duplicate_score: float,
    evidence_confidence: float,
    maturity: float,
    execution_success: float,
    source_diversity: float,
    stability: float,
) -> float:
    """Merge Readiness Score (P3-001 §2).

    0.25 semantic + 0.20 evidence + 0.20 maturity + 0.15 execution
    + 0.10 diversity + 0.10 stability.  A freshly-created rule with strong
    evidence but no age lands around 0.6x; a 90-day, heavily-followed rule
    lands above the auto threshold.
    """
    return (
        W_READINESS_SEMANTIC * max(0.0, min(1.0, duplicate_score))
        + W_READINESS_EVIDENCE * max(0.0, min(1.0, evidence_confidence))
        + W_READINESS_MATURITY * max(0.0, min(1.0, maturity))
        + W_READINESS_EXECUTION * max(0.0, min(1.0, execution_success))
        + W_READINESS_DIVERSITY * max(0.0, min(1.0, source_diversity))
        + W_READINESS_STABILITY * max(0.0, min(1.0, stability))
    )


@dataclass(frozen=True)
class LayerScores:
    """The three independent similarity layers and their composition."""
    hash_score: float
    intent_score: float
    semantic_score: float
    duplicate_score: float
    judge: Any | None = None  # JudgeVerdict (audit evidence, never a gate)


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


def compute_layers(a: RuleDefinition, b: RuleDefinition, *, judge: Any | None = None) -> LayerScores:
    """Compose the three layers for a Definition pair.

    ``judge`` is an optional semantic judge (P3.3).  When None the semantic
    layer is the deterministic character-bigram Dice over the synonym-collapsed
    surface — exactly the pre-judge behaviour.  When provided, the judge's
    verdict is attached to the result for audit; the judge may *refine* the
    semantic score but never relaxes the hard gates that consume it.
    """
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

    # P3.3: a pluggable embedding/LLM judge may refine the semantic layer and
    # always attaches its verdict as audit evidence.
    judge_verdict = None
    if judge is not None:
        try:
            judge_verdict = judge.judge(a, b)
            refined = float(getattr(judge_verdict, "semantic_score", semantic_score))
            if 0.0 <= refined <= 1.0:
                semantic_score = refined
        except Exception:
            judge_verdict = None

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
        judge=judge_verdict,
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
    strength_ok: bool
    negative_ok: bool
    can_auto_merge: bool
    reasons: tuple[str, ...]
    judge: Any | None = None  # JudgeVerdict, audit-only (never gates)

    @property
    def conflict_type(self) -> str:
        """Hard-governance conflict category, if the pair cannot merge."""
        if not self.strength_ok:
            return "strength"
        if not self.polarity_ok:
            return "polarity"
        if not self.parameters_ok:
            return "parameter"
        if not self.negative_ok:
            return "negative_evidence"
        return ""


def evaluate_candidate(
    a: RuleDefinition,
    b: RuleDefinition,
    *,
    evidence: list[Any] | tuple[Any, ...] | None = None,
    min_score: float = AUTO_MERGE_SCORE,
    min_evidence: int = AUTO_MERGE_MIN_EVIDENCE,
    min_agents: int = AUTO_MERGE_MIN_AGENTS,
    min_projects: int = AUTO_MERGE_MIN_PROJECTS,
    negative_score: float = 0.0,
    negative_threshold: float = NEGATIVE_EVIDENCE_THRESHOLD,
    judge: Any | None = None,
) -> MergeAssessment:
    """Evaluate the merge safety conditions (P3 §5 + P3-002 + P3-001 §5).

    ``judge`` is a P3.3 semantic judge: its verdict is attached to the
    assessment as audit evidence only and never relaxes the hard gates.
    """
    layers = compute_layers(a, b, judge=judge)
    reasons: list[str] = []

    if layers.duplicate_score < min_score:
        reasons.append("insufficient_similarity")

    polarity_ok = a.polarity == b.polarity
    if not polarity_ok:
        reasons.append("polarity_conflict")

    params_ok = not parameter_conflict(a, b)
    if not params_ok:
        reasons.append("parameter_conflict")

    # P3-002: strength is a hard gate.  MUST vs SHOULD on the same intent is a
    # governance conflict, never a duplicate to collapse.  An ``unknown``
    # strength (pre-migration orphan with an unrecoverable original body) can
    # never be asserted safe, so it also blocks the merge.
    strength_ok = (
        str(a.rule_strength or "") == str(b.rule_strength or "")
        and str(a.rule_strength or "") != STRENGTH_UNKNOWN
    )
    if not strength_ok:
        reasons.append("strength_conflict")

    # P3-001 §5: a weighted negative-evidence fraction above the threshold
    # means real projects contradict this rule; do not merge it up.
    negative_ok = float(negative_score or 0.0) < float(negative_threshold or 0.0)
    if not negative_ok:
        reasons.append("negative_evidence")

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
        and strength_ok
        and negative_ok
    )
    return MergeAssessment(
        duplicate_score=layers.duplicate_score,
        polarity_ok=polarity_ok,
        parameters_ok=params_ok,
        evidence_ok=evidence_ok,
        contradiction_ok=contradiction_ok,
        strength_ok=strength_ok,
        negative_ok=negative_ok,
        can_auto_merge=can_auto_merge,
        reasons=tuple(reasons),
        judge=layers.judge,
    )
