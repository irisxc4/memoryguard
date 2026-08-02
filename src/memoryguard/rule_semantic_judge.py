"""Semantic judge layer: pluggable embedding/LLM evidence for merge policy (P3.3).

The deterministic three-layer matcher in ``rule_merge_policy`` treats a rule
pair's *semantic* similarity as a character-bigram Dice coefficient over the
synonym-collapsed surface projection.  That is a pure, offline approximation.

This module makes the semantic layer pluggable without changing the default:

  * ``DiceJudge``        -- the current behaviour, byte-for-byte; the default.
  * ``EmbeddingJudge``   -- an embedding backend (HashBackend offline, the
    configured provider's model when one is set) with cosine similarity.
  * ``LLMJudge``         -- asks the configured provider's chat model to rate
    the pair ``merge / review / conflict`` and to explain itself.
  * ``default_judge()``  -- provider configured -> EmbeddingJudge(provider);
    otherwise DiceJudge.  Deterministic offline, live online.

A judge produces a ``JudgeVerdict``: a semantic score plus a merge
recommendation and rationale.  That verdict is *audit evidence*: it is stored
on the proposal and the merge decision so governance can show *why* a pair was
or was not merged.  It never loosens a hard gate -- ``evaluate_candidate``'s
strength / polarity / parameter / negative-evidence checks are untouched and
cannot be overridden by a judge recommendation.

Everything here is a pure function; a judge needs no database.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .rule_definition import RuleDefinition, semantic_surface
from .rule_merge_policy import AUTO_MERGE_SCORE, dice_coefficient

# Recommendation bands (aligned with the auto-merge threshold so the judge's
# default reading of a score matches the policy's).
JUDGE_MERGE_THRESHOLD = AUTO_MERGE_SCORE          # 0.95
JUDGE_REVIEW_THRESHOLD = 0.70                     # below -> conflict

# Verdict sources.  "dice" is the offline default; the embedding/llm sources
# only appear when a backend is configured or injected.
SOURCE_DICE = "dice"
SOURCE_HASH_EMBEDDING = "hash-embedding"
SOURCE_PROVIDER_EMBEDDING = "provider-embedding"
SOURCE_LLM = "llm"

# Recommendation values.
RECOMMEND_MERGE = "merge"
RECOMMEND_REVIEW = "review"
RECOMMEND_CONFLICT = "conflict"


@dataclass(frozen=True)
class JudgeVerdict:
    """One judge's opinion on a Definition pair (audit evidence, never a gate)."""
    semantic_score: float       # 0..1
    confidence: float           # 0..1 (how much the judge trusts its own score)
    source: str                 # dice | hash-embedding | provider-embedding | llm
    model: str                  # e.g. "dice" | "hash-256" | provider model name
    recommendation: str         # merge | review | conflict
    rationale: str              # human-readable why

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_score": round(float(self.semantic_score), 4),
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
            "model": self.model,
            "recommendation": self.recommendation,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JudgeVerdict":
        return cls(
            semantic_score=float(data.get("semantic_score", 0.0)),
            confidence=float(data.get("confidence", 0.0)),
            source=str(data.get("source", SOURCE_DICE)),
            model=str(data.get("model", "") or ""),
            recommendation=str(data.get("recommendation", RECOMMEND_REVIEW)),
            rationale=str(data.get("rationale", "") or ""),
        )


def recommend(semantic_score: float) -> str:
    """Map a semantic score to a merge recommendation (same bands as policy)."""
    if semantic_score >= JUDGE_MERGE_THRESHOLD:
        return RECOMMEND_MERGE
    if semantic_score >= JUDGE_REVIEW_THRESHOLD:
        return RECOMMEND_REVIEW
    return RECOMMEND_CONFLICT


class SemanticJudge(Protocol):
    """A judge rates one Definition pair into a verdict."""

    def judge(self, a: RuleDefinition, b: RuleDefinition) -> JudgeVerdict:
        ...


class DiceJudge:
    """The offline default: Dice over the synonym-collapsed surface.

    Byte-for-byte the semantic layer ``compute_layers`` used before the judge
    existed, so a policy configured with no judge behaves exactly as before.
    """

    source = SOURCE_DICE
    model = "dice"

    def judge(self, a: RuleDefinition, b: RuleDefinition) -> JudgeVerdict:
        score = dice_coefficient(
            semantic_surface(a.canonical_text),
            semantic_surface(b.canonical_text),
        )
        return JudgeVerdict(
            semantic_score=score,
            confidence=1.0,
            source=self.source,
            model=self.model,
            recommendation=recommend(score),
            rationale="deterministic Dice over synonym-collapsed surface",
        )


class EmbeddingJudge:
    """Semantic score from an embedding backend + cosine similarity.

    ``backend`` must implement ``embed_text(text) -> list[float]`` (the same
    duck-typed protocol ``semantic_dedup`` uses).  Offline that is
    ``HashBackend``; when a provider is configured it is the provider's
    embedding model.  Any backend failure falls back to ``DiceJudge`` so a
    live-model outage never breaks a scan.
    """

    def __init__(
        self,
        backend: Any | None = None,
        *,
        source: str = SOURCE_HASH_EMBEDDING,
        model: str = "hash-256",
        fallback: SemanticJudge | None = None,
    ):
        from .semantic_dedup import cosine_similarity

        self._cosine = cosine_similarity
        if backend is None:
            from .semantic_dedup import HashBackend

            backend = HashBackend(dim=256, ngram=2)
        self.backend = backend
        self.source = source
        self.model = model
        self.fallback = fallback or DiceJudge()

    def _score(self, a: RuleDefinition, b: RuleDefinition) -> float:
        try:
            vec_a = self.backend.embed_text(semantic_surface(a.canonical_text))
            vec_b = self.backend.embed_text(semantic_surface(b.canonical_text))
            score = self._cosine(vec_a, vec_b)
            if 0.0 <= score <= 1.0:
                return score
        except Exception:
            pass
        return -1.0  # signals fallback

    def judge(self, a: RuleDefinition, b: RuleDefinition) -> JudgeVerdict:
        score = self._score(a, b)
        if score < 0.0:
            verdict = self.fallback.judge(a, b)
            return JudgeVerdict(
                semantic_score=verdict.semantic_score,
                confidence=min(verdict.confidence, 0.5),
                source=SOURCE_HASH_EMBEDDING if self.source != SOURCE_PROVIDER_EMBEDDING else self.source,
                model=self.model,
                recommendation=verdict.recommendation,
                rationale=f"{self.model} unavailable; fallback {verdict.source}",
            )
        return JudgeVerdict(
            semantic_score=score,
            confidence=0.9,
            source=self.source,
            model=self.model,
            recommendation=recommend(score),
            rationale=f"cosine similarity of {self.model} embeddings",
        )


class LLMJudge:
    """Ask the configured provider's chat model to rate the pair.

    The model must answer ``MERGE | REVIEW | CONFLICT`` on its own line; the
    rest of the reply becomes the rationale.  Parsing is tolerant, and any
    failure (no provider, timeout, malformed reply) falls back to ``DiceJudge``
    so a scan never breaks because a live model is slow or absent.
    """

    def __init__(
        self,
        chat: Any | None = None,
        *,
        model: str = "llm",
        provider_workspace: str | None = None,
        fallback: SemanticJudge | None = None,
    ):
        self._chat = chat
        self.model = model
        self.provider_workspace = provider_workspace
        self.fallback = fallback or DiceJudge()

    def _ask(self, a: RuleDefinition, b: RuleDefinition) -> str | None:
        if self._chat is None:
            from .provider_api import get_provider

            provider = get_provider(self.provider_workspace)
            if provider is None:
                return None
            self._chat = provider.chat
        prompt = (
            "你是规则治理系统。判断两条规则是否应合并为一条(同一意图),\n"
            "只需输出一行: MERGE / REVIEW / CONFLICT,再简单说明理由。\n"
            f"规则A: {a.canonical_text}\n"
            f"规则B: {b.canonical_text}\n"
            "注意: 仅当意图完全一致才 MERGE; 语义相近但有争议给 REVIEW;\n"
            "明显冲突(禁止vs必须、不同对象)给 CONFLICT。"
        )
        try:
            return str(self._chat(
                system="你只输出 MERGE/REVIEW/CONFLICT 及一句话理由。",
                user=prompt,
                max_tokens=80,
            ) or "").strip()
        except Exception:
            return None

    def judge(self, a: RuleDefinition, b: RuleDefinition) -> JudgeVerdict:
        reply = self._ask(a, b)
        if not reply:
            verdict = self.fallback.judge(a, b)
            return JudgeVerdict(
                semantic_score=verdict.semantic_score,
                confidence=min(verdict.confidence, 0.5),
                source=SOURCE_LLM,
                model=self.model,
                recommendation=verdict.recommendation,
                rationale=f"{self.model} unavailable; fallback {verdict.source}",
            )
        match = re.search(r"\b(MERGE|REVIEW|CONFLICT)\b", reply.upper())
        recommendation = (
            (match.group(1).lower() if match else RECOMMEND_REVIEW)
        )
        rationale = re.sub(r"\s*MERGE|REVIEW|CONFLICT\b\s*", "", reply, flags=re.I)
        rationale = " ".join(rationale.split()).strip() or "llm judgment"
        score = {
            RECOMMEND_MERGE: 0.99,
            RECOMMEND_REVIEW: 0.85,
            RECOMMEND_CONFLICT: 0.40,
        }[recommendation]
        return JudgeVerdict(
            semantic_score=score,
            confidence=0.85,
            source=SOURCE_LLM,
            model=self.model,
            recommendation=recommendation,
            rationale=rationale,
        )


def default_judge(workspace: str | None = None) -> SemanticJudge:
    """Offline-deterministic by default; live embedding when a provider is set.

    ``DiceJudge`` is returned when no provider is configured, so a scan with no
    external service behaves exactly like the pre-judge policy.  When the
    provider is configured, ``EmbeddingJudge`` uses the provider's embedding
    model (via ``ProviderEmbeddingBackend``) and falls back to Dice on failure.
    """
    from .provider_api import get_provider

    provider = get_provider(workspace)
    if provider is not None:
        from .semantic_dedup import ProviderEmbeddingBackend

        try:
            backend = ProviderEmbeddingBackend()
            return EmbeddingJudge(
                backend,
                source=SOURCE_PROVIDER_EMBEDDING,
                model="provider-embed",
            )
        except Exception:
            pass
    return DiceJudge()


def verdict_to_json(verdict: JudgeVerdict | None) -> str:
    return json.dumps(verdict.to_dict(), ensure_ascii=False) if verdict else ""
