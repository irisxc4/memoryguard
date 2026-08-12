"""P1 semantic scope inference and V2 native rule-boundary tests.

Text inference is deliberately pure and explainable.  Persistence is tested
through the V2 native rule port, whose automatic path must keep the trusted
agent audience even when the text asks for a broad audience.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.rule_scope import infer_scope_from_text
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate(workspace: Path) -> None:
    initialize_all(WorkspaceV2Layout(workspace))
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="scope-inference-v2")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="scope-source",
        target_digest="scope-target",
        manifest_digest="scope-manifest",
        digests={"validator_passed": True, "checkpoints": {"rules": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _context(workspace: Path, *, agent: str = "a", admin: bool = False):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"scope-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="team",
        project_ref=str((workspace / "project").resolve()),
        provider="codex",
        runtime_role="root",
        entrypoint="test",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 7},
    )


def _data(result: dict) -> dict:
    assert result["ok"] is True, result
    return result["data"]


def _after_scope(decision: dict) -> dict:
    raw = decision.get("after_json") or decision.get("after") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    assert isinstance(raw, dict), raw
    return dict(raw["scope"])


def test_text_scoped_to_current_project_infers_agent_project():
    result = infer_scope_from_text(
        "current agent + project: run focused tests",
        agent_instance_id="a",
        project_ref="/p",
    )
    assert result.selected.target_type == "agent_project"
    assert result.selected.target_id == "a"
    assert result.selected.project_ref == "/p"
    assert result.selected.confidence >= 0.90
    assert not result.fallback_used


def test_broad_text_never_claims_wide_scope_and_falls_back():
    result = infer_scope_from_text(
        "所有 Agent 都必须用中文提交信息",
        agent_instance_id="a",
        project_ref="/p",
    )
    assert result.fallback_used
    assert result.selected.confidence < 0.80
    assert result.selected.target_type in ("agent", "agent_project")
    assert result.selected.target_id == "a"


def test_no_signal_falls_back_to_safe_current_context():
    with_project = infer_scope_from_text(
        "run the focused test",
        agent_instance_id="a",
        project_ref="/p",
    )
    assert with_project.selected.target_type == "agent_project"
    assert with_project.selected.target_id == "a"
    assert with_project.selected.confidence <= 0.85

    no_project = infer_scope_from_text(
        "run the focused test",
        agent_instance_id="a",
        project_ref="",
    )
    assert no_project.selected.target_type == "agent"
    assert no_project.selected.target_id == "a"


@pytest.mark.parametrize(
    "text, expected_type, expected_fallback",
    [
        ("current agent + project: keep this local", "agent_project", False),
        ("只让当前 Agent 使用中文", "agent", False),
        ("所有 agent 都必须使用中文", "agent_project", True),
        ("以后先运行测试", "agent_project", True),
    ],
)
def test_scope_golden_cases(text, expected_type, expected_fallback):
    result = infer_scope_from_text(text, agent_instance_id="a", project_ref="/p")
    assert result.selected.target_type == expected_type
    assert result.selected.target_id == "a"
    assert result.fallback_used is expected_fallback
    assert any(candidate == result.selected for candidate in result.candidates)


def test_native_v2_creation_records_trusted_agent_scope(tmp_path: Path):
    _activate(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent(
        "a", "team", idempotency_key="scope-bind-a",
    )
    created = _data(_port(tmp_path).dispatch_mcp(
        "memoryguard_rule_create_auto",
        {
            "text": "current agent + project: preserve this rule",
            "idempotency_key": "scope-create-project",
        },
        context=_context(tmp_path),
        generation=7,
        mutation=True,
        state="V2_ACTIVE",
    ))
    scope = _after_scope(created["decision"])
    # The native V2 automatic lifecycle owns the final authorization boundary;
    # it never persists the wider project audience inferred from free text.
    assert scope["target_type"] == "agent"
    assert scope["target_id"] == "a"
    assert created["decision"]["action"] == "rule_create_auto"
    assert RuleV2Store(tmp_path).get_definition(created["definition_id"]) is not None


def test_native_v2_broad_text_stays_narrow_and_auditable(tmp_path: Path):
    _activate(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent(
        "a", "team", idempotency_key="scope-bind-b",
    )
    created = _data(_port(tmp_path).dispatch_mcp(
        "memoryguard_rule_create_auto",
        {
            "text": "所有 Agent 都必须使用中文",
            "idempotency_key": "scope-create-broad",
        },
        context=_context(tmp_path),
        generation=7,
        mutation=True,
        state="V2_ACTIVE",
    ))
    scope = _after_scope(created["decision"])
    assert scope["target_type"] == "agent"
    assert scope["target_id"] == "a"
    assert scope["target_type"] not in {"group", "project", "system"}
    assert created["decision"]["source_ref"] == "native-v2:mcp:rule_create_auto"


def test_native_v2_decision_records_scope_details(tmp_path: Path):
    _activate(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent(
        "a", "team", idempotency_key="scope-bind-c",
    )
    created = _data(_port(tmp_path).dispatch_mcp(
        "memoryguard_rule_create_auto",
        {"text": "record the trusted scope", "idempotency_key": "scope-create-audit"},
        context=_context(tmp_path),
        generation=7,
        mutation=True,
        state="V2_ACTIVE",
    ))
    decision = created["decision"]
    assert decision["decision_id"]
    assert decision["before_json"]
    after = _after_scope(decision)
    assert after["target_id"] == "a"
    assert after["effect"] == "include"
