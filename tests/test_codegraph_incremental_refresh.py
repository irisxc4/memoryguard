"""Incremental CodeGraph refresh after trusted host file mutations."""
from __future__ import annotations

from pathlib import Path

import pytest

from memoryguard.codegraph_v2 import CodeGraphScope, CodeGraphStore
from memoryguard.codegraph_v2.graphify_adapter import GraphifyExportAdapter
from memoryguard.codegraph_v2.refresh import (
    apply_incremental_refresh,
    queue_host_file_refresh,
)
from memoryguard.context_bootstrap import consume_codegraph_affected_receipt
from memoryguard.graphify_core import export_repository
from memoryguard.host_hooks import run_hook
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore
from memoryguard.content import ContentStore
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.rule_scope import canonical_project_ref


def _activate(workspace: Path) -> None:
    manager = ManifestManager(workspace)
    if manager.current().state is ManifestState.V2_ACTIVE:
        return
    initialize_all(WorkspaceV2Layout(workspace))
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    RuleV2Store(workspace)
    ProjectionStore(workspace)
    ContentStore(workspace)
    RuntimeStore(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="codegraph-refresh")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="refresh-source",
        target_digest="refresh-target",
        manifest_digest="refresh-manifest",
        digests={"validator_passed": True, "checkpoints": {"refresh": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _scope(root: Path, group: str = "group-a") -> CodeGraphScope:
    return CodeGraphScope.from_value({
        "workspace_id": str(root.resolve()),
        "share_group_id": group,
        "agent_instance_id": "",
        "project_ref": canonical_project_ref(str(root.resolve())),
        "provider": "graphify",
        "runtime_role": "",
        "trusted_context": True,
    })


def _write_py(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _build_graph(root: Path, files: list[Path]) -> tuple[CodeGraphStore, CodeGraphScope]:
    store = CodeGraphStore(root)
    scope = _scope(root)
    export = export_repository(root, paths=files, complete=True, parallel=False)
    GraphifyExportAdapter(store).project(export, scope=scope, full_snapshot=True)
    return store, scope


def _bind_refresh_agent(root: Path) -> None:
    _activate(root)
    GroupControlService(root, write=True).bind_agent(
        "codex-agent", "group-a", idempotency_key="refresh-bind",
    )


def test_successful_file_write_queues_refresh_and_same_hash_is_noop(tmp_path: Path) -> None:
    src = tmp_path / "src" / "mod.py"
    _write_py(src, "def alpha():\n    return 1\n")
    store, scope = _build_graph(tmp_path, [src])
    revision = store.list_source_files(scope=scope)[0].revision_id
    counts = store.counts(scope=scope)
    unbound = queue_host_file_refresh(
        tmp_path,
        payload={
            "cwd": str(tmp_path), "share_group_id": "group-a",
            "agent_instance_id": "codex-agent",
        },
        tool_name="Write",
        tool_input={"file_path": str(src)},
        tool_result={"ok": True},
        host_event="post_tool",
        trusted_host=True,
    )
    assert unbound == {"status": "ignored", "reason": "binding_unavailable"}
    _bind_refresh_agent(tmp_path)

    queued = queue_host_file_refresh(
        tmp_path,
        payload={
            "cwd": str(tmp_path), "share_group_id": "group-a",
            "agent_instance_id": "codex-agent",
        },
        tool_name="Write",
        tool_input={"file_path": str(src)},
        tool_result={"ok": True},
        host_event="post_tool",
        trusted_host=True,
    )
    assert queued["status"] in {"noop", "updated"}
    files = store.list_source_files(scope=scope)
    assert files[0].revision_id == revision
    assert queued.get("revision_advanced") in {False, None}
    after_counts = store.counts(scope=scope)
    assert after_counts["revisions"] == counts["revisions"]
    assert after_counts["outbox"] == counts["outbox"]


def test_failed_non_file_and_out_of_scope_do_not_refresh(tmp_path: Path) -> None:
    src = tmp_path / "src" / "mod.py"
    _write_py(src, "def alpha():\n    return 1\n")
    store, scope = _build_graph(tmp_path, [src])
    _bind_refresh_agent(tmp_path)
    outside = tmp_path.parent / "escape.py"
    _write_py(outside, "def leaked():\n    return 0\n")

    failed = queue_host_file_refresh(
        tmp_path,
        payload={"cwd": str(tmp_path), "share_group_id": "group-a", "agent_instance_id": "codex-agent"},
        tool_name="Write",
        tool_input={"file_path": str(src)},
        tool_result={"ok": False, "error": "disk full"},
        host_event="post_tool",
        trusted_host=True,
    )
    assert failed["status"] == "ignored"
    non_file = queue_host_file_refresh(
        tmp_path,
        payload={"cwd": str(tmp_path), "share_group_id": "group-a", "agent_instance_id": "codex-agent"},
        tool_name="Bash",
        tool_input={"command": "echo hi"},
        tool_result={"ok": True},
        host_event="post_tool",
        trusted_host=True,
    )
    assert non_file["status"] == "ignored"
    escaped = queue_host_file_refresh(
        tmp_path,
        payload={"cwd": str(tmp_path), "share_group_id": "group-a", "agent_instance_id": "codex-agent"},
        tool_name="Write",
        tool_input={"file_path": str(outside)},
        tool_result={"ok": True},
        host_event="post_tool",
        trusted_host=True,
    )
    assert escaped["status"] == "ignored"
    unknown = queue_host_file_refresh(
        tmp_path,
        payload={"cwd": str(tmp_path), "share_group_id": "group-a", "agent_instance_id": "codex-agent"},
        tool_name="Write",
        tool_input={"file_path": str(src)},
        tool_result=None,
        host_event="post_tool",
        trusted_host=True,
    )
    assert unknown == {"status": "ignored", "reason": "tool_not_confirmed"}
    untrusted = queue_host_file_refresh(
        tmp_path,
        payload={"cwd": str(tmp_path), "share_group_id": "group-a", "agent_instance_id": "codex-agent"},
        tool_name="Write",
        tool_input={"file_path": str(src)},
        tool_result={"ok": True},
    )
    assert untrusted == {"status": "ignored", "reason": "untrusted_host_event"}


def test_changed_hash_replaces_graph_and_receipt_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "src" / "mod.py"
    _write_py(src, "def alpha():\n    return 1\n")
    store, scope = _build_graph(tmp_path, [src])
    original = store.list_source_files(scope=scope)[0]
    original_hash = original.content_hash
    original_revision = original.revision_id
    symbols_before = {item.name for item in store.get_symbols(original.file_id, scope=scope)}
    assert "alpha" in symbols_before

    _write_py(src, "def beta():\n    return 2\n")
    updated = apply_incremental_refresh(
        store, scope=scope, source_root=tmp_path, relative_paths=["src/mod.py"],
    )
    assert updated["status"] == "updated"
    assert updated["revision_advanced"] is True
    after = store.list_source_files(scope=scope)[0]
    assert after.content_hash != original_hash
    assert after.revision_id != original_revision
    names = {item.name for item in store.get_symbols(after.file_id, scope=scope)}
    assert "beta" in names
    assert "alpha" not in names
    receipt = consume_codegraph_affected_receipt(tmp_path, scope=scope)
    assert receipt is not None
    assert receipt["consumed"] is True
    assert len(receipt["result_ids"]) <= 32
    assert consume_codegraph_affected_receipt(tmp_path, scope=scope) is None

    fingerprint = store.get_fingerprint("src/mod.py", scope=scope)
    assert fingerprint is not None
    outbox_count = store.counts(scope=scope)["outbox"]
    _write_py(src, "def gamma():\n    return 3\n")

    def reject_receipt(*args, **kwargs):
        raise RuntimeError("injected receipt failure")

    monkeypatch.setattr(store, "_put_affected_receipt_conn", reject_receipt)
    with pytest.raises(RuntimeError, match="injected receipt failure"):
        apply_incremental_refresh(
            store,
            scope=scope,
            source_root=tmp_path,
            relative_paths=["src/mod.py"],
        )
    rolled = store.list_source_files(scope=scope)[0]
    assert rolled.revision_id == after.revision_id
    assert store.get_fingerprint("src/mod.py", scope=scope)["content_hash"] == fingerprint["content_hash"]
    assert store.consume_affected_receipt(scope=scope) is None
    assert store.counts(scope=scope)["outbox"] == outbox_count


def test_deleted_file_is_tombstoned_and_receipt_is_once_only(tmp_path: Path) -> None:
    src = tmp_path / "src" / "removed.py"
    _write_py(src, "def removed():\n    return 1\n")
    store, scope = _build_graph(tmp_path, [src])
    source = store.list_source_files(scope=scope)[0]
    src.unlink()

    result = apply_incremental_refresh(
        store, scope=scope, source_root=tmp_path, relative_paths=["src/removed.py"],
    )

    assert result["status"] == "updated"
    assert result["deleted"] == 1
    inactive = store.list_source_files(scope=scope, active_only=False)[0]
    assert inactive.file_id == source.file_id
    assert inactive.active is False
    assert store.get_symbols(source.file_id, scope=scope) == ()
    assert store.get_fingerprint("src/removed.py", scope=scope) is None
    receipt = consume_codegraph_affected_receipt(tmp_path, scope=scope)
    assert receipt is not None
    assert receipt["depth"] == 2
    assert receipt["limit"] == 32
    assert receipt["provenance"] == "production"
    assert len(receipt["start_ids"]) <= 8
    assert len(receipt["result_ids"]) <= 32
    assert consume_codegraph_affected_receipt(tmp_path, scope=scope) is None


def test_host_post_tool_write_triggers_refresh(tmp_path: Path) -> None:
    _activate(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent(
        "codex-agent", "group-a", idempotency_key="refresh-bind",
    )
    src = tmp_path / "src" / "mod.py"
    _write_py(src, "def alpha():\n    return 1\n")
    store, scope = _build_graph(tmp_path, [src])
    _write_py(src, "def delta():\n    return 4\n")
    result = run_hook(
        provider="codex",
        event="post_tool",
        workspace=tmp_path,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-refresh",
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"file_path": str(src)},
            "tool_result": {"ok": True},
        },
    )
    assert result == {} or isinstance(result, dict)
    after = store.list_source_files(scope=scope)[0]
    names = {item.name for item in store.get_symbols(after.file_id, scope=scope)}
    assert "delta" in names
