"""Deterministic, read-only long-term-memory context bootstrap.

This module does not inspect or replace the host's current conversation.  It
selects a bounded packet from an already-open, trusted SharedMemoryStore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .auto_organizer import SECRET_PATTERNS
from .schema_v3 import (
    EffectiveAgentContext,
    MemoryKind,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
    stable_hash,
)
from .rule_scope import effective_assignments, normalize_assignment
from .rule_scope import canonical_project_ref
from .rule_read_path import MODE_LEGACY, resolve_read_path_mode
from .shared_memory_store import (
    MANDATORY_MAX_CHARS,
    MANDATORY_MAX_ITEMS,
    SharedMemoryStore,
)


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
    effective_context: EffectiveAgentContext | None = None,
    read_path: str = "legacy",
) -> dict[str, Any]:
    """Build bounded long-term-memory context from a read-only trusted store.

    ``read_path`` controls the Phase5 canonical read path.  It is shadow by
    default: ``legacy`` (the default and the env fallback) forces the old
    byte-for-byte path; ``auto`` / ``rule-intelligence`` prefer the canonical
    layer when it has data for this group (falling back to legacy when it does
    not), deduplicating merged duplicates only *after* the active/audience/
    exclude match so a collapse can never under-expose a rule.
    """
    task = (task or "").strip()
    if not task:
        raise ValueError("task is required")
    if not 1 <= int(max_items) <= MAX_ITEMS_LIMIT:
        raise ValueError(f"max_items must be between 1 and {MAX_ITEMS_LIMIT}")
    if not 256 <= int(max_chars) <= MAX_CHARS_LIMIT:
        raise ValueError(f"max_chars must be between 256 and {MAX_CHARS_LIMIT}")

    direct_compat = effective_context is None
    if direct_compat:
        effective_context = EffectiveAgentContext("__direct__", store.group_id)
    else:
        if not effective_context.agent_instance_id:
            raise ValueError("effective agent context is required for mandatory rule bootstrap")
        if effective_context.share_group_id != store.group_id:
            raise ValueError("effective agent context share_group_id mismatch")
    project_ref = canonical_project_ref(effective_context.project_ref)
    session_id = str(effective_context.session_id or "").strip()
    context_hash = str(effective_context.context_hash or "").strip() or stable_hash(
        "rule-context",
        effective_context.agent_instance_id,
        effective_context.share_group_id,
        project_ref,
        effective_context.provider,
        effective_context.runtime_role,
        session_id,
    )
    task_hash = stable_hash(
        "rule-bootstrap", task, store.group_id, effective_context.agent_instance_id,
        project_ref, effective_context.provider, effective_context.runtime_role,
        session_id, context_hash,
    )
    query_tokens = _tokens(f"{task} {project_hint}")
    all_records = store.list_records()
    # Phase5 canonical read path (shadow by default: ``read_path`` and the env
    # fallback resolve to legacy unless explicitly opted in).  The mapping is
    # resolved up front but applied *after* active/audience/exclude matching
    # below, so a canonical collapse can never drop the one record that applies
    # to the current Agent/project.  Body text still comes from the legacy
    # record; old tables are untouched.
    canonical_mapping: dict[str, Any] | None = None
    read_path_summary: dict[str, Any] = {
        "mode": MODE_LEGACY,
        "canonical_definitions": 0,
        "records_before": 0,
        "records_after": 0,
        "deduplicated": 0,
    }
    if resolve_read_path_mode(read_path) != MODE_LEGACY:
        try:
            from .rule_read_path import RuleReadPath

            read = RuleReadPath(store.workspace, store.group_id)
            if read.has_intelligence():
                canonical_mapping = read.resolve_canonical_map(
                    known_memory_ids={r.memory_id for r in all_records},
                    legacy_store=store,
                    context=effective_context,
                )
        except Exception:
            canonical_mapping = None  # keep the legacy path; canonical is advisory
    omitted = {
        "non_active": 0,
        "sensitive": 0,
        "irrelevant": 0,
        "duplicate": 0,
        "budget": 0,
        "unsupported_kind": 0,
    }
    omitted_details: list[dict[str, str]] = []

    # Stage 1: a separate mandatory package.  It intentionally bypasses task
    # overlap and ordinary recall budgets, but remains status/safety/dedup
    # governed.  The raw active set is checked first: historical corruption is
    # a hard error, never a silent bootstrap truncation.
    # Public MCP and host hooks always pass a trusted context.  Keep the
    # in-process helper backward compatible for offline reports/tests only;
    # it is never reachable through the MCP capability boundary.
    all_assignments = store.list_rule_assignments()
    assignments_by_memory: dict[str, list[Any]] = {}
    malformed_assignments: set[str] = set()
    for assignment in all_assignments:
        assignments_by_memory.setdefault(assignment.memory_id, []).append(assignment)
        try:
            normalize_assignment(assignment)
        except ValueError:
            malformed_assignments.add(assignment.memory_id)
    raw_mandatory: list[SharedMemoryRecord] = []
    assignment_receipt: dict[str, list[dict[str, Any]]] = {
        "system": [], "group": [], "project": [], "agent": [], "role": [],
        "excluded": [], "skipped": [], "corrupt": [],
    }
    mandatory_receipt_refs: dict[str, list[str]] = {}
    effective_priorities: dict[str, int] = {}
    legacy_unscoped: list[str] = []
    scoped_corrupt: list[SharedMemoryRecord] = []
    for record in all_records:
        if record.status != SharedMemoryStatus.ACTIVE:
            continue
        record_assignments = assignments_by_memory.get(record.memory_id, [])
        if record.injection_policy not in {"always", "relevant"}:
            if direct_compat:
                assignment_receipt["corrupt"].append({
                    "memory_id": record.memory_id,
                    "reason": "corrupt_rule_governance_quarantine",
                })
                continue
            includes, excludes = effective_assignments(
                record_assignments, effective_context,
            )
            # A known audience only fail-closes its own packet.  A malformed
            # assignment cannot be safely targeted, so it is governed as a
            # quarantine condition and never injected anywhere.
            if includes or excludes:
                scoped_corrupt.append(record)
                assignment_receipt["corrupt"].append({
                    "memory_id": record.memory_id,
                    "reason": "corrupt_rule_matched_audience",
                    "assignment_ids": [item.assignment_id for item in includes + excludes],
                })
            elif record.memory_id in malformed_assignments or not record_assignments:
                assignment_receipt["corrupt"].append({
                    "memory_id": record.memory_id,
                    "reason": "corrupt_rule_governance_quarantine",
                })
            continue
        if record.injection_policy != "always":
            continue
        if direct_compat:
            includes, excludes = ([object()], [])
        else:
            includes, excludes = effective_assignments(
                assignments_by_memory.get(record.memory_id, []), effective_context,
            )
        if not record_assignments and not direct_compat:
            legacy_unscoped.append(record.memory_id)
            assignment_receipt["skipped"].append({
                "memory_id": record.memory_id,
                "reason": "legacy_unscoped_governance_required",
            })
            continue
        if excludes:
            for assignment in excludes:
                assignment_receipt["excluded"].append({
                    "assignment_id": assignment.assignment_id,
                    "memory_id": record.memory_id,
                    "target_type": assignment.target_type,
                    "target_id": assignment.target_id,
                    "project_ref": assignment.project_ref,
                    "base_priority": record.priority,
                    "effective_priority": record.priority,
                    "reason": "matched_exclude_precedence",
                })
            continue
        if not includes:
            for assignment in record_assignments:
                assignment_receipt["skipped"].append({
                    "assignment_id": assignment.assignment_id,
                    "memory_id": record.memory_id,
                    "reason": "audience_not_matched",
                })
            continue
        raw_mandatory.append(record)
        overrides = [
            item.priority_override for item in includes
            if hasattr(item, "priority_override")
            and item.priority_override is not None
        ]
        effective_priorities[record.memory_id] = (
            max(overrides) if overrides else record.priority
        )
        for assignment in includes:
            if not hasattr(assignment, "target_type"):
                continue
            mandatory_receipt_refs.setdefault(record.memory_id, []).append(
                assignment.assignment_id
            )
            bucket = {
                "runtime_role": "role", "agent_project": "project"
            }.get(
                assignment.target_type, assignment.target_type,
            )
            if bucket in assignment_receipt:
                assignment_receipt[bucket].append({
                    "assignment_id": assignment.assignment_id,
                    "memory_id": record.memory_id,
                    "target_type": assignment.target_type,
                    "target_id": assignment.target_id,
                    "project_ref": assignment.project_ref,
                    "effect": assignment.effect,
                    "base_priority": record.priority,
                    "effective_priority": effective_priorities[record.memory_id],
                    "reason": "matched_include",
                })
    # Canonical dedup of the *matched* mandatory set (post audience/exclude):
    # a merged duplicate collapses to its strongest representative, but only
    # among records that actually apply to this Agent/project.
    if canonical_mapping:
        from .rule_read_path import dedupe_records

        def _mandatory_key(record: Any) -> tuple:
            return (
                -int(effective_priorities.get(record.memory_id, record.priority) or 0),
                -int(1 if record.locked else 0),
                -float(record.confidence or 0.0),
                str(record.memory_id or ""),
            )

        read_path_summary["records_before"] += len(raw_mandatory)
        raw_mandatory = dedupe_records(
            raw_mandatory, canonical_mapping, key=_mandatory_key,
        )
        read_path_summary["records_after"] += len(raw_mandatory)

    invalid_mandatory_priorities = [
        record for record in raw_mandatory
        if isinstance(effective_priorities.get(record.memory_id), bool)
        or not isinstance(effective_priorities.get(record.memory_id), int)
        or not -100 <= effective_priorities.get(record.memory_id, 0) <= 100
    ]
    sensitive_mandatory = [
        record for record in raw_mandatory
        if _contains_sensitive_content(record.body)
    ]
    mandatory_chars = sum(len(record.body or "") for record in raw_mandatory)
    mandatory_overflow = (
        len(raw_mandatory) > MANDATORY_MAX_ITEMS
        or mandatory_chars > MANDATORY_MAX_CHARS
        or bool(invalid_mandatory_priorities)
        or bool(sensitive_mandatory)
        or bool(scoped_corrupt)
    )
    mandatory_error = ""
    if mandatory_overflow:
        omitted["mandatory_overflow"] = len(raw_mandatory) + len(invalid_mandatory_priorities)
        if sensitive_mandatory:
            omitted["sensitive"] += len(sensitive_mandatory)
            omitted_details.extend(
                {"memory_id": record.memory_id, "reason": "mandatory_sensitive"}
                for record in sensitive_mandatory
            )
        mandatory_error = (
            "mandatory_rule_package_invalid: active always rules exceed the "
            "configured limit, are sensitive, contain invalid settings, or have a corrupt matched rule"
        )

    mandatory_items: list[dict[str, Any]] = []
    mandatory_ids: list[str] = []
    mandatory_seen: set[str] = set()
    mandatory_match_receipts: list[dict[str, Any]] = []
    if not mandatory_overflow:
        for record in sorted(
            raw_mandatory,
            key=lambda item: (
                -int(effective_priorities[item.memory_id]), -int(item.locked),
                -float(item.confidence), item.memory_id,
            ),
        ):
            if _contains_sensitive_content(record.body):
                omitted["sensitive"] += 1
                omitted_details.append({"memory_id": record.memory_id, "reason": "sensitive"})
                continue
            normalized = _normalized_body(record.body)
            if normalized in mandatory_seen:
                omitted["duplicate"] += 1
                omitted_details.append({"memory_id": record.memory_id, "reason": "duplicate"})
                continue
            mandatory_seen.add(normalized)
            mandatory_ids.append(record.memory_id)
            mandatory_items.append({
                "memory_id": record.memory_id,
                "kind": record.kind.value,
                "body": record.body.strip(),
                "reason": "mandatory_rule",
                "confidence": record.confidence,
                "manual_override": bool(record.locked),
                "priority": effective_priorities[record.memory_id],
                "base_priority": record.priority,
                "truncated": False,
            })
            mandatory_match_receipts.append(
                RuleMatchReceipt(
                    receipt_id=stable_hash(
                        "rule-bootstrap-receipt-v2",
                        store.group_id,
                        effective_context.agent_instance_id,
                        session_id,
                        effective_context.session_source,
                        context_hash,
                        task_hash,
                        record.memory_id,
                    ),
                    memory_id=record.memory_id,
                    share_group_id=store.group_id,
                    agent_instance_id=effective_context.agent_instance_id,
                    task_hash=task_hash,
                    task=task,
                    assignment_ids=mandatory_receipt_refs.get(record.memory_id, []),
                    selection_reason="mandatory_rule",
                    matcher_version="rule-bootstrap-v1",
                    confidence=float(record.confidence),
                    created_at=_now_iso(),
                    project_ref=project_ref,
                    provider=effective_context.provider,
                    runtime_role=effective_context.runtime_role,
                    session_id=session_id,
                    context_hash=context_hash,
                    session_trusted=effective_context.session_trusted,
                    session_source=effective_context.session_source,
                ).to_dict()
            )

    # Stage 2: ordinary task-relevant recall.  Mandatory records never consume
    # these slots/characters and are not reconsidered as preferences.
    candidates: list[_Candidate] = []

    for record in all_records:
        if record.status != SharedMemoryStatus.ACTIVE:
            omitted["non_active"] += 1
            continue
        if record.injection_policy == "always":
            continue
        if record.injection_policy != "relevant":
            omitted["unsupported_kind"] += 1
            omitted_details.append({"memory_id": record.memory_id, "reason": "invalid_injection_policy"})
            continue
        if _contains_sensitive_content(record.body):
            omitted["sensitive"] += 1
            omitted_details.append({"memory_id": record.memory_id, "reason": "sensitive"})
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
            omitted_details.append({"memory_id": record.memory_id, "reason": "unsupported_kind"})
            continue
        overlap = len(query_tokens & _tokens(record.body))
        if overlap < 2:
            omitted["irrelevant"] += 1
            omitted_details.append({"memory_id": record.memory_id, "reason": "irrelevant"})
            continue
        candidates.append(_Candidate(record, f"task_overlap:{overlap}", overlap))

    candidates.sort(key=_sort_key)

    # Exact normalized-body dedup happens after ranking so manual/stronger
    # records win deterministically.
    unique: list[_Candidate] = []
    # Relevant recall and mandatory rules are distinct injection semantics;
    # equal prose may legitimately appear in both packets.
    seen_bodies: set[str] = set()
    for candidate in candidates:
        normalized = _normalized_body(candidate.record.body)
        if normalized in seen_bodies:
            omitted["duplicate"] += 1
            omitted_details.append({"memory_id": candidate.record.memory_id, "reason": "duplicate"})
            continue
        seen_bodies.add(normalized)
        unique.append(candidate)

    # Canonical dedup of the *matched* relevant set (post active/relevance): the
    # first (best-ranked) candidate per canonical definition survives.
    if canonical_mapping:
        from .rule_read_path import dedupe_records

        memory_to_definition = canonical_mapping.get("memory_to_definition") or {}
        read_path_summary["records_before"] += len(unique)

        def _relevant_key(candidate: _Candidate) -> tuple:
            return (0,)  # keep ranking order; first per definition wins

        deduped = dedupe_records(
            [c.record for c in unique], canonical_mapping, key=_relevant_key,
        )
        kept_ids = {r.memory_id for r in deduped}
        unique = [c for c in unique if c.record.memory_id in kept_ids]
        read_path_summary["records_after"] += len(unique)

    if canonical_mapping:
        from .rule_read_path import canonical_read_summary

        read_path_summary = canonical_read_summary(
            canonical_mapping,
            int(read_path_summary["records_before"]),
            int(read_path_summary["records_after"]),
        )

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
            omitted_details.append({"memory_id": candidate.record.memory_id, "reason": "budget"})
            continue
        if not _select(candidate, preference_char_budget - used_chars):
            omitted["budget"] += 1
            omitted_details.append({"memory_id": candidate.record.memory_id, "reason": "budget"})

    for candidate in relevant:
        if len(items) >= max_items:
            omitted["budget"] += 1
            omitted_details.append({"memory_id": candidate.record.memory_id, "reason": "budget"})
            continue
        if not _select(candidate, max_chars - used_chars):
            omitted["budget"] += 1

    # Stage 3: KAG 知识书库检索（独立预算，不占记忆名额）
    knowledge_items: list[dict[str, Any]] = []
    try:
        from .knowledge_retriever import search as _ksearch
        from .knowledge_store import open_shared_knowledge_store
        kstore = open_shared_knowledge_store(read_only=True, must_exist=True)
        if kstore is not None:
            with kstore:
                k_results = _ksearch(kstore, task, top_k=6)
            k_chars = 0
            for r in k_results:
                # 非控制面文件才进入普通 KAG Bootstrap（指令文件可浏览但不可注入）
                if r.get("content_role") == "control_surface":
                    continue
                text = r.get("text", "")
                if k_chars + len(text) > 6000:
                    break
                knowledge_items.append({
                    "chunk_id": r.get("chunk_id", ""),
                    "book_title": r.get("book_title", ""),
                    "chapter": r.get("chapter", ""),
                    "section": r.get("section", ""),
                    "relative_path": r.get("relative_path", ""),
                    "line_start": r.get("line_start", 0),
                    "line_end": r.get("line_end", 0),
                    "text": text,
                    "retrieval_method": "fts5",
                    "trust": "reference_only",
                })
                k_chars += len(text)
    except Exception:
        pass  # 知识库不可用时不阻断 bootstrap

    return {
        "context_packet": {
            "scope": "long_term_memory_only",
            "host_conversation": "unchanged_not_duplicated",
            "task": task,
            "project_hint": (project_hint or "").strip(),
            "items": items,
            "mandatory_items": mandatory_items,
            "knowledge_items": knowledge_items,
        },
        "share_group_id": store.group_id,
        "active_version": store.get_active_version_id(),
        "selection": {
            "policy": "mandatory_rules_then_active_preferences_then_task_relevant",
            "selected_count": len(items),
            "omitted": omitted,
            "omitted_details": omitted_details,
        },
        "mandatory_rule_ids": mandatory_ids,
        "mandatory_match_receipts": mandatory_match_receipts,
        "effective_agent": {
            "agent_instance_id": effective_context.agent_instance_id,
            "provider": effective_context.provider,
            "project_ref": effective_context.project_ref,
            "runtime_role": effective_context.runtime_role,
            "runtime_agent_id": effective_context.runtime_agent_id,
            "parent_agent_id": effective_context.parent_agent_id,
            "session_id": session_id,
            "context_hash": context_hash,
        },
        "assignment_receipt": assignment_receipt,
        "legacy_unscoped_rule_ids": legacy_unscoped,
        "read_path": read_path_summary,
        "recalled_memory_ids": [item["memory_id"] for item in items],
        "mandatory_overflow": mandatory_overflow,
        "mandatory_invalid_reason": mandatory_error,
        "error": mandatory_error,
        "budget": {
            "max_items": max_items,
            "used_items": len(items),
            "max_chars": max_chars,
            "used_chars": used_chars,
            "per_item_max_chars": PER_ITEM_CHAR_LIMIT,
            "preference_max_items": PREFERENCE_MAX_ITEMS,
            "preference_char_budget": preference_char_budget,
            "mandatory_max_items": MANDATORY_MAX_ITEMS,
            "mandatory_max_chars": MANDATORY_MAX_CHARS,
            "mandatory_used_items": len(raw_mandatory),
            "mandatory_used_chars": mandatory_chars,
        },
    }
