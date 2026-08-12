from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
NATIVE_MODULES = (
    "memoryguard.runtime_v2.extraction_native",
    "memoryguard.runtime_v2.safe_services",
    "memoryguard.runtime_v2.source_native",
    "memoryguard.runtime_v2.text_native",
)
LEGACY_MODULES = {"memoryguard.memory_ir", "memoryguard.source_registry"}


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("module_name", NATIVE_MODULES)
def test_native_modules_have_no_legacy_import_nodes(module_name: str) -> None:
    relative = Path("src") / Path(*module_name.split("."))
    source = (ROOT / relative).with_suffix(".py").read_text(encoding="utf-8")
    assert not {
        name
        for name in _imports(ast.parse(source, filename=str(relative)))
        if name == "memory_ir"
        or name == "source_registry"
        or name.endswith(".memory_ir")
        or name.endswith(".source_registry")
    }


@pytest.mark.parametrize("module_name", NATIVE_MODULES)
def test_importing_native_module_does_not_load_legacy_modules(module_name: str) -> None:
    script = (
        f"import sys\nimport {module_name}\n"
        "leaked = sorted(name for name in sys.modules if name in "
        f"{LEGACY_MODULES!r})\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
