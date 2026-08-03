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
from collections import Counter
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
                projection = self._projection_status_for_group(
                    projection_status_fn,
                )
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
                    for field in ("migration_loss", "binding_contribution_diff"):
                        if field == "migration_loss":
                            metric = self._migration_loss_for_group(
                                store, legacy_store,
                            )
                        else:
                            metric = self._derive_binding_contribution_diff(store)
                            if metric is not _MISSING:
                                checks["binding_contribution_diff_source"] = (
                                    "persisted_contributions"
                                )
                        if metric is _MISSING:
                            allow_aggregate = (
                                not callable(getattr(store, "get_source_link", None))
                                if field == "migration_loss" else
                                not callable(
                                    getattr(store, "list_binding_contributions", None),
                                )
                            )
                            metric = self._metric_for_group(
                                metrics, field,
                                allow_aggregate=allow_aggregate,
                            )
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
    def _projection_from_scopes(
        value: Mapping[str, Any],
        group_id: str,
    ) -> Mapping[str, Any]:
        scopes = value.get("scopes", _MISSING)
        if not isinstance(scopes, (list, tuple)):
            for key in ("per_group", "group_metrics", "groups", "by_group"):
                grouped = value.get(key, _MISSING)
                if isinstance(grouped, Mapping):
                    selected = grouped.get(group_id, _MISSING)
                    if isinstance(selected, Mapping):
                        return selected
            return value

        selected = [
            scope for scope in scopes
            if isinstance(scope, Mapping)
            and str(scope.get("scope_id", "") or "") == group_id
        ]
        if not selected:
            # Pre-group stores used one global checkpoint.  Keep it as the
            # legacy fallback, never include another concrete group's state.
            selected = [
                scope for scope in scopes
                if isinstance(scope, Mapping)
                and str(scope.get("scope_id", "") or "") == "rule-intelligence"
            ]
        return {
            **dict(value),
            "scopes": selected,
            "projection_lag": sum(
                int(scope.get("projection_lag", 0) or 0)
                for scope in selected
            ),
            "projection_error": next(
                (
                    str(scope.get("projection_error", "") or "")
                    for scope in selected
                    if scope.get("projection_error")
                ),
                "",
            ),
        }

    def _projection_status_for_group(
        self,
        projection_status_fn: Callable[..., Any],
    ) -> Mapping[str, Any] | None:
        """Read projection state for this group, preserving legacy scope."""
        try:
            value = projection_status_fn(group_ids={self.group_id})
            group_call_succeeded = True
        except TypeError:
            try:
                value = projection_status_fn()
            except Exception:
                return None
            group_call_succeeded = False
        except Exception:
            return None
        if not isinstance(value, Mapping):
            return None

        scoped = self._projection_from_scopes(value, self.group_id)
        if (
            not group_call_succeeded
            or "scopes" not in value
            or scoped.get("scopes")
        ):
            return scoped

        # The current Store uses a concrete group scope when present, while
        # old workspaces may only have the global legacy checkpoint.
        try:
            global_value = projection_status_fn()
        except Exception:
            return scoped
        if not isinstance(global_value, Mapping):
            return scoped
        return self._projection_from_scopes(global_value, self.group_id)

    @staticmethod
    def _row_group_id(row: Any) -> str:
        if isinstance(row, Mapping):
            return str(
                row.get("share_group_id", row.get("group_id", "")) or ""
            ).strip()
        return str(
            getattr(row, "share_group_id", getattr(row, "group_id", ""))
            or ""
        ).strip()

    def _list_group_rows(
        self,
        fn: Callable[..., Any],
        *,
        active: bool | None = None,
        status: str | None = None,
    ) -> list[Any] | Any:
        """Call a public list API with group scope; filter old APIs safely."""
        kwargs: dict[str, Any] = {"share_group_id": self.group_id}
        if active is not None:
            kwargs["active"] = active
        if status is not None:
            kwargs["status"] = status
        try:
            rows = fn(**kwargs)
        except TypeError:
            fallback = {
                key: value for key, value in kwargs.items()
                if key != "share_group_id"
            }
            try:
                rows = fn(**fallback)
            except Exception:
                return _MISSING
            if not isinstance(rows, (list, tuple)):
                return _MISSING
            if not rows:
                return []
            filtered: list[Any] = []
            for row in rows:
                row_group = self._row_group_id(row)
                if not row_group:
                    return _MISSING
                if row_group == self.group_id:
                    filtered.append(row)
            return filtered
        except Exception:
            return _MISSING

        if not isinstance(rows, (list, tuple)):
            return _MISSING
        # A group-aware API is authoritative.  If it still annotates rows,
        # reject cross-group leakage rather than trusting a malformed result.
        annotated = [self._row_group_id(row) for row in rows]
        if any(group and group != self.group_id for group in annotated):
            return _MISSING
        return list(rows)

    def _derive_binding_contribution_diff(self, store: Any) -> Any:
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
        bindings = self._list_group_rows(bindings_fn, status="active")
        contributions = self._list_group_rows(contributions_fn, active=True)
        if bindings is _MISSING or contributions is _MISSING:
            return _MISSING
        binding_ids = {
            (
                str(
                    binding.get("binding_id", "")
                    if isinstance(binding, Mapping)
                    else getattr(binding, "binding_id", "")
                ).strip(),
                str(
                    binding.get("definition_id", "")
                    if isinstance(binding, Mapping)
                    else getattr(binding, "definition_id", "")
                ).strip(),
            )
            for binding in bindings
        }
        contribution_ids = {
            (
                str(
                    row.get("binding_id", "")
                    if isinstance(row, Mapping)
                    else getattr(row, "binding_id", "")
                ).strip(),
                str(
                    row.get("definition_id", "")
                    if isinstance(row, Mapping)
                    else getattr(row, "definition_id", "")
                ).strip(),
            )
            for row in contributions
        }
        return len(binding_ids.symmetric_difference(contribution_ids))

    def _metric_for_group(
        self,
        metrics: Mapping[str, Any],
        field: str,
        *,
        allow_aggregate: bool,
    ) -> Any:
        for key in ("per_group", "group_metrics", "groups", "by_group"):
            grouped = metrics.get(key, _MISSING)
            if isinstance(grouped, Mapping) and self.group_id in grouped:
                value = grouped[self.group_id]
                if isinstance(value, Mapping):
                    return value.get(field, _MISSING)
        value = metrics.get(field, _MISSING)
        if isinstance(value, Mapping):
            return value.get(self.group_id, _MISSING)
        # A global zero cannot hide a non-zero current-group value.  Preserve
        # this safe compatibility fact for old callers that do not provide a
        # legacy store or per-group metric shape; non-zero aggregates remain
        # unavailable unless current-group evidence was obtained above.
        if not allow_aggregate and value == 0:
            return 0
        return value if allow_aggregate else _MISSING

    def _migration_loss_for_group(
        self,
        store: Any,
        legacy_store: Any | None,
    ) -> Any:
        """Compute migration loss from current group's public source links."""
        if legacy_store is None:
            return _MISSING
        list_records = getattr(legacy_store, "list_records", None)
        get_source_link = getattr(store, "get_source_link", None)
        get_definition = getattr(store, "get_definition", None)
        resolve_canonical = getattr(store, "resolve_canonical", None)
        if not all(
            callable(fn)
            for fn in (list_records, get_source_link, get_definition, resolve_canonical)
        ):
            return _MISSING
        legacy_group = str(getattr(legacy_store, "group_id", "") or "").strip()
        if legacy_group and legacy_group != self.group_id:
            return _MISSING
        try:
            records = list_records()
        except Exception:
            return _MISSING

        loss = 0
        links: list[Mapping[str, Any]] = []
        for record in records:
            memory_id = str(getattr(record, "memory_id", "") or "")
            try:
                link = get_source_link(self.group_id, memory_id)
            except Exception:
                return _MISSING
            if isinstance(link, Mapping):
                links.append(link)
            policy = str(getattr(record, "injection_policy", "") or "")
            status = getattr(record, "status", "")
            status = str(getattr(status, "value", status) or "")
            if policy == "always" and status != "deleted" and link is None:
                loss += 1

        for link in links:
            canonical = str(link.get("canonical_definition_id", "") or "")
            if not canonical:
                continue
            try:
                target = get_definition(resolve_canonical(canonical))
            except Exception:
                return _MISSING
            target_status = getattr(target, "status", "") if target else ""
            target_status = getattr(target_status, "value", target_status)
            if target is None or str(target_status or "") != "active":
                loss += 1
        return loss

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
            selected = self._shadow_for_group(shadow_summary)
            if selected is not None:
                return selected

        if legacy_store is not None and context is not None:
            try:
                value = self.shadow_compare(legacy_store, context)
            except Exception:
                value = None
            if isinstance(value, Mapping):
                return value

        if metrics is not None:
            metric_shadow = metrics.get("shadow_summary", _MISSING)
            if isinstance(metric_shadow, Mapping):
                selected = self._shadow_for_group(metric_shadow)
                if selected is not None:
                    return selected

        for name in ("shadow_summary", "get_shadow_summary"):
            candidate = getattr(store, name, None)
            if isinstance(candidate, Mapping):
                selected = self._shadow_for_group(candidate)
                if selected is not None:
                    return selected
            if not callable(candidate):
                continue
            values: list[Any] = []
            for kwargs in (
                {"share_group_id": self.group_id},
                {"group_id": self.group_id},
                {},
            ):
                try:
                    values.append(candidate(**kwargs))
                    break
                except TypeError:
                    continue
                except Exception:
                    break
            if not values and legacy_store is not None and context is not None:
                try:
                    values.append(candidate(legacy_store, context))
                except Exception:
                    pass
            for value in values:
                if isinstance(value, Mapping):
                    selected = self._shadow_for_group(value)
                    if selected is not None:
                        return selected

        return None

    def _shadow_for_group(
        self,
        value: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Select a public shadow result without accepting another group."""
        direct = value.get(self.group_id, _MISSING)
        if isinstance(direct, Mapping):
            return direct
        for key in ("per_group", "group_metrics", "groups", "by_group"):
            grouped = value.get(key, _MISSING)
            if isinstance(grouped, Mapping):
                selected = grouped.get(self.group_id, _MISSING)
                return selected if isinstance(selected, Mapping) else None
        declared_group = str(value.get("group_id", "") or "").strip()
        if declared_group and declared_group != self.group_id:
            return None
        return value

    @staticmethod
    def _shadow_value(value: Any, key: str, default: Any = "") -> Any:
        if isinstance(value, Mapping):
            return value.get(key, default)
        return getattr(value, key, default)

    @classmethod
    def _shadow_audience_key(
        cls,
        value: Any,
        share_group_id: str,
        *,
        priority_field: str,
    ) -> tuple[str, str, str, str, str, str, int, str] | None:
        """Normalize one legacy assignment or P3 binding audience.

        Permission equivalence is about the audience boundary, not whether a
        target type happens to be broad.  Legacy provider/runtime-role
        assignments encode their dedicated value in ``target_id``; P3 keeps
        that value in the dedicated field as well, so both representations
        must collapse to the same key.
        """
        target_type = str(
            cls._shadow_value(value, "target_type", "") or ""
        )
        target_id = str(cls._shadow_value(value, "target_id", "") or "")
        from .rule_scope import canonical_project_ref

        project_ref = canonical_project_ref(
            str(cls._shadow_value(value, "project_ref", "") or "")
        )
        provider = str(cls._shadow_value(value, "provider", "") or "")
        runtime_role = str(
            cls._shadow_value(value, "runtime_role", "") or ""
        )
        effect = str(
            cls._shadow_value(value, "effect", "include") or "include"
        )
        raw_priority = cls._shadow_value(value, priority_field, 0)
        try:
            priority = int(raw_priority or 0)
        except (TypeError, ValueError):
            return None

        if target_type == "project":
            if not project_ref:
                project_ref = canonical_project_ref(target_id)
            target_id = ""
        if target_type == "provider" and not provider:
            provider = target_id
        if target_type == "runtime_role" and not runtime_role:
            runtime_role = target_id
        return (
            target_type,
            target_id,
            project_ref,
            provider.casefold(),
            runtime_role.casefold(),
            effect,
            priority,
            str(share_group_id or ""),
        )

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

        Recomputes ``missing`` / ``extra`` / ``permission_diff`` from public
        group-scoped APIs.  The
        canonical read path may switch on for this context only when all three
        are 0 — a canonical collapse must never under-expose a rule that the
        legacy path would have injected.
        """
        store = self._open()
        if store is None:
            return None
        if str(getattr(context, "share_group_id", "") or "") != self.group_id:
            return None
        try:
            legacy_records = [
                (r.memory_id, legacy_store.list_rule_assignments(r.memory_id))
                for r in legacy_store.list_records()
            ]
        except Exception:
            return None
        bindings_fn = getattr(store, "list_bindings", None)
        list_evidence = getattr(store, "list_evidence", None)
        get_definition = getattr(store, "get_definition", None)
        if not all(
            callable(fn)
            for fn in (bindings_fn, list_evidence, get_definition)
        ):
            return None
        bindings = self._list_group_rows(bindings_fn, status="active")
        if bindings is _MISSING:
            return None

        from .rule_scope import assignment_matches, normalize_assignment
        from .schema_v3 import RuleAssignment

        legacy_matched: set[str] = set()
        legacy_audiences: Counter[tuple[Any, ...]] = Counter()
        for memory_id, assignments in legacy_records:
            for assignment in assignments:
                try:
                    normalized = normalize_assignment(assignment)
                except ValueError:
                    continue
                if not assignment_matches(normalized, context):
                    continue
                legacy_matched.add(memory_id)
                audience = self._shadow_audience_key(
                    normalized,
                    context.share_group_id,
                    priority_field="priority_override",
                )
                if audience is None:
                    return None
                legacy_audiences[audience] += 1

        legacy_ids = {memory_id for memory_id, _ in legacy_records}
        new_matched: set[str] = set()
        new_audiences: Counter[tuple[Any, ...]] = Counter()
        for binding in bindings:
            target_type = str(
                self._shadow_value(binding, "target_type", "") or ""
            )
            try:
                binding_assignment = RuleAssignment(
                    memory_id=str(
                        self._shadow_value(binding, "definition_id", "") or ""
                    ),
                    target_type=target_type,
                    target_id=str(
                        self._shadow_value(binding, "target_id", "") or ""
                    ),
                    project_ref=str(
                        self._shadow_value(binding, "project_ref", "") or ""
                    ),
                    effect=str(
                        self._shadow_value(binding, "effect", "include")
                        or "include"
                    ),
                )
            except Exception:
                continue
            if not assignment_matches(binding_assignment, context):
                continue
            audience = self._shadow_audience_key(
                binding,
                context.share_group_id,
                priority_field="priority",
            )
            if audience is None:
                return None
            new_audiences[audience] += 1
            definition_id = str(
                self._shadow_value(binding, "definition_id", "") or ""
            )
            try:
                definition = get_definition(definition_id)
            except Exception:
                return None
            definition_status = (
                self._shadow_value(definition, "status", "")
                if definition else ""
            )
            definition_status = str(
                getattr(definition_status, "value", definition_status) or ""
            )
            if definition is None or definition_status not in {"active", "alias"}:
                continue
            definition_id = str(
                self._shadow_value(definition, "definition_id", "")
                or definition_id
            )
            try:
                evidence_rows = list_evidence(definition_id=definition_id)
            except Exception:
                return None
            for evidence in evidence_rows:
                source_id = str(getattr(evidence, "source_rule_id", "") or "")
                if source_id and source_id in legacy_ids:
                    new_matched.add(source_id)

        permission_missing = legacy_audiences - new_audiences
        permission_extra = new_audiences - legacy_audiences
        permission_diff = sum(permission_missing.values()) + sum(
            permission_extra.values()
        )
        return {
            "missing": sorted(legacy_matched - new_matched),
            "extra": sorted(new_matched - legacy_matched),
            "permission_diff": permission_diff,
        }


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
