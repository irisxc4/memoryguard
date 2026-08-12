from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
COMPAT_PACKAGE = SRC / "memoryguard" / "compat_v2"


def _retired_import_references(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    references: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "memoryguard.compat_v2"
                or alias.name.startswith("memoryguard.compat_v2.")
                for alias in node.names
            ):
                references.append(f"{path}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "memoryguard.compat_v2" or module.startswith("memoryguard.compat_v2."):
                references.append(f"{path}:{node.lineno}")
            elif node.level and module == "compat_v2":
                references.append(f"{path}:{node.lineno}")
            elif module == "memoryguard" and any(
                alias.name == "compat_v2" for alias in node.names
            ):
                references.append(f"{path}:{node.lineno}")
            elif node.level and not module and any(
                alias.name == "compat_v2" for alias in node.names
            ):
                references.append(f"{path}:{node.lineno}")
        elif isinstance(node, ast.Call):
            is_import_call = (
                isinstance(node.func, ast.Name) and node.func.id == "__import__"
            ) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            )
            if not is_import_call or not node.args:
                continue
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                if target.value == "memoryguard.compat_v2" or target.value.startswith(
                    "memoryguard.compat_v2."
                ):
                    references.append(f"{path}:{node.lineno}")

    return references


def test_compat_v2_is_absent_and_unimportable() -> None:
    assert not COMPAT_PACKAGE.exists()

    script = """
import importlib
import importlib.util

try:
    spec = importlib.util.find_spec("memoryguard.compat_v2")
except ModuleNotFoundError:
    spec = None
assert spec is None, spec

try:
    importlib.import_module("memoryguard.compat_v2")
except ModuleNotFoundError:
    pass
else:
    raise AssertionError("retired memoryguard.compat_v2 is still importable")
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(SRC), os.environ.get("PYTHONPATH", "")))}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_production_modules_have_no_retired_compat_imports() -> None:
    production_root = SRC / "memoryguard"
    references = [
        reference
        for path in sorted(production_root.rglob("*.py"))
        for reference in _retired_import_references(path)
    ]
    assert references == []
