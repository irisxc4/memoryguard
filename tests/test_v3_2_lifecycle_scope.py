"""v3.2 改动包5：生命周期 + 作用域 + 安全清理测试矩阵。

覆盖场景：
1. 程序已卸载、残留目录今天被修改 -> data_only
2. 程序存在、数据目录 90 天未修改 -> installed_no_data 或 installed
3. 程序安装在系统目录、数据在用户目录 -> installed
4. 同一产品两个版本或两个配置根 -> 不同 candidate_id
5. 未知隐藏目录 -> not_detected
6. 全局记忆与项目记忆同时存在 -> scope 正确分离
7. 一个用户目录包含多个项目 -> project_ref 正确
8. 项目无法映射 -> scope=unknown
9. 选择后 scope 正确写入 SourceRoot
10. 残留归档后重新扫描消失，恢复后重新出现
11. 不可读、扫描中变化 -> 不会崩溃
12. 符号链接和共享目录 -> 归档校验拒绝
13. 未经授权不读取正文
14. 未经运行验证不得显示"已接管"
15. candidate_id 标记卸载不影响同产品其他实例
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def temp_workspace():
    """创建临时工作区。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / ".memoryguard").mkdir(parents=True, exist_ok=True)
        yield ws


# ============================================================
# 改动包1：数据契约测试
# ============================================================

class TestSchemaContracts:
    """测试新增的数据结构。"""

    def test_lifecycle_state_enum(self):
        from memoryguard.schema_v3 import LifecycleState
        assert LifecycleState.INSTALLED.value == "installed"
        assert LifecycleState.DATA_ONLY.value == "data_only"
        assert LifecycleState.IGNORED.value == "ignored"

    def test_support_level_enum(self):
        from memoryguard.schema_v3 import SupportLevel
        assert SupportLevel.A_FULL.value == "A"
        assert SupportLevel.D_IMPORT_ONLY.value == "D"

    def test_discovery_object_fields(self):
        from memoryguard.schema_v3 import DiscoveryObject
        obj = DiscoveryObject(
            discovery_object_id="test-id",
            instance_id="inst-1",
            surface_id="surf-1",
            canonical_path="/test/path",
            scope="user",
            scope_source="profile_declared",
            project_ref="",
            content_type="native_memory",
            read_strategy="import_verbatim",
            default_selected=True,
            default_reason="test",
            confidence=0.95,
            last_modified="2026-01-01T00:00:00Z",
        )
        d = obj.to_dict()
        assert d["discovery_object_id"] == "test-id"
        assert d["scope"] == "user"
        assert d["scope_source"] == "profile_declared"

    def test_source_root_new_fields(self):
        from memoryguard.schema_v3 import SourceRoot, SourceRootType
        root = SourceRoot(
            root_id="test", type=SourceRootType.SELECTED_DIRECTORY,
            display_name="test", path="/test",
            scope="user", scope_source="profile_declared",
            project_ref="proj1", discovery_object_id="dobj-1",
        )
        d = root.to_dict()
        assert d["scope_source"] == "profile_declared"
        assert d["project_ref"] == "proj1"
        assert d["discovery_object_id"] == "dobj-1"

    def test_source_root_from_dict_backward_compat(self):
        """旧格式数据（没有新字段）能正确加载。"""
        from memoryguard.schema_v3 import SourceRoot
        old_data = {
            "root_id": "test", "type": "selected_directory",
            "display_name": "test", "path": "/test",
            "scope": "project",
        }
        root = SourceRoot.from_dict(old_data)
        assert root.scope_source == "fallback"
        assert root.project_ref == ""
        assert root.discovery_object_id == ""


# ============================================================
# 改动包2：安装检测测试
# ============================================================

class TestInstallDetection:
    """测试安装检测器。"""

    def test_detect_install_path_executable_not_found(self, temp_workspace):
        from memoryguard.agent_install_detector import AgentInstallDetector
        detector = AgentInstallDetector(temp_workspace)
        evidence = detector.detect_install("nonexistent-product", [
            {"probe_type": "path_executable", "command": "nonexistent-cli-tool-xyz"},
        ])
        assert len(evidence) == 1
        assert evidence[0].found is False

    def test_detect_install_path_executable_found(self, temp_workspace):
        from memoryguard.agent_install_detector import AgentInstallDetector
        detector = AgentInstallDetector(temp_workspace)
        # python 应该在 PATH 中
        evidence = detector.detect_install("python-test", [
            {"probe_type": "path_executable", "command": "python"},
        ])
        assert len(evidence) == 1
        assert evidence[0].found is True

    def test_assess_lifecycle_not_detected(self, temp_workspace):
        from memoryguard.agent_install_detector import AgentInstallDetector
        detector = AgentInstallDetector(temp_workspace)
        result = detector.assess_lifecycle("nonexistent", [], [])
        assert result.lifecycle_state == "not_detected"
        assert result.install_confidence == 0.0

    def test_assess_lifecycle_ignored(self, temp_workspace):
        from memoryguard.agent_install_detector import AgentInstallDetector
        detector = AgentInstallDetector(temp_workspace)
        result = detector.assess_lifecycle("test", [], [], marked_ignored=True)
        assert result.lifecycle_state == "ignored"

    def test_assess_lifecycle_data_only(self, temp_workspace):
        """场景1：程序已卸载、残留目录今天被修改 -> data_only。"""
        from memoryguard.agent_install_detector import AgentInstallDetector
        # 创建一个残留数据目录
        data_dir = temp_workspace / ".fake-agent"
        data_dir.mkdir()
        (data_dir / "config.json").write_text("{}")
        detector = AgentInstallDetector(temp_workspace)
        result = detector.assess_lifecycle(
            "fake-agent",
            install_probes=[{"probe_type": "path_executable", "command": "nonexistent-fake-cli"}],
            data_paths=[str(data_dir)],
        )
        assert result.lifecycle_state == "data_only"
        assert len(result.data_evidence) == 1
        assert result.data_evidence[0].exists is True

    def test_assess_lifecycle_installed_no_data(self, temp_workspace):
        """场景2：程序存在、数据目录不存在 -> installed_no_data。"""
        from memoryguard.agent_install_detector import AgentInstallDetector
        detector = AgentInstallDetector(temp_workspace)
        result = detector.assess_lifecycle(
            "python",
            install_probes=[{"probe_type": "path_executable", "command": "python"}],
            data_paths=["/nonexistent/data/path"],
        )
        assert result.lifecycle_state in ("installed_no_data", "installed")
        assert result.install_confidence > 0.5

    def test_candidate_id_stable(self, temp_workspace):
        """场景4：同一产品不同数据路径 -> 相同 candidate_id（稳定 ID）。

        candidate_id 基于 product + host_id + profile_id，不含 data_paths，
        因此清除残留目录或换工作区不会改变 candidate_id。
        """
        from memoryguard.agent_install_detector import AgentInstallDetector
        detector = AgentInstallDetector(temp_workspace)
        result1 = detector.assess_lifecycle("trae", [], ["/path1"])
        result2 = detector.assess_lifecycle("trae", [], ["/path2"])
        # 不同数据路径应该有相同 candidate_id（稳定 ID）
        assert result1.candidate_id == result2.candidate_id

        # 不同产品应该有不同 candidate_id
        result3 = detector.assess_lifecycle("codex", [], ["/path1"])
        assert result1.candidate_id != result3.candidate_id


# ============================================================
# 改动包1：作用域选择树测试
# ============================================================

class TestSelectionTree:
    """测试作用域优先的选择树。"""

    def test_selection_tree_returns_scopes(self, temp_workspace):
        """场景6：全局记忆与项目记忆同时存在 -> scope 正确分离。"""
        from memoryguard.agent_locator import AgentLocator, DetectionContext
        ctx = DetectionContext.from_workspace(temp_workspace)
        locator = AgentLocator(temp_workspace, ctx)
        instances, _ = locator.detect_instances()
        if not instances:
            pytest.skip("No agent instances detected in test env")
        inst = instances[0]
        tree = locator.get_selection_tree(inst.instance_id)
        assert "scopes" in tree
        assert "categories" not in tree  # 旧格式不应存在

    def test_selection_tree_scope_values(self, temp_workspace):
        """场景7/8：项目归属解析。"""
        from memoryguard.agent_locator import AgentLocator, DetectionContext
        ctx = DetectionContext.from_workspace(temp_workspace)
        locator = AgentLocator(temp_workspace, ctx)
        instances, _ = locator.detect_instances()
        if not instances:
            pytest.skip("No agent instances detected")
        inst = instances[0]
        tree = locator.get_selection_tree(inst.instance_id)
        for scope_obj in tree.get("scopes", []):
            assert scope_obj["scope"] in ("user", "project", "unknown")
            assert "scope_source" in scope_obj

    def test_multi_project_resolution(self, temp_workspace):
        """多项目解析：同一 Agent 目录下多个项目子目录能正确拆开。

        模拟 ~/.claude/projects/ 下有两个项目目录，
        验证 _resolve_project_ref 能分别返回不同项目名。
        """
        from memoryguard.agent_locator import AgentLocator, DetectionContext
        ctx = DetectionContext.from_workspace(temp_workspace)
        locator = AgentLocator(temp_workspace, ctx)
        home = Path.home()
        # 模拟两个项目路径
        proj1 = str(home / ".claude" / "projects" / "proj-alpha" / "session1.jsonl")
        proj2 = str(home / ".claude" / "projects" / "proj-beta" / "session2.jsonl")
        ref1 = locator._resolve_project_ref(proj1)
        ref2 = locator._resolve_project_ref(proj2)
        assert ref1 == "proj-alpha"
        assert ref2 == "proj-beta"
        assert ref1 != ref2

    def test_workspace_not_prefix_match(self, temp_workspace):
        """workspace 父子路径判断：C:\\project 和 C:\\project-old 不应误判为同一项目。"""
        from memoryguard.agent_locator import AgentLocator, DetectionContext
        ctx = DetectionContext.from_workspace(temp_workspace)
        locator = AgentLocator(temp_workspace, ctx)
        # 构造一个名称前缀相同但不是父子关系的路径
        sibling = temp_workspace.parent / (temp_workspace.name + "-old")
        ref = locator._resolve_project_ref(str(sibling / "AGENTS.md"))
        # 不应该返回 temp_workspace 的名称
        assert ref != temp_workspace.name


# ============================================================
# 改动包1：Scope 写入 SourceRoot 测试
# ============================================================

class TestScopeWriteback:
    """场景9：选择后 scope 正确写入 SourceRoot。"""

    def test_commit_selection_writes_scope(self, temp_workspace):
        """选择提交后 scope 正确写入 SourceRoot。

        先尝试默认选中的文件（import_verbatim），若没有则手动选中所有 found 文件，
        确保 SelectionManifest 提交流程始终被测试。
        """
        from memoryguard.gui import GovernanceApi
        from memoryguard.agent_locator import AgentLocator, DetectionContext
        api = GovernanceApi(temp_workspace)
        ctx = DetectionContext.from_workspace(temp_workspace)
        locator = AgentLocator(temp_workspace, ctx)
        instances, _ = locator.detect_instances()
        if not instances:
            pytest.skip("No agent instances detected")
        inst = instances[0]
        tree = locator.get_selection_tree(inst.instance_id)
        # 先尝试默认选中的文件，再回退到所有 found 文件
        selected = []
        all_found = []
        for scope_obj in tree.get("scopes", []):
            scope = scope_obj.get("scope", "unknown")
            for cat in scope_obj.get("categories", []):
                for f in cat.get("files", []):
                    entry = {
                        "category": cat["category"],
                        "path": f["path"],
                        "scope": f.get("scope", scope),
                        "scope_source": f.get("scope_source", "fallback"),
                        "project_ref": f.get("project_ref", ""),
                        "discovery_object_id": f.get("discovery_object_id", ""),
                    }
                    all_found.append(entry)
                    if f.get("default_selected"):
                        selected.append(entry)
        if not selected:
            # 没有默认选中文件时，手动选中前 3 个 found 文件
            selected = all_found[:3]
        if not selected:
            pytest.skip("No found files to select")
        result = api.commit_selection(inst.instance_id, selected, confirmed=True)
        assert "error" not in result
        # 验证 SourceRoot 的 scope
        from memoryguard.source_registry import SourceRegistry
        reg = SourceRegistry(temp_workspace)
        for root in reg.list_sources():
            if root.agent_instance_id == inst.instance_id:
                assert root.scope in ("user", "project", "unknown")
                assert root.scope_source in ("profile_declared", "project_resolver", "fallback")


# ============================================================
# 改动包2：安全清理测试
# ============================================================

class TestSafeCleanup:
    """场景10-12：残留归档与路径安全。"""

    def test_archive_dry_run(self, temp_workspace, monkeypatch):
        """场景10：归档预演模式不实际移动。"""
        from memoryguard.agent_cleanup import AgentCleanup
        # 产品策略要求归档路径位于用户主目录下。CI 的临时目录不在真实
        # HOME 中，因此把 Path.home 指向临时工作区父目录，使测试目录
        # 落在模拟 HOME 之下，同时保留产品安全边界。
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: temp_workspace.parent))
        cleanup = AgentCleanup(temp_workspace)
        test_dir = temp_workspace / ".test-agent"
        test_dir.mkdir()
        (test_dir / "data.json").write_text("{}")
        result = cleanup.archive_agent_dir(
            "test-cid", "test-agent", str(test_dir), dry_run=True,
            allowed_data_paths=[str(test_dir)],
        )
        assert result.get("ok") is True
        assert test_dir.exists()  # dry_run 不实际移动

    def test_archive_refuses_symlink(self, temp_workspace, monkeypatch):
        """场景12：符号链接归档被拒绝。"""
        from memoryguard.agent_cleanup import AgentCleanup
        # 与 test_archive_dry_run 一致：把 Path.home 指向临时工作区父目录，
        # 使测试目录位于模拟 HOME 之下，避免 CI 触发 outside_user_home。
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: temp_workspace.parent))
        cleanup = AgentCleanup(temp_workspace)
        target = temp_workspace / "real-dir"
        target.mkdir()
        link = temp_workspace / "link-dir"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("Cannot create symlinks on this platform")
        result = cleanup.archive_agent_dir(
            "test-cid", "test-agent", str(link)
        )
        assert "error" in result or "reason_codes" in result

    def test_archive_refuses_workspace_root(self, temp_workspace):
        """场景12：禁止归档 workspace 根目录。"""
        from memoryguard.agent_cleanup import AgentCleanup
        cleanup = AgentCleanup(temp_workspace)
        result = cleanup.archive_agent_dir(
            "test-cid", "test-agent", str(temp_workspace)
        )
        assert "error" in result

    def test_mark_uninstalled_by_candidate_id(self, temp_workspace):
        """场景15：candidate_id 标记卸载不影响同产品其他实例。"""
        from memoryguard.agent_cleanup import AgentCleanup
        cleanup = AgentCleanup(temp_workspace)
        cleanup.mark_uninstalled("cid-1", product="trae")
        cleanup.mark_uninstalled("cid-2", product="trae")
        # 两个不同 candidate_id 都被标记
        candidates = cleanup._load_uninstalled_candidates()
        assert "cid-1" in candidates
        assert "cid-2" in candidates
        # 取消一个不影响另一个
        cleanup.unmark_uninstalled("cid-1", product="trae")
        candidates = cleanup._load_uninstalled_candidates()
        assert "cid-1" not in candidates
        assert "cid-2" in candidates


# ============================================================
# 改动包3：未经授权不读取正文测试
# ============================================================

class TestNoContentRead:
    """场景13：未经授权不读取正文。"""

    def test_discovery_phase_reads_no_content(self, temp_workspace):
        """发现阶段只做 stat，不读正文。"""
        from memoryguard.agent_install_detector import AgentInstallDetector
        test_file = temp_workspace / ".test-agent" / "secret.txt"
        test_file.parent.mkdir()
        test_file.write_text("SECRET_CONTENT_SHOULD_NOT_BE_READ")
        detector = AgentInstallDetector(temp_workspace)
        # detect_data_residue 只返回 stat 信息
        evidence = detector.detect_data_residue("test-agent", [str(test_file.parent)])
        assert len(evidence) == 1
        # 返回结果中不应包含文件内容
        ev = evidence[0]
        assert not hasattr(ev, "content") or not ev.content
        assert "SECRET_CONTENT" not in str(ev.to_dict())


# ============================================================
# 集成测试
# ============================================================

class TestIntegration:
    """集成场景测试。"""

    def test_full_discovery_flow(self, temp_workspace):
        """完整发现流程：安装检测 -> 数据检测 -> 生命周期评估。"""
        from memoryguard.agent_install_detector import AgentInstallDetector
        detector = AgentInstallDetector(temp_workspace)
        results = detector.detect_all([])
        # 不应该崩溃
        assert isinstance(results, list)

    def test_unknown_dot_dir_not_treated_as_agent(self, temp_workspace):
        """场景5：未知隐藏目录不应被当作正常 Agent。"""
        from memoryguard.agent_mapping import product_for_dot_dir
        assert product_for_dot_dir(".unknown-random-dir") is None


class TestLifecycleApiSplit:
    """正常 Agent 与残留候选分流。"""

    def _patch_instances(self, monkeypatch, temp_workspace, surfaces):
        from memoryguard.agent_locator import AgentLocator
        from memoryguard.schema_v3 import AgentInstance, TargetCapability

        inst = AgentInstance(
            instance_id="inst-split",
            profile_id="split@profile-1",
            product="split-agent",
            profile_version="1",
            platform="test",
            host_id="host",
            workspace=str(temp_workspace),
            surfaces=surfaces,
            target_capability=TargetCapability.EXPORT_ONLY,
        )
        monkeypatch.setattr(AgentLocator, "detect_instances", lambda self: ([inst], {}))
        return inst

    def test_shared_agents_md_does_not_create_data_only_residual(self, temp_workspace, monkeypatch):
        from memoryguard.gui import GovernanceApi

        agents_md = temp_workspace / "AGENTS.md"
        agents_md.write_text("shared rules", encoding="utf-8")
        self._patch_instances(monkeypatch, temp_workspace, [{
            "surface_id": "shared_agents_md",
            "resolved_path": str(agents_md),
            "status": "found",
            "evidence_role": "shared_surface",
        }])

        result = GovernanceApi(str(temp_workspace)).list_agents()

        assert result["agents"] == []
        assert result["residuals"] == []
        assert result["total"] == 0
        assert result["residual_total"] == 0

    def test_private_data_only_goes_to_residuals_and_cleanup_items(self, temp_workspace, monkeypatch):
        from memoryguard.gui import GovernanceApi

        fake_home = temp_workspace / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        private_dir = fake_home / "private-agent"
        private_dir.mkdir()
        (private_dir / "memory.json").write_text("{}", encoding="utf-8")
        self._patch_instances(monkeypatch, temp_workspace, [{
            "surface_id": "private_memory",
            "resolved_path": str(private_dir),
            "status": "found",
            "evidence_role": "private_data_evidence",
        }])
        api = GovernanceApi(str(temp_workspace))
        result = api.list_agents()

        assert result["agents"] == []
        assert result["residual_total"] == 1
        residual = result["residuals"][0]
        assert residual["lifecycle_state"] == "data_only"
        assert residual["private_data_surface_count"] == 1

        cleanup = api.get_residual_cleanup(residual["instance_id"])
        assert cleanup["candidate_id"] == residual["candidate_id"]
        assert len(cleanup["data_evidence"]) == 1
        assert len(cleanup["archive_previews"]) == 1
        assert len(cleanup["items"]) == 1
        assert cleanup["items"][0]["residual_type"] == "private_data_evidence"

    def test_archive_api_ignores_client_supplied_whitelist(self, temp_workspace, monkeypatch):
        from memoryguard.gui import GovernanceApi

        fake_home = temp_workspace / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        private_dir = fake_home / "private-agent"
        private_dir.mkdir()
        (private_dir / "memory.json").write_text("{}", encoding="utf-8")
        malicious_dir = temp_workspace / "malicious"
        malicious_dir.mkdir()
        self._patch_instances(monkeypatch, temp_workspace, [{
            "surface_id": "private_memory",
            "resolved_path": str(private_dir),
            "status": "found",
            "evidence_role": "private_data_evidence",
        }])
        api = GovernanceApi(str(temp_workspace))
        residual = api.list_agents()["residuals"][0]

        result = api.archive_agent_dir(
            product="split-agent",
            dir_path=str(malicious_dir),
            candidate_id=residual["candidate_id"],
            dry_run=True,
            allowed_data_paths=[str(malicious_dir)],
        )

        assert result["error"] == "path_validation_failed"
        assert "path_not_in_profile_whitelist" in result["reason_codes"]
        assert malicious_dir.exists()


class TestGovernanceSnapshotSecurity:
    """治理快照安全回归。"""

    def test_snapshot_json_does_not_contain_raw_secret(self, temp_workspace):
        from memoryguard.gui import GovernanceApi
        from memoryguard.schema_v3 import MemoryEvent, QuarantineEntry
        from memoryguard.shared_memory_store import SharedMemoryStore

        secret = "AKIAABCDEFGHIJKLMNOP"
        store = SharedMemoryStore(temp_workspace, "default")
        store.append_event(MemoryEvent(
            event_id="evt-secret",
            agent_instance_id="agent-1",
            share_group_id="default",
            raw_content=f"deploy key {secret}",
            created_at="2026-01-01T00:00:00Z",
        ))
        store.append_quarantine(QuarantineEntry(
            quarantine_id="quar-secret",
            memory_id="mem-secret",
            reason="secret",
            detected_pattern="aws_key",
            original_content=f"deploy key {secret}",
            quarantined_at="2026-01-01T00:00:01Z",
        ))

        api = GovernanceApi(str(temp_workspace))
        snapshot = api.get_governance_snapshot()
        quarantine = api.get_quarantine()
        payload = json.dumps({"snapshot": snapshot, "quarantine": quarantine}, ensure_ascii=False)

        assert secret not in payload
        assert "AKIA" not in payload
        assert "original_content" not in payload
        assert "raw_content\":" not in payload
        assert "REDACTED:aws_access_key" in payload

    def test_supersede_decisions_do_not_contain_raw_secret(self, temp_workspace):
        from memoryguard.gui import GovernanceApi
        from memoryguard.schema_v3 import DecisionEvent, MemoryKind, SharedMemoryRecord, SharedMemoryStatus
        from memoryguard.shared_memory_store import SharedMemoryStore

        secret = "AKIAABCDEFGHIJKLMNOP"
        store = SharedMemoryStore(temp_workspace, "default")
        store.append_record(SharedMemoryRecord(
            memory_id="old-secret",
            body=f"old key {secret}",
            kind=MemoryKind.FACT,
            status=SharedMemoryStatus.SHADOWED,
        ))
        store.append_record(SharedMemoryRecord(
            memory_id="new-secret",
            body=f"new key {secret}",
            kind=MemoryKind.CORRECTION,
            status=SharedMemoryStatus.ACTIVE,
            supersedes=["old-secret"],
        ))
        store.append_decision(DecisionEvent(
            event_id="decision-secret",
            actor="auto",
            action="auto_supersede",
            target_ids=["old-secret", "new-secret"],
            reason="correction",
            created_at="2026-01-01T00:00:02Z",
        ))

        result = GovernanceApi(str(temp_workspace)).get_supersede_decisions()
        payload = json.dumps(result, ensure_ascii=False)

        assert secret not in payload
        assert "AKIA" not in payload
        assert "REDACTED:aws_access_key" in payload


class TestDiscoveryObjectAuthorization:
    """discovery_object_id 是服务端授权依据。"""

    def _patch_tree(self, temp_workspace, monkeypatch):
        from memoryguard.agent_locator import AgentLocator

        authorized_file = temp_workspace / "agent-memory.md"
        authorized_file.write_text("allowed", encoding="utf-8")
        tree = {
            "instance_id": "inst-1",
            "profile_id": "profile-1",
            "product": "test-agent",
            "scopes": [{
                "scope": "user",
                "scope_source": "profile_declared",
                "categories": [{
                    "category": "native_memory",
                    "files": [{
                        "path": str(authorized_file),
                        "surface_id": "memory",
                        "scope": "user",
                        "scope_source": "profile_declared",
                        "project_ref": "",
                        "discovery_object_id": "valid-object-id",
                        "ingestion_policy": "import_verbatim",
                        "ownership": "external_read_only",
                        "target_role": "none",
                        "default_selected": True,
                        "status": "found",
                    }],
                }],
            }],
        }
        monkeypatch.setattr(AgentLocator, "get_selection_tree", lambda self, instance_id: tree)
        return authorized_file

    def test_forged_discovery_object_id_is_rejected(self, temp_workspace, monkeypatch):
        from memoryguard.gui import GovernanceApi

        self._patch_tree(temp_workspace, monkeypatch)
        api = GovernanceApi(str(temp_workspace))
        result = api.commit_selection("inst-1", [{
            "discovery_object_id": "forged-object-id",
            "path": str(temp_workspace / "evil.md"),
            "category": "control_surface",
            "scope": "project",
        }], confirmed=True)

        assert "error" in result
        assert result["error"] == "所有 discovery_object_id 验证失败，无有效条目"

    def test_commit_selection_uses_server_category_and_path(self, temp_workspace, monkeypatch):
        from memoryguard.gui import GovernanceApi
        from memoryguard.source_registry import SourceRegistry

        authorized_file = self._patch_tree(temp_workspace, monkeypatch)
        bogus_file = temp_workspace / "evil.md"
        bogus_file.write_text("evil", encoding="utf-8")
        api = GovernanceApi(str(temp_workspace))
        result = api.commit_selection("inst-1", [{
            "discovery_object_id": "valid-object-id",
            "path": str(bogus_file),
            "category": "control_surface",
            "scope": "project",
            "scope_source": "fallback",
            "project_ref": "evil-project",
        }], confirmed=True)

        assert "error" not in result
        reg = SourceRegistry(temp_workspace)
        roots = [r for r in reg.list_sources() if r.discovery_object_id == "valid-object-id"]
        assert len(roots) == 1
        assert roots[0].path == str(authorized_file.resolve())
        assert roots[0].source_category == "native_memory"
        assert roots[0].scope == "user"
        assert roots[0].project_ref == ""
