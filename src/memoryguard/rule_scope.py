"""Rule-audience validation and deterministic matching.

This deliberately has no database dependency so the same matcher is used by
MCP, hooks, bootstrap and future GUI governance.
"""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from typing import Iterable

from .schema_v3 import EffectiveAgentContext, RuleAssignment


TARGET_TYPES = {
    "agent", "group", "project", "agent_project", "provider",
    "runtime_role", "system",
}
EFFECTS = {"include", "exclude"}


def validate_automatic_assignment(
    value: dict | RuleAssignment,
    *,
    actor_agent_id: str = "",
) -> RuleAssignment:
    """Validate an assignment emitted by automation.

    Manual governance may still use ``system`` and broad dimensions.  An
    automatic lifecycle update is deliberately narrower: it may only retain
    the current Agent (optionally paired with its project), never manufacture
    a system/group/provider/runtime-role/project broadcast or target another
    Agent.  This is a mutation boundary, not a matcher restriction.
    """
    assignment = normalize_assignment(value)
    if assignment.target_type not in {"agent", "agent_project"}:
        raise ValueError(
            "automatic assignment cannot broaden target_type; "
            "allowed target types are agent and agent_project"
        )
    if actor_agent_id and assignment.target_id != actor_agent_id:
        raise ValueError("automatic assignment cannot target another agent")
    if not actor_agent_id:
        # Without a trusted runtime identity an automatic write cannot prove
        # ownership of an Agent audience, so fail closed.
        raise ValueError("automatic assignment requires actor_agent_id")
    return assignment


def canonical_project_ref(value: str | os.PathLike[str] | None) -> str:
    """Canonical project identity shared by rules and raw history.

    Only filesystem-looking values are resolved.  Legacy logical labels stay
    labels, while Windows spelling/slash aliases collapse to one stable key.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    filesystem_ref = (
        "/" in text or "\\" in text or bool(os.path.isabs(text))
        or (len(text) >= 2 and text[1] == ":")
    )
    if not filesystem_ref:
        return text
    try:
        resolved = Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return ""
    normalized = os.path.normcase(str(resolved)).replace("\\", "/")
    if normalized == "/" or (
        len(normalized) == 3
        and normalized[1:] == ":/"
    ):
        return normalized
    return normalized.rstrip("/")


def normalize_assignment(
    value: dict | RuleAssignment, *, automatic: bool = False,
    actor_agent_id: str = "",
) -> RuleAssignment:
    if isinstance(value, RuleAssignment):
        assignment = value
    elif isinstance(value, dict):
        assignment = RuleAssignment(
            memory_id=str(value.get("memory_id", "")),
            target_type=str(value.get("target_type", "")),
            target_id=str(value.get("target_id", "")),
            project_ref=str(value.get("project_ref", "")),
            effect=str(value.get("effect", "include")),
            priority_override=value.get("priority_override"),
        )
    else:
        raise ValueError("audience assignment must be an object")
    if assignment.target_type not in TARGET_TYPES:
        raise ValueError("invalid audience target_type")
    if assignment.effect not in EFFECTS:
        raise ValueError("invalid audience effect")
    if assignment.target_type in {"project", "agent_project"}:
        raw_project_ref = (
            assignment.project_ref
            or (
                assignment.target_id
                if assignment.target_type == "project" else ""
            )
        )
        project_ref = canonical_project_ref(raw_project_ref)
        if not project_ref:
            raise ValueError(
                f"audience {assignment.target_type} requires a valid project_ref"
            )
        assignment = replace(
            assignment,
            target_id=(
                "" if assignment.target_type == "project"
                else assignment.target_id
            ),
            project_ref=project_ref,
        )
    elif assignment.project_ref:
        raise ValueError(
            f"audience {assignment.target_type} cannot carry project_ref; "
            "use agent_project for Agent + project scope"
        )
    if assignment.target_type in {"agent", "provider", "runtime_role"} and not assignment.target_id:
        raise ValueError(f"audience {assignment.target_type} requires target_id")
    if assignment.target_type == "agent_project" and (not assignment.target_id or not assignment.project_ref):
        raise ValueError("audience agent_project requires target_id and project_ref")
    if assignment.priority_override is not None and (
        isinstance(assignment.priority_override, bool)
        or not isinstance(assignment.priority_override, int)
        or not -100 <= assignment.priority_override <= 100
    ):
        raise ValueError("audience priority_override must be an integer between -100 and 100")
    if automatic:
        # Keep manual ``system`` matching compatible while making the
        # opt-in automatic boundary explicit for callers outside Store.
        if assignment.target_type not in {"agent", "agent_project"}:
            raise ValueError(
                "automatic assignment cannot broaden target_type; "
                "allowed target types are agent and agent_project"
            )
        if not actor_agent_id:
            raise ValueError("automatic assignment requires actor_agent_id")
        if assignment.target_id != actor_agent_id:
            raise ValueError("automatic assignment cannot target another agent")
    return assignment


def assignment_matches(assignment: RuleAssignment, context: EffectiveAgentContext) -> bool:
    """Return whether audience shape applies; exclude precedence is caller-owned."""
    try:
        assignment = normalize_assignment(assignment)
    except ValueError:
        return False
    kind = assignment.target_type
    context_project_ref = canonical_project_ref(context.project_ref)
    if kind == "system":
        return True
    if kind == "group":
        return not assignment.target_id or assignment.target_id == context.share_group_id
    if kind == "agent":
        return assignment.target_id == context.agent_instance_id
    if kind == "project":
        return (
            bool(context_project_ref)
            and assignment.project_ref == context_project_ref
        )
    if kind == "agent_project":
        return (assignment.target_id == context.agent_instance_id
                and bool(context_project_ref)
                and assignment.project_ref == context_project_ref)
    if kind == "provider":
        return bool(context.provider) and assignment.target_id.casefold() == context.provider.casefold()
    if kind == "runtime_role":
        # Unknown runtime role must not be guessed into a role-scoped rule.
        return bool(context.runtime_role) and assignment.target_id.casefold() == context.runtime_role.casefold()
    return False


def effective_assignments(
    assignments: Iterable[RuleAssignment], context: EffectiveAgentContext,
) -> tuple[list[RuleAssignment], list[RuleAssignment]]:
    matched = [item for item in assignments if assignment_matches(item, context)]
    excludes = [item for item in matched if item.effect == "exclude"]
    includes = [item for item in matched if item.effect == "include"]
    return includes, excludes


def can_manage_assignment(assignment: RuleAssignment, *, actor_agent_id: str, is_admin: bool) -> bool:
    """Non-admins may manage only their own agent or agent-project audience."""
    try:
        assignment = normalize_assignment(assignment)
    except ValueError:
        return False
    if is_admin:
        return True
    return (
        assignment.target_type in {"agent", "agent_project"}
        and assignment.target_id == actor_agent_id
    )


# ---------------------------------------------------------------------------
# Semantic scope inference (P1)
#
# One-sentence rule creation should not guess a scope from a hard-coded table.
# The text is scanned for explicit scope signals; only the trusted current
# agent and its canonical project are ever selected automatically, so the
# safety boundary (no auto group/provider/runtime_role/system/other-agent)
# is unchanged.  Broad or ambiguous requests fall back to the narrowest
# trusted scope with a lowered confidence and ``fallback_used`` so the cockpit
# can ask for human confirmation instead of claiming a wide scope confidently.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field
import re as _re


@dataclass
class ScopeCandidate:
    target_type: str
    target_id: str
    project_ref: str
    confidence: float
    reasons: list[str] = field(default_factory=list)

    def __hash__(self) -> int:
        return hash((self.target_type, self.target_id, self.project_ref))


@dataclass
class ScopeInferenceResult:
    candidates: list[ScopeCandidate]
    selected: ScopeCandidate
    margin: float
    fallback_used: bool
    policy_version: str

    def to_dict(self) -> dict:
        return {
            "selected": {
                "target_type": self.selected.target_type,
                "target_id": self.selected.target_id,
                "project_ref": self.selected.project_ref,
                "confidence": self.selected.confidence,
                "reasons": self.selected.reasons,
            },
            "candidates": [
                {
                    "target_type": c.target_type,
                    "target_id": c.target_id,
                    "project_ref": c.project_ref,
                    "confidence": c.confidence,
                    "reasons": c.reasons,
                }
                for c in self.candidates
            ],
            "margin": self.margin,
            "fallback_used": self.fallback_used,
            "policy_version": self.policy_version,
        }


_TEXT_SIGNALS: list[tuple[_re.Pattern[str], str, float, str]] = [
    (_re.compile(r"当前\s+agent\s*\+\s*项目|current\s+agent\s*\+\s*project", _re.IGNORECASE),
     "agent_project", 0.98, "text explicitly combines the current agent and project scopes"),
    (_re.compile(r"本项目|当前仓库|当前代码库|当前项目|本仓库|本代码库|这个项目|这个仓库|这个代码库", _re.IGNORECASE),
     "agent_project", 0.96, "text scopes the rule to the current project"),
    # ``infer_scope_from_text`` normalizes Latin text with ``casefold`` below;
    # keep the regex case-insensitive too so mixed-case Agent wording cannot
    # silently bypass an explicit scope signal.
    (_re.compile(r"当前 agent|只让当前 agent|仅当前 agent|这个 agent|当前助手", _re.IGNORECASE),
     "agent", 0.90, "text scopes the rule to the current agent"),
    (_re.compile(r"子 agent|子代理|仅子 agent|所有子 agent", _re.IGNORECASE),
     "subagent", 0.60, "text suggests a runtime subagent scope"),
    (_re.compile(r"所有 agent|所有项目|全局|任何 agent|全部项目|全局都必须|任何项目", _re.IGNORECASE),
     "broad", 0.45, "text asks for a wide scope that automatic flow must never claim"),
]


def infer_scope_from_text(
    text: str,
    *,
    agent_instance_id: str,
    project_ref: str = "",
) -> ScopeInferenceResult:
    """Semantic layer over trusted context; never widens authority."""
    lowered = (text or "").strip().casefold()
    candidates: list[ScopeCandidate] = []
    seen: set[ScopeCandidate] = set()
    broad_requested = False

    for pattern, hint, base_conf, reason in _TEXT_SIGNALS:
        if not pattern.search(lowered):
            continue
        if hint == "broad":
            broad_requested = True
        cand = ScopeCandidate(
            target_type={
                "agent_project": "agent_project",
                "agent": "agent",
                "subagent": "runtime_role",
                "broad": "broad",
            }[hint],
            target_id=agent_instance_id if hint in ("agent", "agent_project") else "",
            project_ref=project_ref if hint == "agent_project" else "",
            confidence=base_conf,
            reasons=[reason],
        )
        if cand not in seen:
            candidates.append(cand)
            seen.add(cand)

    # Trusted context pass: only the current agent and its canonical project
    # may be auto-selected.  No text signal -> safest fallback to current agent.
    trusted = [
        c for c in candidates
        if c.target_type in ("agent", "agent_project")
        and c.target_id == agent_instance_id
        and (c.target_type != "agent_project" or bool(c.project_ref))
    ]
    used_default_fallback = not bool(trusted)
    if not trusted:
        fallback_candidate = ScopeCandidate(
            target_type="agent_project" if project_ref else "agent",
            target_id=agent_instance_id,
            project_ref=project_ref if project_ref else "",
            confidence=0.80 if project_ref else 0.85,
            reasons=["no explicit scope signal; safe fallback to trusted current context"],
        )
        # The selected candidate must be part of the explainable candidate set;
        # previously the safe fallback was returned out-of-band.
        trusted = [fallback_candidate]
        candidates.append(fallback_candidate)

    trusted.sort(key=lambda c: c.confidence, reverse=True)
    selected = trusted[0]
    margin = (selected.confidence - trusted[1].confidence) if len(trusted) > 1 else 0.15
    fallback = used_default_fallback or broad_requested or len(trusted) > 1
    if broad_requested:
        # A wide scope must be human-confirmed: keep the narrowest trusted
        # selection but never report a confident wide claim.
        selected.confidence = 0.55 if selected.target_type == "agent_project" else 0.60
        selected.reasons = selected.reasons + [
            "text requested a wide scope; auto-flow falls back to the narrowest trusted scope and asks for confirmation",
        ]
    return ScopeInferenceResult(
        candidates=candidates,
        selected=selected,
        margin=margin,
        fallback_used=fallback,
        policy_version="scope-infer-v1",
    )
