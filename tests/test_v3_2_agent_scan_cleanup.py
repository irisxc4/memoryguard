"""v3.2 Agent 扫描 + stale 检测 + 清理流程测试。"""
from __future__ import annotations

import os
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memoryguard.agent_mapping import (
    AGENT_PRODUCT_MAP, detect_stale_status,
    STALE_THRESHOLD_SECONDS, LIKELY_UNINSTALLED_THRESHOLD_SECONDS,
    product_for_dot_dir, is_known_product,
)
from memoryguard.agent_cleanup import AgentCleanup
from memoryguard.gui import GovernanceApi
from memoryguard.runtime_v2.agent_native import AgentNativeService


def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    msg = f"[{status}] {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg)
    return ok


def main() -> int:
    all_pass = True

    print("\n=== 1. 映射表覆盖国产 Agent ===")
    products = {v for v in AGENT_PRODUCT_MAP.values()}
    all_pass &= _check("trae 已映射", "trae" in products)
    all_pass &= _check("zcode 已映射", "zcode" in products)
    all_pass &= _check("echobird 已映射", "echobird" in products)
    all_pass &= _check("claude-code 已映射", "claude-code" in products)
    all_pass &= _check("windsurf 已映射", "windsurf" in products)
    all_pass &= _check("product_for_dot_dir", product_for_dot_dir(".trae-cn") == "trae")
    all_pass &= _check("未知目录返回 None", product_for_dot_dir(".unknown-thing") is None)

    print("\n=== 2. stale 检测：active ===")
    with tempfile.TemporaryDirectory() as tmp:
        active_dir = Path(tmp) / ".active-agent"
        active_dir.mkdir()
        (active_dir / "config.json").write_text("{}", encoding="utf-8")
        # 设置 mtime 为当前时间
        os.utime(active_dir, (time.time(), time.time()))
        stale = detect_stale_status(active_dir)
        all_pass &= _check("active 目录 -> active", stale["stale_status"] == "active",
                           f"status={stale['stale_status']}, days={stale['days_since_modified']}")

    print("\n=== 3. stale 检测：stale (30+ 天) ===")
    with tempfile.TemporaryDirectory() as tmp:
        stale_dir = Path(tmp) / ".stale-agent"
        stale_dir.mkdir()
        (stale_dir / "config.json").write_text("{}", encoding="utf-8")
        old_time = time.time() - (35 * 24 * 3600)  # 35 天前
        os.utime(stale_dir, (old_time, old_time))
        os.utime(stale_dir / "config.json", (old_time, old_time))
        stale = detect_stale_status(stale_dir)
        all_pass &= _check("35 天前 -> stale", stale["stale_status"] == "stale",
                           f"status={stale['stale_status']}, days={stale['days_since_modified']}")

    print("\n=== 4. stale 检测：likely_uninstalled (60+ 天) ===")
    with tempfile.TemporaryDirectory() as tmp:
        uninstalled_dir = Path(tmp) / ".old-agent"
        uninstalled_dir.mkdir()
        (uninstalled_dir / "config.json").write_text("{}", encoding="utf-8")
        old_time = time.time() - (65 * 24 * 3600)  # 65 天前
        os.utime(uninstalled_dir, (old_time, old_time))
        os.utime(uninstalled_dir / "config.json", (old_time, old_time))
        stale = detect_stale_status(uninstalled_dir)
        all_pass &= _check("65 天前 -> likely_uninstalled",
                           stale["stale_status"] == "likely_uninstalled",
                           f"status={stale['stale_status']}, days={stale['days_since_modified']}")

    print("\n=== 5. stale 检测：有可执行文件不标 stale ===")
    with tempfile.TemporaryDirectory() as tmp:
        exe_dir = Path(tmp) / ".exe-agent"
        exe_dir.mkdir()
        exe_file = exe_dir / "agent.exe"
        exe_file.write_text("fake", encoding="utf-8")
        old_time = time.time() - (90 * 24 * 3600)  # 90 天前
        os.utime(exe_dir, (old_time, old_time))
        os.utime(exe_file, (old_time, old_time))
        stale = detect_stale_status(exe_dir)
        all_pass &= _check("有 .exe 但 90 天 -> active（has_executable=True）",
                           stale["stale_status"] == "active",
                           f"status={stale['stale_status']}, has_executable={stale['has_executable']}")

    print("\n=== 6. 清理：标记/取消标记 uninstalled ===")
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        api = GovernanceApi(str(workspace))
        marked = api.mark_agent_uninstalled("test-agent", dir_path="/fake/path", reason="test")
        all_pass &= _check("标记成功", marked.get("marked_uninstalled") is True)
        uninstalled_file = workspace / ".memoryguard" / "cleanup" / "uninstalled.json"
        all_pass &= _check("uninstalled.json 存在", uninstalled_file.exists())
        unmarked = api.unmark_agent_uninstalled("test-agent")
        all_pass &= _check("取消标记成功", unmarked.get("marked_uninstalled") is False)
        history = api.list_cleanup_history()
        all_pass &= _check("历史含 mark + unmark", len(history.get("history", [])) >= 2,
                           f"count={len(history.get('history', []))}")

    print("\n=== 7. 清理：归档/恢复 ===")
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        fake_home = workspace / "fake-home"
        fake_home.mkdir()
        with patch.object(Path, "home", classmethod(lambda cls: fake_home)):
            cleanup = AgentCleanup(workspace)
            api = GovernanceApi(str(workspace))
            agent_dir = fake_home / "archive-agent"
            agent_dir.mkdir()
            (agent_dir / "memory.json").write_text("{}", encoding="utf-8")
            archived = cleanup.archive_agent_dir(
                "fake-candidate", "fake-agent", str(agent_dir), reason="cleanup test",
                allowed_data_paths=[str(agent_dir)],
            )
            all_pass &= _check("归档成功", archived.get("ok") is True)
            all_pass &= _check("原目录已移走", not agent_dir.exists())
            archive_id = archived.get("archive_id", "")
            restored = api.restore_archived_agent(archive_id)
            all_pass &= _check("恢复成功", restored.get("ok") is True)
            all_pass &= _check("原目录已恢复", agent_dir.exists())
            archives = api.list_archived_agents()
            all_pass &= _check("归档列表有记录", archives.get("total", 0) >= 1)
            fake_api_archive = api.archive_agent_dir("fake-agent", str(agent_dir), reason="unsafe")
            all_pass &= _check("API 无 candidate_id 拒绝归档", fake_api_archive.get("error") == "candidate_id_required")

    print("\n=== 8. 候选列表 API（通过 GovernanceApi） ===")
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        fake_home = workspace / "fake-home"
        fake_home.mkdir()
        with patch.object(Path, "home", classmethod(lambda cls: fake_home)):
            api = GovernanceApi(str(workspace))
            candidates = api.list_agent_candidates()
        all_pass &= _check("候选 API 返回列表", isinstance(candidates.get("candidates"), list))
        all_pass &= _check("候选含 total 字段", "total" in candidates)

    print("\n=== 9. cleanup result must match filesystem state ===")
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        fake_home = workspace / "fake-home"
        fake_home.mkdir()
        agent_dir = fake_home / "stale-agent"
        agent_dir.mkdir()
        (agent_dir / "memory.json").write_text("{}", encoding="utf-8")
        original_rmtree = shutil.rmtree

        def _delete_then_recreate(path: str) -> None:
            original_rmtree(path)
            agent_dir.mkdir()

        with (
            patch.object(Path, "home", classmethod(lambda cls: fake_home)),
            patch("memoryguard.agent_cleanup.shutil.rmtree", side_effect=_delete_then_recreate),
        ):
            cleanup = AgentCleanup(workspace)
            result = cleanup.purge_agent_dir(
                "fake-candidate", "fake-agent", str(agent_dir),
                allowed_data_paths=[str(agent_dir)],
            )
        all_pass &= _check(
            "recreated directory is distinguished from a file lock",
            result.get("error") == "purge_recreated" and agent_dir.exists(),
            f"result={result}",
        )

        denied_target = fake_home / "denied-agent"
        denied_target.mkdir()
        with (
            patch.object(Path, "home", classmethod(lambda cls: fake_home)),
            patch.object(Path, "rename", side_effect=PermissionError(5, "Access denied")),
        ):
            denied_result = cleanup.purge_agent_dir(
                "fake-candidate", "fake-agent", str(denied_target),
                allowed_data_paths=[str(denied_target)],
            )
        all_pass &= _check(
            "permission error on root falls back to content cleanup",
            denied_result.get("ok") is True and denied_result.get("root_preserved") is True and denied_target.exists(),
            f"result={denied_result}",
        )

        content_fallback_target = fake_home / "content-fallback-agent"
        content_fallback_target.mkdir()
        (content_fallback_target / "memory.json").write_text("{}", encoding="utf-8")
        original_rename = Path.rename

        def _fail_root_rename(self, target):
            if self == content_fallback_target:
                raise PermissionError(5, "Access denied")
            return original_rename(self, target)

        with (
            patch.object(Path, "home", classmethod(lambda cls: fake_home)),
            patch.object(Path, "rename", _fail_root_rename),
        ):
            fallback_result = cleanup.purge_agent_dir(
                "fake-candidate", "fake-agent", str(content_fallback_target),
                allowed_data_paths=[str(content_fallback_target)],
            )
        all_pass &= _check(
            "content fallback clears children while preserving root",
            fallback_result.get("ok") is True
            and fallback_result.get("root_preserved") is True
            and content_fallback_target.exists()
            and not (content_fallback_target / "memory.json").exists(),
            f"result={fallback_result}",
        )

        stale_target = fake_home / "stale-agent-after-delete"
        stale_target.mkdir()
        (stale_target / "memory.json").write_text("{}", encoding="utf-8")
        original_rename_for_stale = Path.rename

        def _fail_stale_root_rename(self, target):
            if self == stale_target:
                raise PermissionError(5, "Access denied")
            return original_rename_for_stale(self, target)

        with (
            patch.object(Path, "home", classmethod(lambda cls: fake_home)),
            patch.object(Path, "rename", _fail_stale_root_rename),
            patch("memoryguard.agent_cleanup.shutil.rmtree", return_value=None),
        ):
            stale_result = cleanup.purge_agent_dir(
                "fake-candidate", "fake-agent", str(stale_target),
                allowed_data_paths=[str(stale_target)],
            )
        all_pass &= _check(
            "content fallback verifies children are actually gone",
            stale_result.get("error") == "purge_contents_partial"
            and stale_result.get("blocked")
            and (stale_target / "memory.json").exists(),
            f"result={stale_result}",
        )

        external_target = fake_home / "external-delete-agent"
        external_target.mkdir()
        external_child = external_target / "memory.json"
        external_child.write_text("{}", encoding="utf-8")
        original_rename_for_external = Path.rename

        def _fail_external_root_and_child_rename(self, target):
            if self == external_target or self == external_child:
                raise PermissionError(5, "Access denied")
            return original_rename_for_external(self, target)

        def _fake_external_run(cmd, **kwargs):
            import re
            script = cmd[-1]
            match = re.search(r"-File','([^']+)'", script)
            script_path = Path(match.group(1))
            text = script_path.read_text(encoding="utf-8")
            result_match = re.search(r"\$result = '([^']+)'", text)
            result_path = Path(result_match.group(1))
            external_child.unlink()
            result_path.write_text(json.dumps({"ok": True, "exists": False, "path": str(external_child)}), encoding="utf-8")
            class Completed:
                returncode = 0
                stderr = ""
            return Completed()

        with (
            patch.object(Path, "home", classmethod(lambda cls: fake_home)),
            patch.object(Path, "rename", _fail_external_root_and_child_rename),
            patch("memoryguard.agent_cleanup.sys.platform", "win32"),
            patch("memoryguard.agent_cleanup.shutil.which", lambda name: "powershell.exe" if name == "powershell.exe" else None),
            patch("memoryguard.agent_cleanup.subprocess.run", _fake_external_run),
            patch.dict("os.environ", {"MEMORYGUARD_ENABLE_EXTERNAL_DELETE": "1"}),
        ):
            external_result = cleanup.purge_agent_dir(
                "fake-candidate", "fake-agent", str(external_target),
                allowed_data_paths=[str(external_target)],
            )
        all_pass &= _check(
            "content fallback can use external Windows deletion backend",
            external_result.get("ok") is True
            and external_result.get("root_preserved") is True
            and not external_child.exists(),
            f"result={external_result}",
        )

        blocked_target = fake_home / "blocked-agent"
        blocked_target.mkdir()
        (blocked_target / "memory.json").write_text("{}", encoding="utf-8")
        with (
            patch.object(Path, "home", classmethod(lambda cls: fake_home)),
            patch("memoryguard.agent_cleanup.shutil.rmtree", side_effect=PermissionError("locked")),
        ):
            blocked_result = cleanup.purge_agent_dir(
                "fake-candidate", "fake-agent", str(blocked_target),
                allowed_data_paths=[str(blocked_target)],
            )
        all_pass &= _check(
            "failed tombstone deletion restores the original path",
            blocked_result.get("error") == "purge_failed" and blocked_target.exists(),
            f"result={blocked_result}",
        )

        purge_target = fake_home / "purge-agent"
        purge_target.mkdir()
        (purge_target / "memory.json").write_text("{}", encoding="utf-8")
        with patch.object(Path, "home", classmethod(lambda cls: fake_home)):
            purge_result = cleanup.purge_agent_dir(
                "fake-candidate", "fake-agent", str(purge_target),
                allowed_data_paths=[str(purge_target)],
            )
        all_pass &= _check(
            "successful purge removes the target",
            purge_result.get("ok") is True and not purge_target.exists(),
            f"result={purge_result}",
        )

        archived_source = fake_home / "archived-agent"
        archived_source.mkdir()
        with patch.object(Path, "home", classmethod(lambda cls: fake_home)):
            archived = cleanup.archive_agent_dir(
                "fake-candidate", "fake-agent", str(archived_source),
                allowed_data_paths=[str(archived_source)],
            )
        archive_id = archived.get("archive_id", "")
        archive_root = cleanup.archived_dir / archive_id
        with patch("memoryguard.agent_cleanup.shutil.rmtree", return_value=None):
            delete_result = cleanup.delete_archived(archive_id)
        all_pass &= _check(
            "existing archive must not be reported as deleted",
            delete_result.get("error") == "archive_delete_incomplete" and archive_root.exists(),
            f"result={delete_result}",
        )

    print("\n=== 10. public V2 residual paths follow agent-scan evidence ===")
    nested_root = Path(tmpfile := tempfile.mkdtemp()) / ".agent"
    nested_child = nested_root / "memories"
    nested_child.mkdir(parents=True)

    class _FakeInstance:
        instance_id = "nested-agent"
        product = "fake-agent"
        surfaces = [
            {
                "status": "found",
                "resolved_path": str(nested_root),
                "evidence_role": "private_data_evidence",
            },
            {
                "status": "found",
                "resolved_path": str(nested_child),
                "evidence_role": "private_data_evidence",
            },
        ]

    class _FakeLocator:
        context = SimpleNamespace(platform="test", host_id="test")

        def __init__(self, _workspace):
            pass

        def detect_instances(self):
            return [_FakeInstance()], {}

        def discover_candidates(self, **_kwargs):
            return []

    residual = AgentNativeService(
        workspace,
        locator_factory=_FakeLocator,
    ).residual_cleanup(instance_id="nested-agent")
    paths = [item["path"] for item in residual["items"]]
    all_pass &= _check(
        "V2 cleanup targets are limited to discovered private-data evidence",
        paths == [str(nested_root.resolve()), str(nested_child.resolve())],
        f"paths={paths}, evidence={residual['data_evidence']}",
    )

    print("\n" + "=" * 50)
    if all_pass:
        print("All v3.2 Agent scan + cleanup tests PASSED")
        return 0
    print("Some Agent scan + cleanup tests FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
