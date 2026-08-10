from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from memoryguard.content import ContentStore, ConversationShadowBridge, ConversationSync
from memoryguard.conversation_history import ConversationHistoryStore, HistoryScope
from memoryguard.system.manifest import ManifestManager, ManifestState


def _scope() -> HistoryScope:
    return HistoryScope(agent_instance_id="agent-a", provider="codex", share_group_id="share-a")


def _bridge(root: Path) -> tuple[ContentStore, ConversationShadowBridge]:
    manifest = ManifestManager(root)
    manifest.transition(ManifestState.V2_BUILDING, migration_id="shadow-tests")
    content = ContentStore(root)
    return content, ConversationShadowBridge(root, content_store=content, manifest=manifest, enabled=True)


def test_shadow_requires_explicit_store_and_manifest(tmp_path: Path):
    bridge = ConversationShadowBridge(tmp_path, enabled=True)
    result = bridge.sync_turn(
        external_session_id="s", provider="codex", role="user", content="x", event_id="e",
        title="", created_at="", scope=_scope(),
    )
    assert result["status"] == "disabled"
    assert not (tmp_path / ".memoryguard").exists()


def test_dual_write_replay_and_v1_primary(tmp_path: Path):
    content, bridge = _bridge(tmp_path)
    history = ConversationHistoryStore(tmp_path)
    conversation = SimpleNamespace(
        conv_id="session-1", title="", project_ref="",
        messages=[{"role": "user", "content": "hello", "event_id": "e1"}],
    )
    first = history.import_conversations([conversation], provider="codex", scope=_scope(), shadow=bridge)
    before = content.counts()
    replay = history.import_conversations([conversation], provider="codex", scope=_scope(), shadow=bridge)
    assert first["conversation_count"] == replay["conversation_count"] == 1
    assert first["shadow"][0]["status"] == "complete"
    assert replay["shadow"][0]["status"] == "complete"
    assert content.counts() == before


def test_shadow_outbox_retry_after_projection_failure(tmp_path: Path):
    content, bridge = _bridge(tmp_path)
    conversation = SimpleNamespace(
        conv_id="retry", title="", messages=[{"role": "user", "content": "hello", "event_id": "e1"}],
    )
    failed = bridge.sync_conversation(conversation, provider="codex", scope=_scope(), max_chars=1)
    assert failed["status"] == "failed"
    recovered = bridge.sync_conversation(conversation, provider="codex", scope=_scope())
    assert recovered["status"] == "complete"
    assert content.counts()["content_occurrences"] == 1


def test_shadow_outbox_recovers_after_finish_crash(tmp_path: Path, monkeypatch):
    content, bridge = _bridge(tmp_path)
    conversation = SimpleNamespace(
        conv_id="crash", title="", messages=[{"role": "user", "content": "hello", "event_id": "e1"}],
    )
    original = ConversationSync.finish_sync
    failed = {"once": True}

    def crash_once(self, *args, **kwargs):
        if failed["once"]:
            failed["once"] = False
            raise RuntimeError("simulated crash")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ConversationSync, "finish_sync", crash_once)
    first = bridge.sync_conversation(conversation, provider="codex", scope=_scope())
    assert first["status"] == "failed"
    recovered = bridge.sync_conversation(conversation, provider="codex", scope=_scope())
    assert recovered["status"] == "complete"
    assert content.counts()["content_occurrences"] == 1


def test_over_10k_shadow_continuation_completes(tmp_path: Path):
    content, bridge = _bridge(tmp_path)
    conversation = SimpleNamespace(
        conv_id="large", title="",
        messages=[{"role": "user", "content": str(i), "event_id": f"e-{i}"} for i in range(10_001)],
    )
    result = bridge.sync_conversation(conversation, provider="codex", scope=_scope(), max_turns=1000, finalize=False)
    while result.get("continuation"):
        result = bridge.sync_conversation(
            conversation, provider="codex", scope=_scope(), max_turns=1000,
            continuation=result["continuation"], finalize=False,
        )
    assert result["status"] == "complete"
    assert content.counts()["content_occurrences"] == 10_001


def test_empty_shadow_finish_does_not_delete(tmp_path: Path):
    content, bridge = _bridge(tmp_path)
    conversation = SimpleNamespace(
        conv_id="partial", title="", messages=[{"role": "user", "content": "kept", "event_id": "e1"}],
    )
    assert bridge.sync_conversation(conversation, provider="codex", scope=_scope())["status"] == "complete"
    empty = SimpleNamespace(conv_id="partial", title="", messages=[])
    result = bridge.sync_conversation(empty, provider="codex", scope=_scope())
    assert result["status"] == "partial"
    with content.connection() as conn:
        assert conn.execute("SELECT deleted_scan_id FROM content_occurrences").fetchone()[0] == ""
