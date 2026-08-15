from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from memoryguard import toml_compat as tomllib

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repair_codex_memoryguard_transport.py"
SPEC = importlib.util.spec_from_file_location("repair_codex_memoryguard_transport", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
repair = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repair
SPEC.loader.exec_module(repair)


def original_config() -> str:
    return '''model = "gpt-test"

[mcp_servers.other]
command = "other"
args = []

[mcp_servers.memoryguard]
command = "python"
args = ["-m", "memoryguard.mcp_server"]
startup_timeout_sec = 5
tool_timeout_sec = 60

[mcp_servers.memoryguard.env]
MEMORYGUARD_WORKSPACE = "C:\\\\Users\\\\tester\\\\MemoryGuard"
MEMORYGUARD_AGENT_ID = "agent-a"
MEMORYGUARD_SHARE_GROUP_ID = "shared-a"
'''


def test_repair_config_preserves_binding_and_canonicalizes_transport(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(original_config(), encoding="utf-8")

    server, changed, backup = repair.repair_config(path)

    assert changed is True
    assert backup is not None and backup.is_file()
    assert server["args"] == ["-X", "utf8", "-m", "memoryguard.mcp_server"]
    assert server["enabled"] is True
    assert server["env"]["MEMORYGUARD_AGENT_ID"] == "agent-a"
    assert server["env"]["PYTHONUTF8"] == "1"
    assert server["env"]["PYTHONIOENCODING"] == "utf-8"
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    assert parsed["mcp_servers"]["memoryguard"]["command"] == repair.sys.executable


def test_repair_recovers_duplicate_invalid_memoryguard_sections(tmp_path: Path):
    path = tmp_path / "config.toml"
    duplicate = original_config() + '''
[mcp_servers.memoryguard]
command = "old-python"
args = ["-m", "old.server"]

[mcp_servers.memoryguard.env]
MEMORYGUARD_AGENT_ID = "agent-new"
MEMORYGUARD_SHARE_GROUP_ID = "shared-new"
'''
    path.write_text(duplicate, encoding="utf-8")

    server, changed, _ = repair.repair_config(path)

    assert changed is True
    assert server["enabled"] is True
    assert server["env"]["MEMORYGUARD_AGENT_ID"] == "agent-new"
    assert server["env"]["MEMORYGUARD_SHARE_GROUP_ID"] == "shared-new"
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert list(parsed["mcp_servers"]).count("memoryguard") == 1


def test_strip_removes_memoryguard_subtables_only():
    stripped = repair._strip_memoryguard_sections(original_config())
    assert "[mcp_servers.memoryguard]" not in stripped
    assert "[mcp_servers.memoryguard.env]" not in stripped
    assert "[mcp_servers.other]" in stripped
    assert 'model = "gpt-test"' in stripped


def test_backup_retention_keeps_only_latest_three(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(original_config(), encoding="utf-8")
    for index in range(5):
        backup = tmp_path / f"config.toml.memoryguard-transport-20260815T00000{index}.bak"
        backup.write_text(str(index), encoding="utf-8")
        backup.touch()
    repair._prune_backups(path)
    assert len(list(tmp_path.glob("config.toml.memoryguard-transport-*.bak"))) == 3


def test_touch_rewrites_even_when_canonical(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(original_config(), encoding="utf-8")
    repair.repair_config(path)
    server, changed, backup = repair.repair_config(path, touch=True)
    assert changed is False
    assert backup is not None and backup.is_file()
    assert server["env"]["PYTHONUTF8"] == "1"
