"""CLI doctor 在 Windows 中文控制台上的编码回归测试。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.cli import cmd_doctor


def test_doctor_output_is_gbk_encodable(tmp_path, monkeypatch):
    """doctor 不能因 ✓/✗ 等 GBK 不支持字符而在输出阶段崩溃。"""
    captured: list[str] = []

    def gbk_print(value=""):
        text = str(value)
        text.encode("gbk")
        captured.append(text)

    monkeypatch.setattr("builtins.print", gbk_print)

    result = cmd_doctor(argparse.Namespace(workspace=str(tmp_path)))

    assert result in (0, 1)
    assert captured
