"""Rule Evidence: *why* several rules are believed to be the same (P3).

Evidence links a definition to its observed origins: which Agent, which
project, which session, which receipt produced the observation.  Evidence is
append-only and de-duplicated on (session, project, content) so a repeated
observation never inflates the merge confidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from .rule_definition import semantic_hash
from .rule_scope import canonical_project_ref
from .schema_v3 import _now_iso, stable_hash


def _trusted_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


@dataclass(frozen=True)
class RuleEvidence:
    """One observed origin of a definition."""
    evidence_id: str
    definition_id: str = ""
    source_rule_id: str = ""       # legacy memory_id / source record
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    session_id: str = ""
    receipt_id: str = ""
    content_hash: str = ""
    semantic_hash: str = ""
    confidence: float = 1.0
    observed_at: str = ""
    # Independence identity (PR5): two receipts of the same fact must collapse
    # to ONE independent observation even if their evidence_id differs.
    independence_key: str = ""
    share_group_id: str = ""
    source_root_id: str = ""
    source_object_id: str = ""
    session_trusted: bool = False
    feedback_id: str = ""
    feedback_authority: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "definition_id": self.definition_id,
            "source_rule_id": self.source_rule_id,
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "session_id": self.session_id,
            "receipt_id": self.receipt_id,
            "content_hash": self.content_hash,
            "semantic_hash": self.semantic_hash,
            "confidence": self.confidence,
            "observed_at": self.observed_at,
            "independence_key": self.independence_key,
            "share_group_id": self.share_group_id,
            "source_root_id": self.source_root_id,
            "source_object_id": self.source_object_id,
            "session_trusted": self.session_trusted,
            "feedback_id": self.feedback_id,
            "feedback_authority": self.feedback_authority,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleEvidence":
        return cls(
            evidence_id=data["evidence_id"],
            definition_id=data.get("definition_id", ""),
            source_rule_id=data.get("source_rule_id", ""),
            agent_instance_id=data.get("agent_instance_id", ""),
            project_ref=data.get("project_ref", ""),
            provider=data.get("provider", ""),
            session_id=data.get("session_id", ""),
            receipt_id=data.get("receipt_id", ""),
            content_hash=data.get("content_hash", ""),
            semantic_hash=data.get("semantic_hash", ""),
            confidence=float(data.get("confidence", 1.0)),
            observed_at=data.get("observed_at", ""),
            independence_key=data.get("independence_key", ""),
            share_group_id=data.get("share_group_id", ""),
            source_root_id=data.get("source_root_id", ""),
            source_object_id=data.get("source_object_id", ""),
            session_trusted=_trusted_flag(data.get("session_trusted", False)),
            feedback_id=data.get("feedback_id", ""),
            feedback_authority=int(data.get("feedback_authority", 0) or 0),
        )


def evidence_independence_key(evidence: RuleEvidence) -> str:
    """The identity used to collapse duplicate observations (PR5).

    The same fact reported through different receipts collapses to one
    independent observation only when it really is one: same share group, same
    Agent, same canonical project, same source root/object, same session, same
    content.  Two distinct sessions (or distinct source objects) count as two
    independent observations even if the prose matches.
    """
    return stable_hash(
        "rule-evidence-independence",
        evidence.share_group_id or "",
        evidence.agent_instance_id or "",
        canonical_project_ref(evidence.project_ref),
        evidence.source_root_id or "",
        evidence.source_object_id or evidence.session_id or "",
        evidence.content_hash or "",
    )


def evidence_dedup_key(evidence: RuleEvidence) -> str:
    """The identity used to collapse duplicate observations.

    Prefers the PR5 independence key; falls back to the legacy
    (session + project + content) projection for rows that predate it.
    """
    if evidence.independence_key:
        return evidence.independence_key
    return evidence_independence_key(evidence)


def dedupe_evidence(
    evidences: list[RuleEvidence] | tuple[RuleEvidence, ...],
) -> list[RuleEvidence]:
    """Collapse duplicate observations with one deterministic winner.

    The database uniqueness constraint is the primary guard.  This helper is
    the in-memory snapshot equivalent used by proposal/readiness code, so it
    must choose the same winner rather than depending on query order.
    """
    best: dict[str, RuleEvidence] = {}
    for evidence in evidences:
        key = evidence_dedup_key(evidence)
        current = best.get(key)
        if current is None:
            best[key] = evidence
            continue
        candidate_rank = (
            int(getattr(evidence, "feedback_authority", 0) or 0),
            float(
                getattr(evidence, "confidence", 1.0)
                if getattr(evidence, "confidence", None) is not None else 0.0
            ),
            str(getattr(evidence, "observed_at", "") or ""),
            str(getattr(evidence, "evidence_id", "") or ""),
        )
        current_rank = (
            int(getattr(current, "feedback_authority", 0) or 0),
            float(
                getattr(current, "confidence", 1.0)
                if getattr(current, "confidence", None) is not None else 0.0
            ),
            str(getattr(current, "observed_at", "") or ""),
            str(getattr(current, "evidence_id", "") or ""),
        )
        if candidate_rank > current_rank:
            best[key] = evidence
    return [best[key] for key in sorted(best)]


def build_evidence(
    *,
    source_rule_id: str = "",
    agent_instance_id: str = "",
    project_ref: str = "",
    provider: str = "",
    session_id: str = "",
    receipt_id: str = "",
    content: str = "",
    confidence: float = 1.0,
    definition_id: str = "",
    observed_at: str = "",
    share_group_id: str = "",
    source_root_id: str = "",
    source_object_id: str = "",
    session_trusted: bool = False,
    feedback_id: str = "",
    feedback_authority: int = 0,
) -> RuleEvidence:
    content_hash = stable_hash("rule-content", str(content or "").strip())
    canonical_project = canonical_project_ref(project_ref)
    evidence = RuleEvidence(
        evidence_id=stable_hash(
            "rule-evidence", source_rule_id, agent_instance_id,
            canonical_project, session_id, receipt_id,
            content_hash,
        ),
        definition_id=definition_id,
        source_rule_id=source_rule_id,
        agent_instance_id=agent_instance_id,
        project_ref=canonical_project,
        provider=provider,
        session_id=session_id,
        receipt_id=receipt_id,
        content_hash=content_hash,
        semantic_hash=semantic_hash(content),
        confidence=confidence,
        observed_at=observed_at or _now_iso(),
        share_group_id=share_group_id,
        source_root_id=source_root_id,
        source_object_id=source_object_id,
        session_trusted=_trusted_flag(session_trusted),
        feedback_id=feedback_id,
        feedback_authority=int(feedback_authority or 0),
    )
    # Freeze the independence key so re-computation stays stable even if
    # optional context is empty at build time.
    return replace(
        evidence,
        independence_key=evidence_independence_key(evidence),
    )


@dataclass(frozen=True)
class NegativeEvidence:
    """Empirical contradiction: a project/agent where the rule did not hold.

    This is the P3-001 counterweight to positive evidence.  One observation
    never rejects a merge; a weighted fraction above the threshold does.
    Two counter-examples prove independence only when they come from distinct
    trusted sessions / source objects, so negative evidence carries the same
    independence identity as positive evidence.
    """

    evidence_id: str
    definition_id: str = ""
    source_rule_id: str = ""
    agent_instance_id: str = ""
    project_ref: str = ""
    content_hash: str = ""
    confidence: float = 1.0
    observed_at: str = ""
    independence_key: str = ""
    share_group_id: str = ""
    session_id: str = ""
    receipt_id: str = ""
    feedback_id: str = ""
    feedback_authority: int = 0
    source_root_id: str = ""
    source_object_id: str = ""
    session_trusted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "definition_id": self.definition_id,
            "source_rule_id": self.source_rule_id,
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "content_hash": self.content_hash,
            "confidence": self.confidence,
            "observed_at": self.observed_at,
            "independence_key": self.independence_key,
            "share_group_id": self.share_group_id,
            "session_id": self.session_id,
            "receipt_id": self.receipt_id,
            "feedback_id": self.feedback_id,
            "feedback_authority": self.feedback_authority,
            "source_root_id": self.source_root_id,
            "source_object_id": self.source_object_id,
            "session_trusted": self.session_trusted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NegativeEvidence":
        return cls(
            evidence_id=data["evidence_id"],
            definition_id=data.get("definition_id", ""),
            source_rule_id=data.get("source_rule_id", ""),
            agent_instance_id=data.get("agent_instance_id", ""),
            project_ref=data.get("project_ref", ""),
            content_hash=data.get("content_hash", ""),
            confidence=float(data.get("confidence", 1.0)),
            observed_at=data.get("observed_at", ""),
            independence_key=data.get("independence_key", ""),
            share_group_id=data.get("share_group_id", ""),
            session_id=data.get("session_id", ""),
            receipt_id=data.get("receipt_id", ""),
            feedback_id=data.get("feedback_id", ""),
            feedback_authority=int(data.get("feedback_authority", 0) or 0),
            source_root_id=data.get("source_root_id", ""),
            source_object_id=data.get("source_object_id", ""),
            session_trusted=_trusted_flag(data.get("session_trusted", False)),
        )


def negative_evidence_independence_key(evidence: NegativeEvidence) -> str:
    """Independence identity for a counter-example (PR5)."""
    return stable_hash(
        "rule-negative-evidence-independence",
        evidence.share_group_id or "",
        evidence.agent_instance_id or "",
        canonical_project_ref(evidence.project_ref),
        evidence.source_root_id or "",
        evidence.source_object_id or evidence.session_id or "",
        evidence.content_hash or "",
    )


def build_negative_evidence(
    *,
    source_rule_id: str = "",
    agent_instance_id: str = "",
    project_ref: str = "",
    content: str = "",
    confidence: float = 1.0,
    definition_id: str = "",
    observed_at: str = "",
    share_group_id: str = "",
    session_id: str = "",
    receipt_id: str = "",
    feedback_id: str = "",
    feedback_authority: int = 0,
    source_root_id: str = "",
    source_object_id: str = "",
    session_trusted: bool = False,
) -> NegativeEvidence:
    content_hash = stable_hash("rule-content", str(content or "").strip())
    canonical_project = canonical_project_ref(project_ref)
    evidence = NegativeEvidence(
        evidence_id=stable_hash(
            "rule-negative-evidence", source_rule_id, agent_instance_id,
            canonical_project, session_id, receipt_id, content_hash,
        ),
        definition_id=definition_id,
        source_rule_id=source_rule_id,
        agent_instance_id=agent_instance_id,
        project_ref=canonical_project,
        content_hash=content_hash,
        confidence=confidence,
        observed_at=observed_at or _now_iso(),
        share_group_id=share_group_id,
        session_id=session_id,
        receipt_id=receipt_id,
        feedback_id=feedback_id,
        feedback_authority=int(feedback_authority or 0),
        source_root_id=source_root_id,
        source_object_id=source_object_id,
        session_trusted=_trusted_flag(session_trusted),
    )
    return replace(
        evidence,
        independence_key=negative_evidence_independence_key(evidence),
    )
