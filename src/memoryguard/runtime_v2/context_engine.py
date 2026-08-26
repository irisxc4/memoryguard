"""Shadow V2 context engine.

This module is the sole context-packet builder for the Phase4 shadow runtime.
It accepts retrieval/planner protocols instead of constructing a legacy store;
no Hook, MCP, GUI, history database, or tool output is read here.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from ..rule_scope import canonical_project_ref
from .governance_semantics import classify_governance_relation
from .context_budget import (
    BudgetLedger,
    ContextBudget,
    ContextBudgetError,
    ContextSafetyError,
    DeterministicTokenCounter,
    TokenCounter,
)
from ..sensitive_content import contains_sensitive_content


class ContextEngineError(ValueError):
    """Invalid request, retrieval shape, or blocked context build."""


@runtime_checkable
class RetrievalPort(Protocol):
    def retrieve(self, request: "ContextRequest") -> Any: ...


@runtime_checkable
class PlannerPort(Protocol):
    def plan(self, request: "ContextRequest", candidates: Sequence[Mapping[str, Any]]) -> Any: ...


# Names used by early retrieval_v2 drafts.  Protocol aliases keep this module
# importable while that parallel package is still being built.
RetrievalProtocol = RetrievalPort
PlannerProtocol = PlannerPort


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _identity_value(
    mapping: Mapping[str, Any],
    names: tuple[str, ...],
    field_name: str,
    *,
    normalizer: Any | None = None,
) -> str:
    values = [_text(mapping[name]) for name in names if name in mapping and mapping[name] is not None]
    values = [normalizer(value) if normalizer is not None else value for value in values]
    values = [value for value in values if value]
    if len(set(values)) > 1:
        raise ContextEngineError(f"conflicting_context_identity:{field_name}")
    return values[0] if values else ""


@dataclass(frozen=True)
class RetrievalPlan:
    """Minimal retrieval_v2-compatible plan envelope.

    A planner may return this object, a mapping, or a list of known item IDs;
    ContextEngine uses it only for ordering and never trusts it for scope or
    mandatory elevation.
    """

    item_ids: tuple[str, ...] = ()
    layers: tuple[str, ...] = ("mandatory", "relevant", "knowledge", "reference_only")

    def to_dict(self) -> dict[str, Any]:
        return {"item_ids": list(self.item_ids), "layers": list(self.layers)}


@dataclass(frozen=True)
class ContextRequest:
    task: str = ""
    project_hint: str = ""
    max_items: int | None = None
    max_chars: int | None = None
    max_tokens: int | None = None
    read_path: str = "auto"
    agent: str = ""
    project: str = ""
    group: str = ""
    provider: str = ""
    runtime: str = ""
    agent_instance_id: str = ""
    project_ref: str = ""
    share_group_id: str = ""
    runtime_role: str = ""
    trusted_identity: Mapping[str, Any] = field(default_factory=dict)
    workspace_id: str = ""
    namespace_id: str = ""
    sensitivity: str = ""
    policy_class: str = ""

    def __post_init__(self) -> None:
        trusted = dict(self.trusted_identity) if isinstance(self.trusted_identity, Mapping) else self.trusted_identity
        trusted = trusted if isinstance(trusted, dict) else {}
        agent_sources = {
            "agent": self.agent,
            "agent_instance_id": self.agent_instance_id,
            "agent_id": trusted.get("agent_id", ""),
            "trusted_agent": trusted.get("agent", ""),
            "trusted_agent_instance_id": trusted.get("agent_instance_id", ""),
            "trusted_agent_id": trusted.get("agent_id", ""),
        }
        agent = _identity_value(
            agent_sources,
            ("agent", "agent_instance_id", "agent_id", "trusted_agent", "trusted_agent_instance_id", "trusted_agent_id"),
            "agent",
        )
        project_sources = {
            "project": self.project,
            "project_ref": self.project_ref,
            "project_id": trusted.get("project_id", ""),
            "trusted_project": trusted.get("project", ""),
            "trusted_project_ref": trusted.get("project_ref", ""),
            "trusted_project_id": trusted.get("project_id", ""),
        }
        project = _identity_value(
            project_sources,
            ("project", "project_ref", "project_id", "trusted_project", "trusted_project_ref", "trusted_project_id"),
            "project",
            normalizer=canonical_project_ref,
        )
        group_sources = {
            "group": self.group,
            "share_group_id": self.share_group_id,
            "group_id": trusted.get("group_id", ""),
            "trusted_group": trusted.get("group", ""),
            "trusted_share_group_id": trusted.get("share_group_id", ""),
            "trusted_group_id": trusted.get("group_id", ""),
        }
        group = _identity_value(
            group_sources,
            ("group", "share_group_id", "group_id", "trusted_group", "trusted_share_group_id", "trusted_group_id"),
            "group",
        )
        runtime_sources = {
            "runtime": self.runtime,
            "runtime_role": self.runtime_role,
            "trusted_runtime": trusted.get("runtime", ""),
            "trusted_runtime_role": trusted.get("runtime_role", ""),
        }
        runtime = _identity_value(
            runtime_sources,
            ("runtime", "runtime_role", "trusted_runtime", "trusted_runtime_role"),
            "runtime",
        )
        provider = _identity_value(
            {"provider": self.provider, "trusted_provider": trusted.get("provider", "")},
            ("provider", "trusted_provider"),
            "provider",
        )
        workspace = _identity_value(
            {
                "workspace_id": self.workspace_id,
                "trusted_workspace_id": trusted.get("workspace_id", ""),
                "trusted_workspace": trusted.get("workspace", ""),
            },
            ("workspace_id", "trusted_workspace_id", "trusted_workspace"),
            "workspace_id",
        )
        object.__setattr__(self, "task", _text(self.task))
        object.__setattr__(self, "project_hint", _text(self.project_hint))
        object.__setattr__(self, "agent", agent)
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "group", group)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "runtime", runtime)
        object.__setattr__(self, "agent_instance_id", agent)
        object.__setattr__(self, "project_ref", project)
        object.__setattr__(self, "share_group_id", group)
        object.__setattr__(self, "runtime_role", runtime)
        object.__setattr__(self, "workspace_id", workspace)
        object.__setattr__(self, "namespace_id", _text(self.namespace_id))
        object.__setattr__(self, "sensitivity", _text(self.sensitivity))
        object.__setattr__(self, "policy_class", _text(self.policy_class))
        object.__setattr__(self, "read_path", _text(self.read_path).casefold() or "auto")
        for name in ("max_items", "max_chars", "max_tokens"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ContextEngineError(f"invalid_context_request:{name}")
        if not isinstance(self.trusted_identity, Mapping):
            raise ContextEngineError("trusted_identity_required")
        object.__setattr__(self, "trusted_identity", dict(self.trusted_identity))

    @property
    def effective_agent(self) -> str:
        return self.agent

    @classmethod
    def from_mapping(cls, value: Any) -> "ContextRequest":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ContextEngineError("context_request_required")
        identity = value.get("trusted_identity", value.get("trusted_context", value.get("identity", {})))
        if identity is None:
            identity = {}
        if not isinstance(identity, Mapping):
            raise ContextEngineError("trusted_identity_required")
        fields: dict[str, Any] = {
            "task": value.get("task", ""),
            "project_hint": value.get("project_hint", ""),
            "read_path": value.get("read_path", "auto"),
            "trusted_identity": dict(identity),
        }
        merged = dict(value)
        for key, alias in (
            ("trusted_agent", "agent"), ("trusted_agent_instance_id", "agent_instance_id"), ("trusted_agent_id", "agent_id"),
            ("trusted_project", "project"), ("trusted_project_ref", "project_ref"), ("trusted_project_id", "project_id"),
            ("trusted_group", "group"), ("trusted_share_group_id", "share_group_id"), ("trusted_group_id", "group_id"),
            ("trusted_runtime", "runtime"), ("trusted_runtime_role", "runtime_role"),
            ("trusted_workspace_id", "workspace_id"), ("trusted_workspace", "workspace"),
        ):
            if alias in identity:
                merged[key] = identity[alias]
        fields["agent"] = _identity_value(merged, ("agent", "agent_instance_id", "agent_id", "trusted_agent", "trusted_agent_instance_id", "trusted_agent_id"), "agent")
        fields["project"] = _identity_value(
            merged,
            ("project", "project_ref", "project_id", "trusted_project", "trusted_project_ref", "trusted_project_id"),
            "project",
            normalizer=canonical_project_ref,
        )
        fields["group"] = _identity_value(merged, ("group", "share_group_id", "group_id", "trusted_group", "trusted_share_group_id", "trusted_group_id"), "group")
        provider_values = dict(merged)
        if "provider" in identity:
            provider_values["trusted_provider"] = identity["provider"]
        fields["provider"] = _identity_value(provider_values, ("provider", "trusted_provider"), "provider")
        fields["runtime"] = _identity_value(merged, ("runtime", "runtime_role", "trusted_runtime", "trusted_runtime_role"), "runtime")
        fields["workspace_id"] = _identity_value(
            merged,
            ("workspace_id", "workspace", "trusted_workspace_id", "trusted_workspace"),
            "workspace_id",
        )
        fields["namespace_id"] = _text(value.get("namespace_id", identity.get("namespace_id", "")))
        fields["sensitivity"] = _text(value.get("sensitivity", identity.get("sensitivity", "")))
        fields["policy_class"] = _text(value.get("policy_class", identity.get("policy_class", "")))
        for name in ("max_items", "max_chars", "max_tokens"):
            if name in value:
                fields[name] = value[name]
        return cls(**fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "project_hint": self.project_hint,
            "max_items": self.max_items,
            "max_chars": self.max_chars,
            "max_tokens": self.max_tokens,
            "read_path": self.read_path,
            "agent": self.agent,
            "agent_instance_id": self.agent,
            "project": self.project,
            "project_ref": self.project,
            "group": self.group,
            "share_group_id": self.group,
            "provider": self.provider,
            "runtime": self.runtime,
            "runtime_role": self.runtime,
            "workspace_id": self.workspace_id,
            "namespace_id": self.namespace_id,
            "sensitivity": self.sensitivity,
            "policy_class": self.policy_class,
            "trusted_identity": dict(self.trusted_identity),
        }


@dataclass(frozen=True)
class ContextCandidate:
    item_id: str
    body: str
    memory_id: str = ""
    layer: str = "relevant"
    kind: str = "fact"
    source: str = "retrieval"
    scope: Mapping[str, Any] = field(default_factory=dict)
    evidence: Any = None
    priority: int = 0
    score: float = 0.0
    sensitive: bool = False
    raw_history: bool = False
    tool_output: bool = False
    is_rule: bool = False
    lifecycle_invalid: bool = False
    lifecycle_reason: str = ""
    unsafe_payload: bool = False
    layer_invalid: bool = False
    scope_invalid: bool = False
    scope_reason: str = ""
    summary: str = ""
    reference: str = ""
    content_hash: str = ""
    injection_policy: str = ""
    rule_strength: str = ""
    semantic_identity: str = ""

    @staticmethod
    def _strict_flag(value: Any) -> bool | None:
        if type(value) is bool:
            return value
        if type(value) is int and value in (0, 1):
            return value == 1
        if type(value) is str and value in {"0", "1"}:
            return value == "1"
        return None

    @classmethod
    def _scan_payload(cls, value: Any, *, root: bool = True) -> bool:
        """Find recursive raw/body/control payload markers.

        Top-level ``body`` is the normalized candidate text.  Every nested
        body/payload/content/history/tool/evidence value is treated as raw or
        untrusted transport content and blocked before rendering.
        """
        if isinstance(value, Mapping):
            for key, child in value.items():
                name = _text(key).casefold()
                if name in {"raw", "transcript", "full_text", "source_text", "payload", "content", "history", "turns", "conversation", "tool_output", "tool_result", "mcp_output"}:
                    if child not in (None, "", [], {}, ()):  # control marker/source exists
                        return True
                if name == "evidence" and child not in (None, "", [], {}, ()):
                    if isinstance(child, Mapping):
                        if any(
                            _text(key).casefold() not in {"ref", "evidence_ref", "digest", "evidence_digest", "hash", "id"}
                            for key in child
                        ):
                            return True
                    elif not isinstance(child, str) or len(child) > 128 or any(char.isspace() for char in child):
                        return True
                if name == "body" and not root and child not in (None, "", [], {}, ()):
                    return True
                if name in {"source", "source_type"} and _text(child).casefold() in _RAW_SOURCE_NAMES:
                    return True
                if name in {"kind", "status", "marker", "trust"} and _text(child).casefold() in {"raw", "history", "transcript", "tool", "tool_output"}:
                    return True
                if cls._scan_payload(child, root=False):
                    return True
        elif isinstance(value, (list, tuple, set)):
            return any(cls._scan_payload(child, root=False) for child in value)
        return False

    @classmethod
    def from_value(cls, value: Any, *, layer: str = "relevant", index: int = 0) -> "ContextCandidate":
        if isinstance(value, cls):
            if value.layer == layer:
                return value
            return replace(value, layer=layer)
        if isinstance(value, str):
            body = value
            data: Mapping[str, Any] = {}
        elif isinstance(value, Mapping):
            data = value
            body = data.get("body", data.get("text", data.get("content", data.get("summary", ""))))
        else:
            raise ContextEngineError("invalid_retrieval_candidate")
        declared_layer = _text(data.get("layer", layer)).casefold() or layer
        allowed_layers = {"mandatory", "relevant", "knowledge", "reference_only"}
        layer_invalid = declared_layer not in allowed_layers or declared_layer != layer
        is_reference = layer == "reference_only"
        summary = _text(data.get("summary", ""))
        reference = _text(data.get("ref", data.get("reference", data.get("source_ref", ""))))
        content_hash = _text(data.get("hash", data.get("content_hash", data.get("digest", ""))))
        # Reference-only items may carry metadata/summary only; body/content
        # is raw正文 and is rejected rather than copied into the packet.
        unsafe_payload = cls._scan_payload(data)
        if is_reference and ("body" in data or "text" in data or "content" in data):
            unsafe_payload = True
        body_text = summary if is_reference else _text(body)
        memory_id = _text(data.get("memory_id", ""))
        candidate_id = _text(data.get("item_id", data.get("memory_id", data.get("id", ""))))
        digest = hashlib.sha256(f"{layer}|{data.get('kind', '')}|{body_text}".encode("utf-8")).hexdigest()[:20]
        if not candidate_id:
            candidate_id = f"ctx-{digest}-{index}"
        if "scope" in data:
            raw_scope = data.get("scope")
        elif "audience" in data:
            raw_scope = data.get("audience")
        else:
            raw_scope = {}
        scope_invalid = False
        scope_reason = ""
        if isinstance(raw_scope, Mapping):
            scope = dict(raw_scope)
        else:
            scope = {}
            scope_invalid, scope_reason = True, "scope_shape_rejected"
        # Candidate-level aliases are another representation of scope. Keep
        # them only for validation; conflicting top-level/nested values must
        # never turn into an implicit wildcard. ``id`` is normally the
        # candidate/item identifier (all legacy fixtures use it that way),
        # therefore treat it as a target-id alias only when the candidate
        # explicitly carries target metadata. Direct/nested scope ``id`` is
        # still handled by ``_scope_decision`` below.
        target_metadata_present = any(
            key in data for key in ("target_type", "type", "scope_type", "target_id", "scope_id")
        )
        scope_aliases = (
            "target_type", "type", "target_id", "scope_type", "scope_id",
            "agent", "agent_id", "agent_instance_id", "project", "project_id", "project_ref",
            "group", "group_id", "share_group_id", "provider", "runtime", "runtime_role", "workspace", "workspace_id",
        )
        if target_metadata_present:
            scope_aliases += ("id",)
        for alias in scope_aliases:
            if alias in data:
                nested_value = _text(scope.get(alias, ""))
                top_value = _text(data[alias])
                if alias in {"target_type", "type", "scope_type"}:
                    nested_value, top_value = nested_value.casefold(), top_value.casefold()
                elif alias in {"project", "project_id", "project_ref"}:
                    nested_value, top_value = canonical_project_ref(nested_value), canonical_project_ref(top_value)
                if alias in scope and nested_value and nested_value != top_value:
                    scope_invalid, scope_reason = True, "scope_alias_conflict"
                scope[f"__top_{alias}"] = data[alias]
        evidence = data.get("evidence", data.get("evidence_ref", data.get("evidence_digest")))
        kind = _text(data.get("kind", "fact")).casefold() or "fact"
        source = _text(data.get("source", data.get("source_type", "retrieval"))) or "retrieval"
        # Native memory adapters expose atom identity as item_id while source
        # mapping retains logical memory_id. Preserve logical ID in public
        # packet so revisions do not leak storage IDs into recall contract.
        if source.casefold() == "native-v2-memory" and not memory_id:
            memory_id = _text(data.get("source_ref", ""))
        if source.casefold() == "native-v2-memory" and not _text(data.get("item_id", "")):
            candidate_id = memory_id or candidate_id
        injection_policy = _text(data.get("injection_policy", data.get("dedup_domain", ""))).casefold()
        rule_strength = _text(data.get("rule_strength", data.get("strength", ""))).casefold()
        if source.casefold() == "native-v2-memory" and not injection_policy:
            injection_policy = "always" if layer == "mandatory" else "relevant"
        if source.casefold() == "native-v2-rule" and not rule_strength:
            rule_strength = "must" if layer == "mandatory" else "observation"
        if source.casefold() == "native-v2-rule" and not injection_policy:
            injection_policy = "always" if layer == "mandatory" else "relevant"
        semantic_identity = ""
        for identity_key in ("semantic_identity", "stable_identity", "dedup_identity", "semantic_id"):
            value = data.get(identity_key)
            if isinstance(value, (str, int, float, bool)) and _text(value):
                semantic_identity = _text(value)
                break
        explicit_rule = bool(data.get("is_rule", False)) or kind in {"rule", "mandatory_rule", "always"}
        risk = _text(data.get("risk_level", "")).casefold()
        sensitive = data.get("sensitive") is True or risk in {"high", "critical", "secret"}
        try:
            priority = int(data.get("priority", 0) or 0)
        except (TypeError, ValueError):
            priority = 0
        try:
            score = float(data.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        content_value = data.get("content")
        content_tool = isinstance(content_value, (list, tuple)) and any(
            isinstance(part, Mapping) and _text(part.get("type", "")).casefold() in {"tool", "tool_output", "tool_result"}
            for part in content_value
        )
        content_history = any(key in data for key in ("history", "turns", "conversation"))
        lifecycle_values: dict[str, bool | None] = {}
        lifecycle_invalid = False
        lifecycle_reason = ""
        lifecycle_values_raw: list[tuple[str, str]] = []
        for lifecycle_alias in ("status", "state", "lifecycle_status"):
            if lifecycle_alias in data and data[lifecycle_alias] is not None:
                lifecycle_values_raw.append((lifecycle_alias, _text(data[lifecycle_alias]).casefold()))
        lifecycle = data.get("lifecycle")
        if isinstance(lifecycle, Mapping):
            for lifecycle_alias in ("status", "state", "lifecycle_status"):
                if lifecycle_alias in lifecycle and lifecycle[lifecycle_alias] is not None:
                    lifecycle_values_raw.append((f"lifecycle.{lifecycle_alias}", _text(lifecycle[lifecycle_alias]).casefold()))
        elif lifecycle is not None:
            lifecycle_values_raw.append(("lifecycle", _text(lifecycle).casefold()))
        lifecycle_values_raw = [(name, value) for name, value in lifecycle_values_raw if value]
        lifecycle_alias_conflict = len({value for _, value in lifecycle_values_raw}) > 1
        status = lifecycle_values_raw[0][1] if lifecycle_values_raw else "active"
        valid_statuses = {"active", "ready", "live", "published", "approved", "relevant", "mandatory"}
        if lifecycle_alias_conflict:
            lifecycle_invalid, lifecycle_reason = True, "lifecycle_alias_conflict"
        elif status not in valid_statuses:
            lifecycle_invalid, lifecycle_reason = True, "lifecycle_status_rejected"
        for validity_key in ("lifecycle_valid", "valid"):
            if validity_key in data and cls._strict_flag(data[validity_key]) is not True:
                lifecycle_invalid, lifecycle_reason = True, "lifecycle_flag_invalid"
        for flag in ("active", "locked", "quarantined", "quarantine", "deleted", "shadowed", "superseded"):
            if flag in data:
                parsed = cls._strict_flag(data[flag])
                lifecycle_values[flag] = parsed
                if parsed is None:
                    lifecycle_invalid, lifecycle_reason = True, "lifecycle_flag_invalid"
                elif flag != "active" and parsed:
                    lifecycle_invalid, lifecycle_reason = True, "lifecycle_flag_rejected"
                elif flag == "active" and not parsed:
                    lifecycle_invalid, lifecycle_reason = True, "lifecycle_flag_rejected"
        raw_flag_maps: list[Any] = []
        if "flags" in data:
            raw_flag_maps.append(data.get("flags"))
        if isinstance(lifecycle, Mapping) and "flags" in lifecycle:
            raw_flag_maps.append(lifecycle.get("flags"))
        for raw_flags in raw_flag_maps:
            if not isinstance(raw_flags, Mapping):
                lifecycle_invalid, lifecycle_reason = True, "lifecycle_flags_invalid"
            else:
                for flag in ("active", "locked", "quarantined", "quarantine", "deleted", "shadowed", "superseded"):
                    if flag in raw_flags:
                        parsed = cls._strict_flag(raw_flags[flag])
                        if parsed is None or (flag == "active" and not parsed) or (flag != "active" and parsed):
                            lifecycle_invalid, lifecycle_reason = True, "lifecycle_flags_conflict"
                        if flag in lifecycle_values and lifecycle_values[flag] is not None and lifecycle_values[flag] != parsed:
                            lifecycle_invalid, lifecycle_reason = True, "lifecycle_flags_conflict"
        if status != "active" and lifecycle_values.get("active") is True:
            lifecycle_invalid, lifecycle_reason = True, "lifecycle_flags_conflict"
        if status == "active" and lifecycle_values.get("active") is False:
            lifecycle_invalid, lifecycle_reason = True, "lifecycle_flags_conflict"
        return cls(
            item_id=candidate_id,
            body=body_text,
            memory_id=memory_id,
            layer=declared_layer,
            kind=kind,
            source=source,
            scope=dict(scope),
            evidence=evidence,
            priority=priority,
            score=score,
            sensitive=sensitive,
            raw_history=bool(data.get("raw_history", False)) or content_history or source.casefold() in {"history", "raw_history", "conversation"},
            tool_output=bool(data.get("tool_output", False)) or content_tool or source.casefold() in {"tool", "tool_output", "mcp_output"},
            is_rule=explicit_rule,
            lifecycle_invalid=lifecycle_invalid,
            lifecycle_reason=lifecycle_reason,
            unsafe_payload=unsafe_payload,
            layer_invalid=layer_invalid,
            scope_invalid=scope_invalid,
            scope_reason=scope_reason,
            summary=summary,
            reference=reference,
            content_hash=content_hash,
            injection_policy=injection_policy,
            rule_strength=rule_strength,
            semantic_identity=semantic_identity,
        )

    @property
    def digest(self) -> str:
        # Rendered content digest stays body-based for compatibility.  It is
        # not the authority used for candidate deduplication.
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    @property
    def has_governance_semantics(self) -> bool:
        return bool(self.injection_policy or self.rule_strength or self.semantic_identity)

    @property
    def dedup_key(self) -> str:
        """Return content identity plus governed injection semantics.

        Legacy adapters may provide only body text; those candidates retain
        body-level cross-layer deduplication through the compatibility body
        set.  Once an adapter provides governed semantics,
        layer/kind/policy/strength become part of identity so a mandatory and
        relevant representation of the same body can both survive when their
        obligations differ.
        """
        if self.layer == "reference_only":
            # Reference identity is source/hash based, not summary based.
            # Same digest from two adapters must consume one context slot.
            parts = ("reference", self.content_hash or self.body, self.reference)
        elif not self.has_governance_semantics:
            parts = ("content", self.body, self.layer, self.kind)
        else:
            parts = (
                "governed",
                self.body,
                self.layer,
                self.kind,
                self.injection_policy,
                self.rule_strength,
                self.semantic_identity,
            )
        material = "".join(f"{len(part)}:{part}" for part in parts)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def public(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "memory_id": self.memory_id or self.item_id,
            "kind": self.kind,
            "layer": self.layer,
            "source": self.source,
            "scope": dict(self.scope),
            "priority": self.priority,
            "score": self.score,
            "injection_policy": self.injection_policy or ("always" if self.layer == "mandatory" else "relevant"),
        }


@dataclass(frozen=True)
class ContextReceipt:
    item_id: str
    layer: str
    hit: bool
    reason: str
    scope: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    token_cost: int = 0
    char_cost: int = 0

    def to_dict(self) -> dict[str, Any]:
        if not self.hit:
            reason_map = {
                "scope_mismatch": "scope_rejected",
                "scope_alias_conflict": "scope_rejected",
                "scope_shape_rejected": "scope_rejected",
                "scope_target_invalid": "scope_rejected",
                "scope_omitted": "scope_omitted",
                "knowledge_scope_required": "scope_required",
                "history_scope_required": "scope_required",
                "codegraph_scope_required": "scope_required",
                "knowledge_source_unavailable": "source_unavailable",
                "history_source_unavailable": "source_unavailable",
                "codegraph_source_unavailable": "source_unavailable",
                "history_unsummarized": "summary_omitted",
                "retrieval_omitted": "omitted",
                "raw_source_blocked": "source_rejected",
                "lifecycle_status_rejected": "lifecycle_rejected",
                "lifecycle_alias_conflict": "lifecycle_rejected",
                "lifecycle_flag_rejected": "lifecycle_rejected",
                "lifecycle_flag_invalid": "lifecycle_rejected",
                "lifecycle_flags_invalid": "lifecycle_rejected",
                "lifecycle_flags_conflict": "lifecycle_rejected",
                "injection_policy_layer_conflict": "policy_rejected",
                "injection_policy_invalid": "policy_rejected",
                "mandatory_policy_requires_rule": "policy_rejected",
                "unsafe_payload": "content_rejected",
                "mandatory_content_blocked": "content_rejected",
                "unknown_layer": "layer_rejected",
                "layer_conflict": "layer_rejected",
                "sensitive_blocked": "safety_rejected",
                "mandatory_sensitive_blocked": "safety_rejected",
                "duplicate": "duplicate_rejected",
                "governance_duplicate": "governance_duplicate",
                "governance_update_shadowed": "governance_update_shadowed",
                "budget": "budget_rejected",
                "item_budget": "budget_rejected",
                "empty": "content_rejected",
            }
            return {
                "item_hash": hashlib.sha256(f"{self.layer}|{self.item_id}".encode("utf-8")).hexdigest()[:24],
                "layer": self.layer if self.layer in {"mandatory", "relevant", "knowledge", "reference_only"} else "unknown",
                "hit": False,
                "reason": reason_map.get(self.reason, "rejected"),
            }
        return {
            "item_id": self.item_id,
            "layer": self.layer,
            "hit": self.hit,
            "reason": self.reason,
            "scope": dict(self.scope),
            "evidence": dict(self.evidence),
            "token_cost": self.token_cost,
            "char_cost": self.char_cost,
        }


@dataclass(frozen=True)
class ContextPacket:
    mandatory: tuple[Mapping[str, Any], ...] = ()
    relevant: tuple[Mapping[str, Any], ...] = ()
    knowledge: tuple[Mapping[str, Any], ...] = ()
    reference_only: tuple[Mapping[str, Any], ...] = ()
    budget: Mapping[str, Any] = field(default_factory=dict)
    effective_agent: str = ""
    receipts: tuple[Mapping[str, Any], ...] = ()
    ready: bool = False
    state: str = "V2_BUILDING"
    status: str = "shadow"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mandatory": [dict(item) for item in self.mandatory],
            "relevant": [dict(item) for item in self.relevant],
            "knowledge": [dict(item) for item in self.knowledge],
            "reference_only": [dict(item) for item in self.reference_only],
            "budget": dict(self.budget),
            "effective_agent": self.effective_agent,
            "receipts": [dict(item) for item in self.receipts],
            "ready": self.ready,
            "state": self.state,
            "status": self.status,
            # Keep the packet contract shape stable: successful packets carry
            # an explicit empty error, while failures carry the same field
            # with their diagnostic.
            "error": self.error,
        }
        return payload

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


_SECRET_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|BEGIN (?:RSA|OPENSSH) PRIVATE KEY)")
_RAW_SOURCE_NAMES = {"history", "raw_history", "conversation", "transcript", "chat", "tool", "tool_output", "tool_result", "tool_call", "mcp", "mcp_tool", "mcp_output"}


class ContextEngine:
    """Deterministic retrieval packer; no runtime wiring or persistence."""

    def __init__(
        self,
        retriever: RetrievalPort | Any | None = None,
        planner: PlannerPort | Any | None = None,
        *,
        token_counter: TokenCounter | Any | None = None,
        budget: ContextBudget | Mapping[str, Any] | None = None,
        ready: bool = False,
        state: str = "V2_BUILDING",
    ) -> None:
        self.retriever = retriever
        self.planner = planner
        self.token_counter = token_counter or DeterministicTokenCounter()
        if not callable(getattr(self.token_counter, "count", None)) and not callable(getattr(self.token_counter, "count_tokens", None)) and not callable(self.token_counter):
            raise ContextEngineError("token_counter_required")
        self.budget = ContextBudget.from_mapping(budget)
        self.ready = type(ready) is bool and ready
        self.state = self._normalize_state(state)

    @staticmethod
    def _normalize_state(value: Any) -> str:
        marker = _text(getattr(value, "value", value)).upper()
        return marker if marker in {"V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE"} else "UNKNOWN"

    def _count(self, text: str) -> int:
        counter = self.token_counter
        if callable(getattr(counter, "count", None)):
            result = counter.count(text)
        elif callable(getattr(counter, "count_tokens", None)):
            result = counter.count_tokens(text)
        else:
            result = counter(text)
        if type(result) is not int or result < 0:
            raise ContextEngineError("invalid_token_counter_result")
        return result

    def _request_budget(self, request: ContextRequest) -> ContextBudget:
        updates: dict[str, Any] = {}
        if request.max_items is not None:
            updates["max_items"] = request.max_items
        if request.max_chars is not None:
            updates["max_chars"] = request.max_chars
        if request.max_tokens is not None:
            updates["max_tokens"] = request.max_tokens
        return replace(self.budget, **updates) if updates else self.budget

    def _retrieve(self, request: ContextRequest, supplied: Any | None) -> Any:
        if supplied is not None:
            return supplied
        if self.retriever is None:
            return {}
        retriever = self.retriever
        if callable(retriever) and not hasattr(retriever, "retrieve"):
            return retriever(request)
        method = getattr(retriever, "retrieve", None)
        if callable(method):
            return method(request)
        method = getattr(retriever, "search", None)
        if callable(method):
            return method(request)
        raise ContextEngineError("retrieval_port_required")

    @staticmethod
    def _groups(result: Any) -> dict[str, list[Any]]:
        groups = {"mandatory": [], "relevant": [], "knowledge": [], "reference_only": []}
        if result is None:
            return groups
        if isinstance(result, Mapping):
            found = False
            for layer in groups:
                value = result.get(layer)
                if value is not None:
                    found = True
                    groups[layer].extend(value if isinstance(value, (list, tuple)) else [value])
            for layer, value in result.items():
                if layer not in groups and layer not in {"items", "candidates", "omissions"}:
                    groups.setdefault("__unknown__", []).extend(value if isinstance(value, (list, tuple)) else [value])
            if found:
                return groups
            items = result.get("items", result.get("candidates", []))
            if items is not None:
                groups["relevant"].extend(items if isinstance(items, (list, tuple)) else [items])
            return groups
        if isinstance(result, (list, tuple)):
            groups["relevant"].extend(result)
            return groups
        raise ContextEngineError("invalid_retrieval_result")

    @staticmethod
    def _omissions(result: Any) -> list[Mapping[str, Any]]:
        if not isinstance(result, Mapping):
            return []
        raw = result.get("omissions", ())
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        return [item for item in values if isinstance(item, Mapping)]

    def _collapse_mandatory_render_duplicates(
        self,
        candidates: list[ContextCandidate],
    ) -> tuple[list[ContextCandidate], list[tuple[ContextCandidate, ContextCandidate, str]]]:
        """Collapse safe duplicate/update rules only for this rendered packet.

        Persistence remains owned by governance/reconciliation.  This is the
        emergency-safe read path that prevents temporary semantic duplicates
        from exhausting the mandatory budget before reconciliation can run.
        Conflicts and different resolved scopes are never collapsed.
        """

        kept: list[ContextCandidate] = []
        omitted: list[tuple[ContextCandidate, ContextCandidate, str]] = []
        for candidate in candidates:
            matched = False
            for index, existing in enumerate(kept):
                if self._scope_public(existing.scope) != self._scope_public(candidate.scope):
                    continue
                relation = classify_governance_relation(existing.body, candidate.body)
                if not relation.mergeable:
                    continue
                if relation.winner == "right":
                    kept[index] = candidate
                    omitted.append((
                        existing,
                        candidate,
                        "governance_update_shadowed" if relation.kind in {"update", "additive"} else "governance_duplicate",
                    ))
                else:
                    omitted.append((
                        candidate,
                        existing,
                        "governance_update_shadowed" if relation.kind in {"update", "additive"} else "governance_duplicate",
                    ))
                matched = True
                break
            if not matched:
                kept.append(candidate)
        kept.sort(key=lambda c: (-c.priority, c.item_id, c.digest, c.dedup_key))
        return kept, omitted

    def _ordered(self, candidates: list[ContextCandidate], request: ContextRequest) -> list[ContextCandidate]:
        if self.planner is None:
            return sorted(candidates, key=lambda c: (-c.priority, -c.score, c.item_id, c.digest, c.dedup_key))
        try:
            planner = self.planner
            method = getattr(planner, "plan", None)
            result = method(request, [candidate.public() for candidate in candidates]) if callable(method) else planner(request, [candidate.public() for candidate in candidates])
            if isinstance(result, Mapping):
                order = result.get("item_ids", result.get("candidate_ids", result.get("order", [])))
            elif isinstance(result, RetrievalPlan):
                order = result.item_ids
            else:
                order = result
            if not isinstance(order, (list, tuple)):
                return sorted(candidates, key=lambda c: (-c.priority, -c.score, c.item_id, c.digest, c.dedup_key))
            positions = {str(item_id): index for index, item_id in enumerate(order)}
            # Planner only reorders known IDs; it cannot create or elevate a
            # candidate into mandatory scope.
            return sorted(candidates, key=lambda c: (positions.get(c.item_id, len(positions)), -c.priority, -c.score, c.item_id, c.digest, c.dedup_key))
        except Exception:
            return sorted(candidates, key=lambda c: (-c.priority, -c.score, c.item_id, c.digest, c.dedup_key))

    @staticmethod
    def _scope_public(scope: Mapping[str, Any]) -> dict[str, Any]:
        public: dict[str, Any] = {}
        for key in (
            "target_type", "target_id", "agent", "agent_instance_id",
            "project", "project_ref", "group", "share_group_id",
            "provider", "runtime", "runtime_role", "workspace_id",
        ):
            if key in scope and isinstance(scope[key], (str, int, bool, float)):
                public[key] = scope[key]
        for key, aliases in {
            "project_ref": ("__top_project_ref", "__top_project", "__top_project_id"),
            "workspace_id": ("__top_workspace_id", "__top_workspace", "__top_workspace_path"),
            "agent_instance_id": ("__top_agent_instance_id", "__top_agent_id", "__top_agent"),
            "share_group_id": ("__top_share_group_id", "__top_group_id", "__top_group"),
            "runtime_role": ("__top_runtime_role", "__top_runtime"),
        }.items():
            if key in public:
                continue
            for alias in aliases:
                value = scope.get(alias)
                if isinstance(value, (str, int, bool, float)):
                    public[key] = value
                    break
        return public

    @staticmethod
    def _evidence_public(evidence: Any) -> dict[str, Any]:
        if evidence is None:
            return {}
        if isinstance(evidence, Mapping):
            result: dict[str, Any] = {}
            for key in ("ref", "evidence_ref", "id", "digest", "evidence_digest", "source"):
                if key in evidence and isinstance(evidence[key], (str, int, bool, float)):
                    result[key] = evidence[key]
            return result
        if isinstance(evidence, (str, int, float)):
            return {"ref": str(evidence)}
        return {}

    @staticmethod
    def _scope_alias(
        scope: Mapping[str, Any],
        names: tuple[str, ...],
        *,
        casefold: bool = False,
        normalizer: Any | None = None,
    ) -> tuple[str, bool]:
        values: list[str] = []
        for name in names:
            if name in scope and scope[name] is not None:
                value = _text(scope[name])
                if normalizer is not None:
                    value = normalizer(value)
                if casefold:
                    value = value.casefold()
                if value:
                    values.append(value)
        if len(set(values)) > 1:
            return "", True
        return (values[0] if values else ""), False

    @staticmethod
    def _scope_decision(candidate: ContextCandidate, request: ContextRequest) -> tuple[bool, str]:
        scope = candidate.scope
        if candidate.scope_invalid:
            return False, candidate.scope_reason or "scope_rejected"
        if not scope:
            return True, "included"
        identity = {
            "agent": (("agent", "agent_id", "agent_instance_id", "actor_agent_id", "__top_agent", "__top_agent_id", "__top_agent_instance_id"), request.agent),
            "project": (("project", "project_id", "project_ref", "__top_project", "__top_project_id", "__top_project_ref"), request.project),
            "group": (("group", "group_id", "share_group_id", "__top_group", "__top_group_id", "__top_share_group_id"), request.group),
            "provider": (("provider", "__top_provider"), request.provider),
            "runtime": (("runtime", "runtime_role", "__top_runtime", "__top_runtime_role"), request.runtime),
        }
        for _key, (aliases, expected) in identity.items():
            normalizer = canonical_project_ref if _key == "project" else None
            supplied, conflict = ContextEngine._scope_alias(scope, aliases, normalizer=normalizer)
            if conflict:
                return False, "scope_alias_conflict"
            if normalizer is not None:
                expected = normalizer(expected)
            if supplied and expected and supplied != expected:
                return False, "scope_mismatch"
            if supplied and not expected:
                return False, "scope_mismatch"
        workspace, workspace_conflict = ContextEngine._scope_alias(
            scope,
            ("workspace_id", "workspace", "workspace_path", "__top_workspace", "__top_workspace_id", "__top_workspace_path"),
        )
        if workspace_conflict:
            return False, "scope_alias_conflict"
        if workspace and (not request.workspace_id or workspace != request.workspace_id):
            return False, "scope_mismatch"
        target_type, type_conflict = ContextEngine._scope_alias(scope, ("target_type", "type", "scope_type", "__top_target_type", "__top_type", "__top_scope_type"), casefold=True)
        target_id, id_conflict = ContextEngine._scope_alias(scope, ("target_id", "id", "scope_id", "__top_target_id", "__top_id", "__top_scope_id"))
        nested_target = scope.get("target")
        if nested_target is not None:
            if not isinstance(nested_target, Mapping):
                return False, "scope_shape_rejected"
            nested_type, nested_type_conflict = ContextEngine._scope_alias(nested_target, ("target_type", "type", "scope_type"), casefold=True)
            nested_id, nested_id_conflict = ContextEngine._scope_alias(nested_target, ("target_id", "id", "scope_id"))
            if nested_type_conflict or nested_id_conflict:
                return False, "scope_alias_conflict"
            if nested_type and target_type and nested_type.casefold() != target_type.casefold():
                return False, "scope_alias_conflict"
            if nested_id and target_id and nested_id != target_id:
                return False, "scope_alias_conflict"
            target_type = target_type or nested_type
            target_id = target_id or nested_id
        if type_conflict or id_conflict:
            return False, "scope_alias_conflict"
        target_type = target_type.casefold()
        if target_type in {"agent", "agent_instance"}:
            return (bool(request.agent and target_id == request.agent), "included" if request.agent and target_id == request.agent else "scope_mismatch")
        if target_type == "project":
            target_project = canonical_project_ref(target_id)
            request_project = canonical_project_ref(request.project)
            return (bool(request_project and target_project == request_project), "included" if request_project and target_project == request_project else "scope_mismatch")
        if target_type in {"group", "share_group"}:
            return (bool(request.group and target_id == request.group), "included" if request.group and target_id == request.group else "scope_mismatch")
        if target_type == "provider":
            return (bool(request.provider and target_id == request.provider), "included" if request.provider and target_id == request.provider else "scope_mismatch")
        if target_type in {"runtime", "runtime_role"}:
            return (bool(request.runtime and target_id == request.runtime), "included" if request.runtime and target_id == request.runtime else "scope_mismatch")
        if target_type in {"system", "global"}:
            return (not target_id, "included" if not target_id else "scope_target_invalid")
        if not target_type:
            return (not target_id, "included" if not target_id else "scope_target_invalid")
        return False, "scope_target_invalid"

    @staticmethod
    def _scope_allowed(candidate: ContextCandidate, request: ContextRequest) -> bool:
        return ContextEngine._scope_decision(candidate, request)[0]

    def _safe_text(self, candidate: ContextCandidate) -> tuple[str, int, bool]:
        text = candidate.body
        if not text:
            return "", 0, False
        chars = len(text)
        tokens = self._count(text)
        if chars <= self._active_budget.item_max_chars and tokens <= self._active_budget.item_max_tokens:
            return text, tokens, False
        # Binary-search a deterministic prefix satisfying both item limits.
        limit = min(len(text), self._active_budget.item_max_chars)
        lo, hi = 0, limit
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._count(text[:mid]) <= self._active_budget.item_max_tokens:
                lo = mid
            else:
                hi = mid - 1
        clipped = text[:lo]
        return clipped, self._count(clipped), True

    @staticmethod
    def _is_sensitive(candidate: ContextCandidate) -> bool:
        # Use the shared production detector for unmarked historical rows;
        # retain the narrow legacy key forms for compatibility with already
        # persisted V2 candidates.  The body is never copied into the blocked
        # packet or receipt.
        return (
            candidate.sensitive
            or contains_sensitive_content(candidate.body)
            or bool(_SECRET_RE.search(candidate.body))
        )

    def _render(self, candidate: ContextCandidate, text: str, *, layer: str, truncated: bool = False) -> dict[str, Any]:
        if layer == "reference_only":
            payload: dict[str, Any] = {"summary": text, "hash": candidate.content_hash or candidate.digest, "trust": "reference_only"}
            if candidate.reference:
                payload["ref"] = candidate.reference
            return payload
        payload: dict[str, Any] = {
            "item_id": candidate.item_id,
            "memory_id": candidate.memory_id or candidate.item_id,
            "body": text,
            "kind": candidate.kind,
            "layer": layer,
            "source": candidate.source,
            "scope": self._scope_public(candidate.scope),
            "evidence": self._evidence_public(candidate.evidence),
            "digest": candidate.digest,
            "injection_policy": candidate.injection_policy or ("always" if layer == "mandatory" else "relevant"),
            "priority": candidate.priority,
        }
        if truncated:
            payload["truncated"] = True
        return payload

    def bootstrap(self, request: ContextRequest | Mapping[str, Any], candidates: Any | None = None) -> ContextPacket:
        req = ContextRequest.from_mapping(request)
        active_budget = self._request_budget(req)
        self._active_budget = active_budget
        ledger = BudgetLedger()
        receipts: list[ContextReceipt] = []
        if self.state == "UNKNOWN":
            return ContextPacket(
                budget={"limits": active_budget.to_dict()},
                effective_agent=req.effective_agent,
                ready=False,
                state=self.state,
                status="blocked",
                error="unknown_runtime_state",
            )
        if not req.effective_agent:
            return ContextPacket(
                budget={"limits": active_budget.to_dict()},
                ready=False,
                state=self.state,
                status="blocked",
                error="missing_trusted_identity",
            )
        try:
            retrieved = self._retrieve(req, candidates)
            groups_raw = self._groups(retrieved)
            groups: dict[str, list[ContextCandidate]] = {layer: [] for layer in groups_raw}
            index = 0
            allowed_omission_reasons = {
                "scope_omitted", "knowledge_scope_required", "history_scope_required",
                "codegraph_scope_required", "knowledge_source_unavailable",
                "history_source_unavailable", "codegraph_source_unavailable",
                "history_unsummarized", "retrieval_omitted",
            }
            for omission in self._omissions(retrieved):
                reason = _text(omission.get("reason"))
                if reason not in allowed_omission_reasons:
                    reason = "retrieval_omitted"
                layer = _text(omission.get("layer")).casefold()
                if layer not in {"mandatory", "relevant", "knowledge", "reference_only"}:
                    layer = "reference_only"
                # Omission receipts intentionally carry no source-controlled
                # identifier, scope, or evidence.  The reason is a bounded
                # public enum and cannot become a data oracle.
                receipts.append(ContextReceipt("", layer, False, reason, {}, {}))
                ledger.omit(reason)
            for layer, values in groups_raw.items():
                for value in values:
                    if layer not in {"mandatory", "relevant", "knowledge", "reference_only"}:
                        opaque_id = _text(value.get("id", value.get("item_id", ""))) if isinstance(value, Mapping) else ""
                        receipts.append(ContextReceipt(opaque_id, "unknown", False, "unknown_layer", {}, {}))
                        ledger.omit("unknown_layer")
                        continue
                    candidate = ContextCandidate.from_value(value, layer=layer, index=index)
                    index += 1
                    if candidate.layer_invalid:
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "unknown_layer", {}, {}))
                        ledger.omit("unknown_layer")
                        continue
                    if candidate.lifecycle_invalid:
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, candidate.lifecycle_reason or "lifecycle_status_rejected", {}, {}))
                        ledger.omit("lifecycle_rejected")
                        continue
                    policy = candidate.injection_policy
                    if policy and policy not in {"always", "relevant"}:
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "injection_policy_invalid", {}, {}))
                        raise ContextSafetyError("injection_policy_invalid")
                    if policy == "always" and layer != "mandatory":
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "injection_policy_layer_conflict", {}, {}))
                        raise ContextSafetyError("injection_policy_layer_conflict")
                    if policy == "relevant" and layer == "mandatory":
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "injection_policy_layer_conflict", {}, {}))
                        raise ContextSafetyError("injection_policy_layer_conflict")
                    # Never inject raw history or tool output, regardless of
                    # planner/retriever labels.
                    source = candidate.source.casefold()
                    if candidate.raw_history or candidate.tool_output or source in _RAW_SOURCE_NAMES:
                        if layer == "mandatory":
                            receipts.append(ContextReceipt(candidate.item_id, layer, False, "mandatory_content_blocked", {}, {}))
                            raise ContextSafetyError("mandatory_content_blocked")
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "raw_source_blocked", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence)))
                        ledger.omit("raw_source_blocked")
                        continue
                    if candidate.unsafe_payload:
                        if layer == "mandatory":
                            receipts.append(ContextReceipt(candidate.item_id, layer, False, "mandatory_content_blocked", {}, {}))
                            raise ContextSafetyError("mandatory_content_blocked")
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "unsafe_payload", {}, {}))
                        ledger.omit("unsafe_payload")
                        continue
                    scope_allowed, scope_reason = self._scope_decision(candidate, req)
                    if not scope_allowed:
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, scope_reason, {}, {}))
                        ledger.omit(scope_reason if scope_reason in {"scope_mismatch", "scope_alias_conflict", "scope_shape_rejected", "scope_target_invalid"} else "scope_mismatch")
                        continue
                    # A non-rule item can never enter mandatory.  Demote to
                    # relevant so it remains recallable without authority.
                    if layer == "mandatory" and not candidate.is_rule:
                        if policy == "always":
                            receipts.append(ContextReceipt(candidate.item_id, layer, False, "mandatory_policy_requires_rule", {}, {}))
                            raise ContextSafetyError("mandatory_policy_requires_rule")
                        candidate = replace(candidate, layer="relevant")
                        layer = "relevant"
                    groups.setdefault(layer, []).append(candidate)

            # Planner may only reorder known optional candidates; mandatory
            # rules remain priority/digest ordered and cannot be elevated.
            for layer in ("relevant", "knowledge", "reference_only"):
                groups[layer] = self._ordered(groups[layer], req)
            groups["mandatory"] = sorted(groups["mandatory"], key=lambda c: (-c.priority, c.item_id, c.digest, c.dedup_key))
            groups["mandatory"], render_omissions = self._collapse_mandatory_render_duplicates(
                groups["mandatory"]
            )
            for omitted_candidate, winner_candidate, reason in render_omissions:
                receipts.append(ContextReceipt(
                    omitted_candidate.item_id,
                    "mandatory",
                    False,
                    reason,
                    self._scope_public(omitted_candidate.scope),
                    self._evidence_public(omitted_candidate.evidence),
                ))
                ledger.omit(reason)

            selected: dict[str, list[dict[str, Any]]] = {layer: [] for layer in groups}
            seen: set[str] = set()
            seen_bodies: set[str] = set()
            for candidate in groups["mandatory"]:
                if candidate.dedup_key in seen or (not candidate.has_governance_semantics and candidate.digest in seen_bodies):
                    receipts.append(ContextReceipt(candidate.item_id, "mandatory", False, "duplicate", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence)))
                    ledger.omit("duplicate")
                    continue
                if self._is_sensitive(candidate):
                    receipts.append(ContextReceipt(candidate.item_id, "mandatory", False, "mandatory_sensitive_blocked", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence)))
                    raise ContextSafetyError("mandatory_sensitive_blocked")
                text, token_cost, truncated = self._safe_text(candidate)
                char_cost = len(text)
                if not text:
                    receipts.append(ContextReceipt(candidate.item_id, "mandatory", False, "mandatory_content_blocked", {}, {}))
                    raise ContextSafetyError("mandatory_content_blocked")
                if truncated or active_budget.mandatory_item_oversize(len(candidate.body), self._count(candidate.body)):
                    raise ContextSafetyError("mandatory_item_limit_exceeded")
                if active_budget.mandatory_aggregate_overflow(
                    ledger.mandatory_chars + char_cost,
                    ledger.mandatory_tokens + token_cost,
                ):
                    raise ContextSafetyError("mandatory_budget_exceeded")
                selected["mandatory"].append(self._render(candidate, text, layer="mandatory"))
                seen.add(candidate.dedup_key)
                seen_bodies.add(candidate.digest)
                ledger.mandatory_items += 1
                ledger.mandatory_chars += char_cost
                ledger.mandatory_tokens += token_cost
                receipts.append(ContextReceipt(candidate.item_id, "mandatory", True, "included", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence), token_cost, char_cost))
            ledger.warn(active_budget.item_count_warning(ledger.mandatory_items))

            for layer in ("relevant", "knowledge", "reference_only"):
                for candidate in groups[layer]:
                    if candidate.dedup_key in seen or (not candidate.has_governance_semantics and candidate.digest in seen_bodies):
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "duplicate", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence)))
                        ledger.omit("duplicate")
                        continue
                    if self._is_sensitive(candidate):
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "sensitive_blocked", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence)))
                        ledger.omit("sensitive_blocked")
                        continue
                    if ledger.optional_items >= active_budget.max_items:
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "item_budget", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence)))
                        ledger.omit("item_budget")
                        continue
                    text, token_cost, truncated = self._safe_text(candidate)
                    if not text and not (layer == "reference_only" and (candidate.reference or candidate.content_hash)):
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "empty", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence)))
                        ledger.omit("empty")
                        continue
                    char_cost = len(text)
                    if ledger.optional_chars + char_cost > active_budget.max_chars or ledger.optional_tokens + token_cost > active_budget.max_tokens:
                        receipts.append(ContextReceipt(candidate.item_id, layer, False, "budget", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence), token_cost, char_cost))
                        ledger.omit("budget")
                        continue
                    selected[layer].append(self._render(candidate, text, layer=layer, truncated=truncated))
                    seen.add(candidate.dedup_key)
                    seen_bodies.add(candidate.digest)
                    ledger.optional_items += 1
                    ledger.optional_chars += char_cost
                    ledger.optional_tokens += token_cost
                    receipts.append(ContextReceipt(candidate.item_id, layer, True, "included", self._scope_public(candidate.scope), self._evidence_public(candidate.evidence), token_cost, char_cost))

            return ContextPacket(
                mandatory=tuple(selected["mandatory"]),
                relevant=tuple(selected["relevant"]),
                knowledge=tuple(selected["knowledge"]),
                reference_only=tuple(selected["reference_only"]),
                budget={**ledger.to_dict(), "limits": active_budget.to_dict()},
                effective_agent=req.effective_agent,
                receipts=tuple(receipt.to_dict() for receipt in receipts),
                ready=self.ready and self.state == "V2_ACTIVE",
                state=self.state,
                status="ok" if self.ready and self.state == "V2_ACTIVE" else "shadow",
            )
        except ContextSafetyError as exc:
            return ContextPacket(
                budget={**ledger.to_dict(), "limits": active_budget.to_dict()},
                effective_agent=req.effective_agent,
                receipts=tuple(receipt.to_dict() for receipt in receipts),
                ready=False,
                state=self.state,
                status="blocked",
                error=str(exc),
            )
        except (ContextEngineError, ContextBudgetError) as exc:
            return ContextPacket(
                budget={**ledger.to_dict(), "limits": active_budget.to_dict()},
                effective_agent=req.effective_agent,
                receipts=tuple(receipt.to_dict() for receipt in receipts),
                ready=False,
                state=self.state,
                status="blocked",
                error=str(exc),
            )
        except Exception:
            # Retrieval/planner implementations are untrusted ports.  Never
            # leak their exception or partial raw payload into the packet.
            return ContextPacket(
                budget={**ledger.to_dict(), "limits": active_budget.to_dict()},
                effective_agent=req.effective_agent,
                receipts=tuple(receipt.to_dict() for receipt in receipts),
                ready=False,
                state=self.state,
                status="blocked",
                error="context_build_failed",
            )

    build_context = bootstrap
    build_context_packet = bootstrap
    context_bootstrap = bootstrap
    build = bootstrap
    __call__ = bootstrap


__all__ = [
    "ContextEngineError", "RetrievalPort", "RetrievalProtocol", "PlannerPort", "PlannerProtocol", "RetrievalPlan",
    "ContextRequest", "ContextCandidate", "ContextReceipt", "ContextPacket", "ContextEngine",
    "ContextBudget", "ContextSafetyError", "TokenCounter", "DeterministicTokenCounter",
]
