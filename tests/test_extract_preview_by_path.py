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


def test_extract_preview_by_path_allows_discovered_session(tmp_path, monkeypatch):
    from memoryguard.gui import GovernanceApi
    from memoryguard.agent_locator import AgentLocator
    from memoryguard.schema_v3 import AgentInstance, DiscoveryLedger, TargetCapability

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

    api = GovernanceApi(tmp_path)
    out = api.extract_preview_by_path(str(target), agent_instance_id="codex-x")
    assert out.get("ok") is True, out
    assert out.get("extract_id")
    assert isinstance(out.get("candidates"), list)


def test_extract_preview_by_path_rejects_unknown(tmp_path):
    from memoryguard.gui import GovernanceApi

    orphan = tmp_path / "secret.txt"
    orphan.write_text("api_key = sk-test", encoding="utf-8")
    api = GovernanceApi(tmp_path)
    out = api.extract_preview_by_path(str(orphan))
    assert "error" in out
