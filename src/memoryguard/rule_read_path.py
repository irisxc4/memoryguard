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
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

# Read-path modes.  "auto" prefers the canonical layer when it has data for the
# group and otherwise behaves exactly like "legacy".
MODE_AUTO = "auto"
MODE_LEGACY = "legacy"
MODE_RULE_INTELLIGENCE = "rule-intelligence"
_MODES = {MODE_AUTO, MODE_LEGACY, MODE_RULE_INTELLIGENCE}
_MISSING = object()


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
        self._last_readiness: dict[str, Any] = {
            "ready": False,
            "group_id": self.group_id,
            "failures": ["not_checked"],
            "wiring_requirements": [],
        }

    @property
    def last_readiness(self) -> dict[str, Any]:
        """Return last public readiness decision for diagnostics/tests."""
        return dict(self._last_readiness)

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

    def canonical_readiness(
        self,
        *,
        shadow_summary: Mapping[str, Any] | None = None,
        legacy_store: Any | None = None,
        context: Any | None = None,
    ) -> dict[str, Any]:
        """Check whether canonical enforcement may replace legacy reads.

        This is deliberately fail-closed.  It consumes only public Store
        state/metrics and the normalized shadow audience diff; it never
        infers permission changes from binding target types or private tables.
        """
        failures: list[str] = []
        wiring_requirements: list[str] = []
        checks: dict[str, Any] = {}
        store = self._open()

        if store is None:
            failures.append("store_unavailable")
        else:
            projection_status_fn = getattr(store, "projection_status", None)
            if not callable(projection_status_fn):
                failures.append("projection_status_unavailable")
                wiring_requirements.append(
                    "Store.projection_status() must expose projection_lag and projection_error"
                )
            else:
                try:
                    projection = projection_status_fn()
                except Exception:
                    projection = None
                if not isinstance(projection, Mapping):
                    failures.append("projection_status_unavailable")
                    wiring_requirements.append(
                        "Store.projection_status() must expose projection_lag and projection_error"
                    )
                else:
                    lag = projection.get("projection_lag", _MISSING)
                    error = projection.get("projection_error", _MISSING)
                    checks["projection_lag"] = (
                        None if lag is _MISSING else lag
                    )
                    checks["projection_error"] = (
                        None if error is _MISSING else error
                    )
                    if lag is _MISSING:
                        failures.append("projection_lag_unavailable")
                        wiring_requirements.append(
                            "Store.projection_status() must expose projection_lag"
                        )
                    elif lag != 0:
                        failures.append("projection_lag_nonzero")
                    if error is _MISSING:
                        failures.append("projection_error_unavailable")
                        wiring_requirements.append(
                            "Store.projection_status() must expose projection_error"
                        )
                    elif error not in ("", None):
                        failures.append("projection_error_present")

            metrics_fn = getattr(store, "metrics", None)
            metrics: Mapping[str, Any] | None = None
            if not callable(metrics_fn):
                failures.append("metrics_unavailable")
                wiring_requirements.append(
                    "Store.metrics() must expose migration_loss and binding_contribution_diff"
                )
            else:
                try:
                    value = metrics_fn()
                except Exception:
                    value = None
                if not isinstance(value, Mapping):
                    failures.append("metrics_unavailable")
                    wiring_requirements.append(
                        "Store.metrics() must expose migration_loss and binding_contribution_diff"
                    )
                else:
                    metrics = value
                    if "binding_contribution_diff" not in metrics:
                        derived = self._derive_binding_contribution_diff(store)
                        if derived is not _MISSING:
                            metrics = dict(metrics)
                            metrics["binding_contribution_diff"] = derived
                            checks["binding_contribution_diff_source"] = (
                                "persisted_contributions"
                            )
                    for field in ("migration_loss", "binding_contribution_diff"):
                        metric = metrics.get(field, _MISSING)
                        checks[field] = None if metric is _MISSING else metric
                        if metric is _MISSING:
                            failures.append(f"{field}_unavailable")
                            wiring_requirements.append(
                                f"Store.metrics() must expose {field}"
                            )
                        elif metric != 0:
                            failures.append(f"{field}_nonzero")

            shadow = self._resolve_shadow_summary(
                store,
                shadow_summary=shadow_summary,
                legacy_store=legacy_store,
                context=context,
                metrics=metrics,
            )
            if not isinstance(shadow, Mapping):
                failures.append("shadow_summary_unavailable")
                wiring_requirements.append(
                    "Store.shadow_summary() or explicit normalized shadow_summary is required"
                )
            else:
                normalized_shadow: dict[str, Any] = {}
                for field in ("missing", "extra", "permission_diff"):
                    diff = shadow.get(field, _MISSING)
                    normalized_shadow[field] = (
                        None if diff is _MISSING else diff
                    )
                    if diff is _MISSING:
                        failures.append(f"shadow_{field}_unavailable")
                    elif not self._is_zero_diff(diff):
                        failures.append(f"shadow_{field}_nonzero")
                checks["shadow"] = normalized_shadow

        result = {
            "ready": not failures,
            "group_id": self.group_id,
            "checks": checks,
            "failures": failures,
            "wiring_requirements": wiring_requirements,
        }
        self._last_readiness = result
        return result

    @staticmethod
    def _derive_binding_contribution_diff(store: Any) -> Any:
        """Derive contribution materialization diff from public persisted APIs.

        Real ``RuleMergeStore`` versions before the aggregate metric was added
        still expose both active bindings and active source contributions.  A
        test double or an incomplete Store does not get an optimistic zero:
        missing APIs remain ``_MISSING`` and keep the canonical gate closed.
        """
        bindings_fn = getattr(store, "list_bindings", None)
        contributions_fn = getattr(store, "list_binding_contributions", None)
        if not callable(bindings_fn) or not callable(contributions_fn):
            return _MISSING
        try:
            bindings = bindings_fn(status="active")
            contributions = contributions_fn(active=True)
        except (TypeError, AttributeError, OSError, RuntimeError):
            return _MISSING
        except Exception:
            return _MISSING
        if not isinstance(bindings, (list, tuple)) or not isinstance(
            contributions, (list, tuple),
        ):
            return _MISSING
        binding_ids = {
            str(getattr(binding, "binding_id", "") or "")
            for binding in bindings
        }
        contribution_ids = {
            str(
                row.get("binding_id", "")
                if isinstance(row, Mapping)
                else getattr(row, "binding_id", "")
                or ""
            )
            for row in contributions
        }
        return len(binding_ids.symmetric_difference(contribution_ids))

    def _resolve_shadow_summary(
        self,
        store: Any,
        *,
        shadow_summary: Mapping[str, Any] | None,
        legacy_store: Any | None,
        context: Any | None,
        metrics: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        """Resolve an already-normalized public shadow audience diff."""
        if isinstance(shadow_summary, Mapping):
            return shadow_summary

        if metrics is not None:
            metric_shadow = metrics.get("shadow_summary", _MISSING)
            if isinstance(metric_shadow, Mapping):
                return metric_shadow

        for name in ("shadow_summary", "get_shadow_summary"):
            candidate = getattr(store, name, None)
            if isinstance(candidate, Mapping):
                return candidate
            if not callable(candidate):
                continue
            try:
                value = candidate()
            except TypeError:
                try:
                    value = candidate(legacy_store, context)
                except Exception:
                    continue
            except Exception:
                continue
            if isinstance(value, Mapping):
                return value

        if legacy_store is not None and context is not None:
            try:
                value = self.shadow_compare(
                    legacy_store, context,
                )
            except Exception:
                return None
            return value if isinstance(value, Mapping) else None
        return None

    @staticmethod
    def _is_zero_diff(value: Any) -> bool:
        """Accept only normalized empty diff values; unknown stays unsafe."""
        if value is None:
            return False
        if isinstance(value, (str, bytes, Mapping)):
            return len(value) == 0
        if isinstance(value, (list, tuple, set, frozenset)):
            return len(value) == 0
        return value == 0

    def resolve_canonical_map(
        self,
        known_memory_ids: set[str] | None = None,
        *,
        shadow_summary: Mapping[str, Any] | None = None,
        legacy_store: Any | None = None,
        context: Any | None = None,
    ) -> dict[str, Any] | None:
        """Build ``memory_id -> definition_id`` for active definitions bound to
        this group.  Returns None when the layer has no data for the group.

        The mapping is derived from Evidence: every ``source_rule_id`` of a
        bound Definition is a legacy memory_id (backfill/dual-write anchor).
        Stale evidence whose source id no longer exists in the legacy store is
        dropped so the canonical read never invents records.
        """
        readiness = self.canonical_readiness(
            shadow_summary=shadow_summary,
            legacy_store=legacy_store,
            context=context,
        )
        if not readiness["ready"]:
            return None

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
            "readiness": readiness,
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
