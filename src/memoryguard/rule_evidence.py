"""Rule Evidence: *why* several rules are believed to be the same (P3).

Evidence links a definition to its observed origins: which Agent, which
project, which session, which receipt produced the observation.  Evidence is
append-only and de-duplicated on (session, project, content) so a repeated
observation never inflates the merge confidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .rule_definition import semantic_hash
from .rule_scope import canonical_project_ref
from .schema_v3 import _now_iso, stable_hash


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
        )


def evidence_dedup_key(evidence: RuleEvidence) -> str:
    """The identity used to collapse duplicate observations.

    Same session + same project + same content hash counts as one observation,
    no matter how many times a hook reported it.
    """
    return stable_hash(
        "rule-evidence", evidence.session_id or "",
        canonical_project_ref(evidence.project_ref),
        evidence.content_hash or "",
    )


def dedupe_evidence(
    evidences: list[RuleEvidence] | tuple[RuleEvidence, ...],
) -> list[RuleEvidence]:
    """Collapse duplicate observations (session + project + content)."""
    seen: set[str] = set()
    kept: list[RuleEvidence] = []
    for evidence in evidences:
        key = evidence_dedup_key(evidence)
        if key in seen:
            continue
        seen.add(key)
        kept.append(evidence)
    return kept


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
) -> RuleEvidence:
    content_hash = stable_hash("rule-content", str(content or "").strip())
    return RuleEvidence(
        evidence_id=stable_hash(
            "rule-evidence", source_rule_id, agent_instance_id,
            canonical_project_ref(project_ref), session_id, receipt_id,
            content_hash,
        ),
        definition_id=definition_id,
        source_rule_id=source_rule_id,
        agent_instance_id=agent_instance_id,
        project_ref=canonical_project_ref(project_ref),
        provider=provider,
        session_id=session_id,
        receipt_id=receipt_id,
        content_hash=content_hash,
        semantic_hash=semantic_hash(content),
        confidence=confidence,
        observed_at=observed_at or _now_iso(),
    )


@dataclass(frozen=True)
class NegativeEvidence:
    """Empirical contradiction: a project/agent where the rule did not hold.

    This is the P3-001 counterweight to positive evidence.  One observation
    never rejects a merge; a weighted fraction above the threshold does.
    The identity is the same (session+project+content) dedup discipline as
    positive evidence so one repeated report never inflates the score.
    """

    evidence_id: str
    definition_id: str = ""
    source_rule_id: str = ""
    agent_instance_id: str = ""
    project_ref: str = ""
    content_hash: str = ""
    confidence: float = 1.0
    observed_at: str = ""

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
) -> NegativeEvidence:
    content_hash = stable_hash("rule-content", str(content or "").strip())
    return NegativeEvidence(
        evidence_id=stable_hash(
            "rule-negative-evidence", source_rule_id, agent_instance_id,
            canonical_project_ref(project_ref), content_hash,
        ),
        definition_id=definition_id,
        source_rule_id=source_rule_id,
        agent_instance_id=agent_instance_id,
        project_ref=canonical_project_ref(project_ref),
        content_hash=content_hash,
        confidence=confidence,
        observed_at=observed_at or _now_iso(),
    )
