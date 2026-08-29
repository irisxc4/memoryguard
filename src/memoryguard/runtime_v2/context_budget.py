"""Deterministic, storage-agnostic budgets for V2 context packing.

The runtime adapter is intentionally small: callers provide a token counter
and retrieval candidates, while this module enforces independent mandatory
and optional limits without importing any legacy store or runtime hook.

``mandatory_max_items`` is a health/warning threshold for the *effective*
injected set after scope/exclude/conflict resolution and semantic
dedup/consolidation.  It is not a storage ceiling and not a hard injection
block.  Aggregate char/token overflow, per-item oversize, sensitive content,
and corrupt governance still fail closed with no partial mandatory packet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

# Effective-count health threshold.  Storage may hold more; injection warns
# instead of blocking when the post-dedup mandatory set exceeds this.
MANDATORY_ITEM_WARNING_THRESHOLD = 20
MANDATORY_ITEM_COUNT_WARNING = "mandatory_item_count_warning"
MANDATORY_ITEM_COUNT_WARNING_MESSAGE = (
    "生效强制规则数量超过健康阈值，建议通过现有规则合并/治理收敛，不要截断。"
)
MANDATORY_ITEM_COUNT_GOVERNANCE_ACTION = "rule_merge"


class ContextBudgetError(ValueError):
    """Budget configuration or packing refusal."""


class ContextSafetyError(ContextBudgetError):
    """Sensitive/unsafe mandatory data must fail closed."""


@runtime_checkable
class TokenCounter(Protocol):
    """Pluggable deterministic token counter.

    Implementations must return a stable non-negative integer for one text.
    """

    def count(self, text: str) -> int: ...


class DeterministicTokenCounter:
    """Unicode-stable baseline counter used by tests and shadow runtime.

    Counting code points is deliberately conservative and independent of a
    model tokenizer.  Production can inject an exact tokenizer later.
    """

    def count(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        return len(text)

    __call__ = count

    count_tokens = count


# Friendly aliases used by callers that prefer the shorter names.
DeterministicCounter = DeterministicTokenCounter
Utf8TokenCounter = DeterministicTokenCounter


def mandatory_item_count_warning(
    count: int,
    *,
    threshold: int = MANDATORY_ITEM_WARNING_THRESHOLD,
) -> dict[str, Any] | None:
    """Return the machine-readable count warning, or None when under threshold."""

    if type(count) is not int or type(threshold) is not int or count <= threshold:
        return None
    return {
        "code": MANDATORY_ITEM_COUNT_WARNING,
        "count": count,
        "threshold": threshold,
        "message": MANDATORY_ITEM_COUNT_WARNING_MESSAGE,
        "governance_action": MANDATORY_ITEM_COUNT_GOVERNANCE_ACTION,
    }


@dataclass(frozen=True)
class ContextBudget:
    """Independent mandatory and optional context limits.

    ``max_*`` limits optional layers.  Mandatory rules have their own char and
    token caps so relevant recall cannot crowd them out.  ``mandatory_max_items``
    is the effective-count warning threshold, not a hard block.  ``item_*``
    bounds one candidate.
    """

    max_items: int = 32
    max_chars: int = 12_000
    max_tokens: int = 3_000
    mandatory_max_items: int = MANDATORY_ITEM_WARNING_THRESHOLD
    mandatory_max_chars: int = 6_000
    mandatory_max_tokens: int = 1_000
    item_max_chars: int = 4_000
    item_max_tokens: int = 800

    def __post_init__(self) -> None:
        fields = (
            "max_items", "max_chars", "max_tokens", "mandatory_max_items",
            "mandatory_max_chars", "mandatory_max_tokens", "item_max_chars",
            "item_max_tokens",
        )
        for field_name in fields:
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ContextBudgetError(f"invalid_context_budget:{field_name}")

    @property
    def optional_max_items(self) -> int:
        return self.max_items

    @property
    def optional_max_chars(self) -> int:
        return self.max_chars

    @property
    def optional_max_tokens(self) -> int:
        return self.max_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_items": self.max_items,
            "max_chars": self.max_chars,
            "max_tokens": self.max_tokens,
            "mandatory_max_items": self.mandatory_max_items,
            "mandatory_max_chars": self.mandatory_max_chars,
            "mandatory_max_tokens": self.mandatory_max_tokens,
            "item_max_chars": self.item_max_chars,
            "item_max_tokens": self.item_max_tokens,
            "mandatory_item_warning_threshold": self.mandatory_max_items,
            "mandatory_cap": {
                "items": self.mandatory_max_items,
                "chars": self.mandatory_max_chars,
                "tokens": self.mandatory_max_tokens,
                "item_count_warning_threshold": self.mandatory_max_items,
            },
            "optional_cap": {
                "items": self.max_items,
                "chars": self.max_chars,
                "tokens": self.max_tokens,
            },
        }

    @classmethod
    def from_mapping(cls, value: Any | None) -> "ContextBudget":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ContextBudgetError("context_budget_required")
        aliases = {
            "item_limit": "max_items", "char_limit": "max_chars", "token_limit": "max_tokens",
            "mandatory_items": "mandatory_max_items", "mandatory_chars": "mandatory_max_chars",
            "mandatory_tokens": "mandatory_max_tokens",
        }
        known = {
            name: value[name]
            for name in (
                "max_items", "max_chars", "max_tokens", "mandatory_max_items",
                "mandatory_max_chars", "mandatory_max_tokens", "item_max_chars",
                "item_max_tokens",
            )
            if name in value
        }
        for source, target in aliases.items():
            if target not in known and source in value:
                known[target] = value[source]
        return cls(**known)

    def mandatory_item_oversize(self, chars: int, tokens: int) -> bool:
        return chars > self.item_max_chars or tokens > self.item_max_tokens

    def mandatory_aggregate_overflow(self, chars: int, tokens: int) -> bool:
        return chars > self.mandatory_max_chars or tokens > self.mandatory_max_tokens

    def item_count_warning(self, count: int) -> dict[str, Any] | None:
        return mandatory_item_count_warning(count, threshold=self.mandatory_max_items)


@dataclass
class BudgetLedger:
    """Counters emitted in the packet envelope and useful to acceptance tests."""

    mandatory_items: int = 0
    mandatory_chars: int = 0
    mandatory_tokens: int = 0
    optional_items: int = 0
    optional_chars: int = 0
    optional_tokens: int = 0
    # Candidate counters are intentionally separate from rendered counters:
    # rejected, deduplicated, or budget-omitted candidates still contribute to
    # the deterministic baseline without changing the injection decision.
    candidate_items: int = 0
    candidate_chars: int = 0
    candidate_tokens: int = 0
    candidate_mandatory_items: int = 0
    candidate_mandatory_chars: int = 0
    candidate_mandatory_tokens: int = 0
    candidate_relevant_items: int = 0
    candidate_relevant_chars: int = 0
    candidate_relevant_tokens: int = 0
    rendered_relevant_items: int = 0
    rendered_relevant_chars: int = 0
    rendered_relevant_tokens: int = 0
    omitted: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def record_candidate(self, layer: str, chars: int, tokens: int) -> None:
        """Record a candidate before filtering; never affects packing."""

        if type(chars) is not int or type(tokens) is not int or chars < 0 or tokens < 0:
            raise ContextBudgetError("invalid_candidate_measurement")
        self.candidate_items += 1
        self.candidate_chars += chars
        self.candidate_tokens += tokens
        if layer == "mandatory":
            self.candidate_mandatory_items += 1
            self.candidate_mandatory_chars += chars
            self.candidate_mandatory_tokens += tokens
        elif layer == "relevant":
            self.candidate_relevant_items += 1
            self.candidate_relevant_chars += chars
            self.candidate_relevant_tokens += tokens

    def record_rendered(self, layer: str, chars: int, tokens: int) -> None:
        """Record selected rendered text while preserving existing totals."""

        if layer == "mandatory":
            self.mandatory_items += 1
            self.mandatory_chars += chars
            self.mandatory_tokens += tokens
        elif layer == "relevant":
            self.rendered_relevant_items += 1
            self.rendered_relevant_chars += chars
            self.rendered_relevant_tokens += tokens
            self.optional_items += 1
            self.optional_chars += chars
            self.optional_tokens += tokens
        else:
            self.optional_items += 1
            self.optional_chars += chars
            self.optional_tokens += tokens

    def omit(self, reason: str) -> None:
        self.omitted += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def warn(self, warning: Mapping[str, Any] | None) -> None:
        if warning:
            self.warnings.append(dict(warning))

    @property
    def total_items(self) -> int:
        return self.mandatory_items + self.optional_items

    @property
    def total_chars(self) -> int:
        return self.mandatory_chars + self.optional_chars

    @property
    def total_tokens(self) -> int:
        return self.mandatory_tokens + self.optional_tokens

    def to_dict(self) -> dict[str, Any]:
        candidate = {
            "items": self.candidate_items,
            "chars": self.candidate_chars,
            "tokens": self.candidate_tokens,
        }
        rendered = {
            "items": self.total_items,
            "chars": self.total_chars,
            "tokens": self.total_tokens,
        }
        mandatory_candidate = {
            "items": self.candidate_mandatory_items,
            "chars": self.candidate_mandatory_chars,
            "tokens": self.candidate_mandatory_tokens,
        }
        relevant_candidate = {
            "items": self.candidate_relevant_items,
            "chars": self.candidate_relevant_chars,
            "tokens": self.candidate_relevant_tokens,
        }
        mandatory_rendered = {
            "items": self.mandatory_items,
            "chars": self.mandatory_chars,
            "tokens": self.mandatory_tokens,
        }
        relevant_rendered = {
            "items": self.rendered_relevant_items,
            "chars": self.rendered_relevant_chars,
            "tokens": self.rendered_relevant_tokens,
        }
        # The engine can measure only the body that was considered and the
        # body that survived packing.  A host wrapper is added later by the
        # Hook/provider boundary, so total/baseline units are deliberately
        # unknown here instead of pretending that the wrapper is zero.
        candidate_body_units = self.candidate_tokens
        delivered_body_units = self.total_tokens
        saved_body_units = max(0, candidate_body_units - delivered_body_units)
        usage = {
            "measurement_basis": "mg_deterministic_unit",
            "counter": "deterministic_codepoint_v1",
            "candidate_baseline": candidate,
            "rendered": rendered,
            # The context engine's rendered packet is the delivered payload
            # before a host adds its provider-specific wrapper.  Host Hooks
            # replace this with the final injected count when recording usage.
            "delivered": dict(rendered),
            "mandatory": {
                "candidate_baseline": mandatory_candidate,
                "rendered": mandatory_rendered,
                "delivered": dict(mandatory_rendered),
            },
            "relevant": {
                "candidate_baseline": relevant_candidate,
                "rendered": relevant_rendered,
                "delivered": dict(relevant_rendered),
            },
            "candidate_body_units": candidate_body_units,
            "delivered_body_units": delivered_body_units,
            "saved_body_units": saved_body_units,
            "baseline_total_units": None,
            "delivered_total_units": None,
            "wrapper_overhead_units": None,
            # Deprecated aliases remain explicit nulls.  They used to expose
            # body counts under total-looking names and caused false savings
            # when a provider wrapper was present.
            "baseline_units": None,
            "delivered_units": None,
            "saved_units": None,
        }
        return {
            "mandatory": {
                "items": self.mandatory_items,
                "chars": self.mandatory_chars,
                "tokens": self.mandatory_tokens,
            },
            "optional": {
                "items": self.optional_items,
                "chars": self.optional_chars,
                "tokens": self.optional_tokens,
            },
            "total_items": self.total_items,
            "total_chars": self.total_chars,
            "total_tokens": self.total_tokens,
            "items": self.total_items,
            "chars": self.total_chars,
            "tokens": self.total_tokens,
            "omitted": self.omitted,
            "reasons": dict(sorted(self.reasons.items())),
            "warnings": [dict(item) for item in self.warnings],
            "usage": usage,
        }


__all__ = [
    "ContextBudgetError", "ContextSafetyError", "TokenCounter",
    "DeterministicTokenCounter", "DeterministicCounter", "Utf8TokenCounter",
    "ContextBudget", "BudgetLedger",
    "MANDATORY_ITEM_WARNING_THRESHOLD", "MANDATORY_ITEM_COUNT_WARNING",
    "MANDATORY_ITEM_COUNT_WARNING_MESSAGE",
    "MANDATORY_ITEM_COUNT_GOVERNANCE_ACTION", "mandatory_item_count_warning",
]
