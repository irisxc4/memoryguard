"""P3.3 semantic judge layer tests.

The judge is *audit evidence, never a gate*: it refines the semantic layer and
attaches a ``JudgeVerdict`` to proposals and decisions, but it can never relax
a hard gate (strength / polarity / parameters / negative evidence).  These
tests assert:

  * the offline default (``DiceJudge``) is byte-for-byte the pre-judge policy;
  * an embedding judge returns a stable, bounded score and a recommendation;
  * the LLM judge parses ``MERGE/REVIEW/CONFLICT`` and falls back on failure;
  * ``compute_layers`` with no judge is unchanged (zero regression);
  * the judge verdict is persisted on proposals and merge decisions;
  * a judge cannot turn a refused merge into an accepted one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.rule_merge_policy import compute_layers, evaluate_candidate
from memoryguard.rule_semantic_judge import (
    DiceJudge,
    EmbeddingJudge,
    JudgeVerdict,
    LLMJudge,
    RECOMMEND_CONFLICT,
    RECOMMEND_MERGE,
    RECOMMEND_REVIEW,
    SOURCE_DICE,
    SOURCE_HASH_EMBEDDING,
    SOURCE_LLM,
    default_judge,
    recommend,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aged(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _def(text: str, *, created_at: str = "") -> object:
    return build_definition(text, created_at=created_at or _now_iso())


def _strong_evidence(store: RuleMergeStore, definition_id: str, content: str):
    for i, (agent, project) in enumerate(
        zip(("agent-a", "agent-b", "agent-c"), ("p1", "p2", "p3"))
    ):
        store.upsert_evidence(build_evidence(
            definition_id=definition_id, source_rule_id=f"r-{i}",
            agent_instance_id=agent, project_ref=project,
            session_id=f"s{i}", content=content, observed_at=_aged(60),
        ))
    for agent in ("agent-a", "agent-b", "agent-c"):
        store.upsert_agent_reputation(
            agent_id=agent, success_rate=0.98, rule_accuracy=0.98,
            sample_count=200, feedback_quality=0.95,
        )
    for project in ("p1", "p2", "p3"):
        store.upsert_project_profile(
            project_ref=project, production_level=1.0, owner_verified=True,
        )


# ---------------------------------------------------------------------------
# Verdict construction + recommendation bands
# ---------------------------------------------------------------------------


def test_recommend_bands_match_auto_merge_threshold():
    assert recommend(0.99) == RECOMMEND_MERGE
    assert recommend(0.95) == RECOMMEND_MERGE
    assert recommend(0.85) == RECOMMEND_REVIEW
    assert recommend(0.70) == RECOMMEND_REVIEW
    assert recommend(0.5) == RECOMMEND_CONFLICT


def test_dice_judge_is_deterministic_and_bounded():
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    judge = DiceJudge()
    v1 = judge.judge(a, b)
    v2 = judge.judge(a, b)
    assert v1 == v2  # pure function
    assert 0.0 <= v1.semantic_score <= 1.0
    assert v1.source == SOURCE_DICE
    assert v1.model == "dice"
    assert v1.confidence == 1.0


def test_judge_verdict_roundtrips_through_dict():
    verdict = JudgeVerdict(
        semantic_score=0.97, confidence=0.9, source="hash-embedding",
        model="hash-256", recommendation=RECOMMEND_MERGE,
        rationale="cosine similarity of embeddings",
    )
    assert JudgeVerdict.from_dict(verdict.to_dict()) == verdict


# ---------------------------------------------------------------------------
# Embedding judge (offline HashBackend)
# ---------------------------------------------------------------------------


def test_embedding_judge_stable_and_bounded():
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    judge = EmbeddingJudge()  # HashBackend default, deterministic
    v = judge.judge(a, b)
    assert 0.0 <= v.semantic_score <= 1.0
    assert v.source == SOURCE_HASH_EMBEDDING
    assert v.recommendation in {RECOMMEND_MERGE, RECOMMEND_REVIEW, RECOMMEND_CONFLICT}
    # Deterministic offline.
    assert v == judge.judge(a, b)


def test_embedding_judge_falls_back_on_broken_backend():
    class BrokenBackend:
        def embed_text(self, text: str) -> list[float]:
            raise RuntimeError("model offline")

    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    judge = EmbeddingJudge(BrokenBackend())
    v = judge.judge(a, b)
    assert v.semantic_score == pytest.approx(
        DiceJudge().judge(a, b).semantic_score
    )
    assert v.confidence < 1.0  # degraded, not fabricated
    assert "fallback" in v.rationale


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------


def test_llm_judge_parses_reply_and_scores():
    class FakeChat:
        def __call__(self, system, user, max_tokens=80):
            return "MERGE 两句话意图一致，都是提交前必须测试。"

    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    judge = LLMJudge(chat=FakeChat(), model="fake-llm")
    v = judge.judge(a, b)
    assert v.source == SOURCE_LLM
    assert v.model == "fake-llm"
    assert v.recommendation == RECOMMEND_MERGE
    assert "意图一致" in v.rationale


def test_llm_judge_parses_conflict_and_falls_back_on_failure():
    class ConflictChat:
        def __call__(self, system, user, max_tokens=80):
            return "CONFLICT 禁止使用与必须使用相反。"

    a = _def("必须使用pnpm安装依赖")
    b = _def("禁止使用pnpm安装依赖")
    judge = LLMJudge(chat=ConflictChat(), model="fake-llm")
    assert judge.judge(a, b).recommendation == RECOMMEND_CONFLICT

    class FailChat:
        def __call__(self, system, user, max_tokens=80):
            raise RuntimeError("timeout")

    judge = LLMJudge(chat=FailChat(), model="fake-llm")
    v = judge.judge(a, b)
    assert v.source == SOURCE_LLM
    assert v.recommendation == judge.fallback.judge(a, b).recommendation
    assert v.confidence < 1.0


def test_default_judge_is_dice_without_provider():
    # No provider configured in the test environment -> offline deterministic.
    judge = default_judge()
    assert judge.source == SOURCE_DICE
    assert judge.model == "dice"


# ---------------------------------------------------------------------------
# compute_layers / evaluate_candidate zero regression + judge attachment
# ---------------------------------------------------------------------------


def test_compute_layers_no_judge_is_unchanged():
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    plain = compute_layers(a, b)
    assert plain.judge is None
    # With no judge the semantic layer is exactly the pre-judge Dice.
    from memoryguard.rule_definition import semantic_surface
    from memoryguard.rule_merge_policy import dice_coefficient

    assert plain.semantic_score == dice_coefficient(
        semantic_surface(a.canonical_text),
        semantic_surface(b.canonical_text),
    )


def test_compute_layers_with_judge_attaches_verdict():
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    layers = compute_layers(a, b, judge=DiceJudge())
    assert layers.judge is not None
    assert layers.judge.source == SOURCE_DICE


def test_judge_never_relaxes_a_hard_gate():
    # MUST vs SHOULD on the same intent is a strength conflict.  Even a judge
    # that claims "merge" cannot turn it into a mergeable candidate.
    must = _def("提交代码前必须运行测试")
    should = _def("提交代码前建议运行测试")

    class MergeHappyJudge:
        def judge(self, a, b):
            return JudgeVerdict(
                semantic_score=0.99, confidence=1.0, source="llm",
                model="fake", recommendation=RECOMMEND_MERGE, rationale="",
            )

    assessment = evaluate_candidate(must, should, judge=MergeHappyJudge())
    assert assessment.strength_ok is False
    assert assessment.can_auto_merge is False
    assert assessment.conflict_type == "strength"


# ---------------------------------------------------------------------------
# Audit: judge verdict persisted on proposal and merge decision
# ---------------------------------------------------------------------------


def test_judge_verdict_audited_on_proposal_and_decision(tmp_path):
    store = RuleMergeStore(tmp_path)
    svc = RuleMergeService(store, judge=DiceJudge())
    a = _def("提交代码前必须运行测试", created_at=_aged(90))
    b = _def("提交前必须执行测试", created_at=_aged(90))
    store.upsert_definition(a)
    store.upsert_definition(b)
    _strong_evidence(store, a.definition_id, a.canonical_text)
    _strong_evidence(store, b.definition_id, b.canonical_text)
    for d in (a, b):
        store.upsert_binding(build_binding(
            d.definition_id, share_group_id="g1", target_type="agent",
            target_id="agent-1", owner_agent_id="agent-1", created_by="backfill",
        ))

    proposals = svc.scan_and_propose()
    candidates = [p for p in proposals if p["status"] == "candidate"]
    assert candidates
    proposal = candidates[0]
    assert proposal["judge_source"] == SOURCE_DICE
    assert proposal["judge_recommendation"] == RECOMMEND_MERGE
    assert proposal["judge_score"] > 0.0

    pid = proposal["proposal_id"]
    store.acknowledge_first_merge(pid, actor="human")
    store.clear_proposal_cooldown(pid)
    result = svc.merge_proposal(pid)
    assert result["ok"] is True
    decision = result["decision"]
    assert decision["judge_source"] == SOURCE_DICE
    assert decision["judge_recommendation"] == RECOMMEND_MERGE
    assert decision["judge_score"] > 0.0
    # Stored decision survives a reload from the database.
    reloaded = store.get_merge_decision(decision["decision_id"])
    assert reloaded["judge_recommendation"] == RECOMMEND_MERGE


def test_judge_verdict_audited_even_on_human_approved_merge(tmp_path):
    store = RuleMergeStore(tmp_path)
    svc = RuleMergeService(store, judge=DiceJudge())
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    for d in (a, b):
        store.upsert_binding(build_binding(
            d.definition_id, share_group_id="g1", target_type="agent",
            target_id="agent-1", owner_agent_id="agent-1", created_by="backfill",
        ))
    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.99,
    )
    store.set_proposal_status(proposal["proposal_id"], "approved")
    result = svc.merge_proposal(proposal["proposal_id"], actor="admin")
    assert result["ok"] is True
    assert result["decision"]["judge_source"] == SOURCE_DICE
