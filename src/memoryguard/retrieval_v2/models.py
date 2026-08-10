"""Pure data contracts for deterministic V2 recall planning.

The planner deliberately has no knowledge of MemoryGuard's stores.  Ports
return small mappings (or objects exposing ``to_dict``); this module keeps the
public request/plan shape stable while preventing source payloads from leaking
through recall responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _layer_name(value: Any) -> str:
    aliases = {
        "content": "content_reference",
        "content-reference": "content_reference",
        "content_ref": "content_reference",
        "history_reference": "history",
        "code-graph": "codegraph",
        "code_graph": "codegraph",
        "skills": "skill",
    }
    normalized = _text(value).lower()
    return aliases.get(normalized, normalized)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_digest(value: Any) -> str:
    """Return a stable SHA-256 digest suitable for request/plan identities."""

    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecallScope:
    """Exact read boundary for recall.

    ``workspace_id`` and ``share_group_id`` are mandatory.  Other dimensions
    are optional only when the caller intentionally leaves them empty; a
    candidate missing a requested dimension never matches (no wildcard
    widening).  This is a planner boundary, not an authorization bypass.
    """

    workspace_id: str
    share_group_id: str
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    runtime_role: str = ""

    def __post_init__(self) -> None:
        for name in ("workspace_id", "share_group_id"):
            if not _text(getattr(self, name)):
                raise ValueError(f"{name} is required")
        for name in ("workspace_id", "share_group_id", "agent_instance_id", "project_ref", "provider", "runtime_role"):
            value = _text(getattr(self, name))
            object.__setattr__(self, name, value)

    @classmethod
    def from_value(cls, value: "RecallScope | Mapping[str, Any]") -> "RecallScope":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("scope must be RecallScope or mapping")
        data = dict(value)
        aliases = {
            "group_id": "share_group_id",
            "group": "share_group_id",
            "workspace": "workspace_id",
            "agent": "agent_instance_id",
            "project": "project_ref",
            "runtime": "runtime_role",
        }
        for alias, canonical in aliases.items():
            if alias not in data:
                continue
            if canonical in data and _text(data[canonical]) != _text(data[alias]):
                raise ValueError(f"conflicting scope alias: {alias}/{canonical}")
            data.setdefault(canonical, data[alias])
        return cls(
            workspace_id=_text(data.get("workspace_id")),
            share_group_id=_text(data.get("share_group_id")),
            agent_instance_id=_text(data.get("agent_instance_id")),
            project_ref=_text(data.get("project_ref")),
            provider=_text(data.get("provider")),
            runtime_role=_text(data.get("runtime_role")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "share_group_id": self.share_group_id,
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
        }

    as_dict = to_dict

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def matches(self, candidate: Mapping[str, Any]) -> bool:
        """Exact candidate scope match; absent target scope is denied."""

        def value(*names: str) -> str:
            for name in names:
                if name in candidate:
                    return _text(candidate.get(name))
            return ""

        if value("workspace_id", "workspace") != self.workspace_id:
            return False
        if value("share_group_id", "group_id", "group") != self.share_group_id:
            return False
        dimensions = (
            ("agent_instance_id", ("agent_instance_id", "agent")),
            ("project_ref", ("project_ref", "project")),
            ("provider", ("provider",)),
            ("runtime_role", ("runtime_role", "runtime")),
        )
        for target, names in dimensions:
            expected = getattr(self, target)
            # Empty request dimension means exact group-level scope, not a
            # wildcard.  This prevents accidental cross-agent/project/provider
            # enumeration when callers omit a dimension.
            if value(*names) != expected:
                return False
        return True


@dataclass(frozen=True)
class RecallRequest:
    """Bounded, deterministic planner request."""

    query: str
    scope: RecallScope
    budget_items: int = 20
    budget_chars: int = 12000
    layers: tuple[str, ...] = (
        "working",
        "rules",
        "atoms",
        "scenario",
        "profile",
        "content_reference",
        "history",
        "knowledge",
        "codegraph",
        "skill",
    )
    include_history: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", _text(self.query))
        object.__setattr__(self, "scope", RecallScope.from_value(self.scope))
        try:
            items = int(self.budget_items)
            chars = int(self.budget_chars)
        except (TypeError, ValueError) as exc:
            raise ValueError("recall budgets must be integers") from exc
        if isinstance(self.budget_items, bool) or isinstance(self.budget_chars, bool):
            raise ValueError("recall budgets must be integers")
        if items < 1 or items > 10000:
            raise ValueError("budget_items out of range")
        if chars < 1 or chars > 2_000_000:
            raise ValueError("budget_chars out of range")
        object.__setattr__(self, "budget_items", items)
        object.__setattr__(self, "budget_chars", chars)
        raw_layers = self.layers if self.layers is not None else ()
        if isinstance(raw_layers, str):
            raw_layers = (raw_layers,)
        normalized = tuple(dict.fromkeys(_layer_name(layer) for layer in raw_layers if _text(layer)))
        object.__setattr__(self, "layers", normalized)
        if not isinstance(self.include_history, bool):
            raise ValueError("include_history must be bool")

    @classmethod
    def from_value(cls, value: "RecallRequest | Mapping[str, Any]") -> "RecallRequest":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("request must be RecallRequest or mapping")
        data = dict(value)
        if "budget" in data and "budget_items" not in data:
            data["budget_items"] = data["budget"]
        if "max_items" in data and "budget_items" not in data:
            data["budget_items"] = data["max_items"]
        if "scope" in data:
            raw_scope = data["scope"]
            if isinstance(raw_scope, RecallScope):
                nested_scope = raw_scope.to_dict()
            elif isinstance(raw_scope, Mapping):
                nested_scope = dict(raw_scope)
            else:
                raise TypeError("scope must be RecallScope or mapping")

            def alias_value(mapping: Mapping[str, Any], names: tuple[str, ...], label: str) -> str:
                values = [_text(mapping[name]) for name in names if name in mapping and _text(mapping[name])]
                if len(set(values)) > 1:
                    raise ValueError(f"conflicting scope alias: {label}")
                return values[0] if values else ""

            dimensions = (
                ("workspace_id", ("workspace_id", "workspace")),
                ("share_group_id", ("share_group_id", "group_id", "group")),
                ("agent_instance_id", ("agent_instance_id", "agent")),
                ("project_ref", ("project_ref", "project")),
                ("provider", ("provider",)),
                ("runtime_role", ("runtime_role", "runtime")),
            )
            merged_scope = dict(nested_scope)
            for canonical, aliases in dimensions:
                nested_value = alias_value(nested_scope, aliases, canonical)
                top_value = alias_value(data, aliases, canonical)
                if nested_value and top_value and nested_value != top_value:
                    raise ValueError(f"conflicting scope alias: nested/{canonical}")
                if top_value and not nested_value:
                    merged_scope[canonical] = top_value
            scope_value: Mapping[str, Any] = merged_scope
        else:
            scope_value = data
        return cls(
            query=_text(data.get("query")),
            scope=RecallScope.from_value(scope_value),
            budget_items=data.get("budget_items", 20),
            budget_chars=data.get("budget_chars", 12000),
            layers=tuple(data.get("layers") or cls.layers),
            include_history=data.get("include_history", True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "scope": self.scope.to_dict(),
            "budget_items": self.budget_items,
            "budget_chars": self.budget_chars,
            "layers": list(self.layers),
            "include_history": self.include_history,
        }

    as_dict = to_dict

    @property
    def request_id(self) -> str:
        return stable_digest(self.to_dict())

    @property
    def max_items(self) -> int:
        """Compatibility alias for context-engine style callers."""

        return self.budget_items

    @property
    def max_chars(self) -> int:
        """Compatibility alias for context-engine style callers."""

        return self.budget_chars


@dataclass(frozen=True)
class RecallDecision:
    """One auditable include/exclude decision.

    Only safe summary metadata is carried.  ``body``/``text``/transcript
    fields intentionally do not exist on this type.
    """

    item_id: str
    layer: str
    action: str
    trust: str = "relevant"
    score: float = 0.0
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    source_digest: str = ""
    summary: str = ""
    status: str = "valid"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        action = _text(self.action).lower()
        if action not in {"include", "exclude"}:
            raise ValueError("decision action must be include or exclude")
        trust = _text(self.trust).lower() or "relevant"
        if trust not in {"mandatory", "enforceable", "relevant", "reference_only"}:
            raise ValueError("invalid decision trust")
        object.__setattr__(self, "item_id", _text(self.item_id))
        object.__setattr__(self, "layer", _text(self.layer).lower())
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "trust", trust)
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "reason", _text(self.reason))
        object.__setattr__(self, "evidence_refs", tuple(sorted({_text(ref) for ref in self.evidence_refs if _text(ref)})))
        object.__setattr__(self, "source_digest", _text(self.source_digest))
        object.__setattr__(self, "summary", _text(self.summary))
        object.__setattr__(self, "status", _text(self.status).lower() or "valid")
        safe = {}
        for key, value in dict(self.metadata or {}).items():
            key = _text(key).lower()
            if key in {"body", "text", "raw", "transcript", "content", "full_text", "document", "payload"}:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value
        object.__setattr__(self, "metadata", dict(sorted(safe.items())))

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "layer": self.layer,
            "action": self.action,
            "trust": self.trust,
            "score": self.score,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "source_digest": self.source_digest,
            "summary": self.summary,
            "status": self.status,
            "metadata": dict(self.metadata),
        }

    as_dict = to_dict


@dataclass(frozen=True)
class RecallPlan:
    """Immutable planner output and audit trail."""

    request_id: str
    scope: RecallScope
    decisions: tuple[RecallDecision, ...] = ()
    selected: tuple[RecallDecision, ...] = ()
    status: str = "ok"
    reason: str = ""
    mandatory_overflow: bool = False
    layer_status: Mapping[str, str] = field(default_factory=dict)
    counts: Mapping[str, int] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text(self.request_id))
        object.__setattr__(self, "scope", RecallScope.from_value(self.scope))
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "selected", tuple(self.selected))
        status = _text(self.status).upper() if _text(self.status) == "NOT_CONFIGURED" else _text(self.status).lower()
        object.__setattr__(self, "status", status or "ok")
        object.__setattr__(self, "reason", _text(self.reason))
        object.__setattr__(self, "layer_status", dict(sorted((str(k), str(v)) for k, v in dict(self.layer_status or {}).items())))
        object.__setattr__(self, "counts", dict(sorted((str(k), int(v)) for k, v in dict(self.counts or {}).items())))
        if not self.digest:
            object.__setattr__(self, "digest", stable_digest(self._digest_payload()))

    def _digest_payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "scope": self.scope.to_dict(),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "selected": [decision.to_dict() for decision in self.selected],
            "status": self.status,
            "reason": self.reason,
            "mandatory_overflow": self.mandatory_overflow,
            "layer_status": dict(self.layer_status),
            "counts": dict(self.counts),
        }

    @property
    def excluded(self) -> tuple[RecallDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.action == "exclude")

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(decision.item_id for decision in self.selected)

    @property
    def mandatory(self) -> tuple[RecallDecision, ...]:
        return tuple(decision for decision in self.selected if decision.trust == "mandatory")

    @property
    def relevant(self) -> tuple[RecallDecision, ...]:
        return tuple(decision for decision in self.selected if decision.trust == "relevant")

    @property
    def reference_only(self) -> tuple[RecallDecision, ...]:
        return tuple(decision for decision in self.selected if decision.trust == "reference_only")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "scope": self.scope.to_dict(),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "selected": [decision.to_dict() for decision in self.selected],
            "excluded": [decision.to_dict() for decision in self.excluded],
            "status": self.status,
            "reason": self.reason,
            "mandatory_overflow": self.mandatory_overflow,
            "layer_status": dict(self.layer_status),
            "counts": dict(self.counts),
            "digest": self.digest,
        }

    as_dict = to_dict


__all__ = ["RecallScope", "RecallRequest", "RecallDecision", "RecallPlan", "stable_digest"]
