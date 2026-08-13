from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.content.conversation_sync import ConversationEvent, ConversationSync
from memoryguard.content.store import ContentStore
from memoryguard.runtime_v2.history_native import (
    NativeHistoryError,
    NativeHistoryService,
    _native_history_test_capability,
)
from memoryguard.runtime_v2.history_store import ContentHistoryStore, V2HistoryScope as HistoryScope
from memoryguard.runtime_v2.native_ports import bind_native_transport_context
from memoryguard.storage.layout import WorkspaceV2Layout


class _Resolver:
    def __init__(self, scope: HistoryScope | None):
        self.scope = scope

    def resolve(self, trusted_agent_id: str, requested: dict):
        if self.scope is None:
            raise PermissionError("active_binding_required")
        assert requested["agent_instance_id"] == trusted_agent_id
        return self.scope


def _context(workspace: Path, *, agent: str = "agent-a", group: str = "group-a") -> dict:
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="host-session",
            session_source="host",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id=group,
        project_ref="project-a",
        provider="codex",
    )


def _di(value):
    return _native_history_test_capability(value)


def _seed(workspace: Path) -> tuple[HistoryScope, str, str]:
    scope = HistoryScope(
        agent_instance_id="agent-a", project_ref="project-a", provider="codex",
        share_group_id="group-a",
    )
    store = ContentStore(workspace)
    ConversationSync(store).sync(
        "codex-native-history",
        [
            ConversationEvent(
                external_object_key="native-session", event_id="event-1",
                title="native history", role="user", ordinal=0,
                content="secret user body", provider="codex",
                workspace_id=str(workspace.resolve()), agent_instance_id="agent-a",
                project_ref=scope.project_ref, share_group_id="group-a",
            ),
            ConversationEvent(
                external_object_key="native-session", event_id="event-2",
                title="native history", role="assistant", ordinal=1,
                content="secret assistant body", provider="codex",
                workspace_id=str(workspace.resolve()), agent_instance_id="agent-a",
                project_ref=scope.project_ref, share_group_id="group-a",
            ),
        ],
    )
    with store.connection() as conn:
        session_id = str(conn.execute("SELECT session_id FROM conversation_sessions").fetchone()[0])
        turn_id = str(conn.execute("SELECT turn_id FROM conversation_turns ORDER BY ordinal").fetchone()[0])
    return scope, session_id, turn_id


def test_native_history_read_does_not_create_or_repair_missing_db(tmp_path: Path):
    scope = HistoryScope(agent_instance_id="agent-a", share_group_id="group-a")
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)))
    result = service.dispatch("search", {"query": "secret"}, context=_context(tmp_path))
    assert result["ok"] is True and result["status"] == "neutral"
    assert not WorkspaceV2Layout(tmp_path).content_db.exists()


def test_native_history_never_imports_retired_history_store(tmp_path: Path, monkeypatch) -> None:
    scope, _, _ = _seed(tmp_path)
    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name == "memoryguard.conversation_history" or name.startswith("memoryguard.conversation_history."):
            raise AssertionError("retired history runtime was reached")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    result = NativeHistoryService(
        tmp_path, scope_resolver=_di(_Resolver(scope)),
    ).dispatch("search", {"query": "secret"}, context=_context(tmp_path))
    assert result["ok"] is True and result["data"]["results"]


def test_native_history_derives_scope_and_keeps_previews_body_free(tmp_path: Path):
    scope, session_id, turn_id = _seed(tmp_path)
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)))
    context = _context(tmp_path)

    search = service.dispatch(
        "memoryguard_history_search",
        {
            "query": "secret",
            # These claims must be ignored, not used as a scope selector.
            "agent_instance_id": "attacker",
            "share_group_id": "attacker-group",
            "project_ref": "attacker-project",
            "provider": "claude",
        },
        context=context,
    )
    assert search["ok"] is True
    assert search["data"]["results"]
    assert "secret assistant body" not in json.dumps(search, ensure_ascii=False)

    timeline = service.dispatch(
        "timeline", {"session_id": session_id, "anchor_turn_id": turn_id}, context=context,
    )
    assert timeline["data"]["turns"]
    assert all("content_preview" not in turn for turn in timeline["data"]["turns"])

    preview = service.dispatch(
        "extract_preview", {"session_id": session_id}, context=context,
    )
    assert preview["data"]["candidates"]
    assert all("body" not in candidate for candidate in preview["data"]["candidates"])

    raw = service.dispatch("read", {"session_id": session_id}, context=context)
    assert raw["data"]["turns"][0]["content"] == "secret user body"
    turn = service.dispatch("read", {"turn_id": turn_id}, context=context)
    assert turn["data"]["turn"]["content"] == "secret user body"
    invalid = service.dispatch(
        "read", {"session_id": session_id, "turn_id": turn_id}, context=context,
    )
    assert invalid["ok"] is False
    assert invalid["code"] == "conversation_selector_invalid"
    assert "session_id" in invalid["message"] and "turn_id" in invalid["message"]
    exported = service.dispatch("export", {"session_ids": [session_id]}, context=context)
    assert exported["data"]["sessions"][0]["turns"][0]["content"] == "secret user body"


def test_native_history_list_uses_readable_first_user_title_before_source_title(tmp_path: Path):
    scope, _, _ = _seed(tmp_path)
    listed = ContentHistoryStore(tmp_path, readonly=True).list_sessions(scope)
    assert listed["total"] == 1
    session = listed["sessions"][0]
    assert session["display_title"] == "secret user body"
    assert session["source_title"] == "native history"
    assert session["preview_excerpt"] == "secret user body"
    assert session["summarized"] is False
    assert "first_user_text" not in session


def test_native_history_missing_active_binding_is_existence_neutral(tmp_path: Path):
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(None)))
    result = service.dispatch("list_sessions", context=_context(tmp_path))
    assert result == {
        "ok": True,
        "status": "neutral",
        "operation": "list_sessions",
        "data": {
            "sessions": [], "project_groups": [], "total": 0,
            "limit": 0, "offset": 0, "neutral": True,
        },
    }


def test_native_history_delete_requires_capability_state_receipt_and_is_atomic(tmp_path: Path):
    scope, session_id, _ = _seed(tmp_path)
    store = ContentStore(tmp_path)
    ConversationSync(store).add_evidence_link(memory_id="memory-1", turn_id=_)
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)))

    # A plain AccessContext has identity but not the native transport sentinel.
    plain = AccessContext("agent-a", True, True, False)
    denied = service.dispatch(
        "delete", {"session_ids": [session_id]}, context=plain,
        generation=3, state="V2_ACTIVE", mutation_receipt={"receipt_id": "secret-receipt"},
        idempotency_key="delete-1",
    )
    assert denied["ok"] is False and denied["code"] == "native_trusted_capability_required"

    context = _context(tmp_path)
    missing_state = service.dispatch(
        "delete", {"session_ids": [session_id]}, context=context,
        generation=3, state="V2_READY", mutation_receipt={"receipt_id": "r1"},
        idempotency_key="delete-1",
    )
    assert missing_state["code"] == "v2_not_active"

    done = service.dispatch(
        "delete", {"session_ids": [session_id]}, context=context,
        generation=3, state="V2_ACTIVE", mutation_receipt={"receipt_id": "secret-receipt"},
        idempotency_key="delete-1",
    )
    assert done["ok"] is True and done["data"]["deleted_sessions"] == 1
    assert "secret-receipt" not in json.dumps(done)
    with store.connection() as conn:
        row = conn.execute("SELECT status FROM content_evidence_links WHERE memory_id='memory-1'").fetchone()
    assert row[0] == "invalid"

    replay = service.dispatch(
        "delete", {"session_ids": [session_id]}, context=context,
        generation=3, state="V2_ACTIVE", mutation_receipt={"receipt_id": "different"},
        idempotency_key="delete-1",
    )
    assert replay["ok"] is False and replay["code"] == "mutation_idempotency_conflict"


def test_native_history_delete_rejects_sentinel_only_or_tampered_bound_context(tmp_path: Path):
    """P0: transport sentinel cannot authorize without exact bound authority."""
    scope, session_id, _ = _seed(tmp_path)
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)))
    context = _context(tmp_path)

    # A caller can observe the transport sentinel on an in-process envelope,
    # but it is not a capability without the exact registered bound object.
    sentinel_only = {
        "workspace_id": str(tmp_path),
        "agent_instance_id": "agent-a",
        "share_group_id": "group-a",
        "__native_transport_capability": context["__native_transport_capability"],
    }
    removed_bound = dict(context)
    removed_bound.pop("__native_bound_context", None)
    tampered_bound = dict(context)
    tampered_bound["__native_bound_context"] = object()

    for index, forged in enumerate((sentinel_only, removed_bound, tampered_bound)):
        denied = service.dispatch(
            "delete", {"session_ids": [session_id]}, context=forged,
            generation=1, state="V2_ACTIVE",
            mutation_receipt={"receipt_id": f"forged-{index}"},
            idempotency_key=f"forged-delete-{index}",
        )
        assert denied["ok"] is False
        assert denied["code"] == "native_trusted_capability_required"

    # Public aliases cannot redirect a valid envelope; scope comes from the
    # immutable authority object.
    public_tamper = dict(context)
    public_tamper.update({
        "workspace_id": str(tmp_path / "outside"),
        "agent_instance_id": "victim",
        "share_group_id": "victim-group",
    })
    listed = service.dispatch("list_sessions", context=public_tamper)
    assert listed["ok"] is True and listed["data"]["total"] == 1

    with ContentStore(tmp_path).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversation_sessions WHERE active=1").fetchone()[0] == 1


def test_native_history_resolver_internal_typeerror_is_not_retried(tmp_path: Path):
    class Resolver:
        calls = 0

        def resolve(self, trusted_agent_id: str, requested: dict):
            self.calls += 1
            raise TypeError("resolver implementation failure")

    resolver = Resolver()
    service = NativeHistoryService(tmp_path, scope_resolver=_di(resolver))
    result = service.dispatch("search", {"query": "secret"}, context=_context(tmp_path))
    assert result["status"] == "neutral"
    assert resolver.calls == 1


def test_native_history_resolver_signature_selects_legacy_one_argument(tmp_path: Path):
    scope, _, _ = _seed(tmp_path)

    def resolve(trusted_agent_id: str):
        assert trusted_agent_id == "agent-a"
        return scope

    service = NativeHistoryService(tmp_path, scope_resolver=_di(resolve))
    result = service.dispatch("list_sessions", context=_context(tmp_path))
    assert result["ok"] is True and result["data"]["total"] == 1


def test_native_history_factory_internal_typeerror_is_not_retried(tmp_path: Path):
    scope, _, _ = _seed(tmp_path)
    calls = {"count": 0}

    def factory(workspace: Path, *, readonly: bool):
        calls["count"] += 1
        raise TypeError("factory implementation failure")

    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)), store_factory=_di(factory))
    result = service.dispatch("list_sessions", context=_context(tmp_path))
    assert result["ok"] is False and result["code"] == "history_store_unavailable"
    assert calls["count"] == 1


@pytest.mark.parametrize(
    "failure",
    [ValueError("/private/secret.sqlite"), TypeError("secret type"), sqlite3.DatabaseError("SQL /private/secret")],
)
def test_native_history_external_store_failures_use_stable_non_leaking_codes(tmp_path: Path, failure):
    ContentStore(tmp_path)

    class BrokenStore:
        readonly = True
        supports_durable_idempotency = True

        def __init__(self):
            self.workspace = tmp_path
            self.db_path = WorkspaceV2Layout(tmp_path).content_db

        def search(self, *args, **kwargs):
            raise failure

    scope = HistoryScope(agent_instance_id="agent-a", share_group_id="group-a")
    service = NativeHistoryService(
        tmp_path, history_store=_di(BrokenStore()), scope_resolver=_di(_Resolver(scope)),
    )
    result = service.dispatch("search", {"query": "secret"}, context=_context(tmp_path))
    assert result["ok"] is False
    assert result["code"] in {
        "history_store_value_error", "history_store_type_error", "history_store_database_error",
    }
    assert result["error"] == result["code"]
    assert "/private/secret" not in json.dumps(result)


def test_native_history_rejects_writable_dependency_injection(tmp_path: Path):
    scope, _, _ = _seed(tmp_path)
    service = NativeHistoryService(
        tmp_path,
        history_store=_di(ContentHistoryStore(tmp_path, readonly=False)),
        scope_resolver=_di(_Resolver(scope)),
    )
    result = service.dispatch("list_sessions", context=_context(tmp_path))
    assert result == {
        "ok": False,
        "status": "error",
        "operation": "list_sessions",
        "code": "readonly_history_store_required",
        "error": "readonly_history_store_required",
    }


def test_native_history_invalid_limits_use_stable_error_envelope(tmp_path: Path):
    scope, _, _ = _seed(tmp_path)
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)))
    result = service.dispatch("search", {"query": "secret", "limit": None}, context=_context(tmp_path))
    assert result["ok"] is False
    assert result["code"] == "invalid_limit"


def test_native_history_delete_replays_after_service_restart(tmp_path: Path):
    scope, session_id, _ = _seed(tmp_path)
    context = _context(tmp_path)
    first = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope))).dispatch(
        "delete", {"session_ids": [session_id]}, context=context,
        generation=1, state="V2_ACTIVE", mutation_receipt={"receipt_id": "r1"},
        idempotency_key="restart-delete",
    )
    second = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope))).dispatch(
        "delete", {"session_ids": [session_id]}, context=context,
        generation=1, state="V2_ACTIVE", mutation_receipt={"receipt_id": "r1"},
        idempotency_key="restart-delete",
    )
    assert first["data"]["deleted_sessions"] == 1
    assert second["ok"] is True and second["data"]["idempotent_replay"] is True


def test_native_history_delete_blocks_without_durable_receipt_table(tmp_path: Path):
    scope, session_id, _ = _seed(tmp_path)
    store = ContentStore(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE history_mutation_receipts")
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)))
    result = service.dispatch(
        "delete", {"session_ids": [session_id]}, context=_context(tmp_path),
        generation=1, state="V2_ACTIVE", mutation_receipt={"receipt_id": "r"},
        idempotency_key="no-receipt-table",
    )
    assert result["ok"] is False and result["code"] == "history_schema_invalid"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT 1 FROM conversation_sessions WHERE session_id=?", (session_id,)).fetchone()


def test_native_history_future_schema_blocks_reads_and_deletes_without_writes(tmp_path: Path):
    scope, session_id, _ = _seed(tmp_path)
    context = _context(tmp_path)
    db = WorkspaceV2Layout(tmp_path).content_db
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE content_schema_meta SET value='99' WHERE key='version'")
    before = db.read_bytes()
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)))
    read = service.dispatch("search", {"query": "secret"}, context=context)
    delete = service.dispatch(
        "delete", {"session_ids": [session_id]}, context=context,
        generation=1, state="V2_ACTIVE", mutation_receipt={"receipt_id": "r"},
        idempotency_key="future-delete",
    )
    assert read["ok"] is False and read["code"] == "history_schema_future"
    assert delete["ok"] is False and delete["code"] == "history_schema_future"
    assert db.read_bytes() == before


def test_native_history_delete_digest_binds_receipt_and_effect_flags(tmp_path: Path):
    scope, session_id, _ = _seed(tmp_path)
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)))
    context = _context(tmp_path)
    first = service.dispatch(
        "delete", {"session_ids": [session_id], "confirmed": True, "invalidate_evidence": True},
        context=context, generation=1, state="V2_ACTIVE",
        mutation_receipt={"receipt_id": "receipt-a"}, idempotency_key="digest-key",
    )
    assert first["ok"] is True
    conflict = service.dispatch(
        "delete", {"session_ids": [session_id], "confirmed": True, "invalidate_evidence": False},
        context=context, generation=1, state="V2_ACTIVE",
        mutation_receipt={"receipt_id": "receipt-a"}, idempotency_key="digest-key",
    )
    assert conflict["ok"] is False and conflict["code"] == "mutation_idempotency_conflict"
    receipt_conflict = service.dispatch(
        "delete", {"session_ids": [session_id], "confirmed": True, "invalidate_evidence": True},
        context=context, generation=1, state="V2_ACTIVE",
        mutation_receipt={"receipt_id": "receipt-b"}, idempotency_key="digest-key",
    )
    assert receipt_conflict["ok"] is False and receipt_conflict["code"] == "mutation_idempotency_conflict"


def test_native_history_delete_concurrent_replay_is_single_durable_mutation(tmp_path: Path):
    scope, session_id, _ = _seed(tmp_path)
    context = _context(tmp_path)

    def run_delete():
        return NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope))).dispatch(
            "delete", {"session_ids": [session_id]}, context=context,
            generation=1, state="V2_ACTIVE", mutation_receipt={"receipt_id": "r"},
            idempotency_key="concurrent-delete",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_delete(), range(2)))
    assert all(item["ok"] for item in results)
    assert sorted(item["data"]["deleted_sessions"] for item in results) == [1, 1]
    assert sum(bool(item["data"]["idempotent_replay"]) for item in results) == 1


def test_native_history_delete_fault_rolls_back_evidence_and_receipt(tmp_path: Path):
    scope, session_id, _ = _seed(tmp_path)
    store = ContentStore(tmp_path)
    with store.connection() as conn:
        turn_id = str(conn.execute("SELECT turn_id FROM conversation_turns WHERE session_id=? ORDER BY ordinal LIMIT 1", (session_id,)).fetchone()[0])
    ConversationSync(store).add_evidence_link(memory_id="memory-fault", turn_id=turn_id)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_history_delete BEFORE UPDATE OF active ON conversation_sessions "
            "BEGIN SELECT RAISE(ABORT, 'injected fault'); END"
        )
    service = NativeHistoryService(tmp_path, scope_resolver=_di(_Resolver(scope)))
    context = _context(tmp_path)
    failed = service.dispatch(
        "delete", {"session_ids": [session_id]}, context=context,
        generation=1, state="V2_ACTIVE", mutation_receipt={"receipt_id": "r"},
        idempotency_key="fault-delete",
    )
    assert failed["ok"] is False and failed["code"] == "history_delete_failed"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT 1 FROM conversation_sessions WHERE session_id=?", (session_id,)).fetchone()
        assert conn.execute("SELECT 1 FROM content_evidence_links WHERE memory_id='memory-fault' AND status='valid'").fetchone()
        assert conn.execute("SELECT 1 FROM history_mutation_receipts WHERE idempotency_key='fault-delete'").fetchone() is None


@pytest.mark.parametrize("field", ["scope_resolver", "history_store", "store_factory"])
def test_native_history_rejects_unwrapped_dependency_injection(tmp_path: Path, field: str):
    scope = HistoryScope(agent_instance_id="agent-a", share_group_id="group-a")
    values = {
        "scope_resolver": _Resolver(scope),
        "history_store": object(),
        "store_factory": lambda workspace, *, readonly: object(),
    }
    with pytest.raises(NativeHistoryError, match="native_test_capability_required"):
        NativeHistoryService(tmp_path, **{field: values[field]})


def test_native_history_injected_store_requires_exact_readonly_schema_and_path(tmp_path: Path):
    ContentStore(tmp_path)
    scope = HistoryScope(agent_instance_id="agent-a", share_group_id="group-a")

    class FakeStore:
        readonly = True
        supports_durable_idempotency = True

        def __init__(self, workspace: Path, db_path: Path):
            self.workspace = workspace
            self.db_path = db_path

    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"outside")
    fake = FakeStore(tmp_path, outside)
    service = NativeHistoryService(
        tmp_path, history_store=_di(fake), scope_resolver=_di(_Resolver(scope)),
    )
    result = service.dispatch("list_sessions", context=_context(tmp_path))
    assert result["ok"] is False
    assert result["code"] == "history_store_path_or_schema_mismatch"
    assert outside.read_bytes() == b"outside"


def test_native_history_rejects_workspace_and_history_symlink_sidecars_without_touching_target(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(NativeHistoryError, match="history_path_reparse_or_symlink"):
        NativeHistoryService(alias)

    workspace = tmp_path / "workspace"
    ContentStore(workspace)
    db = WorkspaceV2Layout(workspace).content_db
    sidecar_target = tmp_path / "sidecar-target"
    sidecar_target.write_bytes(b"sidecar-secret")
    sidecar = Path(str(db) + "-wal")
    try:
        sidecar.symlink_to(sidecar_target)
    except OSError:
        pytest.skip("sidecar symlink creation unavailable")
    before = sidecar_target.read_bytes()
    with pytest.raises(NativeHistoryError, match="history_path_reparse_or_symlink"):
        NativeHistoryService(workspace)
    assert sidecar_target.read_bytes() == before


def test_native_history_layout_requires_original_source_workspace(tmp_path: Path):
    from memoryguard.storage.layout import WorkspaceV2Layout

    layout = WorkspaceV2Layout(tmp_path)
    with pytest.raises(NativeHistoryError, match="source_workspace_required"):
        NativeHistoryService(layout)
    service = NativeHistoryService(layout, source_workspace=tmp_path)
    assert service.source_workspace == tmp_path.resolve()
