"""Deterministic, storage-agnostic budgets for V2 context packing.

The runtime adapter is intentionally small: callers provide a token counter
and retrieval candidates, while this module enforces independent mandatory
and optional limits without importing any legacy store or runtime hook.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


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


@dataclass(frozen=True)
class ContextBudget:
    """Independent mandatory and optional context limits.

    ``max_*`` limits optional layers.  Mandatory rules have their own cap so
    relevant recall cannot crowd them out.  ``item_*`` bounds one candidate.
    """

    max_items: int = 32
    max_chars: int = 12_000
    max_tokens: int = 3_000
    mandatory_max_items: int = 20
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
            "mandatory_cap": {
                "items": self.mandatory_max_items,
                "chars": self.mandatory_max_chars,
                "tokens": self.mandatory_max_tokens,
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


@dataclass
class BudgetLedger:
    """Counters emitted in the packet envelope and useful to acceptance tests."""

    mandatory_items: int = 0
    mandatory_chars: int = 0
    mandatory_tokens: int = 0
    optional_items: int = 0
    optional_chars: int = 0
    optional_tokens: int = 0
    omitted: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def omit(self, reason: str) -> None:
        self.omitted += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

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
        }


__all__ = [
    "ContextBudgetError", "ContextSafetyError", "TokenCounter",
    "DeterministicTokenCounter", "DeterministicCounter", "Utf8TokenCounter",
    "ContextBudget", "BudgetLedger",
]
