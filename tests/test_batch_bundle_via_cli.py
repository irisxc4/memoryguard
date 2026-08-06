"""Req3: batch_bundle_via_cli scope-bundle 分桶计划。

用一个 fake agent CLI 脚本（经真实 subprocess 执行、从 stdin 读 prompt、
向 stdout 返回 JSON）跑通模型路径；另测非法计划抛 ValueError 与
LLM 失败时回退启发式计划。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from memoryguard.host_agent_backend import batch_bundle_via_cli


def _record(memory_id: str, body: str, priority: int, agent_id: str = "a1"):
    return SimpleNamespace(
        memory_id=memory_id, body=body, priority=priority,
        agent_instance_id=agent_id, injection_policy="always",
        status="active",
    )


def _binding(project_ref: str = "", provider: str = "", target_type: str = "group",
             effect: str = "include"):
    return {
        "target_type": target_type,
        "target_id": "",
        "project_ref": project_ref,
        "provider": provider,
        "runtime_role": "",
        "effect": effect,
    }


def _install_fake(script: Path, payload) -> None:
    """Write a fake agent CLI: consumes the prompt on stdin, prints payload.

    payload: dict -> pretty-printed plan; str -> raw stdout (simulates a CLI
    that failed / returned unparseable text).
    """
    if isinstance(payload, str):
        print_line = f"print({payload!r})"
    else:
        print_line = (
            "print(json.dumps(plan, ensure_ascii=False))"
        )
        script.write_text(
            "import sys, json\n"
            "sys.stdin.read()\n"
            f"plan = {json.dumps(payload, ensure_ascii=False)}\n"
            f"{print_line}\n",
            encoding="utf-8",
        )
        return
    script.write_text(
        "import sys, json\n"
        "sys.stdin.read()\n"
        f"{print_line}\n",
        encoding="utf-8",
    )


@pytest.fixture
def fake_call_cli(tmp_path: Path, monkeypatch):
    import memoryguard.host_agent_backend as hab

    installed = {}

    def install(payload, name="fake.py"):
        script = tmp_path / name
        _install_fake(script, payload)
        installed["script"] = str(script)

    def run(agent, cli_path, prompt, timeout=60):
        r = subprocess.run(
            [sys.executable, installed["script"]],
            input=prompt, capture_output=True, text=True, encoding="utf-8",
        )
        return (r.stdout or "").strip()

    monkeypatch.setattr(hab, "_call_cli", run)
    return install


def test_import() -> None:
    from memoryguard.host_agent_backend import batch_bundle_via_cli
    assert callable(batch_bundle_via_cli)


def test_model_plan_scope_bundle(fake_call_cli) -> None:
    records = [
        _record("m1", "rule one", 10),
        _record("m2", "rule two", 20),
        _record("m3", "shared rule", 5),
    ]
    assignments = {
        "m1": [_binding(project_ref="projA")],
        "m2": [_binding(project_ref="projA")],
        "m3": [_binding()],
    }
    plan = {
        "bundles": [
            {"bundle_kind": "project_overlay",
             "source_memory_ids": ["m1", "m2"],
             "priority": 20,
             "body": "[1] rule one\n[2] rule two",
             "project_ref": "projA", "provider": "", "effect": "include"},
            {"bundle_kind": "shared_baseline",
             "source_memory_ids": ["m3"],
             "priority": 5,
             "body": "shared rule",
             "project_ref": "", "provider": "", "effect": "include"},
        ],
        "kept_separate": [],
    }
    fake_call_cli(plan)

    result = batch_bundle_via_cli(
        records, assignments, agent="codex", cli_path=sys.executable,
    )
    assert result["model_mode"] == "scope_bundle"
    assert result["kept_separate"] == []
    bundles = {b["bundle_kind"]: b for b in result["bundles"]}
    assert bundles["project_overlay"]["source_memory_ids"] == ["m1", "m2"]
    assert bundles["project_overlay"]["priority"] == 20
    assert bundles["project_overlay"]["project_ref"] == "projA"
    assert bundles["project_overlay"]["effect"] == "include"
    assert bundles["shared_baseline"]["source_memory_ids"] == ["m3"]
    assert bundles["shared_baseline"]["priority"] == 5


def test_kept_separate_accepted(fake_call_cli) -> None:
    records = [
        _record("m1", "standalone project rule", 10),
        _record("m2", "group rule", 5),
    ]
    assignments = {
        "m1": [_binding(project_ref="projX")],
        "m2": [_binding()],
    }
    plan = {
        "bundles": [
            {"bundle_kind": "shared_baseline",
             "source_memory_ids": ["m2"],
             "priority": 5, "body": "group rule",
             "project_ref": "", "provider": "", "effect": "include"},
        ],
        "kept_separate": ["m1"],
    }
    fake_call_cli(plan)

    result = batch_bundle_via_cli(
        records, assignments, agent="codex", cli_path=sys.executable,
    )
    assert result["model_mode"] == "scope_bundle"
    assert result["kept_separate"] == ["m1"]
    assert len(result["bundles"]) == 1


def test_rejects_cross_project_ref_merge(fake_call_cli) -> None:
    records = [
        _record("m1", "rule one", 10),
        _record("m2", "rule two", 20),
    ]
    assignments = {
        "m1": [_binding(project_ref="projA")],
        "m2": [_binding(project_ref="projB")],
    }
    # 非法：把不同 project_ref 的来源合并进同一个 bundle。
    bad_plan = {
        "bundles": [
            {"bundle_kind": "project_overlay",
             "source_memory_ids": ["m1", "m2"],
             "priority": 20, "body": "merged",
             "project_ref": "projA", "provider": "", "effect": "include"},
        ],
        "kept_separate": [],
    }
    fake_call_cli(bad_plan)

    with pytest.raises(ValueError, match="invalid_scope_bundle"):
        batch_bundle_via_cli(
            records, assignments, agent="codex", cli_path=sys.executable,
        )


def test_rejects_priority_not_source_max(fake_call_cli) -> None:
    records = [
        _record("m1", "rule one", 10),
        _record("m2", "rule two", 20),
    ]
    assignments = {
        "m1": [_binding(project_ref="projA")],
        "m2": [_binding(project_ref="projA")],
    }
    bad_plan = {
        "bundles": [
            {"bundle_kind": "project_overlay",
             "source_memory_ids": ["m1", "m2"],
             "priority": 5,  # 非来源 max（应为 20）
             "body": "merged",
             "project_ref": "projA", "provider": "", "effect": "include"},
        ],
        "kept_separate": [],
    }
    fake_call_cli(bad_plan)

    with pytest.raises(ValueError, match="invalid_scope_bundle"):
        batch_bundle_via_cli(
            records, assignments, agent="codex", cli_path=sys.executable,
        )


def test_falls_back_to_heuristic_on_model_failure(fake_call_cli) -> None:
    records = [
        _record("m1", "rule one", 10),
        _record("m2", "rule two", 20),
    ]
    assignments = {
        "m1": [_binding(project_ref="projA")],
        "m2": [_binding(project_ref="projA")],
    }
    # CLI 返回不可解析文本 -> 回退 build_bundles 启发式。
    fake_call_cli("sorry, no model available right now")

    result = batch_bundle_via_cli(
        records, assignments, agent="codex", cli_path=sys.executable,
    )
    assert result["model_mode"] == "heuristic"
    bundles = {b["bundle_kind"]: b for b in result["bundles"]}
    assert bundles["project_overlay"]["source_memory_ids"] == ["m1", "m2"]
    assert bundles["project_overlay"]["priority"] == 20
    assert bundles["project_overlay"]["project_ref"] == "projA"


def test_empty_records_no_model_call() -> None:
    result = batch_bundle_via_cli([], {})
    assert result == {"bundles": [], "kept_separate": [], "model_mode": "heuristic"}
