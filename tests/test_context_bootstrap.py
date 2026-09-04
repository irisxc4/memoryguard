"""V2 context bootstrap, trusted transport, and scoped mutation coverage."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.cutover_v2.facade import V2RuntimeFacade
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.mcp_server import TOOLS, execute_tool
from memoryguard.runtime_v2.context_engine import ContextEngine
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate_v2(root: Path) -> tuple[MemoryAtomStore, EvidenceStore, GovernanceV2, GroupControlService]:
    initialize_all(WorkspaceV2Layout(root))
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="context-bootstrap-core")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="context-source",
        target_digest="context-target",
        manifest_digest="context-manifest",
        digests={"validator_passed": True, "checkpoints": {"core": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE
    return memory, evidence, governance, GroupControlService(root, write=True)


def _request(
    task: str,
    *,
    agent: str = "agent-a",
    group: str = "trusted-group",
    max_items: int | None = None,
    max_chars: int | None = None,
    max_tokens: int | None = None,
) -> dict:
    value = {
        "task": task,
        "trusted_identity": {"agent": agent, "group": group},
    }
    if max_items is not None:
        value["max_items"] = max_items
    if max_chars is not None:
        value["max_chars"] = max_chars
    if max_tokens is not None:
        value["max_tokens"] = max_tokens
    return value


def _candidate(
    item_id: str,
    body: str,
    *,
    layer: str = "relevant",
    kind: str = "fact",
    group: str = "trusted-group",
    score: float = 0.0,
    priority: int = 0,
    status: str = "active",
    **extra,
) -> dict:
    result = {
        "item_id": item_id,
        "body": body,
        "layer": layer,
        "kind": kind,
        "source": "v2-memory",
        "scope": {"share_group_id": group},
        "score": score,
        "priority": priority,
        "status": status,
    }
    result.update(extra)
    return result


def _native_context(root: Path, agent: str, group: str, *, admin: bool = True):
    access = AccessContext(
        trusted_agent_id=agent,
        is_admin=admin,
        strict_binding=True,
        allow_anon=False,
        session_id=f"session-{agent}",
        session_source="transport",
        session_trusted=True,
    )
    return bind_native_transport_context(
        access,
        workspace_id=str(root.resolve()),
        share_group_id=group,
        provider="codex",
        runtime_role="root",
        entrypoint="test",
    )


def _seed_atom(
    root: Path,
    group: str,
    agent: str,
    memory_id: str,
    body: str,
    *,
    policy: str = "relevant",
    priority: int = 0,
) -> tuple[MemoryAtomStore, EvidenceStore, GovernanceV2, MemoryAtom]:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    project_ref = canonical_project_ref(str(root.resolve()))
    context = V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=project_ref,
        actor=agent,
        admin=True,
        authority="manual",
    )
    atom, _decision = governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind="procedure" if policy == "always" else "fact",
            injection_policy=policy,
            priority=priority,
            share_group_id=group,
            agent_instance_id=agent,
            project_ref=project_ref,
            workspace_id=str(root.resolve()),
        ),
        context=context,
        evidence=[{"source_ref": f"seed/{memory_id}"}],
        reason="V2 context fixture",
        idempotency_key=f"seed-{memory_id}",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("ready")
    return memory, evidence, governance, atom


def _mcp_json(result: dict) -> dict:
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _set_mcp_identity(monkeypatch, root: Path, agent: str) -> None:
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(root.resolve()))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", f"mcp-{agent}")
    monkeypatch.setenv("MEMORYGUARD_SESSION_SOURCE", "transport")
    monkeypatch.setenv("MEMORYGUARD_SESSION_TRUSTED", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(root.resolve()))


def test_bootstrap_is_active_only_relevant_sensitive_safe_and_bounded(tmp_path):
    engine = ContextEngine(ready=True, state="V2_ACTIVE")
    candidates = {
        "relevant": [
            _candidate("pref", "用户长期偏好：输出保持简洁", kind="preference", score=0.9),
            _candidate("project", "MemoryGuard 项目使用 SQLite 存储", kind="project", score=0.8),
            _candidate("outside", "旅行预算使用欧元", group="other-group", score=0.7),
            _candidate("low", "低置信度猜测", status="low_confidence"),
            _candidate("conflicted", "冲突结论", status="conflicted"),
            _candidate("quarantined", "已隔离内容", status="quarantined"),
            _candidate("secret", "sk-super-secret-value", sensitive=True),
            _candidate("redacted", "token [REDACTED:credential]", sensitive=True),
            _candidate("episode", "历史对话全文", source="history", raw_history=True),
        ]
    }
    packet = engine.bootstrap(
        _request("修复 MemoryGuard SQLite 检索", max_items=3, max_chars=256),
        candidates,
    ).to_dict()

    assert packet["ready"] is True
    assert packet["state"] == "V2_ACTIVE"
    assert [item["item_id"] for item in packet["relevant"]] == ["pref", "project"]
    assert packet["budget"]["optional"]["items"] <= 3
    assert all(secret not in str(packet) for secret in ("sk-super-secret-value", "token [REDACTED:credential]"))
    reasons = {item["reason"] for item in packet["receipts"] if not item["hit"]}
    assert {"scope_rejected", "lifecycle_rejected", "safety_rejected", "source_rejected"} <= reasons


def test_bootstrap_dedup_and_deterministic_budget(tmp_path):
    engine = ContextEngine(ready=True, state="V2_ACTIVE")
    candidates = {
        "relevant": [
            _candidate("a-locked", "始终先运行定向测试", kind="preference", score=0.2),
            _candidate("z-copy", "始终先运行定向测试", kind="preference", score=0.1),
            _candidate("b-project", "MemoryGuard 定向测试覆盖 bootstrap", kind="project", score=1.0),
        ]
    }
    one = engine.bootstrap(_request("MemoryGuard bootstrap 定向测试", max_items=2, max_chars=256), candidates).to_dict()
    two = engine.bootstrap(_request("MemoryGuard bootstrap 定向测试", max_items=2, max_chars=256), candidates).to_dict()

    assert one == two
    assert one["relevant"][0]["item_id"] == "b-project"
    assert any(item["reason"] == "duplicate_rejected" for item in one["receipts"] if not item["hit"])
    assert one["budget"]["optional"]["items"] == 2


def test_many_preferences_cannot_starve_relevant_governance(tmp_path):
    engine = ContextEngine(ready=True, state="V2_ACTIVE")
    preferences = [
        _candidate(f"pref-{index:02d}", f"长期输出偏好 {index:02d}", kind="preference")
        for index in range(20)
    ]
    candidates = {
        "relevant": preferences + [
            _candidate("correction", "MemoryGuard bootstrap 纠正：只加载 active 记忆", kind="correction", priority=10),
            _candidate("procedure", "MemoryGuard bootstrap 流程：先解析可信 binding", kind="procedure", priority=9),
        ]
    }
    packet = engine.bootstrap(
        _request("修复 MemoryGuard bootstrap active binding 流程", max_items=5),
        candidates,
    ).to_dict()
    ids = {item["item_id"] for item in packet["relevant"]}
    assert {"correction", "procedure"} <= ids
    assert sum(item.startswith("pref-") for item in ids) <= 3
    assert packet["budget"]["optional"]["items"] <= 5


def test_short_weak_query_does_not_recall_unrelated_fact(tmp_path):
    pref = _candidate("preference", "长期偏好：输出简洁", kind="preference")
    weak = _candidate("weak", "旅行安排已经修复完成", kind="fact")

    def retrieve(request):
        return {"relevant": [pref] if request.task == "修复" else [pref, weak]}

    engine = ContextEngine(retriever=retrieve, ready=True, state="V2_ACTIVE")
    packet = engine.bootstrap(_request("修复"), None).to_dict()
    assert [item["item_id"] for item in packet["relevant"]] == ["preference"]


def test_mcp_schema_and_dispatch_use_trusted_binding(tmp_path, monkeypatch):
    memory, evidence, governance, service = _activate_v2(tmp_path)
    service.bind_agent("trusted-agent", "trusted-group", idempotency_key="bind-trusted")
    _seed_atom(tmp_path, "trusted-group", "trusted-agent", "trusted-memory", "用户偏好 MemoryGuard 启动时简洁", policy="relevant")
    _set_mcp_identity(monkeypatch, tmp_path, "trusted-agent")

    tool = next(item for item in TOOLS if item["name"] == "memoryguard_context_bootstrap")
    schema = tool["inputSchema"]
    assert schema["required"] == ["task"]
    assert set(schema["properties"]) == {
        "task", "project_hint", "max_items", "max_chars", "max_tokens", "read_path",
    }
    assert schema["additionalProperties"] is False

    packet = _mcp_json(execute_tool("memoryguard_context_bootstrap", {
        "task": "MemoryGuard 启动",
        "share_group_id": "attacker-group",
    }))
    assert packet["state"] == "V2_ACTIVE"
    assert packet["data"]["effective_agent"] == "trusted-agent"
    assert any(item["body"] == "用户偏好 MemoryGuard 启动时简洁" for item in packet["data"]["relevant"])


def test_mcp_bootstrap_persists_mandatory_receipt_with_trusted_runtime_context(tmp_path, monkeypatch):
    _memory, _evidence, _governance, service = _activate_v2(tmp_path)
    agent_id, group_id = "trusted-agent", "trusted-group"
    service.bind_agent(agent_id, group_id, idempotency_key="bind-trusted")
    _seed_atom(tmp_path, group_id, agent_id, "mandatory", "始终先运行定向测试", policy="always", priority=7)
    _set_mcp_identity(monkeypatch, tmp_path, agent_id)

    packet = _mcp_json(execute_tool("memoryguard_context_bootstrap", {"task": "修复定向测试流程"}))
    assert packet["data"]["mandatory"]
    assert any(
        item["memory_id"] == "mandatory"
        for item in packet["data"]["mandatory"]
    )
    assert any(
        receipt["hit"]
        and receipt["item_id"] == "mandatory"
        and receipt["layer"] == "mandatory"
        for receipt in packet["data"]["receipts"]
    )
    assert packet["data"]["mandatory"][0]["body"] == "始终先运行定向测试"
    assert any(receipt["hit"] and receipt["layer"] == "mandatory" for receipt in packet["data"]["receipts"])

    deleted = execute_tool("memoryguard_memory_delete", {
        "memory_id": "mandatory",
        "idempotency_key": "delete-mandatory",
    })
    assert deleted.get("isError") is not True, deleted
    deleted_result = _mcp_json(deleted)
    assert deleted_result["ok"] is True, deleted_result
    assert deleted_result["data"]["atom"]["status"] == "deleted"

    deleted_packet = _mcp_json(execute_tool(
        "memoryguard_context_bootstrap",
        {"task": "修复定向测试流程"},
    ))
    assert all(
        item.get("memory_id") != "mandatory"
        for item in deleted_packet["data"]["mandatory"]
    )
    assert not any(
        receipt.get("hit") and receipt.get("item_id") == "mandatory"
        for receipt in deleted_packet["data"]["receipts"]
    )


def test_mcp_bootstrap_fails_closed_when_receipt_persistence_fails(tmp_path, monkeypatch):
    memory, evidence, _governance, _service = _activate_v2(tmp_path)
    context = V2MutationContext(
        workspace_id=str(tmp_path.resolve()),
        share_group_id="trusted-group",
        agent_instance_id="trusted-agent",
        actor="trusted-agent",
        authority="manual",
    )
    atom = memory.put_atom(
        MemoryAtom(memory_id="retry", body="body", share_group_id="trusted-group", agent_instance_id="trusted-agent"),
        evidence=[{"source_ref": "trusted-group/retry"}],
        context=context,
    )

    class FlakyEvidence:
        def __init__(self):
            self.calls = 0

        def project_batch(self, events):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("receipt writer unavailable")
            return evidence.project_batch(events)

    assert memory.project_evidence(FlakyEvidence())["failed"] == 1
    assert memory.validate(evidence).ok is False
    with pytest.raises(RuntimeError, match="outstanding|evidence"):
        memory.set_visibility("ready", atom_ids=[atom.atom_id])


def test_update_delete_preflight_and_handlers_use_same_trusted_group(tmp_path: Path, monkeypatch):
    _memory, _evidence, _governance, service = _activate_v2(tmp_path)
    service.bind_agent("trusted-agent", "trusted-group", idempotency_key="bind-trusted")
    _seed_atom(tmp_path, "trusted-group", "trusted-agent", "update-me", "旧正文")
    _seed_atom(tmp_path, "trusted-group", "trusted-agent", "delete-me", "待删除")
    _set_mcp_identity(monkeypatch, tmp_path, "trusted-agent")

    updated = execute_tool("memoryguard_memory_update", {
        "memory_id": "update-me",
        "body": "可信组新正文",
        "share_group_id": "attacker-group",
        "idempotency_key": "update-trusted",
    })
    assert updated.get("isError") is not True, updated
    deleted = execute_tool("memoryguard_memory_delete", {
        "memory_id": "delete-me",
        "share_group_id": "attacker-group",
        "idempotency_key": "delete-trusted",
    })
    assert deleted.get("isError") is not True, deleted

    memory = MemoryAtomStore(tmp_path)
    scope = MemoryReadScope(workspace_id=str(tmp_path.resolve()), share_group_id="trusted-group", admin=True)
    assert memory.get_atom("update-me", scope=scope, include_building=True).body == "可信组新正文"
    assert memory.get_atom("delete-me", scope=scope, include_building=True).status == "deleted"
    decisions = GovernanceV2(tmp_path, memory_store=memory, evidence_store=EvidenceStore(tmp_path)).list_decisions()
    assert any(item.operation == "tombstone" for item in decisions)


def test_memory_mutation_rejects_cross_agent_record_owner(tmp_path: Path, monkeypatch):
    _memory, _evidence, _governance, service = _activate_v2(tmp_path)
    service.bind_agent("trusted-agent", "trusted-group", idempotency_key="bind-trusted")
    _seed_atom(tmp_path, "trusted-group", "agent-a", "owned-by-a", "仅 agent-a 可改")
    _set_mcp_identity(monkeypatch, tmp_path, "trusted-agent")

    memory = MemoryAtomStore(tmp_path)
    scope = MemoryReadScope(
        workspace_id=str(tmp_path.resolve()),
        share_group_id="trusted-group",
        admin=True,
    )
    before_atom = memory.get_atom("owned-by-a", scope=scope, include_building=True)
    assert before_atom is not None
    before_status = memory.status()

    denied = execute_tool("memoryguard_memory_update", {
        "memory_id": "owned-by-a", "body": "越权修改", "idempotency_key": "cross-agent-update",
    })
    assert denied.get("isError") is True
    denied_payload = json.loads(denied["content"][0]["text"])
    assert denied_payload["code"] == "v2_governance_rejected"

    missing = execute_tool("memoryguard_memory_update", {
        "memory_id": "does-not-exist", "body": "越权修改", "idempotency_key": "missing-update",
    })
    assert missing.get("isError") is True
    missing_payload = json.loads(missing["content"][0]["text"])
    assert missing_payload["code"] == denied_payload["code"]

    after_atom = memory.get_atom("owned-by-a", scope=scope, include_building=True)
    assert after_atom is not None
    assert after_atom.to_dict() == before_atom.to_dict()
    assert memory.status() == before_status


def test_active_binding_allows_write_inactive_or_unbound_denies(tmp_path, monkeypatch):
    _memory, _evidence, _governance, service = _activate_v2(tmp_path)
    binding = service.bind_agent("trusted-agent", "trusted-group", idempotency_key="bind-trusted")["binding"]
    _set_mcp_identity(monkeypatch, tmp_path, "trusted-agent")

    allowed = execute_tool("memoryguard_memory_write", {
        "memory_id": "active-write",
        "body": "active binding write",
        "evidence_ids": ["active-write-evidence"],
        "idempotency_key": "active-write",
    })
    assert allowed.get("isError") is not True, allowed

    service.unbind(binding["binding_id"], idempotency_key="unbind-trusted")
    denied = execute_tool("memoryguard_memory_write", {
        "memory_id": "inactive-write",
        "body": "inactive binding write",
        "evidence_ids": ["inactive-write-evidence"],
        "idempotency_key": "inactive-write",
    })
    assert denied.get("isError") is True
    assert json.loads(denied["content"][0]["text"])["code"] == "request_failed"
