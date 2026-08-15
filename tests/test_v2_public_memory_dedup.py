from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.mcp_server import execute_tool
from memoryguard.migration.upgrade import run_upgrade
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.system.manifest import ManifestManager, ManifestState


def _public_result_failure(result: dict) -> str:
    return json.dumps(
        {
            "isError": bool(result.get("isError")),
            "content_count": len(result.get("content") or []),
            "content_types": [
                str(item.get("type") or "")
                for item in (result.get("content") or [])
                if isinstance(item, dict)
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _dedup_failure_diagnostics(
    workspace: Path,
    first: dict,
    second: dict,
) -> str:
    store = MemoryAtomStore(workspace, readonly=True)
    atoms = store.list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(workspace.resolve()),
            share_group_id="shared-team",
            admin=True,
        ),
        include_building=True,
    )
    bindings = GroupControlService(workspace).list_bindings(include_inactive=False)
    aggregate = GroupControlService(workspace).aggregate_groups()

    atom_rows = []
    for atom in atoms:
        atom_rows.append(
            {
                "atom_id": atom.atom_id,
                "memory_id": atom.memory_id,
                "share_group_id": atom.share_group_id,
                "status": atom.status,
                "visibility": atom.visibility,
                "revision": atom.revision,
                "provenance": [
                    {
                        "agent_instance_id": str(item.get("agent_instance_id") or ""),
                        "source_event_id": str(item.get("source_event_id") or ""),
                        "share_group_id": str(item.get("share_group_id") or ""),
                        "source_ref_present": bool(item.get("source_ref")),
                        "digest_present": bool(item.get("digest")),
                    }
                    for item in atom.provenance
                ],
            }
        )

    group_rows = [
        {
            "share_group_id": str(group.get("share_group_id") or ""),
            "members": [str(member) for member in (group.get("members") or [])],
            "member_count": int(group.get("member_count") or 0),
            "record_count": int(group.get("record_count") or 0),
            "active_count": int(group.get("active_count") or 0),
            "status_counts": dict(group.get("status_counts") or {}),
            "visibility_counts": dict(group.get("visibility_counts") or {}),
        }
        for group in (aggregate.get("groups") or [])
        if isinstance(group, dict)
    ]
    binding_rows = [
        {
            "agent_instance_id": str(item.get("agent_instance_id") or ""),
            "share_group_id": str(item.get("share_group_id") or ""),
            "status": str(item.get("status") or ""),
        }
        for item in (bindings.get("bindings") or [])
        if isinstance(item, dict)
    ]
    return json.dumps(
        {
            "observed": {
                "first_mutation_kind": str(first.get("mutation_kind") or ""),
                "first_memory_id": str(first.get("memory_id") or ""),
                "second_mutation_kind": str(second.get("mutation_kind") or ""),
                "second_memory_id": str(second.get("memory_id") or ""),
            },
            "atoms": atom_rows,
            "bindings": binding_rows,
            "groups": group_rows,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _write_public(
    workspace: Path,
    agent: str,
    memory_id: str,
    event_id: str,
    idempotency_key: str,
    body: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    monkeypatch.setenv("MEMORYGUARD_HOME", str(workspace))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "0")
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "codex")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(workspace))

    result = execute_tool(
        "memoryguard_memory_write",
        {
            "workspace": str(workspace),
            "agent_instance_id": agent,
            "memory_id": memory_id,
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "body": body,
        },
    )
    assert result.get("isError") is not True, _public_result_failure(result)
    payload = json.loads(result["content"][0]["text"])
    assert payload["state"] == ManifestState.V2_ACTIVE.value
    assert payload["data"]["ok"] is True
    return payload["data"]


@pytest.fixture
def active_v2_workspace(tmp_path: Path) -> Path:
    ready = run_upgrade(tmp_path, data_home=tmp_path, apply=True)
    assert ready["ok"] is True
    assert ready["status"] == ManifestState.V2_READY.value

    active = run_upgrade(
        tmp_path,
        data_home=tmp_path,
        apply=True,
        confirm=ManifestState.V2_ACTIVE.value,
    )
    assert active["ok"] is True
    assert ManifestManager(tmp_path).current().state is ManifestState.V2_ACTIVE

    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    GovernanceV2(tmp_path, memory_store=memory, evidence_store=evidence)

    bindings = GroupControlService(tmp_path, write=True)
    bound = bindings.bind_agents(["agent-a", "agent-b"], share_group_id="shared-team")
    assert bound["ok"] is True
    return tmp_path


def test_public_v2_same_group_different_agent_audiences_stay_distinct(
    active_v2_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "The team uses Python for backend tests."

    first = _write_public(
        active_v2_workspace,
        "agent-a",
        "memory-a",
        "event-a",
        "write-a",
        body,
        monkeypatch,
    )
    second = _write_public(
        active_v2_workspace,
        "agent-b",
        "memory-b",
        "event-b",
        "write-b",
        body,
        monkeypatch,
    )

    assert first["mutation_kind"] == "created"
    assert second["mutation_kind"] == "created", _dedup_failure_diagnostics(
        active_v2_workspace,
        first,
        second,
    )
    assert second["memory_id"] != first["memory_id"]

    store = MemoryAtomStore(active_v2_workspace, readonly=True)
    atoms = store.list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(active_v2_workspace.resolve()),
            share_group_id="shared-team",
            admin=True,
        ),
        status="active",
        include_building=True,
    )
    assert len(atoms) == 2
    assert {atom.agent_instance_id for atom in atoms} == {"agent-a", "agent-b"}


def test_public_v2_memory_write_accepts_descriptor_body_only_contract(
    active_v2_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public MCP descriptor requires only ``body`` for a new write."""
    monkeypatch.setenv("MEMORYGUARD_HOME", str(active_v2_workspace))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(active_v2_workspace))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "0")
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "codex")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(active_v2_workspace))

    result = execute_tool(
        "memoryguard_memory_write",
        {
            "workspace": str(active_v2_workspace),
            "agent_instance_id": "agent-a",
            "body": "Use the shared test fixture for body-only MCP writes.",
            "kind": "procedure",
            "injection_policy": "relevant",
        },
    )

    assert result.get("isError") is not True, _public_result_failure(result)
    payload = json.loads(result["content"][0]["text"])
    data = payload["data"]
    assert data["ok"] is True
    assert data["atom"]["memory_id"]
    assert data["atom"]["kind"] == "procedure"
    assert data["atom"]["injection_policy"] == "relevant"


def test_public_v2_memory_write_can_be_read_immediately(
    active_v2_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful native write must be readable before async promotion."""
    monkeypatch.setenv("MEMORYGUARD_HOME", str(active_v2_workspace))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(active_v2_workspace))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "0")
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "codex")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(active_v2_workspace))

    written = execute_tool(
        "memoryguard_memory_write",
        {
            "workspace": str(active_v2_workspace),
            "agent_instance_id": "agent-a",
            "body": "Read this immediately after the native write.",
            "kind": "procedure",
            "injection_policy": "relevant",
        },
    )
    assert written.get("isError") is not True, _public_result_failure(written)
    write_data = json.loads(written["content"][0]["text"])["data"]
    memory_id = write_data["atom"]["memory_id"]
    assert write_data["atom"]["visibility"] == "building"
    audience = write_data["atom"]["metadata"]["audience"]
    assert audience["target_type"] == "agent"
    assert audience["target_id"] == "agent-a"
    assert audience["project_ref"] == ""
    assert audience["provider"] == ""
    assert audience["runtime_role"] == ""

    # The same trusted Agent must read its ordinary relevant memory from a
    # different project cwd. Project narrowing requires agent_project.
    other_project = active_v2_workspace / "another-project"
    other_project.mkdir()
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(other_project))

    read = execute_tool(
        "memoryguard_memory_read",
        {
            "workspace": str(active_v2_workspace),
            "agent_instance_id": "agent-a",
            "memory_id": memory_id,
        },
    )
    assert read.get("isError") is not True, _public_result_failure(read)
    read_data = json.loads(read["content"][0]["text"])["data"]
    assert read_data["memory_id"] == memory_id
    assert read_data["body"] == "Read this immediately after the native write."
    assert read_data["kind"] == "procedure"
    assert read_data["injection_policy"] == "relevant"

    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-b")
    foreign_read = execute_tool(
        "memoryguard_memory_read",
        {
            "workspace": str(active_v2_workspace),
            "agent_instance_id": "agent-b",
            "memory_id": memory_id,
        },
    )
    assert foreign_read.get("isError") is not True, _public_result_failure(foreign_read)
    foreign_data = json.loads(foreign_read["content"][0]["text"])["data"]
    assert foreign_data is None


def test_public_v2_body_only_write_preserves_explicit_retry_key(
    active_v2_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORYGUARD_HOME", str(active_v2_workspace))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(active_v2_workspace))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "0")
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "codex")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(active_v2_workspace))
    args = {
        "workspace": str(active_v2_workspace),
        "agent_instance_id": "agent-a",
        "body": "Retry this body-only MCP write with the same key.",
        "kind": "procedure",
        "injection_policy": "relevant",
        "idempotency_key": "body-only-retry",
    }

    first_result = execute_tool("memoryguard_memory_write", args)
    second_result = execute_tool("memoryguard_memory_write", args)
    assert first_result.get("isError") is not True, _public_result_failure(first_result)
    assert second_result.get("isError") is not True, _public_result_failure(second_result)
    first = json.loads(first_result["content"][0]["text"])["data"]
    second = json.loads(second_result["content"][0]["text"])["data"]
    assert first["atom"]["memory_id"] == second["atom"]["memory_id"]
    assert second["mutation_kind"] == "deduplicated"
    assert second["idempotent_replay"] is True


def test_public_v2_same_agent_exact_body_reuses_canonical_record(
    active_v2_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "The team uses Python for backend tests."
    first = _write_public(
        active_v2_workspace, "agent-a", "memory-a", "event-a", "write-a", body, monkeypatch,
    )
    second = _write_public(
        active_v2_workspace, "agent-a", "memory-a-copy", "event-a-copy", "write-a-copy", body, monkeypatch,
    )

    assert first["mutation_kind"] == "created"
    assert second["mutation_kind"] == "deduplicated"
    assert second["memory_id"] == first["memory_id"]
    assert second["governance_receipt"]["action"] == "merged"

    store = MemoryAtomStore(active_v2_workspace, readonly=True)
    atoms = store.list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(active_v2_workspace.resolve()),
            share_group_id="shared-team",
            admin=True,
        ),
        status="active",
        include_building=True,
    )
    same_agent = [atom for atom in atoms if atom.agent_instance_id == "agent-a"]
    assert len(same_agent) == 1
    assert len(same_agent[0].provenance) == 2
