"""防腐化：官方验收脚本必须始终可运行且全部通过。

背景：安全模型加固后 accept_v3_2.py 曾因未适配鉴权而崩溃,
验收资产过期导致整体验收失败。此测试把验收脚本纳入测试保护圈。

以子进程方式运行,隔离脚本对环境变量(MEMORYGUARD_*)的修改。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _run_script(name: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYGUARD_")}
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=env,
    )


@pytest.mark.parametrize("script", ["accept_v3_1.py", "accept_v3_2.py"])
def test_acceptance_script_passes(script):
    result = _run_script(script)
    assert result.returncode == 0, (
        f"{script} 退出码 {result.returncode}\n"
        f"--- stdout(尾部) ---\n{result.stdout[-2000:]}\n"
        f"--- stderr(尾部) ---\n{result.stderr[-2000:]}"
    )
    assert "FAIL" not in result.stdout, f"{script} 输出含 FAIL:\n{result.stdout[-2000:]}"
