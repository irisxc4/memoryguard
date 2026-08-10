from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from memoryguard.runtime_v2 import (
    MutationContext,
    RuntimeSchemaError,
    RuntimeScope,
    RuntimeScopeError,
    RuntimeV2Error,
    TaskCanvas,
)


def _scope(path: Path, **changes: str) -> RuntimeScope:
    values = {
        "workspace_id": str(path.resolve()),
        "agent_instance_id": "agent-a",
        "project_ref": "project-a",
        "share_group_id": "group-a",
        "provider": "provider-a",
        "runtime_scope": "interactive",
    }
    values.update(changes)
    return RuntimeScope(**values)


def _mutation(scope: RuntimeScope, key: str) -> MutationContext:
    return MutationContext(scope, key, actor="test")


def _ref(value: str = "e-1", digest: str = "h-1", kind: str = "evidence") -> dict[str, str]:
    return {"kind": kind, "value": value, "hash": digest, "relation": "supports"}


class _Sneaky:
    def __str__(self) -> str:
        return "secret-body-that-must-not-be-stringified"


def test_ref_and_tool_fields_reject_non_scalar_stringification_and_conflicting_status(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    canvas = TaskCanvas(tmp_path, scope=scope)
    canvas.create_run("run", mutation=_mutation(scope, "create"))
    bad_values = [_Sneaky(), {"raw": "body"}, ["list"], ("tuple",)]
    for bad in bad_values:
        with pytest.raises(RuntimeV2Error):
            canvas.add_node("run", f"node-{len(str(type(bad)))}", refs=[{"kind": "evidence", "value": bad, "hash": "h"}], mutation=_mutation(scope, f"ref-{len(str(type(bad)))}"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_tool_ref("run", tool_name=_Sneaky(), provider="provider-a", output_hash="h", mutation=_mutation(scope, "tool-name"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_tool_ref("run", tool_name="read", provider=["provider-a"], output_hash="h", mutation=_mutation(scope, "provider"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_tool_ref("run", tool_name="read", provider="provider-a", path_ref={"raw": "body"}, output_hash="h", mutation=_mutation(scope, "path"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_tool_ref("run", tool_name="read", provider="provider-a", output_hash=_Sneaky(), mutation=_mutation(scope, "hash"))
    canvas.add_node("run", "node", refs=[_ref()], mutation=_mutation(scope, "node"))
    with pytest.raises(RuntimeV2Error):
        canvas.transition("run", state="running", status="failed", mutation=_mutation(scope, "conflict"))
    assert canvas.load("run").status == "queued"


def test_public_scalar_fields_reject_stringification_before_any_fact_or_event(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    canvas = TaskCanvas(tmp_path, scope=scope)
    canvas.create_run("run", mutation=_mutation(scope, "create"))
    baseline = canvas.counts()

    bad_values = (_Sneaky(), {"raw": "body"}, ["list"], ("tuple",))
    attacks = []
    for index, bad in enumerate(bad_values):
        attacks.extend(
            [
                lambda bad=bad, index=index: canvas.create_run(
                    f"bad-task-type-{index}", task_type=bad, mutation=_mutation(scope, f"bad-task-type-{index}"),
                ),
                lambda bad=bad, index=index: canvas.create_run(
                    f"bad-goal-{index}", goal=bad, mutation=_mutation(scope, f"bad-goal-{index}"),
                ),
                lambda bad=bad, index=index: canvas.create_run(
                    f"bad-requested-by-{index}", requested_by=bad, mutation=_mutation(scope, f"bad-requested-by-{index}"),
                ),
                lambda bad=bad, index=index: canvas.create_run(
                    f"bad-importance-{index}", importance=bad, mutation=_mutation(scope, f"bad-importance-{index}"),
                ),
                lambda bad=bad, index=index: canvas.add_node(
                    "run", f"bad-node-id-{index}", node_type=bad, refs=[_ref()],
                    mutation=_mutation(scope, f"bad-node-type-{index}"),
                ),
                lambda bad=bad, index=index: canvas.add_node(
                    "run", f"bad-goal-node-{index}", goal=bad, refs=[_ref()],
                    mutation=_mutation(scope, f"bad-node-goal-{index}"),
                ),
                lambda bad=bad, index=index: canvas.add_node(
                    "run", f"bad-dep-{index}", depends=[bad], refs=[_ref()],
                    mutation=_mutation(scope, f"bad-dep-{index}"),
                ),
                lambda bad=bad, index=index: canvas.add_node(
                    "run", f"bad-importance-node-{index}", importance=bad, refs=[_ref()],
                    mutation=_mutation(scope, f"bad-node-importance-{index}"),
                ),
                lambda bad=bad, index=index: canvas.checkpoint(
                    "run", {"phase": "safe"}, checkpoint_key=bad,
                    mutation=_mutation(scope, f"bad-checkpoint-key-{index}"),
                ),
                lambda bad=bad, index=index: canvas.add_blocker(
                    "run", "missing-node", {"summary": bad}, mutation=_mutation(scope, f"bad-blocker-{index}"),
                ),
            ]
        )
    attacks.extend(
        [
            lambda: canvas.create_run("bad-content", goal="raw body", mutation=_mutation(scope, "bad-content")),
            lambda: canvas.create_run("bad-control", requested_by="admin", mutation=_mutation(scope, "bad-control")),
            lambda: canvas.add_node("run", "bad-content-node", goal="history transcript", refs=[_ref()], mutation=_mutation(scope, "bad-content-node")),
            lambda: canvas.checkpoint("run", {"nested": {"scope": "leak"}}, mutation=_mutation(scope, "bad-scope")),
        ]
    )
    for attack in attacks:
        with pytest.raises(RuntimeV2Error):
            attack()
        assert canvas.counts() == baseline
        with sqlite3.connect(canvas.store.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == baseline["runs"]
            assert conn.execute("SELECT COUNT(*) FROM task_nodes").fetchone()[0] == baseline["nodes"]
            assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == baseline["events"]


def test_canvas_run_node_refs_checkpoint_tool_and_integrity(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    canvas = TaskCanvas(tmp_path, scope=scope)
    run = canvas.create_run("run-1", task_type="investigate", goal="short goal", importance=3, mutation=_mutation(scope, "create"))
    node = canvas.add_node(
        "run-1", "node-1", node_type="lookup", goal="derive result",
        refs=[_ref()], mutation=_mutation(scope, "node"), result_ref={"path_ref": "out/result.json", "hash": "rh"},
    )
    assert run.status == "queued"
    assert node.status == "pending"
    canvas.transition("run-1", "running", mutation=_mutation(scope, "run-running"))
    canvas.transition("run-1", "running", node_id="node-1", mutation=_mutation(scope, "node-running"))
    canvas.add_blocker("run-1", "node-1", {"code": "waiting", "summary": "dependency"}, mutation=_mutation(scope, "blocker"))
    canvas.add_ref("run-1", "node-1", _ref("source-1", "source-hash", "source"), mutation=_mutation(scope, "ref-2"))
    canvas.add_tool_ref(
        "run-1", node_id="node-1", tool_name="read", provider="provider-a",
        path_ref="out/result.json", output_hash="out-hash", summary_ref="summary-1",
        mutation=_mutation(scope, "tool"),
    )
    canvas.checkpoint("run-1", {"phase": "lookup", "count": 1}, node_id="node-1", mutation=_mutation(scope, "checkpoint"))
    canvas.transition("run-1", "succeeded", node_id="node-1", result_ref={"path_ref": "out/result.json", "hash": "rh"}, mutation=_mutation(scope, "node-done"))
    canvas.transition("run-1", "succeeded", mutation=_mutation(scope, "run-done"))
    view = canvas.load("run-1")
    assert view is not None
    assert view.status == "succeeded"
    assert view.nodes[0].status == "succeeded"
    assert len(view.nodes[0].refs) == 2
    assert len(view.checkpoints) == 1
    assert len(view.tool_refs) == 1
    assert view.tool_refs[0].response_digest == "out-hash"
    assert canvas.counts() == {"runs": 1, "nodes": 1, "events": 9, "refs": 2, "checkpoints": 1}
    assert canvas.integrity_check() == ["ok"]
    assert canvas.foreign_key_check() == []
    # No raw tool output or authority tables/fields are introduced.
    with sqlite3.connect(canvas.store.db_path) as conn:
        dumped = " ".join(str(row[0]) for row in conn.execute("SELECT payload_json FROM task_events"))
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "raw" not in dumped.lower()
    assert "memory_atoms" not in tables and "rule_definitions" not in tables


def test_idempotency_payload_conflict_and_failure_rollback(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    canvas = TaskCanvas(tmp_path, scope=scope)
    first = canvas.create_run("run", goal="same", mutation=_mutation(scope, "same"))
    again = canvas.create_run("run", goal="same", mutation=_mutation(scope, "same"))
    assert first.run_id == again.run_id
    with pytest.raises(RuntimeV2Error):
        canvas.create_run("run", goal="different", mutation=_mutation(scope, "same"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_node("run", "node", refs=[_ref()], mutation=_mutation(scope, "node"), fail_at="after_refs")
    assert canvas.counts() == {"runs": 1, "nodes": 0, "events": 1, "refs": 0, "checkpoints": 0}
    node = canvas.add_node("run", "node", refs=[_ref()], mutation=_mutation(scope, "node-ok"))
    assert node.node_id == "node"
    with pytest.raises(RuntimeV2Error):
        canvas.checkpoint("run", {"ok": True}, mutation=_mutation(scope, "cp"), fail_at="after_checkpoint")
    assert canvas.counts()["checkpoints"] == 0
    with pytest.raises(RuntimeV2Error):
        canvas.transition("run", "running", mutation=_mutation(scope, "run"), fail_at="after_event")
    assert canvas.load("run").status == "queued"


def test_scope_existence_neutral_and_explicit_mutation_scope(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    canvas = TaskCanvas(tmp_path, scope=scope)
    canvas.create_run("run", goal="goal", mutation=_mutation(scope, "create"))
    foreign = _scope(tmp_path, provider="other-provider")
    assert canvas.load("run", scope=foreign) is None
    assert canvas.list_nodes("run", scope=foreign) == ((), None)
    with pytest.raises(RuntimeScopeError):
        canvas.transition("run", "running", mutation=_mutation(_scope(tmp_path, workspace_id=str((tmp_path / "other").resolve())), "bad"))
    unknown = RuntimeScope(workspace_id=str(tmp_path.resolve()), provider="__UNKNOWN__")
    assert canvas.load("run", scope=unknown) is None


def test_state_machine_dependencies_cycle_and_restart_recovery(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    canvas = TaskCanvas(tmp_path, scope=scope)
    canvas.create_run("run", mutation=_mutation(scope, "create"))
    canvas.add_node("run", "a", refs=[_ref("a", "ha")], mutation=_mutation(scope, "a"))
    canvas.add_node("run", "b", depends=["a"], refs=[_ref("b", "hb")], mutation=_mutation(scope, "b"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_node("run", "c", depends=["missing"], refs=[_ref("c", "hc")], mutation=_mutation(scope, "c"))
    # Simulate a stale/corrupt dependency edge; the cycle detector still
    # refuses to append the new node.
    with sqlite3.connect(canvas.store.db_path) as conn:
        conn.execute("UPDATE task_nodes SET depends_json='[\"a2\"]' WHERE node_id='b'")
        conn.commit()
    with pytest.raises(RuntimeV2Error):
        canvas.add_node("run", "a2", depends=["b"], refs=[_ref("a2", "ha2")], mutation=_mutation(scope, "a2"))
    canvas.transition("run", "running", mutation=_mutation(scope, "running"))
    canvas.transition("run", "running", node_id="a", mutation=_mutation(scope, "a-running"))
    # Re-opening the store sees the persisted running head and can finish it.
    reopened = TaskCanvas(tmp_path, scope=scope)
    assert reopened.load("run").status == "running"
    reopened.transition("run", "succeeded", node_id="a", mutation=_mutation(scope, "a-done"))
    assert reopened.load("run").nodes[0].status == "succeeded"
    with pytest.raises(RuntimeV2Error):
        reopened.transition("run", "queued", mutation=_mutation(scope, "invalid"))


def test_raw_output_and_control_metadata_rejected(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    canvas = TaskCanvas(tmp_path, scope=scope)
    canvas.create_run("run", mutation=_mutation(scope, "create"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_node("run", "node", refs=[_ref()], result_ref={"raw_output": "secret"}, mutation=_mutation(scope, "bad-result"))
    canvas.add_node("run", "node", refs=[_ref()], mutation=_mutation(scope, "node"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_tool_ref("run", node_id="node", tool_name="shell", provider="provider-a", raw_output="secret", mutation=_mutation(scope, "raw"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_tool_ref("run", node_id="node", tool_name="shell", provider="provider-a", output_hash="h", metadata={"role": "admin"}, mutation=_mutation(scope, "role"))
    with pytest.raises(RuntimeV2Error):
        canvas.checkpoint("run", {"nested": {"permission": "grant"}}, mutation=_mutation(scope, "permission"))


def test_tool_path_containment_and_reparse_rejection(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    canvas = TaskCanvas(tmp_path, scope=scope)
    canvas.create_run("run", mutation=_mutation(scope, "create"))
    with pytest.raises(RuntimeV2Error):
        canvas.add_tool_ref("run", tool_name="read", provider="provider-a", path_ref=str(tmp_path.parent / "outside.txt"), output_hash="h", mutation=_mutation(scope, "outside"))
    outside = tmp_path.parent / "outside-runtime-output.txt"
    outside.write_text("external", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(RuntimeV2Error):
        canvas.add_tool_ref("run", tool_name="read", provider="provider-a", path_ref="link.txt", output_hash="h", mutation=_mutation(scope, "link"))


def test_readonly_does_not_create_runtime_db(tmp_path: Path) -> None:
    canvas = TaskCanvas(tmp_path, readonly=True)
    assert not canvas.store.db_path.exists()
    assert canvas.load("missing") is None
    assert canvas.counts() == {"runs": 0, "nodes": 0, "events": 0, "refs": 0, "checkpoints": 0}
    scope = RuntimeScope(workspace_id=str(tmp_path.resolve()))
    with pytest.raises(RuntimeV2Error):
        canvas.create_run("run", mutation=_mutation(scope, "write"))
    assert not canvas.store.db_path.exists()


def test_future_runtime_marker_fails_closed_without_mutation(tmp_path: Path) -> None:
    canvas = TaskCanvas(tmp_path)
    db_path = canvas.store.db_path
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE runtime_v2_schema_meta SET value='999' WHERE key='version'")
        conn.commit()
    before = db_path.read_bytes()
    with pytest.raises(RuntimeSchemaError):
        TaskCanvas(tmp_path)
    assert db_path.read_bytes() == before


def test_bounded_pagination_over_10k_nodes(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    canvas = TaskCanvas(tmp_path, scope=scope)
    canvas.create_run("run", mutation=_mutation(scope, "create"))
    now = "2026-01-01T00:00:00+00:00"
    rows = [(f"node-{index:05d}", "run", None, "task", "pending", "{}", "{}", "{}", now, "", "[]", "{}", "{}", 0) for index in range(10001)]
    heads = [(f"head-{index:05d}", "run", f"node-{index:05d}", "pending", 0, 0, now) for index in range(10001)]
    refs = [(f"ref-{index:05d}", f"node-{index:05d}", "evidence", f"e-{index}", hashlib.sha256(f"e-{index}".encode()).hexdigest(), "supports", now) for index in range(10001)]
    with sqlite3.connect(canvas.store.db_path) as conn:
        conn.executemany("INSERT INTO task_nodes(node_id,run_id,parent_node_id,node_type,state,input_json,output_json,error_json,created_at,goal,depends_json,blocker_json,result_ref_json,importance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO task_heads(head_id,run_id,node_id,state,generation,last_event_seq,updated_at) VALUES(?,?,?,?,?,?,?)", heads)
        conn.executemany("INSERT INTO task_refs(ref_id,node_id,ref_kind,ref_value,ref_hash,relation,created_at) VALUES(?,?,?,?,?,?,?)", refs)
        conn.commit()
    seen = 0
    cursor = None
    pages = 0
    while True:
        page, cursor = canvas.list_nodes("run", limit=10_000, cursor=cursor)
        assert len(page) <= 1000
        seen += len(page)
        pages += 1
        if cursor is None:
            break
    assert seen == 10001
    assert pages >= 11
