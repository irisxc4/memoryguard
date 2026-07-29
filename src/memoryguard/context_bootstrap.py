"""Deterministic, read-only long-term-memory context bootstrap.

This module does not inspect or replace the host's current conversation.  It
selects a bounded packet from an already-open, trusted SharedMemoryStore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .auto_organizer import SECRET_PATTERNS
from .schema_v3 import MemoryKind, SharedMemoryRecord, SharedMemoryStatus
from .shared_memory_store import SharedMemoryStore


DEFAULT_MAX_ITEMS = 12
DEFAULT_MAX_CHARS = 6000
MAX_ITEMS_LIMIT = 20
MAX_CHARS_LIMIT = 12000
PER_ITEM_CHAR_LIMIT = 1600
PREFERENCE_MAX_ITEMS = 5

_REDACTED_MARKER = re.compile(r"\[REDACTED(?::[^\]]+)?\]", re.IGNORECASE)
_WORD_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]*")
_HAN_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_RELEVANT_KINDS = {
    MemoryKind.PROCEDURE,
    MemoryKind.PROJECT,
    MemoryKind.CORRECTION,
    MemoryKind.FACT,
}
_KIND_ORDER = {
    MemoryKind.PREFERENCE: 0,
    MemoryKind.CORRECTION: 1,
    MemoryKind.PROCEDURE: 2,
    MemoryKind.PROJECT: 3,
    MemoryKind.FACT: 4,
}


@dataclass(frozen=True)
class _Candidate:
    record: SharedMemoryRecord
    reason: str
    relevance: int


def _tokens(text: str) -> set[str]:
    value = text or ""
    tokens = {token.casefold() for token in _WORD_PATTERN.findall(value)}
    # Chinese single-character overlap is too weak for context injection.
    # Bigrams retain useful matching without network/tokenizer dependencies.
    for run in _HAN_PATTERN.findall(value):
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


def _normalized_body(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _contains_sensitive_content(text: str) -> bool:
    """Fail closed: raw secret or any redaction placeholder is omitted."""
    if not text or _REDACTED_MARKER.search(text):
        return bool(_REDACTED_MARKER.search(text or ""))
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _sort_key(candidate: _Candidate) -> tuple[Any, ...]:
    record = candidate.record
    # Preference is always first.  Then relevance, locked manual overrides,
    # confidence, timestamp, and id make selection stable and explainable.
    return (
        _KIND_ORDER.get(record.kind, 99),
        -candidate.relevance,
        -int(record.locked),
        -float(record.confidence),
        record.updated_at or record.created_at or "",
        record.memory_id,
    )


def build_context_packet(
    store: SharedMemoryStore,
    *,
    task: str,
    project_hint: str = "",
    max_items: int = DEFAULT_MAX_ITEMS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Build bounded long-term-memory context from a read-only trusted store."""
    task = (task or "").strip()
    if not task:
        raise ValueError("task is required")
    if not 1 <= int(max_items) <= MAX_ITEMS_LIMIT:
        raise ValueError(f"max_items must be between 1 and {MAX_ITEMS_LIMIT}")
    if not 256 <= int(max_chars) <= MAX_CHARS_LIMIT:
        raise ValueError(f"max_chars must be between 256 and {MAX_CHARS_LIMIT}")

    query_tokens = _tokens(f"{task} {project_hint}")
    all_records = store.list_records()
    omitted = {
        "non_active": 0,
        "sensitive": 0,
        "irrelevant": 0,
        "duplicate": 0,
        "budget": 0,
        "unsupported_kind": 0,
    }
    candidates: list[_Candidate] = []

    for record in all_records:
        if record.status != SharedMemoryStatus.ACTIVE:
            omitted["non_active"] += 1
            continue
        if _contains_sensitive_content(record.body):
            omitted["sensitive"] += 1
            continue
        if record.kind == MemoryKind.PREFERENCE:
            overlap = len(query_tokens & _tokens(record.body))
            reason = "long_term_preference"
            if overlap:
                reason = f"long_term_preference+task_overlap:{overlap}"
            candidates.append(_Candidate(record, reason, overlap))
            continue
        if record.kind not in _RELEVANT_KINDS:
            omitted["unsupported_kind"] += 1
            continue
        overlap = len(query_tokens & _tokens(record.body))
        if overlap < 2:
            omitted["irrelevant"] += 1
            continue
        candidates.append(_Candidate(record, f"task_overlap:{overlap}", overlap))

    candidates.sort(key=_sort_key)

    # Exact normalized-body dedup happens after ranking so manual/stronger
    # records win deterministically.
    unique: list[_Candidate] = []
    seen_bodies: set[str] = set()
    for candidate in candidates:
        normalized = _normalized_body(candidate.record.body)
        if normalized in seen_bodies:
            omitted["duplicate"] += 1
            continue
        seen_bodies.add(normalized)
        unique.append(candidate)

    preferences = [
        item for item in unique if item.record.kind == MemoryKind.PREFERENCE
    ]
    relevant = [
        item for item in unique if item.record.kind != MemoryKind.PREFERENCE
    ]
    preference_slots = min(PREFERENCE_MAX_ITEMS, max_items)
    if relevant:
        # Preserve at least one task-relevant slot when caller gives capacity.
        preference_slots = min(preference_slots, max(0, max_items - 1))
    preference_char_budget = max_chars
    if relevant:
        preference_char_budget = max_chars // 2

    items: list[dict[str, Any]] = []
    used_chars = 0

    def _select(candidate: _Candidate, char_ceiling: int) -> bool:
        nonlocal used_chars
        body = candidate.record.body.strip()
        remaining = min(max_chars - used_chars, char_ceiling)
        if remaining <= 0:
            return False
        item_limit = min(PER_ITEM_CHAR_LIMIT, remaining)
        selected_body = body[:item_limit]
        if not selected_body:
            return False
        item = {
            "memory_id": candidate.record.memory_id,
            "kind": candidate.record.kind.value,
            "body": selected_body,
            "reason": candidate.reason,
            "confidence": candidate.record.confidence,
            "manual_override": bool(candidate.record.locked),
            "truncated": len(selected_body) < len(body),
        }
        items.append(item)
        used_chars += len(selected_body)
        return True

    for index, candidate in enumerate(preferences):
        if index >= preference_slots:
            omitted["budget"] += 1
            continue
        if not _select(candidate, preference_char_budget - used_chars):
            omitted["budget"] += 1

    for candidate in relevant:
        if len(items) >= max_items:
            omitted["budget"] += 1
            continue
        if not _select(candidate, max_chars - used_chars):
            omitted["budget"] += 1

    return {
        "context_packet": {
            "scope": "long_term_memory_only",
            "host_conversation": "unchanged_not_duplicated",
            "task": task,
            "project_hint": (project_hint or "").strip(),
            "items": items,
        },
        "share_group_id": store.group_id,
        "active_version": store.get_active_version_id(),
        "selection": {
            "policy": "active_preferences_then_task_relevant",
            "selected_count": len(items),
            "omitted": omitted,
        },
        "budget": {
            "max_items": max_items,
            "used_items": len(items),
            "max_chars": max_chars,
            "used_chars": used_chars,
            "per_item_max_chars": PER_ITEM_CHAR_LIMIT,
            "preference_max_items": PREFERENCE_MAX_ITEMS,
            "preference_char_budget": preference_char_budget,
        },
    }
