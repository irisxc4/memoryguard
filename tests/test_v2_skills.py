from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from memoryguard.migration.skills import SkillMigrationItem, SkillMigrationReadError, SkillMigrationReader
from memoryguard.skills_v2 import (
    ExecutionPolicy,
    SkillAuthorizationError,
    SkillBinding,
    SkillConflictError,
    SkillDefinition,
    SkillEvidenceRef,
    SkillMutationContext,
    SkillRuntime,
    SkillRuntimeError,
    SkillSchemaError,
    SkillStore,
    SkillValidationError,
)


HASH = "a" * 64


def ctx(root: Path, **kwargs: object) -> SkillMutationContext:
    values = {
        "workspace_id": str(root),
        "share_group_id": "group-a",
        "agent_instance_id": "agent-a",
        "project_ref": "project-a",
        "provider": "provider-a",
        "runtime_role": "runtime-a",
        "actor": "agent-a",
    }
    values.update(kwargs)
    return SkillMutationContext.trusted(**values)


def definition(*, name: str = "demo", version: int = 1, binding: SkillBinding | None = None, **kwargs: object) -> SkillDefinition:
    selected_bindings = kwargs.pop("bindings", (binding or SkillBinding("agent", "agent-a"),))
    return SkillDefinition(
        name=name,
        version=version,
        entrypoint_ref="scripts/main.py",
        entrypoint_hash=HASH,
        bindings=selected_bindings,
        evidence_refs=(SkillEvidenceRef(source_ref="fixture/manifest", digest=HASH),),
        **kwargs,
    )


def test_skills_db_marker_future_and_readonly_no_create(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    assert store.db_path == tmp_path / ".memoryguard" / "skills" / "skills.db"
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT domain,version,marker FROM schema_meta").fetchone()
        assert row == ("skills", 1, SkillStore.SCHEMA_MARKER)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    before = store.db_path.read_bytes()
    assert SkillStore(tmp_path, readonly=True).integrity()["ok"]
    assert store.db_path.read_bytes() == before

    future = tmp_path / "future" / ".memoryguard" / "skills" / "skills.db"
    future.parent.mkdir(parents=True)
    with sqlite3.connect(future) as conn:
        conn.execute("CREATE TABLE schema_meta(domain TEXT PRIMARY KEY,version INTEGER,marker TEXT,updated_at TEXT)")
        conn.execute("INSERT INTO schema_meta VALUES('skills',99,'future','now')")
        conn.execute("PRAGMA user_version=99")
        conn.commit()
    snapshot = future.read_bytes()
    with pytest.raises(SkillSchemaError):
        SkillStore(future)
    assert future.read_bytes() == snapshot

    missing = tmp_path / "missing-ro"
    with pytest.raises(FileNotFoundError):
        SkillStore(missing, readonly=True)
    assert not (missing / ".memoryguard").exists()


def test_acl_unknown_capability_and_public_context_are_fail_closed(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    with pytest.raises(SkillAuthorizationError):
        store.register(definition(), context={"workspace_id": str(tmp_path)})
    with pytest.raises(SkillValidationError):
        definition(capabilities=("totally.unknown",))
    with pytest.raises(SkillAuthorizationError):
        SkillMutationContext.from_value({"workspace_id": str(tmp_path), "share_group_id": "g", "agent": "a", "actor": "a", "admin": "false"})

    automatic = ctx(tmp_path, authority="auto", automatic=True)
    with pytest.raises(SkillAuthorizationError):
        store.register(definition(binding=SkillBinding("group", "group-a")), context=automatic)
    with pytest.raises(SkillAuthorizationError):
        store.register(definition(binding=SkillBinding("agent", "other-agent")), context=automatic)
    manual = ctx(tmp_path)
    with pytest.raises(SkillAuthorizationError):
        store.register(definition(binding=SkillBinding("provider", "provider-a")), context=manual)
    admin = ctx(tmp_path, admin=True, actor="admin")
    result = store.register(definition(binding=SkillBinding("provider", "provider-a")), context=admin)
    assert result.skill_id


def test_context_capability_is_private_and_trusted_flag_is_strict(tmp_path: Path) -> None:
    values = {
        "workspace_id": str(tmp_path), "share_group_id": "g", "agent_instance_id": "a",
        "project_ref": "p", "provider": "provider", "runtime_role": "runtime", "actor": "a",
    }
    with pytest.raises(SkillAuthorizationError):
        SkillMutationContext(**values)
    with pytest.raises(SkillAuthorizationError):
        SkillMutationContext(_trusted=1, **values)
    with pytest.raises(SkillAuthorizationError):
        SkillMutationContext.trusted(_trusted=True, **values)
    with pytest.raises(SkillAuthorizationError):
        SkillMutationContext.from_value({**values, "admin": True})
    assert SkillMutationContext.trusted(**values)._trusted is True


def test_policy_capability_aliases_must_match_without_union(tmp_path: Path) -> None:
    with pytest.raises(SkillConflictError):
        ExecutionPolicy(capabilities=("skill.read",), allowed_capabilities=("skill.invoke",))
    exact = ExecutionPolicy(capabilities=("skill.read",), allowed_capabilities=("skill.read",))
    assert exact.capabilities == ("skill.read",) and exact.allowed_capabilities == ("skill.read",)
    store = SkillStore(tmp_path)
    before = store.counts()
    with pytest.raises(SkillConflictError):
        store.register(definition(execution_policy={"capabilities": ["skill.read"], "allowed_capabilities": ["skill.invoke"]}), context=ctx(tmp_path))
    assert store.counts() == before


def test_deny_binding_wins_and_reads_are_existence_neutral(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    context = ctx(tmp_path)
    item = definition(bindings=(SkillBinding("agent", "agent-a", effect="allow"), SkillBinding("agent", "agent-a", effect="deny")))
    result = store.register(item, context=context)
    assert store.get(result.skill_id, scope=context) is None
    assert store.list(scope=context) == []
    other = ctx(tmp_path, agent_instance_id="other", actor="other")
    assert store.get(result.skill_id, scope=other) is None
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM skill_definitions").fetchone()[0] == 1


def test_recursive_secret_size_depth_and_sensitive_event_are_rejected_before_write(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    before = store.counts()
    with pytest.raises(SkillValidationError):
        definition(declaration={"nested": {"token": "secret"}})
    with pytest.raises(SkillValidationError):
        definition(metadata={"password": "secret"})
    with pytest.raises(SkillValidationError):
        definition(declaration={"nested": {"v": "x" * (64 * 1024)}})
    nested: object = "leaf"
    for _ in range(10):
        nested = {"nested": nested}
    with pytest.raises(SkillValidationError):
        definition(declaration=nested)  # type: ignore[arg-type]
    assert store.counts() == before


def test_migration_rejects_unsafe_entrypoint_and_ledger_paths(tmp_path: Path) -> None:
    root = tmp_path / ".agents" / "skills" / "demo"
    root.mkdir(parents=True)
    manifest = root / "manifest.json"
    manifest.write_text('{"name":"demo","entrypoint":"../outside.py"}', encoding="utf-8")
    snapshot = SkillMigrationReader(tmp_path).scan(strict=False)
    assert not snapshot.ok and snapshot.errors
    with pytest.raises(SkillMigrationReadError):
        SkillMigrationReader(tmp_path).scan(strict=True)
    with pytest.raises(SkillValidationError):
        SkillMigrationItem(source_path="skills/demo/SKILL.md", source_hash="not-a-hash", entrypoint_hash=HASH)
    store = SkillStore(tmp_path)
    with pytest.raises(SkillValidationError):
        store.record_unknown(source_path="C:/outside.json", field_name="x", value="y")
    with pytest.raises(SkillValidationError):
        store.record_unknown(source_path="skills/../outside.json", field_name="x", value="y")


def test_stable_id_identical_replay_and_key_conflict_are_zero_write(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    context = ctx(tmp_path)
    first = store.register(definition(), context=context, idempotency_key="same")
    before = store.integrity()
    replay = store.register(definition(), context=context, idempotency_key="same")
    assert replay.receipt.receipt_id == first.receipt.receipt_id
    assert store.integrity() == before
    with pytest.raises(SkillConflictError):
        store.register(definition(name="different"), context=context, idempotency_key="same")
    assert store.get(first.skill_id, scope=context) is not None


def test_versions_are_immutable_and_state_undo_is_compensating(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    context = ctx(tmp_path)
    first = store.register(definition(), context=context)
    with pytest.raises(SkillConflictError):
        store.register(definition(description="edited"), context=context)
    second = store.register(definition(version=2, description="new immutable version"), context=context)
    assert second.definition is not None and second.definition.version == 2
    disabled = store.disable(second.skill_id, context=context, expected_hash=second.definition.content_hash)
    assert disabled.definition is not None and disabled.definition.state == "disabled"
    restored = store.undo(disabled.decision.decision_id, context=context)
    assert restored.definition is not None and restored.definition.state == "active"
    with pytest.raises(SkillConflictError):
        store.undo(disabled.decision.decision_id, context=context, idempotency_key="undo-again")


def test_runtime_never_executes_and_policy_rejects_process(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    context = ctx(tmp_path)
    result = store.register(definition(execution_policy=ExecutionPolicy(capabilities=("skill.invoke",))), context=context)
    receipt = SkillRuntime(store).plan(result.skill_id, context=context, requested_capabilities=("skill.invoke",))
    assert receipt.status == "blocked"
    with pytest.raises(SkillRuntimeError):
        SkillRuntime(store).execute(result.skill_id, context=context)
    with pytest.raises(SkillValidationError):
        ExecutionPolicy(allow_external_process=True)


def test_migration_scans_manifest_without_reading_or_following_symlink(tmp_path: Path) -> None:
    root = tmp_path / ".agents" / "skills" / "demo"
    root.mkdir(parents=True)
    manifest = root / "SKILL.md"
    manifest.write_text("---\nname: demo\nversion: 1\nfuture_field: ignored\n---\nDo not persist this body\n", encoding="utf-8")
    before = manifest.read_bytes()
    snapshot = SkillMigrationReader(tmp_path).scan(strict=True)
    assert snapshot.ok and len(snapshot.items) == 1
    assert snapshot.items[0].name == "demo"
    assert "future_field" in snapshot.items[0].unknown_fields
    assert manifest.read_bytes() == before
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / ".agents" / "skills" / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    assert all("escape" not in item.source_path for item in SkillMigrationReader(tmp_path).scan().items)


def test_fault_injected_decision_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SkillStore(tmp_path)
    original = store._record

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("fault")

    monkeypatch.setattr(store, "_record", fail)
    with pytest.raises(RuntimeError):
        store.register(definition(), context=ctx(tmp_path), idempotency_key="fault")
    monkeypatch.setattr(store, "_record", original)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM skill_definitions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
