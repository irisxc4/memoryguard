"""V2 bootstrap and canonical-source gates.

The bootstrap contract is now a native V2 contract: a caller-selected read
path is advisory, while the trusted manifest and exact memory scope decide
whether a packet is active.  Canonical rule readiness is observed through the
native diagnostic port and never by constructing a retired storage object.
"""
from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.context_engine import ContextEngine
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7) -> None:
        self.state = state
        self.generation = generation

    def current(self) -> dict[str, object]:
        return {"state": self.state, "generation": self.generation}


def _context(root: Path, *, agent: str = "agent-1", group: str = "g1"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"bootstrap-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root.resolve()),
        share_group_id=group,
        project_ref=str(root.resolve()),
        provider="codex",
        runtime_role="root",
        entrypoint="test",
    )


def _port(root: Path, *, manifest: _Manifest | None = None) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(root, state_provider=manifest or _Manifest())


def _seed_memory(root: Path, group: str = "g1") -> MemoryAtomStore:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    context = V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id=group,
        agent_instance_id="agent-1",
        project_ref=str(root.resolve()),
        provider="codex",
        runtime_role="root",
        actor="bootstrap-fixture",
        authority="manual",
        admin=True,
    )
    for memory_id, body, policy, priority in (
        ("mandatory-a", "始终先运行定向测试", "always", 20),
        ("relevant-a", "项目使用 V2 原生记忆治理", "relevant", 0),
    ):
        governance.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=body,
                kind="procedure" if policy == "always" else "fact",
                injection_policy=policy,
                priority=priority,
                workspace_id=str(root.resolve()),
                share_group_id=group,
                agent_instance_id="agent-1",
                project_ref=str(root.resolve()),
                provider="codex",
                runtime_role="root",
            ),
            context=context,
            evidence=[{"source_ref": f"bootstrap:{memory_id}"}],
            reason="V2 bootstrap fixture",
            idempotency_key=f"bootstrap-seed:{memory_id}",
        )
    memory.project_evidence(evidence)
    memory.set_visibility("active")
    return memory


def _canonical_state(root: Path, group: str = "g1", *, active: bool = True) -> None:
    rules = RuleV2Store(root)
    definition = rules.upsert_definition(
        build_definition("始终先运行定向测试", kind="procedure")
    )
    rules.record_canonical_state({
        "scope_id": f"bootstrap-scope:{group}",
        "share_group_id": group,
        "activation_status": "active" if active else "shadow",
        "canonical_digest": definition.canonical_text,
        "read_path": "native",
        "source_digest": "source-bootstrap",
        "effective_digest": "effective-bootstrap",
        "runtime_digest": "runtime-bootstrap",
        "assessment_digest": "assessment-bootstrap",
        "policy_version": "v2",
        "updated_at": "2026-08-12T00:00:00+00:00",
    })


def _bootstrap(root: Path, read_path: str) -> dict:
    result = _port(root).dispatch_mcp(
        "memoryguard_context_bootstrap",
        {"task": "修复 bootstrap", "read_path": read_path},
        context=_context(root),
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    return result["data"]


def _diagnostic(root: Path, name: str) -> dict:
    result = _port(root).dispatch_mcp(
        name,
        {},
        context=_context(root),
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    return result["data"]


def test_legacy_request_never_switches_and_fields_exist(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    packets = [_bootstrap(tmp_path, path) for path in ("legacy", "auto", "native")]
    assert all(packet["state"] == "V2_ACTIVE" for packet in packets)
    assert all(packet["ready"] is True for packet in packets)
    assert all(packet["status"] == "ok" for packet in packets)
    assert packets[0]["mandatory"] == packets[1]["mandatory"] == packets[2]["mandatory"]
    assert packets[0]["relevant"] == packets[1]["relevant"] == packets[2]["relevant"]
    assert all("fallback_reason" not in packet for packet in packets)


def test_rule_intelligence_request_without_activation_falls_back(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    status = _diagnostic(tmp_path, "memoryguard_canonical_status")
    assert status["status"] == "NO_SOURCE"
    assert status["canonical_state"] == "absent"
    packet = _bootstrap(tmp_path, "rule-intelligence")
    assert packet["ready"] is True
    assert packet["state"] == "V2_ACTIVE"
    assert packet.get("mandatory_overflow") is not True
    assert packet.get("error") in {"", None}


def test_auto_request_without_activation_falls_back(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    status = _diagnostic(tmp_path, "memoryguard_projection_status")
    assert status["status"] == "NO_SOURCE"
    packet = _bootstrap(tmp_path, "auto")
    assert [item["body"] for item in packet["mandatory"]] == ["始终先运行定向测试"]
    assert packet.get("mandatory_overflow") is not True


def test_inactive_activation_is_not_a_switch(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    _canonical_state(tmp_path, active=False)
    status = _diagnostic(tmp_path, "memoryguard_canonical_status")
    assert status["status"] == "READY"
    assert status["canonical_state"] == "shadow"
    assert status["read_path"] == "native"


def test_activated_but_legacy_request_is_no_fallback(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    _canonical_state(tmp_path)
    status = _diagnostic(tmp_path, "memoryguard_canonical_status")
    assert status["canonical_state"] == "active"
    packet = _bootstrap(tmp_path, "legacy")
    assert packet["ready"] is True
    assert packet["mandatory"]


def test_activated_but_readiness_incomplete_reports_readiness_failed(tmp_path: Path) -> None:
    _canonical_state(tmp_path, active=True)
    projection = _diagnostic(tmp_path, "memoryguard_projection_status")
    canonical = _diagnostic(tmp_path, "memoryguard_canonical_status")
    assert canonical["canonical_state"] == "active"
    assert projection["status"] == "NO_SOURCE"
    assert projection["total_heads"] == 0


def test_activated_and_ready_engages_canonical(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    _canonical_state(tmp_path)
    canonical = _diagnostic(tmp_path, "memoryguard_canonical_status")
    assert canonical["status"] == "READY"
    assert canonical["canonical_state"] == "active"
    assert canonical["read_path"] == "native"
    packet = _bootstrap(tmp_path, "native")
    assert packet["ready"] is True
    assert packet["mandatory"]


def test_default_read_path_uses_canonical_when_gate_passes(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    _canonical_state(tmp_path)
    packet = _bootstrap(tmp_path, "auto")
    assert packet["state"] == "V2_ACTIVE"
    assert packet["mandatory"][0]["body"] == "始终先运行定向测试"
    assert packet["receipts"]


def test_auto_uses_canonical_when_gate_passes(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    _canonical_state(tmp_path)
    first = _diagnostic(tmp_path, "memoryguard_canonical_status")
    second = _diagnostic(tmp_path, "memoryguard_canonical_status")
    assert first == second
    assert _bootstrap(tmp_path, "auto")["ready"] is True


def test_bootstrap_shadow_state_is_not_reported_as_active(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    result = _port(tmp_path, manifest=_Manifest("V2_READY", 8)).dispatch_mcp(
        "memoryguard_context_bootstrap",
        {"task": "shadow bootstrap", "read_path": "auto"},
        context=_context(tmp_path),
        generation=8,
        state="V2_READY",
    )
    assert result["ok"] is True, result
    assert result["data"]["state"] == "V2_READY"
    assert result["data"]["ready"] is False
    assert result["data"]["status"] == "shadow"


def test_bootstrap_scope_cannot_cross_group(tmp_path: Path) -> None:
    _seed_memory(tmp_path, "g1")
    result = _port(tmp_path).dispatch_mcp(
        "memoryguard_context_bootstrap",
        {"task": "cross group", "read_path": "auto"},
        context=_context(tmp_path, agent="agent-2", group="g2"),
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    assert result["data"]["mandatory"] == []
    assert result["data"]["relevant"] == []
