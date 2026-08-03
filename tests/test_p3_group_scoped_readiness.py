"""P3 canonical-readiness group isolation.

Readiness is a per-group enforcement gate.  A broken projection, migration,
binding materialization, or shadow comparison in another group must not close
the current group's canonical read path.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from memoryguard.rule_read_path import RuleReadPath
from memoryguard.schema_v3 import (
    EffectiveAgentContext,
    RuleAssignment,
    SharedMemoryStatus,
)


class _LegacyGroup:
    group_id = "current"

    def __init__(
        self,
        *,
        assignment_target: str = "agent-current",
        target_type: str = "agent",
        effect: str = "include",
        priority_override: int | None = None,
    ):
        self._assignment_target = assignment_target
        self._target_type = target_type
        self._effect = effect
        self._priority_override = priority_override

    def list_records(self):
        return [
            SimpleNamespace(
                memory_id="current-memory",
                injection_policy="always",
                status=SharedMemoryStatus.ACTIVE,
            )
        ]

    def list_rule_assignments(self, memory_id):
        return [
            RuleAssignment(
                memory_id=memory_id,
                target_type=self._target_type,
                target_id=self._assignment_target,
                effect=self._effect,
                priority_override=self._priority_override,
            )
        ]


class _ScopedStore:
    def __init__(self):
        self.projection_calls = []
        self.projection = {
            "current": {"projection_lag": 0, "projection_error": ""},
            "other": {"projection_lag": 9, "projection_error": "other failed"},
        }
        self.source_links = {"current": {"current-memory": "definition-current"}}
        self.binding_rows = {
            "current": [
                SimpleNamespace(
                    binding_id="binding-current",
                    definition_id="definition-current",
                    share_group_id="current",
                    target_type="agent",
                    target_id="agent-current",
                    project_ref="",
                    provider="",
                    runtime_role="",
                    effect="include",
                )
            ],
            "other": [
                SimpleNamespace(
                    binding_id="binding-other",
                    definition_id="definition-other",
                    share_group_id="other",
                    target_type="system",
                    target_id="",
                    project_ref="",
                    provider="",
                    runtime_role="",
                    effect="include",
                )
            ],
        }
        self.contribution_rows = {
            "current": [{
                "binding_id": "binding-current",
                "definition_id": "definition-current",
                "share_group_id": "current",
            }],
            "other": [],
        }
        self.evidence_rows = {
            "definition-current": [
                SimpleNamespace(source_rule_id="current-memory")
            ],
            "definition-other": [],
        }

    def projection_status(self, group_ids=None):
        self.projection_calls.append(group_ids)
        groups = set(self.projection) if group_ids is None else set(group_ids)
        scopes = [
            {"scope_id": group, **self.projection[group]}
            for group in sorted(groups)
            if group in self.projection
        ]
        return {
            "scopes": scopes,
            "projection_lag": sum(item["projection_lag"] for item in scopes),
            "projection_error": next(
                (item["projection_error"] for item in scopes if item["projection_error"]),
                "",
            ),
        }

    def metrics(self):
        # Deliberately aggregate values.  Readiness must derive current-group
        # values from the public group-aware APIs below instead.
        return {"migration_loss": 7, "binding_contribution_diff": 7}

    def list_definitions(self, status="active"):
        return [
            SimpleNamespace(
                definition_id="definition-current",
                status="active",
                rule_strength=0.0,
                maturity_state="mature",
            )
        ]

    def list_bindings(self, share_group_id=None, status="active"):
        if share_group_id is None:
            return [row for rows in self.binding_rows.values() for row in rows]
        return list(self.binding_rows.get(share_group_id, []))

    def list_binding_contributions(
        self, *, share_group_id=None, source_memory_id=None,
        binding_id=None, active=None,
    ):
        if share_group_id is None:
            return [row for rows in self.contribution_rows.values() for row in rows]
        return list(self.contribution_rows.get(share_group_id, []))

    def list_evidence(self, definition_id=None):
        return list(self.evidence_rows.get(definition_id, []))

    def get_source_link(self, share_group_id, memory_id):
        definition_id = self.source_links.get(share_group_id, {}).get(memory_id)
        if not definition_id:
            return None
        return {
            "share_group_id": share_group_id,
            "memory_id": memory_id,
            "canonical_definition_id": definition_id,
        }

    def resolve_canonical(self, definition_id):
        return definition_id

    def get_definition(self, definition_id):
        if definition_id == "definition-current":
            return SimpleNamespace(
                definition_id=definition_id,
                status="active",
            )
        return None


def _reader(store=None):
    read = RuleReadPath(".", "current")
    read._store = store or _ScopedStore()
    return read


def _context():
    return EffectiveAgentContext("agent-current", "current")


def test_unrelated_group_state_does_not_close_current_canonical_read():
    store = _ScopedStore()
    read = _reader(store)

    readiness = read.canonical_readiness(
        legacy_store=_LegacyGroup(),
        context=_context(),
    )

    assert readiness["ready"] is True
    assert readiness["checks"]["migration_loss"] == 0
    assert readiness["checks"]["binding_contribution_diff"] == 0
    assert readiness["checks"]["shadow"] == {
        "missing": [], "extra": [], "permission_diff": 0,
    }
    assert store.projection_calls[0] == {"current"}


@pytest.mark.parametrize(
    "kind",
    ["projection", "migration", "binding", "shadow_missing", "shadow_extra", "shadow_permission"],
)
def test_current_group_readiness_fail_closed(kind):
    store = _ScopedStore()
    legacy = _LegacyGroup()
    context = _context()

    if kind == "projection":
        store.projection["current"] = {
            "projection_lag": 1,
            "projection_error": "current failed",
        }
    elif kind == "migration":
        store.source_links["current"] = {}
    elif kind == "binding":
        store.contribution_rows["current"] = []
    elif kind == "shadow_missing":
        store.evidence_rows["definition-current"] = []
    elif kind == "shadow_extra":
        legacy = _LegacyGroup(assignment_target="other-agent")
    elif kind == "shadow_permission":
        store.binding_rows["current"][0].target_type = "system"

    readiness = _reader(store).canonical_readiness(
        legacy_store=legacy,
        context=context,
    )

    assert readiness["ready"] is False
    if kind == "projection":
        assert "projection_lag_nonzero" in readiness["failures"]
        assert "projection_error_present" in readiness["failures"]
    elif kind == "migration":
        assert "migration_loss_nonzero" in readiness["failures"]
    elif kind == "binding":
        assert "binding_contribution_diff_nonzero" in readiness["failures"]
    elif kind == "shadow_missing":
        assert "shadow_missing_nonzero" in readiness["failures"]
    elif kind == "shadow_extra":
        assert "shadow_extra_nonzero" in readiness["failures"]
    else:
        assert "shadow_permission_diff_nonzero" in readiness["failures"]


@pytest.mark.parametrize(
    ("target_type", "target_id", "context_kwargs"),
    [
        ("system", "", {}),
        ("group", "current", {}),
        ("provider", "codex", {"provider": "codex"}),
    ],
)
def test_migrated_wide_audience_matches_legacy_permission(
    target_type, target_id, context_kwargs,
):
    store = _ScopedStore()
    binding = store.binding_rows["current"][0]
    binding.target_type = target_type
    binding.target_id = target_id
    binding.provider = "codex" if target_type == "provider" else ""

    shadow = _reader(store).shadow_compare(
        _LegacyGroup(
            assignment_target=target_id,
            target_type=target_type,
        ),
        EffectiveAgentContext(
            "agent-current", "current", **context_kwargs,
        ),
    )

    assert shadow == {"missing": [], "extra": [], "permission_diff": 0}


def test_new_wide_audience_is_a_permission_expansion():
    store = _ScopedStore()
    binding = store.binding_rows["current"][0]
    binding.target_type = "group"
    binding.target_id = "current"

    shadow = _reader(store).shadow_compare(
        _LegacyGroup(),
        _context(),
    )

    assert shadow["missing"] == []
    assert shadow["extra"] == []
    assert shadow["permission_diff"] > 0


@pytest.mark.parametrize(
    ("binding_effect", "binding_priority"),
    [("exclude", 1), ("include", 2)],
)
def test_effect_or_priority_change_is_a_permission_diff(
    binding_effect, binding_priority,
):
    store = _ScopedStore()
    binding = store.binding_rows["current"][0]
    binding.target_type = "group"
    binding.target_id = "current"
    binding.effect = binding_effect
    binding.priority = binding_priority

    shadow = _reader(store).shadow_compare(
        _LegacyGroup(
            assignment_target="current",
            target_type="group",
            priority_override=1,
        ),
        _context(),
    )

    assert shadow["permission_diff"] > 0


def test_other_group_wide_audience_does_not_change_current_permission_diff():
    store = _ScopedStore()
    binding = store.binding_rows["current"][0]
    binding.target_type = "group"
    binding.target_id = "current"

    shadow = _reader(store).shadow_compare(
        _LegacyGroup(assignment_target="current", target_type="group"),
        _context(),
    )

    assert shadow["permission_diff"] == 0
