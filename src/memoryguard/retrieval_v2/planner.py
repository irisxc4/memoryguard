"""Deterministic, scope-first V2 recall planner.

This module is intentionally storage agnostic.  Every layer enters through a
read-only :class:`~memoryguard.retrieval_v2.ports.LayerPort`; the planner does
not import V1 stores, open SQLite, or mutate state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import math
import re
from typing import Any

from .models import RecallDecision, RecallPlan, RecallRequest, RecallScope, stable_digest
from .ports import LayerPort
from ..runtime_v2.context_budget import ContextBudget, DeterministicTokenCounter


_LAYER_ORDER = (
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
_FUTURE_LAYERS = frozenset({"knowledge", "codegraph", "skill"})
_REFERENCE_LAYERS = frozenset({"content_reference", "history", "knowledge", "codegraph", "skill"})
_RELEVANT_LAYERS = frozenset({"working", "atoms", "scenario", "profile"})
_ALLOWED_LAYERS = frozenset({
    "working",
    "rules",
    "atoms",
    "scenario",
    "profile",
    "content_reference",
    "content",
    "history",
    "knowledge",
    "codegraph",
    "skill",
})
_TRUST_RANK = {"mandatory": 4, "enforceable": 3, "relevant": 2, "reference_only": 1}
_DENY_STATUS = frozenset({
    "deleted",
    "tombstone",
    "tombstoned",
    "invalid",
    "superseded",
    "conflict",
    "conflicted",
    "locked",
    "quarantine",
    "quarantined",
    "orphaned",
    "inactive",
    "disabled",
    "blocked",
    "expired",
    "building",
    "not_ready",
})
_RAW_KEYS = frozenset({
    "body",
    "text",
    "raw",
    "transcript",
    "content",
    "full_text",
    "document",
    "payload",
    "conversation",
    "source_text",
})
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


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


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
        except Exception:
            return None
        return dict(result) if isinstance(result, Mapping) else None
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        try:
            result = as_dict()
        except Exception:
            return None
        return dict(result) if isinstance(result, Mapping) else None
    if hasattr(value, "__dict__"):
        return dict(getattr(value, "__dict__", {}) or {})
    return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "on", "1"}
    return bool(value)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _bounded(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, _number(value, low)))


def _tokens(value: Any) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(_text(value)) if token}


def _parse_recency(value: Any) -> float:
    if value is None:
        return 0.0
    number = _number(value, 0.0)
    if number:
        # Values in [0, 1] are already normalized.  Larger values remain
        # absolute for deterministic ordering (no wall-clock dependence).
        return number
    raw = _text(value)
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _opaque_id(layer: str, item: Mapping[str, Any]) -> str:
    identity: dict[str, Any] = {"layer": layer}
    for key in (
        "item_id",
        "id",
        "memory_id",
        "atom_id",
        "evidence_id",
        "source_id",
        "canonical_hash",
        "digest",
        "workspace_id",
        "share_group_id",
        "group_id",
        "agent_instance_id",
        "project_ref",
        "provider",
        "runtime_role",
    ):
        if key in item and item.get(key) is not None:
            identity[key] = _text(item.get(key))
    return f"{layer}:opaque-{stable_digest(identity)[:24]}"


def _item_id(item: Mapping[str, Any], layer: str) -> str:
    for key in (
        "item_id",
        "id",
        "memory_id",
        "atom_id",
        "evidence_id",
        "definition_id",
        "source_id",
        "record_id",
        "key",
    ):
        value = _text(item.get(key))
        if value:
            return value
    return _opaque_id(layer, item)


def _scope_mapping(item: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten candidate scope, marking all alias/target disagreements.

    Aliases are not precedence rules.  Any two non-empty representations that
    disagree set an internal marker; caller then returns opaque scope denial.
    """

    result: dict[str, Any] = {}
    conflict = False
    sources: list[Mapping[str, Any]] = []
    seen_sources: set[int] = set()

    def collect(source: Mapping[str, Any], *, record: bool = False) -> None:
        nonlocal conflict
        marker = id(source)
        if marker in seen_sources:
            conflict = True
            return
        seen_sources.add(marker)
        # Explicit scope/audience containers must be mappings.  A string such
        # as ``scope='agent:other'`` is not a safe shorthand: deny it instead
        # of treating it as absent (which would widen access).
        for container_name in ("scope", "audience"):
            if container_name not in source:
                continue
            nested_value = source.get(container_name)
            if not isinstance(nested_value, Mapping):
                conflict = True
            else:
                collect(nested_value)
        sources.append(source)

    collect(item, record=True)
    # Recursive collector appends outer record after nested mappings.  Keep
    # direct record last so generic top-level ``type``/``id`` remain excluded.
    if sources and sources[-1] is not item:
        sources.append(item)
    aliases = {
        "workspace_id": ("workspace_id", "workspace"),
        "share_group_id": ("share_group_id", "group_id", "group"),
        "agent_instance_id": ("agent_instance_id", "agent"),
        "project_ref": ("project_ref", "project"),
        "provider": ("provider",),
        "runtime_role": ("runtime_role", "runtime"),
    }
    for canonical, names in aliases.items():
        values = []
        for source in sources:
            for name in names:
                if name in source and _text(source.get(name)):
                    values.append(_text(source.get(name)))
        if len(set(values)) > 1:
            conflict = True
        if values:
            result[canonical] = values[0]

    target_types: list[str] = []
    target_ids: list[str] = []
    for index, source in enumerate(sources):
        # ``type``/``id`` are accepted only inside an explicit scope/audience
        # mapping.  A top-level record ``type`` is commonly a content kind and
        # must not accidentally become an authorization target.
        is_direct_record = source is item
        target_keys = ("target_type", "type", "scope_type") if not is_direct_record else ("target_type", "scope_type")
        for key in target_keys:
            if key in source and _text(source.get(key)):
                target_types.append(_text(source.get(key)).lower())
        id_keys = ("target_id", "id", "scope_id") if not is_direct_record else ("target_id", "scope_id")
        for key in id_keys:
            if key in source and _text(source.get(key)):
                target_ids.append(_text(source.get(key)))
    if len(set(target_types)) > 1 or len(set(target_ids)) > 1:
        conflict = True
    target_type = target_types[0] if target_types else ""
    target_id = target_ids[0] if target_ids else ""
    target_aliases = {
        "agent": "agent_instance_id",
        "agent_instance": "agent_instance_id",
        "project": "project_ref",
        "group": "share_group_id",
        "share_group": "share_group_id",
        "provider": "provider",
        "runtime": "runtime_role",
        "runtime_role": "runtime_role",
    }
    if target_type:
        if target_type not in target_aliases or not target_id:
            conflict = True
        else:
            target_key = target_aliases[target_type]
            if result.get(target_key) and result[target_key] != target_id:
                conflict = True
            result.setdefault(target_key, target_id)
    elif target_id:
        # Target ID without a typed audience cannot be safely scoped.
        conflict = True
    result["__scope_conflict__"] = conflict
    return result


def _safe_evidence_refs(item: Mapping[str, Any]) -> tuple[str, ...]:
    refs: list[str] = []
    for key in ("evidence_refs", "evidence_ids", "evidence", "provenance_refs", "provenance"):
        value = item.get(key)
        if isinstance(value, Mapping):
            extracted = []
            for ref_key in ("evidence_id", "id", "ref", "evidence_ref", "digest", "evidence_digest"):
                if ref_key in value:
                    extracted.append(value.get(ref_key))
            value = extracted
        if isinstance(value, (str, bytes)):
            value = (value,)
        if isinstance(value, Iterable):
            for ref in value:
                if isinstance(ref, Mapping):
                    ref = ref.get("evidence_id") or ref.get("id") or ref.get("ref")
                value_text = _text(ref)
                if value_text:
                    refs.append(value_text)
    return tuple(sorted(set(refs)))


def _safe_summary(item: Mapping[str, Any], layer: str) -> str:
    for key in ("summary", "snippet", "title", "label", "description"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:2048]
    # History/content/reference layers must never expose source body.  Other
    # layers may expose a bounded atom/rule summary if adapter did not provide
    # one explicitly; still cap it to keep planner output bounded.
    if layer not in _REFERENCE_LAYERS:
        for key in ("body", "text", "value"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:2048]
    return ""


def _safe_metadata(item: Mapping[str, Any], layer: str, status: str) -> dict[str, Any]:
    allowed = (
        "kind",
        "category",
        "source_type",
        "injection_policy",
        "canonical_hash",
        "content_hash",
        "hash",
        "digest",
    )
    result: dict[str, Any] = {}
    for key in allowed:
        value = item.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value not in (None, ""):
                result[key] = value
    if layer in _REFERENCE_LAYERS:
        # Keep references opaque: no source_ref/path/metadata projection.
        result.pop("source_type", None)
    result["status"] = status
    return result


def _source_digest(item: Mapping[str, Any], layer: str, item_id: str, summary: str) -> str:
    for key in ("source_digest", "canonical_hash", "content_hash", "digest", "hash"):
        value = _text(item.get(key))
        if value:
            return value
    return stable_digest({"layer": layer, "item_id": item_id, "summary": summary})


@dataclass(frozen=True)
class _Candidate:
    layer: str
    item_id: str
    dedup_key: str
    trust: str
    summary: str
    status: str
    evidence_refs: tuple[str, ...]
    source_digest: str
    metadata: Mapping[str, Any]
    confidence: float
    relevance: float
    recency: float
    score: float
    raw_id: str = ""
    exclusion: str = ""

    @property
    def rank_key(self) -> tuple[Any, ...]:
        return (
            -_TRUST_RANK[self.trust],
            -self.score,
            -self.relevance,
            -self.confidence,
            -self.recency,
            _LAYER_ORDER.index(self.layer) if self.layer in _LAYER_ORDER else len(_LAYER_ORDER),
            self.item_id,
        )


class RecallPlanner:
    """Build auditable recall plans from injected layer ports."""

    def __init__(self, ports: Mapping[str, LayerPort] | Iterable[LayerPort] | None = None) -> None:
        normalized: dict[str, LayerPort] = {}
        invalid: dict[str, str] = {}
        if isinstance(ports, Mapping):
            values = ports.items()
        else:
            values = ((getattr(port, "layer", ""), port) for port in (ports or ()))
        for key, port in values:
            declared_key = _layer_name(key) if key else ""
            declared_port = _layer_name(getattr(port, "layer", key))
            if declared_key and declared_port and declared_key != declared_port:
                invalid[f"{declared_key}|{declared_port}"] = "LAYER_MISMATCH"
                continue
            layer = declared_port or declared_key
            if not layer:
                invalid[str(key)] = "UNKNOWN_LAYER"
                continue
            if layer not in _ALLOWED_LAYERS:
                invalid[layer] = "UNKNOWN_LAYER"
                continue
            # First registration wins; duplicate ports would otherwise make
            # output depend on injection order and can widen authority.
            normalized.setdefault(layer, port)
        self._ports = normalized
        self._invalid_ports = dict(sorted(invalid.items()))

    @property
    def ports(self) -> Mapping[str, LayerPort]:
        return dict(self._ports)

    @property
    def diagnostics(self) -> Mapping[str, str]:
        """Rejected adapter/layer markers; unknown ports never execute."""

        return dict(self._invalid_ports)

    def plan(self, request: RecallRequest | Mapping[str, Any]) -> RecallPlan:
        req = RecallRequest.from_value(request)
        layer_status: dict[str, str] = {}
        raw_candidates: list[_Candidate] = []
        audit_excluded: list[RecallDecision] = []

        for layer in req.layers:
            if layer not in _ALLOWED_LAYERS:
                layer_status[layer] = "UNKNOWN_LAYER"
                audit_excluded.append(self._layer_decision(layer, "UNKNOWN_LAYER", "UNKNOWN_LAYER"))
                continue
            if layer == "history" and not req.include_history:
                layer_status[layer] = "DISABLED"
                audit_excluded.append(self._layer_decision(layer, "DISABLED", "history_disabled"))
                continue
            port = self._ports.get(layer)
            if port is None or not _bool(getattr(port, "configured", True)) or _text(getattr(port, "status", "READY")).upper() in {
                "NOT_CONFIGURED",
                "UNCONFIGURED",
                "DISABLED",
            }:
                layer_status[layer] = "NOT_CONFIGURED"
                audit_excluded.append(self._layer_decision(layer, "NOT_CONFIGURED", "NOT_CONFIGURED"))
                continue
            layer_status[layer] = "READY"
            try:
                values = self._read_port(port, req)
                for raw in values:
                    candidate, denied = self._normalize_candidate(raw, layer, req)
                    if denied is not None:
                        audit_excluded.append(denied)
                    elif candidate is not None:
                        raw_candidates.append(candidate)
            except Exception:
                # A port failure cannot expose partial payloads.  Keep generic
                # audit marker; caller can inspect adapter diagnostics.
                layer_status[layer] = "ERROR"
                audit_excluded.append(self._layer_decision(layer, "ERROR", "port_error"))
                continue

        # Dedupe before scoring/budgeting.  Scope/status filtering happened in
        # _normalize_candidate, so duplicates cannot cross a security boundary.
        grouped: dict[str, list[_Candidate]] = {}
        for candidate in raw_candidates:
            grouped.setdefault(candidate.dedup_key, []).append(candidate)
        deduped: list[_Candidate] = []
        for key in sorted(grouped):
            values = sorted(grouped[key], key=lambda item: item.rank_key)
            winner = values[0]
            deduped.append(winner)
            for loser in values[1:]:
                audit_excluded.append(
                    self._decision(
                        loser,
                        action="exclude",
                        reason=f"duplicate_of:{winner.item_id}",
                    )
                )

        ranked = sorted(deduped, key=lambda item: item.rank_key)
        mandatory = [item for item in ranked if item.trust == "mandatory"]
        # Mandatory and optional recall have independent budgets.  The request
        # budget belongs only to optional candidates; mandatory safety limits
        # come from the single runtime source of truth.
        context_budget = ContextBudget()
        token_counter = DeterministicTokenCounter()
        mandatory_chars = sum(len(item.summary) for item in mandatory)
        mandatory_tokens = sum(token_counter.count(item.summary) for item in mandatory)
        mandatory_item_overflow = any(
            context_budget.mandatory_item_oversize(
                len(item.summary), token_counter.count(item.summary),
            )
            for item in mandatory
        )
        mandatory_overflow = mandatory_item_overflow or context_budget.mandatory_aggregate_overflow(
            mandatory_chars, mandatory_tokens,
        )
        selected: list[_Candidate] = []
        decisions: list[RecallDecision] = []
        warnings: list[dict[str, Any]] = []
        if mandatory_overflow:
            for item in ranked:
                decisions.append(
                    self._decision(
                        item,
                        action="exclude",
                        reason="mandatory_budget_overflow" if item.trust == "mandatory" else "budget_blocked_by_mandatory",
                    )
                )
            status = "blocked"
            reason = "mandatory_budget_overflow"
        else:
            selected.extend(mandatory)
            optional_count = 0
            optional_chars = 0
            for item in ranked:
                if item.trust == "mandatory":
                    decisions.append(self._decision(item, action="include", reason="selected_mandatory"))
                    continue
                if optional_count >= req.budget_items:
                    decisions.append(self._decision(item, action="exclude", reason="budget"))
                    continue
                if optional_chars + len(item.summary) > req.budget_chars:
                    decisions.append(self._decision(item, action="exclude", reason="budget"))
                    continue
                selected.append(item)
                optional_count += 1
                optional_chars += len(item.summary)
                decisions.append(self._decision(item, action="include", reason="selected"))
            status = "ok"
            reason = ""
            warning = context_budget.item_count_warning(len(mandatory))
            if warning:
                warnings.append(warning)

        # Deterministic audit order: selected/ranked decisions first, then
        # scope/status/dedupe/port exclusions sorted by stable fields.
        decisions.extend(audit_excluded)
        decisions.sort(key=lambda decision: (
            0 if decision.action == "include" else 1,
            _LAYER_ORDER.index(decision.layer) if decision.layer in _LAYER_ORDER else len(_LAYER_ORDER),
            decision.item_id,
            decision.reason,
        ))
        selected_decisions = tuple(decision for decision in decisions if decision.action == "include")
        counts = {
            "candidates": len(raw_candidates),
            "deduped": len(deduped),
            "selected": len(selected_decisions),
            "excluded": len(decisions) - len(selected_decisions),
            "mandatory": len(mandatory),
        }
        if any(value == "ERROR" for value in layer_status.values()) and status == "ok":
            status = "blocked"
            reason = "port_error"
        return RecallPlan(
            request_id=req.request_id,
            scope=req.scope,
            decisions=tuple(decisions),
            selected=selected_decisions,
            status=status,
            reason=reason,
            mandatory_overflow=mandatory_overflow,
            layer_status=layer_status,
            counts=counts,
            warnings=tuple(warnings),
        )

    # Common aliases used by embedding callers.
    build = plan
    recall = plan
    make_plan = plan

    @staticmethod
    def _read_port(port: LayerPort, request: RecallRequest) -> Iterable[Any]:
        reader = getattr(port, "read", None)
        if callable(reader):
            values = reader(request)
        else:
            reader = (
                getattr(port, "retrieve", None)
                or getattr(port, "list", None)
                or getattr(port, "read_candidates", None)
            )
            if callable(reader):
                values = reader(request)
            elif callable(port):
                values = port(request)
            else:
                raise TypeError("layer port has no read method")
        if values is None:
            return ()
        if isinstance(values, Mapping) or isinstance(values, (str, bytes)):
            return (values,)
        return values

    @staticmethod
    def _layer_decision(layer: str, marker: str, reason: str) -> RecallDecision:
        return RecallDecision(
            item_id=f"{layer}:{marker}",
            layer=layer,
            action="exclude",
            trust="reference_only",
            reason=reason,
            source_digest=stable_digest({"layer": layer, "marker": marker}),
            metadata={"status": marker},
        )

    def _normalize_candidate(
        self,
        raw: Any,
        layer: str,
        request: RecallRequest,
    ) -> tuple[_Candidate | None, RecallDecision | None]:
        item = _mapping(raw)
        if item is None:
            return None, self._layer_decision(layer, "invalid", "invalid_candidate")
        item_id = _item_id(item, layer)
        scope_data = _scope_mapping(item)
        if scope_data.get("__scope_conflict__") or not request.scope.matches(scope_data):
            # Scope denial is existence-neutral: no source ID, body, summary,
            # metadata, or evidence refs leave this branch.
            opaque = _opaque_id(layer, item)
            return None, RecallDecision(
                item_id=opaque,
                layer=layer,
                action="exclude",
                trust="reference_only",
                reason="scope_denied",
                source_digest=stable_digest({"layer": layer, "opaque": opaque}),
            )

        status_values = []
        for key in ("status", "state", "lifecycle", "lifecycle_status"):
            value = _text(item.get(key)).lower()
            if value:
                status_values.append(value)
        status = status_values[0] if status_values else "valid"
        denied_status = next((value for value in status_values if value in _DENY_STATUS), "")
        bool_statuses = (
            ("deleted", "deleted"),
            ("tombstone", "tombstone"),
            ("tombstoned", "tombstoned"),
            ("superseded", "superseded"),
            ("quarantined", "quarantine"),
            ("quarantine", "quarantine"),
            ("conflict", "conflict"),
            ("conflicted", "conflict"),
            ("has_conflict", "conflict"),
            ("locked", "locked"),
        )
        denied_bool = next((reason for key, reason in bool_statuses if _bool(item.get(key))), "")
        if denied_status or denied_bool:
            return None, self._denied(item, layer, item_id, status, denied_status or denied_bool)

        summary = _safe_summary(item, layer)
        source_digest = _source_digest(item, layer, item_id, summary)
        trust, trust_reason = self._trust(item, layer)
        confidence = _bounded(item.get("confidence", item.get("certainty", 0.0)))
        relevance = self._relevance(request.query, item, summary)
        explicit_score = _bounded(item.get("score", item.get("relevance_score", 0.0)), 0.0, 1.0)
        recency = _parse_recency(item.get("recency_score", item.get("recency", item.get("updated_at", item.get("created_at")))))
        recency_component = recency if 0.0 <= recency <= 1.0 else 1.0 - (1.0 / (1.0 + max(0.0, recency)))
        score = _bounded(0.45 * explicit_score + 0.35 * relevance + 0.15 * confidence + 0.05 * recency_component)
        dedup_value = _text(item.get("canonical_hash") or item.get("semantic_hash") or item.get("digest") or item.get("content_hash"))
        if not dedup_value:
            # Stable IDs are identity-level dedupe keys when an adapter has no
            # canonical content hash.  This also collapses one atom projected
            # by multiple layers without depending on port order.
            dedup_value = f"id:{item_id}"
        candidate = _Candidate(
            layer=layer,
            item_id=item_id,
            # Canonical hashes intentionally dedupe across layers.  A memory
            # atom and a content reference carrying the same digest represent
            # one recall fact, with trust/rank deciding the surviving layer.
            dedup_key=dedup_value,
            trust=trust,
            summary=summary,
            status=status,
            evidence_refs=_safe_evidence_refs(item),
            source_digest=source_digest,
            metadata=_safe_metadata(item, layer, status),
            confidence=confidence,
            relevance=relevance,
            recency=recency,
            score=score,
            raw_id=item_id,
            exclusion=trust_reason,
        )
        return candidate, None

    @staticmethod
    def _denied(item: Mapping[str, Any], layer: str, item_id: str, status: str, reason: str) -> RecallDecision:
        opaque = _opaque_id(layer, item)
        return RecallDecision(
            item_id=opaque,
            layer=layer,
            action="exclude",
            trust="reference_only",
            reason=reason,
            status=status,
            source_digest=stable_digest({"layer": layer, "opaque": opaque, "reason": reason}),
        )

    @staticmethod
    def _relevance(query: str, item: Mapping[str, Any], summary: str) -> float:
        explicit = item.get("relevance")
        if explicit is not None:
            value = _bounded(explicit)
        else:
            value = 0.0
        query_tokens = _tokens(query)
        if not query_tokens:
            return value
        searchable: set[str] = set(_tokens(summary))
        for key in ("title", "kind", "category", "keywords", "tags", "body", "text"):
            value_raw = item.get(key)
            if isinstance(value_raw, (str, list, tuple, set)):
                if isinstance(value_raw, str):
                    searchable.update(_tokens(value_raw))
                else:
                    for part in value_raw:
                        searchable.update(_tokens(part))
        overlap = len(query_tokens & searchable) / len(query_tokens)
        return max(value, min(1.0, overlap))

    def _trust(self, item: Mapping[str, Any], layer: str) -> tuple[str, str]:
        declared = _text(item.get("trust") or item.get("injection_policy") or item.get("strength") or "").lower()
        if _bool(item.get("mandatory")):
            declared = "mandatory"
        elif _bool(item.get("enforceable")):
            declared = "enforceable"
        if layer == "rules":
            port = self._ports.get(layer)
            trusted = getattr(port, "trusted", None)
            if trusted is False or getattr(port, "authoritative", True) is False:
                return "relevant", "untrusted_mandatory_downgraded" if declared in {"mandatory", "enforceable"} else ""
            if declared in {"mandatory", "enforceable"}:
                return declared, ""
            return "relevant", ""
        # Every non-rule layer is forbidden from creating governance strength.
        if layer in _REFERENCE_LAYERS:
            return "reference_only", "untrusted_mandatory_downgraded" if declared in {"mandatory", "enforceable"} else ""
        return "relevant", "untrusted_mandatory_downgraded" if declared in {"mandatory", "enforceable"} else ""

    @staticmethod
    def _decision(candidate: _Candidate, *, action: str, reason: str) -> RecallDecision:
        if action == "include" and candidate.exclusion:
            reason = f"{reason};{candidate.exclusion}"
        return RecallDecision(
            item_id=candidate.item_id,
            layer=candidate.layer,
            action=action,
            trust=candidate.trust,
            score=candidate.score,
            reason=reason,
            evidence_refs=candidate.evidence_refs,
            source_digest=candidate.source_digest,
            summary=candidate.summary,
            status=candidate.status,
            metadata=candidate.metadata,
        )


RecallPlanBuilder = RecallPlanner


__all__ = ["RecallPlanner", "RecallPlanBuilder"]
