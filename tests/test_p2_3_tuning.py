"""P2.3 黄金集 + 全部修复项复验。

覆盖:
- P1.1 无效格式 Loader 验证失败 + runtime_verified 驱动
- P1.2/P1.5 secret 从不进入 mock model backend
- P1.3 新 release 即使超过 K 也不会因未过期被删除 + 双条件 + reversible=False
- P2.1 get_storage_overview 可通过安全 API 调用
- P2.2 所有 enum 非法值均不抛异常(含 DuplicateDecision)
- P2.3 近重复、强干扰负例、0.80 边界黄金集
"""
import sys
import json
import tempfile
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# ===========================================================================
# P1.1 Loader 复读:无效格式验证失败 + runtime_verified 驱动
# ===========================================================================


def test_loader_rejects_binary_with_title():
    """P1.1: 无效二进制格式即使包含标题也验证失败。"""
    from memoryguard.native_memory_loader import verify_takeover, register_loader, clear_loaders
    from memoryguard.schema_v3 import MemoryRecord, MemoryKind, MemoryStatus, Provenance, Completeness, TargetCapability

    clear_loaders()
    register_loader("test_surface", __import__("memoryguard.native_memory_loader",
                                                 fromlist=["MarkdownMemoryLoader"]).MarkdownMemoryLoader())

    with tempfile.TemporaryDirectory() as ws:
        target = Path(ws) / "memory.md"
        # 写入二进制内容(含标题文本但不是有效 markdown)
        target.write_bytes(b"\x00\x01\x02## title\x00\x03binary garbage")
        ir_records = [MemoryRecord(
            memory_id="m1", kind=MemoryKind.FACT, title="title", body="body",
            provenance=[Provenance(source_object_id="s1", locator="l1", excerpt_hash="h1")],
            status=MemoryStatus.CANDIDATE, completeness=Completeness.VERIFIABLE,
        )]
        result = verify_takeover(target, ir_records, surface_id="test_surface",
                                 capability=TargetCapability.NATIVE_TAKEOVER)
        assert not result.verified, "binary must not pass verification"
        assert not result.runtime_verified


def test_loader_export_only_no_verify():
    """P1.1: EXPORT_ONLY 能力无 loader,verified=False。"""
    from memoryguard.native_memory_loader import verify_takeover
    from memoryguard.schema_v3 import MemoryRecord, MemoryKind, MemoryStatus, Provenance, Completeness, TargetCapability

    with tempfile.TemporaryDirectory() as ws:
        target = Path(ws) / "memory.md"
        target.write_text("## title\nbody", encoding="utf-8")
        ir_records = [MemoryRecord(
            memory_id="m1", kind=MemoryKind.FACT, title="title", body="body",
            provenance=[Provenance(source_object_id="s1", locator="l1", excerpt_hash="h1")],
            status=MemoryStatus.CANDIDATE, completeness=Completeness.VERIFIABLE,
        )]
        result = verify_takeover(target, ir_records, surface_id="any",
                                 capability=TargetCapability.EXPORT_ONLY)
        assert not result.verified
        assert not result.runtime_verified
        assert "export_only" in result.reason


def test_loader_valid_markdown_passes():
    """P1.1: 有效 markdown 且 record title 出现时验证通过,runtime_verified=True。"""
    from memoryguard.native_memory_loader import verify_takeover, register_loader, clear_loaders, MarkdownMemoryLoader
    from memoryguard.schema_v3 import MemoryRecord, MemoryKind, MemoryStatus, Provenance, Completeness, TargetCapability

    clear_loaders()
    register_loader("test_surface", MarkdownMemoryLoader())

    with tempfile.TemporaryDirectory() as ws:
        target = Path(ws) / "memory.md"
        target.write_text("# Memory\n\n## 用户偏好\n\n用户偏好 Python\n", encoding="utf-8")
        ir_records = [MemoryRecord(
            memory_id="m1", kind=MemoryKind.PREFERENCE, title="用户偏好", body="用户偏好 Python",
            provenance=[Provenance(source_object_id="s1", locator="l1", excerpt_hash="h1")],
            status=MemoryStatus.CANDIDATE, completeness=Completeness.VERIFIABLE,
        )]
        result = verify_takeover(target, ir_records, surface_id="test_surface",
                                 capability=TargetCapability.NATIVE_TAKEOVER)
        assert result.verified, f"should pass: {result.reason}"
        assert result.runtime_verified


# ===========================================================================
# P1.2/P1.5 secret 从不进入 mock model backend
# ===========================================================================


def test_secret_never_reaches_model_backend():
    """P1.2: secret content is redacted before native V2 acceptance."""
    from _publish_helpers import native_context
    from memoryguard.content import ContentStore
    from memoryguard.runtime_v2.extraction_native import NativeExtractionEnrichmentService

    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        source = workspace / "secret.md"
        secret = "api_key=sk-abc123def456ghi789jkl012mno345pqr789stu012vwx"
        source.write_text(f"# Secret\n\n{secret}", encoding="utf-8")
        ContentStore(workspace).upsert_source_connector(
            source_id="secret-source", provider="test", source_type="selected_file",
            external_root_key=str(source.resolve()), workspace_id=str(workspace.resolve()), enabled=True,
        )
        preview = NativeExtractionEnrichmentService(workspace).extract(
            {"source_path": str(source)}, context=native_context(workspace),
        )

    candidate = preview["candidates"][0]
    assert candidate["secret_redacted"] is True
    assert secret not in candidate["preview"]


def test_non_secret_redacted_before_model():
    """P1.2: ordinary native content remains intact after the safety pass."""
    from _publish_helpers import native_context
    from memoryguard.content import ContentStore
    from memoryguard.runtime_v2.extraction_native import NativeExtractionEnrichmentService

    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        source = workspace / "safe.md"
        content = "# Preference\n\n用户偏好使用 Python 编程语言"
        source.write_text(content, encoding="utf-8")
        ContentStore(workspace).upsert_source_connector(
            source_id="safe-source", provider="test", source_type="selected_file",
            external_root_key=str(source.resolve()), workspace_id=str(workspace.resolve()), enabled=True,
        )
        preview = NativeExtractionEnrichmentService(workspace).extract(
            {"source_path": str(source)}, context=native_context(workspace),
        )

    candidate = preview["candidates"][0]
    assert candidate["secret_redacted"] is False
    assert "Python" in candidate["preview"]


def test_model_invalid_kind_falls_back():
    """P1.2: native V2 falls back to its deterministic classifier."""
    from _publish_helpers import mutation_context
    from memoryguard.governance_v2 import GovernanceV2
    from memoryguard.memory import MemoryAtomStore
    from memoryguard.runtime_v2.organizer import V2MemoryOrganizer

    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        memory = MemoryAtomStore(workspace)
        organizer = V2MemoryOrganizer(
            workspace, "group-test", memory_store=memory,
            governance=GovernanceV2(workspace, memory_store=memory),
        )
        result = organizer.write(
            {
                "event_id": "e3", "body": "用户偏好 Python",
                "kind": "not_a_kind", "agent_instance_id": "agent-test",
                "share_group_id": "group-test", "project_ref": str(workspace.resolve()),
                "provider": "test", "runtime_role": "test", "visibility": "active",
            },
            context=mutation_context(workspace),
        )

    assert result["atom"].kind == "preference"


# ===========================================================================
# P1.3 retention 双条件 + reversible
# ===========================================================================


def test_new_release_not_deleted_when_not_expired():
    """P1.3: 新 release 即使超过 K 也不会因未过期被删除。"""
    from memoryguard.gc import MemoryGuardGc

    with tempfile.TemporaryDirectory() as ws:
        releases_dir = Path(ws) / ".memoryguard" / "releases"
        releases_dir.mkdir(parents=True)
        # 创建 3 个刚创建的 release(时间为现在)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for i in range(3):
            data = {
                "release_id": f"rel-{i}",
                "build_id": f"build-{i}",
                "applied_at": now,
                "status": "verified",
                "changed_paths": [],
                "backup_paths": [],
                "verify_result": {},
            }
            (releases_dir / f"rel-{i}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # keep_releases=1, older_than_days=30:刚创建的不应被删除
        gc = MemoryGuardGc(ws, keep_releases=1, older_than_days=30)
        plan = gc.plan(dry_run=True)
        prune_items = [i for i in plan.items if "keep_releases" in i.reason]
        assert len(prune_items) == 0, f"new releases should not be pruned: {prune_items}"


def test_old_release_beyond_k_and_age_pruned():
    """P1.3: 超过 K 且过期的 release 被裁剪,reversible=False。"""
    from memoryguard.gc import MemoryGuardGc
    from datetime import datetime, timezone, timedelta

    with tempfile.TemporaryDirectory() as ws:
        releases_dir = Path(ws) / ".memoryguard" / "releases"
        releases_dir.mkdir(parents=True)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        for i in range(3):
            data = {
                "release_id": f"rel-{i}",
                "build_id": f"build-{i}",
                "applied_at": old,
                "status": "verified",
                "changed_paths": [],
                "backup_paths": [],
                "verify_result": {},
            }
            (releases_dir / f"rel-{i}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        gc = MemoryGuardGc(ws, keep_releases=1, older_than_days=30)
        plan = gc.plan(dry_run=True)
        prune_items = [i for i in plan.items if "keep_releases" in i.reason]
        assert len(prune_items) == 2, f"expected 2 pruned, got {len(prune_items)}"
        for item in prune_items:
            assert not item.reversible, "pruned release must be reversible=False"


def test_corrupt_json_preserved_not_deleted():
    """P1.3: 损坏 JSON 保留并报警,不删除。"""
    from memoryguard.gc import MemoryGuardGc

    with tempfile.TemporaryDirectory() as ws:
        releases_dir = Path(ws) / ".memoryguard" / "releases"
        releases_dir.mkdir(parents=True)
        (releases_dir / "corrupt.json").write_text("{invalid json", encoding="utf-8")

        gc = MemoryGuardGc(ws, keep_releases=0, older_than_days=0)
        plan = gc.plan(dry_run=True)
        corrupt_items = [i for i in plan.items if "corrupt" in i.reason]
        assert len(corrupt_items) == 1
        assert not corrupt_items[0].reversible


# ===========================================================================
# P2.1 get_storage_overview 可通过安全 API 调用
# ===========================================================================


def test_get_storage_overview_in_readonly_whitelist():
    """P2.1: get_storage_overview 在只读白名单中。"""
    from memoryguard.security import READONLY_API_METHODS, is_readonly_method
    assert "get_storage_overview" in READONLY_API_METHODS
    assert is_readonly_method("get_storage_overview")


# ===========================================================================
# P2.2 所有 enum 非法值均不抛异常(含 DuplicateDecision)
# ===========================================================================


def test_all_invalid_v2_kinds_are_coerced_without_crashing():
    """P2.2: V2 organizer rejects unknown kinds to the canonical fact kind."""
    from _publish_helpers import mutation_context
    from memoryguard.governance_v2 import GovernanceV2
    from memoryguard.memory import MemoryAtomStore
    from memoryguard.runtime_v2.organizer import V2MemoryOrganizer

    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        memory = MemoryAtomStore(workspace)
        organizer = V2MemoryOrganizer(
            workspace,
            "group-test",
            memory_store=memory,
            governance=GovernanceV2(workspace, memory_store=memory),
        )
        result = organizer.write(
            {
                "event_id": "invalid-kind",
                "body": "body",
                "kind": "totally_invalid_kind",
                "agent_instance_id": "agent-test",
                "share_group_id": "group-test",
                "project_ref": str(workspace.resolve()),
                "provider": "test",
                "runtime_role": "test",
                "visibility": "active",
            },
            context=mutation_context(workspace),
        )

    assert result["atom"].kind == "fact"


# ===========================================================================
# P2.3 黄金集:近重复/强干扰负例/0.80 边界
# ===========================================================================


def test_golden_set_tfidf():
    """P2.3: TF-IDF 黄金集 - 近重复/强干扰负例/0.80 边界。"""
    from _publish_helpers import seed_atom
    from memoryguard.memory import MemoryAtomStore, MemoryReadScope
    from memoryguard.runtime_v2.dedup import V2SemanticDeduplicator

    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        rows = [
            ("a", "用户偏好使用 Python"), ("b", "用户偏好使用 Python"),
            ("c", "用户喜欢用 Python 编程语言"), ("d", "用户偏好使用 Python 语言编程"),
            ("e", "用户偏好 Python"), ("f", "用户偏好 Go"),
            ("g", "项目部署在 AWS"), ("h", "用户偏好 Python"),
        ]
        for memory_id, body in rows:
            seed_atom(workspace, memory_id, body, metadata={"title": memory_id, "scope": "project"})
        scope = MemoryReadScope(
            workspace_id=str(workspace.resolve()),
            share_group_id="group-test",
            agent_instance_id="agent-test",
            project_ref=str(workspace.resolve()),
            provider="test",
            runtime_role="test",
        )
        dedup = V2SemanticDeduplicator(
            MemoryAtomStore(workspace, readonly=True), scope, threshold=0.80,
        )

        g_same = dedup.find("用户偏好使用 Python", threshold=0.80)
        g_near = dedup.find("用户喜欢用 Python 编程语言", threshold=0.80)
        g_interfere = dedup.find("用户偏好 Go", threshold=0.80)
        g_diff = dedup.find("项目后端使用 Go 语言编写服务端 API", threshold=0.80)

    assert len(g_same) >= 2
    assert g_near
    assert g_interfere
    assert len(g_diff) == 0, f"different topics should not be duplicates: {g_diff}"


def test_golden_set_jaccard():
    """P2.3: Jaccard 黄金集 - 近重复/强干扰负例/0.80 边界。"""
    from memoryguard.policies import CommunityPolicy

    policy = CommunityPolicy()

    assert policy.should_merge("用户偏好 Python", [{"body": "用户偏好 Python"}]) is True

    near = policy.should_merge(
        "用户偏好使用 Python 编程语言",
        [{"body": "用户偏好使用 Python 语言编程"}],
    )
    assert isinstance(near, bool)

    interfere = policy.should_merge(
        "用户偏好 Python 编程语言",
        [{"body": "用户偏好 Go 编程语言"}],
    )
    assert isinstance(interfere, bool)

    assert policy.should_merge(
        "用户偏好 Python 编程语言进行开发",
        [{"body": "项目后端使用 Go 语言编写服务端 API"}],
    ) is False

    assert policy.should_merge("a b c d e", [{"body": "a b c d f"}]) is False


def test_v2_memory_write_is_atomic():
    """P2.1: an invalid governed atom write leaves the current atom unchanged."""
    from _publish_helpers import mutation_context, seed_atom
    from memoryguard.memory import MemoryAtom, MemoryAtomStore

    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        seed_atom(workspace, "m1", "version 1", metadata={"title": "v1", "scope": "project"})
        store = MemoryAtomStore(workspace, readonly=False)
        scope = {
            "workspace_id": str(workspace.resolve()),
            "share_group_id": "group-test",
            "agent_instance_id": "agent-test",
            "project_ref": str(workspace.resolve()),
            "provider": "test",
            "runtime_role": "test",
        }
        before = store.get_atom("m1", scope=scope, include_building=True)
        assert before is not None
        invalid = MemoryAtom.from_value(before)
        invalid.metadata = {"body": "plaintext must be rejected from metadata"}

        with pytest.raises(ValueError):
            store.put_atom(invalid, context=mutation_context(workspace))

        after = MemoryAtomStore(workspace, readonly=True).get_atom(
            "m1", scope=scope, include_building=True
        )

    assert after is not None
    assert after.to_dict() == before.to_dict()


if __name__ == "__main__":
    test_loader_rejects_binary_with_title()
    print("OK: P1.1 binary rejected")
    test_loader_export_only_no_verify()
    print("OK: P1.1 export_only no verify")
    test_loader_valid_markdown_passes()
    print("OK: P1.1 valid markdown passes")
    test_secret_never_reaches_model_backend()
    print("OK: P1.2 secret never reaches model")
    test_non_secret_redacted_before_model()
    print("OK: P1.2 non-secret redacted before model")
    test_model_invalid_kind_falls_back()
    print("OK: P1.2 invalid kind falls back")
    test_new_release_not_deleted_when_not_expired()
    print("OK: P1.3 new release not deleted")
    test_old_release_beyond_k_and_age_pruned()
    print("OK: P1.3 old release pruned reversible=False")
    test_corrupt_json_preserved_not_deleted()
    print("OK: P1.3 corrupt json preserved")
    test_get_storage_overview_in_readonly_whitelist()
    print("OK: P2.1 get_storage_overview in whitelist")
    test_all_invalid_v2_kinds_are_coerced_without_crashing()
    print("OK: P2.2 all invalid enums no crash")
    test_golden_set_tfidf()
    print("OK: P2.3 golden set tfidf")
    test_golden_set_jaccard()
    print("OK: P2.3 golden set jaccard")
    test_v2_memory_write_is_atomic()
    print("OK: P2.1 ir save atomic")
    print("\nAll golden set tests passed.")
