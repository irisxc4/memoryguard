from __future__ import annotations

import time
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory.store import MemoryAtom, MemoryAtomStore
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


def _context(workspace: Path):
    access = AccessContext(
        trusted_agent_id="agent-a",
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
        session_id="gui-session",
        session_source="transport",
        session_trusted=True,
    )
    return bind_native_transport_context(
        access,
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
        namespace_id="knowledge-native-namespace",
        sensitivity="normal",
        policy_class="private",
    )


def _seed(workspace: Path) -> None:
    memory = MemoryAtomStore(workspace, readonly=False)
    governance = GovernanceV2(workspace, memory_store=memory)
    ctx = V2MutationContext(
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        actor="test",
    )
    evidence, _ = governance.put_evidence(
        context=ctx,
        reason="projection native fixture",
        source_ref="fixture:projection",
        digest="a" * 64,
        authority="governance",
    )
    atom, _ = governance.put_atom(
        MemoryAtom(
            memory_id="native-m1",
            body="native private body",
            workspace_id=str(workspace.resolve()),
            share_group_id="group-a",
            agent_instance_id="agent-a",
            project_ref=str(workspace.resolve()),
            provider="gui",
            runtime_role="gui",
        ),
        context=ctx,
        evidence=[evidence.to_dict()],
        reason="projection native fixture atom",
        idempotency_key="projection-native-fixture",
    )
    for _ in range(4):
        state = memory.project_evidence(governance.evidence)
        if int(state.get("pending", 0)) == 0:
            break
    assert not memory.pending_outbox(include_failed=True)
    memory.set_visibility("active", atom_ids=[atom.atom_id])


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def _wait(port: NativeV2RuntimePort, context, run_id: str) -> dict:
    latest: dict = {}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        latest = port.dispatch_gui(
            "get_build_progress", [run_id], context=context, generation=11, state="V2_ACTIVE"
        )
        if latest.get("status") in {"succeeded", "failed", "cancelled"}:
            return latest
        time.sleep(0.02)
    return latest


def test_native_projection_build_and_release_transport(tmp_path: Path) -> None:
    _seed(tmp_path)
    port = _port(tmp_path)
    context = _context(tmp_path)

    accepted = port.dispatch_gui(
        "start_build_projection",
        # Browser selectors are business compatibility fields, not trusted
        # identity.  The build must still use the bound agent/group.
        [True, "reconstructed", {}, "attacker-agent", "attacker-group", "", "", "auto"],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert accepted["ok"] is True, accepted
    assert accepted["operation"] == "projection_build"
    run_id = str(accepted["task"]["run_id"])
    final = _wait(port, context, run_id)
    assert final["status"] == "succeeded", final

    graph = port.dispatch_gui(
        "get_neuron_graph",
        {"agent_instance_id": "attacker-agent", "share_group_id": "attacker-group"},
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert graph["ok"] is True, graph
    assert graph["data"]["status"] == "READY"
    assert any(node.get("memory_id") == "native-m1" for node in graph["data"]["nodes"])
    assert "native private body" not in str(graph)

    plan = port.dispatch_gui(
        "create_build_plan",
        ["published/native.json", {}, "agent-a", ""],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert plan["ok"] is True, plan
    plan_id = str(plan["data"]["plan_id"] if "data" in plan else plan["plan_id"])

    apply = port.dispatch_gui(
        "apply_build",
        [plan_id, True, "published/native.json", {}, "agent-a", ""],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert apply["ok"] is True, apply
    apply_run = str(apply["task"]["run_id"])
    applied = _wait(port, context, apply_run)
    assert applied["status"] == "succeeded", applied

    releases = port.dispatch_gui(
        "list_native_memory_releases", [{}, "agent-a"],
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert releases["ok"] is True, releases
    rows = releases.get("releases") or releases.get("data", {}).get("releases") or []
    assert len(rows) == 1
    release_id = rows[0]["release_id"]

    verified = port.dispatch_gui(
        "verify_release",
        [release_id, "published/native.json", {}, "agent-a", ""],
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert verified["ok"] is True, verified

    rolled = port.dispatch_gui(
        "rollback_native_memory_release",
        [release_id, False, True, {}, "agent-a", ""],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert rolled["ok"] is True, rolled
    assert not (tmp_path / "published" / "native.json").exists()
    port._task_service().shutdown(timeout=5.0)


def test_projection_gui_registry_is_implemented(tmp_path: Path) -> None:
    port = _port(tmp_path)
    entries = port.coverage()["surfaces"]["gui"]["entries"]
    names = {
        "get_projection_source_map", "get_build_progress", "build_projection",
        "start_build_projection", "cancel_build_projection", "delete_projection",
        "set_projection_source_enabled", "create_build_plan", "apply_build",
        "publish_reconstructed_memory", "verify_release", "rollback_release",
        "rollback_native_memory_release", "list_native_memory_releases",
        "list_publish_targets", "list_releases", "choose_publish_target_path",
    }
    selected = [item for item in entries if item["name"] in names]
    assert {item["name"] for item in selected} == names
    assert all(item["status"] == "implemented" for item in selected), selected


# ---------------------------------------------------------------------------
# Handoff 0.7.1: honest engine discovery / LLM participation / race-safe cancel
# ---------------------------------------------------------------------------


def test_host_llm_agents_never_synthetic_host(monkeypatch, tmp_path: Path) -> None:
    import memoryguard.host_agent_backend as backend

    port = _port(tmp_path)
    context = _context(tmp_path)
    monkeypatch.setattr(
        backend,
        "detect_available_agents",
        lambda: [
            {"agent": "cursor", "cli": "/fake/cursor", "label": "Cursor Agent"},
            {"agent": "codex", "cli": "/fake/codex", "label": "Codex"},
        ],
    )
    result = port.dispatch_gui(
        "list_host_llm_agents", {}, context=context, generation=11, state="V2_ACTIVE"
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["primary"] == "cursor"
    assert [a["agent"] for a in data["agents"]] == ["cursor", "codex"]
    # 绝无合成「host」行；绝不把本地可执行路径暴露给 Web UI
    assert all(a["agent"] != "host" for a in data["agents"])
    assert all("cli" not in a for a in data["agents"])
    assert all("mode" in a and a["mode"] == "cli" for a in data["agents"])


def test_resolve_engine_id_uses_fresh_allowlist_not_payload(monkeypatch, tmp_path: Path) -> None:
    import memoryguard.host_agent_backend as backend

    port = _port(tmp_path)
    monkeypatch.setattr(
        backend,
        "detect_available_agents",
        lambda: [{"agent": "cursor", "cli": "/real/cursor", "label": "Cursor Agent"}],
    )
    resolved = port._resolve_engine_id("cursor")
    assert resolved == {"agent": "cursor", "cli": "/real/cursor", "label": "Cursor Agent"}
    # 调用方只能命名引擎 id；路径永远来自全新 allowlist，未知 id 直接拒绝
    assert port._resolve_engine_id("/etc/passwd") is None
    assert port._resolve_engine_id("nonexistent") is None


def test_projection_build_metadata_records_llm_used_and_engine(tmp_path: Path) -> None:
    from _publish_helpers import projection_scope, seed_atom
    from memoryguard.projection_v2 import ProjectionStore
    from memoryguard.runtime_v2.projection_build import ProjectionBuildService

    seed_atom(tmp_path, "m-llm", "some fact body")
    scope = projection_scope(tmp_path)
    service = ProjectionBuildService(tmp_path)
    result = service.build(
        mode="reconstructed",
        scope=scope,
        llm_provider="codex",
        llm_used=True,
        llm_engine="codex",
    )
    assert result["status"] == "succeeded"
    key = service._scope_key("reconstructed", scope)
    record = ProjectionStore(tmp_path, initialize=False).get_projection("scenario", key, scope=scope)
    assert record is not None
    metadata = dict(record.payload["metadata"])
    assert metadata["llm_used"] is True
    assert metadata["llm_engine"] == "codex"
    assert metadata["llm_provider"] == "codex"


def test_projection_build_worker_invokes_selected_cli(monkeypatch, tmp_path: Path) -> None:
    import memoryguard.host_agent_backend as backend
    from _publish_helpers import projection_scope, seed_atom
    from memoryguard.projection_v2 import ProjectionStore

    seed_atom(tmp_path, "m-cli", "some fact body", runtime_role="gui")
    scope = projection_scope(tmp_path)
    port = _port(tmp_path)

    calls: dict[str, str] = {}

    def fake_batch(tasks, agent="", cli_path="", workspace=None, execution=None):
        calls["agent"] = agent
        calls["cli_path"] = cli_path
        return [
            {"task_id": t["task_id"], "kind": "fact", "title": "t", "body": "b", "confidence": 0.9}
            for t in tasks
        ]

    monkeypatch.setattr(backend, "batch_enrich_via_cli", fake_batch)

    class FakeExtraction:
        pending = True

        def dispatch(self, operation, payload, context=None, **kw):
            tasks = ([{"task_id": "t1", "memory_id": "m-cli", "ops": ["classify"],
                       "input": {"title": "t", "body": "b", "kind_hint": "fact"}}]
                     if self.pending else [])
            if operation == "memoryguard_build_and_enrich":
                data = {"queued_or_pending": len(tasks), "pending_tasks": tasks}
            elif operation == "memoryguard_list_pending_enrichments":
                data = {"pending_count": len(tasks), "tasks": tasks}
            else:
                self.pending = False
                data = {"applied": 1}
            return {"ok": True, "status": "ok", "data": data}

    port._extraction_service = FakeExtraction()

    class FakeExecution:
        cancelled = False

        def progress(self, *a, **k):
            pass

        def check_cancelled(self):
            pass

        def own_cleanup(self, cb):
            pass

    result = port._build_projection_worker(
        scope, "reconstructed", "gui", {},
        engine={"agent": "cursor", "cli": "/fake/cursor", "label": "Cursor Agent"},
        deterministic=False,
        execution=FakeExecution(),
    )
    assert result["status"] == "succeeded"
    assert calls.get("agent") == "cursor"
    assert calls.get("cli_path") == "/fake/cursor"
    key = port._projection_service()._scope_key("reconstructed", scope)
    record = ProjectionStore(tmp_path, initialize=False).get_projection("scenario", key, scope=scope)
    assert dict(record.payload["metadata"])["llm_used"] is True


def test_projection_build_worker_enriches_validated_shared_group_scope(monkeypatch, tmp_path: Path) -> None:
    """The server-admin bridge must not replace the persisted business scope."""

    import memoryguard.host_agent_backend as backend
    from _publish_helpers import projection_scope, seed_atom
    from memoryguard.content.store import ContentStore
    from memoryguard.projection_v2 import ProjectionStore

    group_id = "shared-business-group"
    seed_atom(
        tmp_path,
        "shared-a",
        "first english memory",
        confidence=0.5,
        agent_id="member-a",
        share_group_id=group_id,
    )
    seed_atom(
        tmp_path,
        "shared-b",
        "second english memory",
        confidence=0.5,
        agent_id="member-b",
        share_group_id=group_id,
    )
    scope = projection_scope(tmp_path, agent_id="", share_group_id=group_id, provider="")
    ContentStore(tmp_path)
    port = _port(tmp_path)
    observed: dict[str, object] = {}

    def fake_batch(tasks, **kwargs):
        observed["task_ids"] = [str(item["task_id"]) for item in tasks]
        return [
            {
                "task_id": item["task_id"],
                "kind": "procedure",
                "title": "已整理",
                "body": "共享组中文整理结果",
                "confidence": 0.9,
            }
            for item in tasks
        ]

    monkeypatch.setattr(backend, "batch_enrich_via_cli", fake_batch)

    class Execution:
        cancelled = False

        def progress(self, *args, **kwargs):
            pass

        def check_cancelled(self):
            pass

        def own_cleanup(self, callback):
            pass

    result = port._build_projection_worker(
        scope,
        "reconstructed",
        "",
        # Deliberately no usable transport context: the real extraction
        # service must consume only the validated projection scope here.
        {},
        engine={"agent": "codex", "cli": "/fake/codex", "label": "Codex"},
        deterministic=False,
        execution=Execution(),
    )

    assert result["status"] == "succeeded"
    assert len(observed.get("task_ids", [])) == 2
    key = port._projection_service()._scope_key("reconstructed", scope)
    record = ProjectionStore(tmp_path, initialize=False).get_projection("scenario", key, scope=scope)
    metadata = dict(record.payload["metadata"])
    assert metadata["llm_used"] is True
    assert metadata["llm_engine"] == "codex"

    atoms = MemoryAtomStore(tmp_path, readonly=True).list_atoms(
        scope={
            "workspace_id": str(tmp_path.resolve()),
            "share_group_id": group_id,
            "agent_instance_id": "",
            "project_ref": "",
            "provider": "",
            "runtime_role": "",
        },
        status="active",
    )
    assert {item.agent_instance_id for item in atoms} == {"member-a", "member-b"}
    assert all(item.metadata.get("enrichment_mode") == "host" for item in atoms)


def test_projection_build_worker_drains_more_than_one_enrichment_page(monkeypatch, tmp_path: Path) -> None:
    import memoryguard.host_agent_backend as backend
    from _publish_helpers import projection_scope, seed_atom

    seed_atom(tmp_path, "m-paged", "some fact body", runtime_role="gui")
    port = _port(tmp_path)
    scope = projection_scope(tmp_path)
    pending = [
        {"task_id": f"t-{index}", "memory_id": "m-paged", "ops": ["classify"],
         "input": {"title": "t", "body": "b", "kind_hint": "fact"}}
        for index in range(101)
    ]
    batch_sizes = []

    def fake_batch(tasks, **kwargs):
        batch_sizes.append(len(tasks))
        return [
            {"task_id": item["task_id"], "kind": "fact", "title": "t", "body": "b", "confidence": 0.9}
            for item in tasks
        ]

    monkeypatch.setattr(backend, "batch_enrich_via_cli", fake_batch)

    class PagedExtraction:
        def dispatch(self, operation, payload, context=None, **kwargs):
            if operation == "memoryguard_build_and_enrich":
                data = {"pending_tasks": pending[:100]}
            elif operation == "memoryguard_list_pending_enrichments":
                data = {"pending_count": min(len(pending), 100), "tasks": pending[:100]}
            else:
                results = list(payload["results"])
                ids = {item["task_id"] for item in results}
                pending[:] = [item for item in pending if item["task_id"] not in ids]
                data = {"applied": len(results)}
            return {"ok": True, "status": "ok", "data": data}

    class Execution:
        cancelled = False
        def progress(self, *args, **kwargs): pass
        def check_cancelled(self): pass
        def own_cleanup(self, callback): pass

    port._extraction_service = PagedExtraction()
    result = port._build_projection_worker(
        scope, "reconstructed", "gui", {},
        engine={"agent": "codex", "cli": "/fake/codex", "label": "Codex"},
        deterministic=False, execution=Execution(),
    )
    assert result["status"] == "succeeded"
    assert pending == []
    assert batch_sizes == [100, 1]


def test_projection_build_worker_deterministic_skips_cli(monkeypatch, tmp_path: Path) -> None:
    import memoryguard.host_agent_backend as backend
    from _publish_helpers import projection_scope, seed_atom
    from memoryguard.projection_v2 import ProjectionStore

    seed_atom(tmp_path, "m-det", "some fact body", runtime_role="gui")
    scope = projection_scope(tmp_path)
    port = _port(tmp_path)

    def fail_batch(*a, **k):
        raise AssertionError("deterministic build must not invoke the CLI")

    monkeypatch.setattr(backend, "batch_enrich_via_cli", fail_batch)

    class FakeExecution:
        cancelled = False

        def progress(self, *a, **k):
            pass

        def check_cancelled(self):
            pass

        def own_cleanup(self, cb):
            pass

    result = port._build_projection_worker(
        scope, "reconstructed", "gui", {},
        engine=None,
        deterministic=True,
        execution=FakeExecution(),
    )
    assert result["status"] == "succeeded"
    key = port._projection_service()._scope_key("reconstructed", scope)
    record = ProjectionStore(tmp_path, initialize=False).get_projection("scenario", key, scope=scope)
    assert dict(record.payload["metadata"])["llm_used"] is False


def test_empty_id_cancel_resolves_single_active_projection_build(tmp_path: Path) -> None:
    import threading

    from memoryguard.runtime_v2.task_coordinator import TaskCoordinator

    port = _port(tmp_path)
    context = _context(tmp_path)
    coordinator = port._task_service()
    scope = TaskCoordinator.scope_from_context(str(tmp_path.resolve()), context)
    started = threading.Event()

    def worker(execution):
        started.set()
        while True:
            execution.check_cancelled()
            time.sleep(0.01)

    acc = coordinator.start_scope_exclusive(operation="projection_build", scope=scope, worker=worker)
    assert started.wait(1.0)
    cancelled = port.dispatch_gui(
        "cancel_build_projection", ["", True], context=context, generation=11,
        mutation=True, state="V2_ACTIVE",
    )
    assert cancelled["ok"] is True
    assert cancelled["status"] == "cancelled"
    assert cancelled["task"]["run_id"] == acc["task"]["run_id"]


def test_empty_id_cancel_no_active_fails_closed(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _context(tmp_path)
    res = port.dispatch_gui(
        "cancel_build_projection", ["", True], context=context, generation=11,
        mutation=True, state="V2_ACTIVE",
    )
    assert res["ok"] is False
    assert res["code"] == "no_active_projection_build"


def test_empty_id_cancel_ambiguous_fails_closed(tmp_path: Path) -> None:
    import threading

    from memoryguard.runtime_v2.task_coordinator import TaskCoordinator

    port = _port(tmp_path)
    context = _context(tmp_path)
    coordinator = port._task_service()
    scope = TaskCoordinator.scope_from_context(str(tmp_path.resolve()), context)
    started = threading.Event()

    def worker(execution):
        started.set()
        while True:
            execution.check_cancelled()
            time.sleep(0.01)

    a = coordinator.start(operation="projection_build", idempotency_key="k1", scope=scope, worker=worker)
    b = coordinator.start(operation="projection_build", idempotency_key="k2", scope=scope, worker=worker)
    assert started.wait(1.0)
    try:
        res = port.dispatch_gui(
            "cancel_build_projection", ["", True], context=context, generation=11,
            mutation=True, state="V2_ACTIVE",
        )
        assert res["ok"] is False
        assert res["code"] == "ambiguous_active_projection_build"
    finally:
        coordinator.cancel(a["task"]["run_id"], scope)
        coordinator.cancel(b["task"]["run_id"], scope)


def test_empty_projection_build_fails_closed_without_creating_projection(tmp_path: Path) -> None:
    from memoryguard.projection_v2 import ProjectionReadScope
    from memoryguard.runtime_v2.projection_build import ProjectionBuildError, ProjectionBuildService

    scope = ProjectionReadScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-empty",
        project_ref=str(tmp_path.resolve()),
        provider="gui",
        share_group_id="group-empty",
        sensitivity="normal",
        policy_class="private",
    )
    service = ProjectionBuildService(tmp_path)
    before = set(tmp_path.rglob("*"))
    try:
        service.build(mode="reconstructed", scope=scope, runtime_role="gui")
    except ProjectionBuildError as exc:
        assert exc.code == "no_projection_sources"
    else:  # pragma: no cover - regression assertion
        raise AssertionError("empty projection build must fail closed")
    assert set(tmp_path.rglob("*")) == before
    assert service.current(mode="reconstructed", scope=scope)["projection"] is None
