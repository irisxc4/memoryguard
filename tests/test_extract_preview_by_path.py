"""extract_preview_by_path：已发现会话无需先勾选也可预览。"""
from __future__ import annotations

from pathlib import Path
import os
import tempfile
import pytest


@pytest.fixture(autouse=True)
def _isolated_test_env(monkeypatch):
    """Keep admin/anonymous test capabilities scoped to each test."""
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")


def _activate_v2_workspace(root: Path) -> None:
    """Path preview enters through the public V2 upgrade/activation contract."""
    from memoryguard.governance_v2 import GovernanceV2
    from memoryguard.migration.upgrade import run_upgrade

    ready = run_upgrade(root, data_home=root, apply=True)
    assert ready["status"] == "V2_READY", ready
    # The extraction writer requires the V2 governance ledger to exist before
    # the explicit activation confirmation is accepted.
    GovernanceV2(root)
    active = run_upgrade(
        root,
        data_home=root,
        apply=True,
        confirm="V2_ACTIVE",
    )
    assert active["v2_active"] is True, active


def test_extract_preview_by_path_allows_discovered_session(tmp_path, monkeypatch):
    from memoryguard.access_context import AccessContext
    from memoryguard.gui import GovernanceApi
    from memoryguard.agent_locator import AgentLocator
    from memoryguard.runtime_v2.group_native import GroupControlService, personal_group_id
    from memoryguard.schema_v3 import AgentInstance, DiscoveryLedger, TargetCapability

    _activate_v2_workspace(tmp_path)
    home = tmp_path / "home"
    sess = home / ".codex" / "sessions" / "2026" / "07" / "28"
    sess.mkdir(parents=True)
    target = sess / "rollout-demo.jsonl"
    target.write_text(
        '{"type":"message","payload":{"role":"user","content":"Please remember I prefer pytest"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    file_info = {
        "path": str(target),
        "surface_id": "codex_sessions:x",
        "scope": "user",
        "scope_source": "profile_declared",
        "project_ref": "",
        "discovery_object_id": "d1",
        "ingestion_policy": "extract_candidates",
        "ownership": "agent_managed",
        "target_role": "none",
        "default_selected": False,
        "default_reason": "extract",
        "status": "found",
        "confidence": 0.9,
        "selectable": False,
        "display_only": True,
        "is_file_node": True,
    }
    inst = AgentInstance(
        instance_id="codex-x",
        profile_id="codex@profile-1",
        product="codex",
        surfaces=[{
            "surface_id": "codex_sessions",
            "resolved_path": str(home / ".codex" / "sessions"),
            "status": "found",
            "scope": "user",
            "category": "conversation_history",
            "ingestion_policy": "extract_candidates",
            "ownership": "agent_managed",
            "target_role": "none",
            "classification_confidence": 0.9,
            "file_globs": ["**/*.jsonl"],
        }],
        target_capability=TargetCapability.EXPORT_ONLY,
    )

    def fake_tree(self, instance_id):
        return {
            "instance_id": instance_id,
            "product": "codex",
            "scopes": [{
                "scope": "user",
                "scope_source": "profile_declared",
                "categories": [{
                    "category": "conversation_history",
                    "files": [file_info],
                }],
            }],
            "discovery_notes": [],
        }

    monkeypatch.setattr(
        AgentLocator,
        "detect_instances",
        lambda self: ([inst], {"codex-x": DiscoveryLedger(instance_id="codex-x")}),
    )
    monkeypatch.setattr(AgentLocator, "get_selection_tree", fake_tree)

    agent = "codex-x"
    GroupControlService(tmp_path, write=True).bind_agent(
        agent,
        personal_group_id(agent),
    )
    api = GovernanceApi(
        tmp_path,
        _trusted_access_context=AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="extract-preview-session",
            session_source="transport",
            session_trusted=True,
        ),
    )
    added = api.add_source(str(target), "selected_file", "session", True)
    assert added["ok"] is True, added
    out = api.extract_preview_by_path(str(target), agent_instance_id="codex-x")
    assert out["ok"] is True, out
    data = out["data"]
    assert data.get("extract_id")
    assert isinstance(data.get("candidates"), list)


def test_extract_preview_by_path_rejects_unknown(tmp_path):
    from memoryguard.gui import GovernanceApi

    _activate_v2_workspace(tmp_path)
    orphan = tmp_path / "secret.txt"
    orphan.write_text("api_key = sk-test", encoding="utf-8")
    api = GovernanceApi(tmp_path)
    out = api.extract_preview_by_path(str(orphan))
    assert "error" in out
