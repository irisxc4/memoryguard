from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.content.store import ContentStore
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory.store import MemoryAtomStore
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _prepare(tmp_path: Path) -> None:
    content = ContentStore(tmp_path)
    MemoryAtomStore(tmp_path)
    EvidenceStore(tmp_path)
    GovernanceV2(tmp_path)
    content.upsert_source_connector(
        source_id="project-root",
        provider="memoryguard-test",
        source_type="selected_directory",
        external_root_key=str(tmp_path.resolve()),
        workspace_id=str(tmp_path.resolve()),
        enabled=True,
    )
    control = GroupControlService(tmp_path, write=True)
    control.record_selection("agent-a", ["project-root"], "selection-project-root")


def _context(tmp_path: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="session-a",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id="group-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    )


def test_native_extraction_accept_and_enrichment_are_v2_only(tmp_path: Path):
    _prepare(tmp_path)
    document = tmp_path / "memory.md"
    document.write_text(
        "# Preference\n\nI prefer dark mode for development tools.\n\n"
        "# Procedure\n\nAlways run the focused tests before the full suite.\n",
        encoding="utf-8",
    )
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)

    preview = port.dispatch_mcp(
        "memoryguard_extract_memories",
        {"source_path": str(document)},
        context=context,
        generation=7,
        state="V2_ACTIVE",
        mutation=True,
    )
    assert preview["ok"] is True, preview
    data = preview["data"]
    assert data["staging"] == "v2_content_plane"
    assert data["candidates"]
    extract_id = data["extract_id"]
    candidate_ids = [item["candidate_id"] for item in data["candidates"]]
    assert not (tmp_path / ".memoryguard" / "staging").exists()

    accepted = port.dispatch_mcp(
        "memoryguard_accept_candidates",
        {"extract_id": extract_id, "candidate_ids": candidate_ids},
        context=context,
        generation=7,
        state="V2_ACTIVE",
        mutation=True,
    )
    assert accepted["ok"] is True, accepted
    assert accepted["data"]["storage"] == "v2_memory"
    assert accepted["data"]["total"] == len(candidate_ids)
    assert not (tmp_path / ".memoryguard" / "shared-memory").exists()

    with sqlite3.connect(tmp_path / ".memoryguard" / "content" / "content.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM knowledge_records WHERE source_table='native_extraction_candidates' AND status='accepted'"
        ).fetchone()[0] == len(candidate_ids)

    build = port.dispatch_mcp(
        "memoryguard_build_and_enrich",
        {},
        context=context,
        generation=7,
        state="V2_ACTIVE",
        mutation=True,
    )
    assert build["ok"] is True, build
    assert build["data"]["projection_mode"] == "v2_native_memory"
    assert build["data"]["host_action_required"] is True
    assert build["data"]["pending_tasks"]

    pending = port.dispatch_mcp(
        "memoryguard_list_pending_enrichments",
        {"limit": 50},
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )
    assert pending["ok"] is True, pending
    assert pending["data"]["storage"] == "v2_content_plane"
    task = pending["data"]["tasks"][0]

    applied = port.dispatch_mcp(
        "memoryguard_apply_enrichments",
        {"results": [{
            "task_id": task["task_id"],
            "kind": "preference",
            "title": "开发工具显示偏好",
            "body": "开发工具优先使用深色模式。",
            "confidence": 0.95,
            "rationale": "explicit preference",
        }]},
        context=context,
        generation=7,
        state="V2_ACTIVE",
        mutation=True,
    )
    assert applied["ok"] is True, applied
    assert applied["data"]["applied"] == 1
    assert applied["data"]["storage"] == "v2_memory"
    assert not (tmp_path / ".memoryguard" / "enrichments" / "pending.jsonl").exists()

    status = port.dispatch_mcp(
        "memoryguard_enrichment_status",
        {},
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )
    assert status["ok"] is True, status
    assert status["data"]["applied"] >= 1


def test_native_extraction_is_scope_bound_and_redacts_secrets(tmp_path: Path):
    _prepare(tmp_path)
    document = tmp_path / "secret.md"
    document.write_text(
        "# Preference\n\nI prefer this token: sk-123456789012345678901234567890123456\n",
        encoding="utf-8",
    )
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    preview = port.dispatch_mcp(
        "memoryguard_extract_memories",
        {"source_path": str(document)},
        context=context,
        generation=7,
        state="V2_ACTIVE",
        mutation=True,
    )
    assert preview["ok"] is True, preview
    candidate = preview["data"]["candidates"][0]
    assert candidate["risk_level"] == "high"
    assert candidate["secret_redacted"] is True
    assert "sk-123" not in candidate["preview"]

    foreign = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-b",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="session-b",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id="group-b",
        project_ref="project-b",
        provider="codex",
        runtime_role="root",
    )
    rejected = port.dispatch_mcp(
        "memoryguard_accept_candidates",
        {
            "extract_id": preview["data"]["extract_id"],
            "candidate_ids": [candidate["candidate_id"]],
        },
        context=foreign,
        generation=7,
        state="V2_ACTIVE",
        mutation=True,
    )
    assert rejected["ok"] is False
    assert rejected["code"] == "candidate_not_found"
