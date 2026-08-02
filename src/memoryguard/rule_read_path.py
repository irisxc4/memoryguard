"""Rule read path: canonical-first enforcement reads with legacy fallback (Phase5).

After backfill + dual-write, the rule-intelligence layer holds the canonical
Definition set.  The *enforcement* read path (context bootstrap, hooks) may
prefer that canonical layer so that merged duplicates inject once, not N times
— while the body text still comes from the legacy record so what an Agent sees
is exactly what its group stored.

**Safety default (v2).**  The canonical read path is a shadow feature, not the
production default.  ``resolve_read_path_mode`` and ``build_context_packet``
default to ``legacy``; the canonical path only runs when explicitly requested
(``rule-intelligence`` / ``auto`` via an explicit opt-in or env var), and it
deduplicates *after* the active/audience/exclude match so a canonical collapse
can never delete the one record that actually applies to the current Agent or
project.  ``shadow_compare`` exposes the legacy-vs-canonical context diff so an
operator can switch to ``rule-intelligence`` only when
``missing == extra == permission_diff == 0``.

This module implements Phase5 of the migration:

  * ``RuleReadPath`` resolves a canonical mapping ``memory_id -> definition_id``
    from the rule-intelligence store (Definitions → Bindings → Evidence), scoped
    to one group;
  * ``dedupe_records`` collapses records that map to the same canonical
    Definition, keeping the strongest representative per definition;
  * if the intelligence layer has no data for the group, every resolver returns
    None and the caller falls back to the legacy read path byte-for-byte.

Old tables are never modified: Phase5 is a read-side preference, not a data
migration.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

# Read-path modes.  "auto" prefers the canonical layer when it has data for the
# group and otherwise behaves exactly like "legacy".
MODE_AUTO = "auto"
MODE_LEGACY = "legacy"
MODE_RULE_INTELLIGENCE = "rule-intelligence"
_MODES = {MODE_AUTO, MODE_LEGACY, MODE_RULE_INTELLIGENCE}


def resolve_read_path_mode(value: str = "") -> str:
    """Normalize a read-path setting; env fallback; never an unknown value.

    Defaults to ``legacy``: the canonical read path is shadow-only until an
    operator opts in (env ``MEMORYGUARD_RULE_READ_PATH`` or an explicit value).
    """
    candidate = str(value or "").strip().lower()
    if candidate not in _MODES:
        candidate = str(
            os.environ.get("MEMORYGUARD_RULE_READ_PATH", MODE_LEGACY)
        ).strip().lower()
    return candidate if candidate in _MODES else MODE_LEGACY


class RuleReadPath:
    """Resolve the canonical rule set for one group, when intelligence exists."""

    def __init__(self, workspace: str | Path, group_id: str):
        self.workspace = Path(workspace).resolve()
        self.group_id = group_id
        self._store: Any | None = None

    def _open(self) -> Any | None:
        if self._store is None:
            try:
                from .rule_merge_store import RuleMergeStore

                self._store = RuleMergeStore(self.workspace)
            except Exception:
                self._store = None
        return self._store

    def has_intelligence(self) -> bool:
        store = self._open()
        if store is None:
            return False
        try:
            if not store.list_definitions(status="active"):
                return False
            if not store.list_bindings(
                share_group_id=self.group_id, status="active",
            ):
                return False
            return True
        except Exception:
            return False

    def resolve_canonical_map(
        self,
        known_memory_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Build ``memory_id -> definition_id`` for active definitions bound to
        this group.  Returns None when the layer has no data for the group.

        The mapping is derived from Evidence: every ``source_rule_id`` of a
        bound Definition is a legacy memory_id (backfill/dual-write anchor).
        Stale evidence whose source id no longer exists in the legacy store is
        dropped so the canonical read never invents records.
        """
        store = self._open()
        if store is None:
            return None
        try:
            definitions = store.list_definitions(status="active")
            if not definitions:
                return None
            bindings = store.list_bindings(
                share_group_id=self.group_id, status="active",
            )
            if not bindings:
                return None
            bound = {b.definition_id for b in bindings}
        except Exception:
            return None

        memory_to_definition: dict[str, str] = {}
        definition_to_memory: dict[str, list[str]] = {}
        for definition in definitions:
            if definition.definition_id not in bound:
                continue
            source_ids: list[str] = []
            try:
                for evidence in store.list_evidence(
                    definition_id=definition.definition_id,
                ):
                    source = str(evidence.source_rule_id or "").strip()
                    if not source:
                        continue
                    if known_memory_ids is not None and source not in known_memory_ids:
                        continue
                    if source not in memory_to_definition:
                        source_ids.append(source)
                        memory_to_definition[source] = definition.definition_id
            except Exception:
                continue
            if source_ids:
                definition_to_memory[definition.definition_id] = source_ids

        if not memory_to_definition:
            return None
        return {
            "mode": MODE_RULE_INTELLIGENCE,
            "group_id": self.group_id,
            "definitions": {
                d.definition_id: {
                    "rule_strength": d.rule_strength,
                    "maturity_state": d.maturity_state,
                }
                for d in definitions
                if d.definition_id in definition_to_memory
            },
            "memory_to_definition": memory_to_definition,
            "definition_to_memory": definition_to_memory,
        }

    def shadow_compare(self, legacy_store: Any, context: Any) -> dict[str, Any] | None:
        """Legacy-vs-canonical context diff for one effective context.

        Reuses ``RuleMergeStore.shadow_verify`` so ``missing`` / ``extra`` /
        ``permission_diff`` are computed against the real legacy matcher.  The
        canonical read path may switch on for this context only when all three
        are 0 — a canonical collapse must never under-expose a rule that the
        legacy path would have injected.
        """
        store = self._open()
        if store is None:
            return None
        try:
            legacy_records = [
                (r.memory_id, legacy_store.list_rule_assignments(r.memory_id))
                for r in legacy_store.list_records()
            ]
        except Exception:
            return None
        try:
            return store.shadow_verify(context, legacy_records)
        except Exception:
            return None


def dedupe_records(
    records: list[Any],
    mapping: dict[str, Any] | None,
    *,
    key: Callable[[Any], tuple] | None = None,
) -> list[Any]:
    """Collapse records that map to the same canonical Definition.

    For every canonical definition, the strongest representative wins
    deterministically.  The default key is higher priority, then locked, then
    confidence, then the lexicographically smallest memory_id; callers that
    already applied audience/status/exclude matching may pass a richer key
    (e.g. effective priority).  Unmapped records pass through unchanged,
    preserving the caller's ordering.
    """
    if not mapping or not records:
        return list(records)
    memory_to_definition = mapping.get("memory_to_definition") or {}
    if key is None:

        def _key(record: Any) -> tuple:
            return (
                -int(getattr(record, "priority", 0) or 0),
                -int(1 if getattr(record, "locked", False) else 0),
                -float(getattr(record, "confidence", 0.0) or 0.0),
                str(getattr(record, "memory_id", "") or ""),
            )

    else:
        _key = key

    by_definition: dict[str, Any] = {}
    representative: dict[str, Any] = {}
    for record in records:
        memory_id = str(getattr(record, "memory_id", "") or "")
        definition_id = memory_to_definition.get(memory_id)
        if definition_id is None:
            continue
        group = by_definition.setdefault(definition_id, [])
        group.append(record)
        current = representative.get(definition_id)
        if current is None or _key(record) < _key(current):
            representative[definition_id] = record

    dropped: set[str] = set()
    for definition_id, members in by_definition.items():
        keep = representative[definition_id]
        for member in members:
            if member is not keep:
                dropped.add(str(getattr(member, "memory_id", "") or ""))

    result: list[Any] = []
    for record in records:
        memory_id = str(getattr(record, "memory_id", "") or "")
        if memory_id in dropped:
            continue
        result.append(record)
    return result


def canonical_read_summary(
    mapping: dict[str, Any] | None,
    records_before: int,
    records_after: int,
) -> dict[str, Any]:
    if mapping is None:
        return {
            "mode": MODE_LEGACY,
            "canonical_definitions": 0,
            "records_before": records_before,
            "records_after": records_after,
            "deduplicated": 0,
        }
    return {
        "mode": mapping.get("mode", MODE_RULE_INTELLIGENCE),
        "group_id": mapping.get("group_id", ""),
        "canonical_definitions": len(mapping.get("definitions", {})),
        "records_before": records_before,
        "records_after": records_after,
        "deduplicated": records_before - records_after,
    }
