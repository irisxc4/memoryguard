"""Release gates for retiring the V1 runtime from production entrypoints.

The gate is intentionally kept in tests instead of production.  It has two
independent views of the boundary:

* an AST walk follows the import closure, including imports nested in lazy
  functions, and reports source locations for every forbidden edge;
* a fresh subprocess imports each public entrypoint with a runtime import
  guard, catching imports that static analysis cannot resolve.

memoryguard.migration is the only namespace allowed to read legacy formats.
That exception is deliberately not applied to the public entrypoints or to
the rest of the production import closure.
"""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
import tokenize
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGE_ROOT = SRC / "memoryguard"

ENTRYPOINTS = (
    "memoryguard.cli",
    "memoryguard.mcp_server",
    "memoryguard.host_hooks",
    "memoryguard.gui",
    "memoryguard.provider_adapters",
    "memoryguard.cutover_v2",
    "memoryguard.runtime_v2",
)

FORBIDDEN_MODULES = frozenset(
    {
        "agent_binding",
        "shared_memory_store",
        "managed_store",
        "memory_ir",
        "source_registry",
        "projection",
        "compat_v2",
        "conversation_history",
    }
)
MIGRATION_NAMESPACE = "memoryguard.migration"

_IDENTIFIER_LIKE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class _Finding:
    """One release-gate offender with a stable, human-readable location."""

    path: Path
    line: int
    kind: str
    detail: str
    module: str = ""

    def render(self) -> str:
        return f"{_display_path(self.path)}:{self.line}: {self.kind}: {self.detail}"


@dataclass(frozen=True)
class _ImportReference:
    module: str
    node: ast.AST


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _line(node: ast.AST) -> int:
    return int(getattr(node, "lineno", 1))


def _is_migration_namespace(module: str) -> bool:
    return module == MIGRATION_NAMESPACE or module.startswith(MIGRATION_NAMESPACE + ".")


def _forbidden_family(module: str) -> str | None:
    prefix = "memoryguard."
    if not module.startswith(prefix):
        return None
    tail = module[len(prefix) :]
    for name in FORBIDDEN_MODULES:
        if tail == name or tail.startswith(name + "."):
            return prefix + name
    return None


def _module_path(module: str) -> Path | None:
    """Resolve an in-repository memoryguard module without importing it."""

    if module == "memoryguard":
        candidate = PACKAGE_ROOT / "__init__.py"
        return candidate if candidate.is_file() else None
    if not module.startswith("memoryguard."):
        return None
    parts = module.split(".")[1:]
    base = PACKAGE_ROOT.joinpath(*parts)
    file_candidate = base.with_suffix(".py")
    if file_candidate.is_file():
        return file_candidate
    package_candidate = base / "__init__.py"
    return package_candidate if package_candidate.is_file() else None


def _module_for_path(path: Path) -> str:
    relative = path.resolve().relative_to(SRC.resolve())
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = Path(parts[-1]).stem
    return ".".join(parts)


def _package_for_module(module: str, path: Path) -> str:
    if path.name == "__init__.py":
        return module
    return module.rsplit(".", 1)[0]


def _resolve_relative_name(
    current_module: str,
    current_path: Path,
    name: str | None,
    level: int,
) -> str | None:
    """Resolve an ImportFrom name using the importing file's package."""

    package_parts = _package_for_module(current_module, current_path).split(".")
    if level < 1 or level > len(package_parts):
        return None
    base = package_parts[: len(package_parts) - level + 1]
    if name:
        base.extend(name.split("."))
    return ".".join(base)


def _resolve_dynamic_name(
    current_module: str,
    current_path: Path,
    name: str,
    package: str | None = None,
) -> str | None:
    if not name.startswith("."):
        return name
    package_name = package or _package_for_module(current_module, current_path)
    dots = len(name) - len(name.lstrip("."))
    package_parts = package_name.split(".")
    if dots > len(package_parts):
        return None
    base = package_parts[: len(package_parts) - dots + 1]
    remainder = name[dots:]
    if remainder:
        base.extend(remainder.split("."))
    return ".".join(base)


def _static_import_references(
    tree: ast.AST,
    current_module: str,
    current_path: Path,
) -> Iterator[_ImportReference]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield _ImportReference(alias.name, node)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level:
            base = _resolve_relative_name(
                current_module,
                current_path,
                node.module,
                node.level,
            )
        else:
            base = node.module
        if not base:
            continue

        # The base is the module actually imported by "from x import y".
        # Alias candidates additionally cover "from memoryguard import
        # forbidden_module" and package submodules exposed that way.
        yield _ImportReference(base, node)
        for alias in node.names:
            if alias.name == "*":
                continue
            yield _ImportReference(f"{base}.{alias.name}", node)


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _target_names(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for item in node.elts:
            yield from _target_names(item)


def _dynamic_bindings(tree: ast.AST) -> tuple[set[str], set[str], set[str], set[str]]:
    """Collect aliases for importlib, import_module, __import__, and builtins."""

    importlib_names = {"importlib"}
    import_module_names: set[str] = set()
    import_names = {"__import__"}
    builtins_names = {"builtins"}

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "importlib":
                        bound = alias.asname or "importlib"
                        if bound not in importlib_names:
                            importlib_names.add(bound)
                            changed = True
                    elif alias.name == "builtins":
                        bound = alias.asname or "builtins"
                        if bound not in builtins_names:
                            builtins_names.add(bound)
                            changed = True
                    elif alias.name.startswith("importlib.") and not alias.asname:
                        # "import importlib.util" binds "importlib" too.
                        if "importlib" not in importlib_names:
                            importlib_names.add("importlib")
                            changed = True
            elif isinstance(node, ast.ImportFrom):
                if node.module == "importlib":
                    for alias in node.names:
                        if alias.name in {"import_module", "*"}:
                            bound = alias.asname or alias.name
                            if alias.name == "*":
                                bound = "import_module"
                            if bound not in import_module_names:
                                import_module_names.add(bound)
                                changed = True
                if node.module == "builtins":
                    for alias in node.names:
                        if alias.name in {"__import__", "*"}:
                            bound = alias.asname or alias.name
                            if alias.name == "*":
                                bound = "__import__"
                            if bound not in import_names:
                                import_names.add(bound)
                                changed = True
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                qualified = _qualified_name(value)
                getter_member = None
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "getattr"
                    and len(value.args) >= 2
                    and isinstance(value.args[1], ast.Constant)
                    and isinstance(value.args[1].value, str)
                ):
                    parent = _qualified_name(value.args[0])
                    if parent in importlib_names and value.args[1].value == "import_module":
                        getter_member = "import_module"
                    elif parent in builtins_names and value.args[1].value == "__import__":
                        getter_member = "__import__"
                if isinstance(node, ast.Assign):
                    targets = tuple(
                        name for target in node.targets for name in _target_names(target)
                    )
                else:
                    targets = tuple(_target_names(node.target))
                for target in targets:
                    if qualified in importlib_names and target not in importlib_names:
                        importlib_names.add(target)
                        changed = True
                    if qualified in import_module_names or (
                        qualified
                        and qualified.rsplit(".", 1)[-1] == "import_module"
                        and qualified.rsplit(".", 1)[0] in importlib_names
                    ) or getter_member == "import_module":
                        if target not in import_module_names:
                            import_module_names.add(target)
                            changed = True
                    if qualified in import_names or qualified == "__import__" or getter_member == "__import__":
                        if target not in import_names:
                            import_names.add(target)
                            changed = True
                    if qualified in builtins_names and target not in builtins_names:
                        builtins_names.add(target)
                        changed = True
    return importlib_names, import_module_names, import_names, builtins_names


def _dynamic_call_kind(
    call: ast.Call,
    importlib_names: set[str],
    import_module_names: set[str],
    import_names: set[str],
    builtins_names: set[str],
) -> str | None:
    function = call.func
    if isinstance(function, ast.Name):
        if function.id in import_module_names:
            return "dynamic importlib.import_module"
        if function.id in import_names:
            return "dynamic __import__"
    if isinstance(function, ast.Attribute):
        qualified_parent = _qualified_name(function.value)
        if function.attr == "import_module" and qualified_parent in importlib_names:
            return "dynamic importlib.import_module"
        if function.attr == "__import__" and qualified_parent in builtins_names:
            return "dynamic __import__"
    # Catch getattr(importlib, "import_module")(...), which hides the loader
    # behind an ordinary AST call expression.
    if isinstance(function, ast.Call) and isinstance(function.func, ast.Name):
        if function.func.id == "getattr" and len(function.args) >= 2:
            parent = _qualified_name(function.args[0])
            member = function.args[1]
            if isinstance(member, ast.Constant) and isinstance(member.value, str):
                if parent in importlib_names and member.value == "import_module":
                    return "dynamic importlib.import_module"
                if parent in builtins_names and member.value == "__import__":
                    return "dynamic __import__"
    return None


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dynamic_targets(
    call: ast.Call,
    kind: str,
    current_module: str,
    current_path: Path,
) -> tuple[str, ...]:
    if not call.args:
        return ()
    name = _literal_string(call.args[0])
    if name is None:
        return ()
    package = None
    if kind == "dynamic importlib.import_module":
        for keyword in call.keywords:
            if keyword.arg == "package":
                package = _literal_string(keyword.value)
                break
    resolved = _resolve_dynamic_name(current_module, current_path, name, package)
    if not resolved:
        return ()
    targets = [resolved]
    if kind == "dynamic __import__" and len(call.args) >= 4:
        fromlist = call.args[3]
        if isinstance(fromlist, (ast.Tuple, ast.List)):
            for item in fromlist.elts:
                member = _literal_string(item)
                if member and member != "*":
                    targets.append(f"{resolved}.{member}")
    return tuple(targets)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    excluded: set[int] = set()
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for owner in ast.walk(tree):
        if not isinstance(owner, owners) or not owner.body:
            continue
        first = owner.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                excluded.update({id(first), id(first.value)})
    return excluded


def _legacy_dispatch_findings(
    tree: ast.AST,
    path: Path,
    module: str,
) -> list[_Finding]:
    """Find executable legacy dispatch identifiers and identifier-like values."""

    excluded = _docstring_nodes(tree)
    findings: list[_Finding] = []

    def add(node: ast.AST, value: str, source: str) -> None:
        if id(node) in excluded:
            return
        if "legacy" not in value.casefold():
            return
        findings.append(
            _Finding(
                path,
                _line(node),
                "legacy-dispatch-name",
                f"{source} {value!r}",
                module,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            add(node, node.id, "identifier")
        elif isinstance(node, ast.arg):
            add(node, node.arg, "argument")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(node, node.name, "definition")
        elif isinstance(node, ast.Attribute):
            add(node, node.attr, "attribute")
        elif isinstance(node, ast.keyword) and node.arg:
            add(node, node.arg, "keyword")
        elif isinstance(node, ast.alias):
            add(node, node.name, "import name")
            if node.asname:
                add(node, node.asname, "import alias")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _IDENTIFIER_LIKE.fullmatch(node.value):
                add(node, node.value, "string")

    unique: dict[tuple[str, int, str, str], _Finding] = {}
    for finding in findings:
        key = (_display_path(finding.path), finding.line, finding.kind, finding.detail)
        unique[key] = finding
    return list(unique.values())


def _parse(path: Path) -> ast.AST:
    with tokenize.open(str(path)) as handle:
        return ast.parse(handle.read(), filename=str(path))


def _entrypoint_files() -> tuple[tuple[str, Path], ...]:
    result: list[tuple[str, Path]] = []
    for module in ENTRYPOINTS:
        path = _module_path(module)
        if path is None:
            continue
        if path.name == "__init__.py":
            for child in sorted(path.parent.rglob("*.py")):
                result.append((_module_for_path(child), child))
        else:
            result.append((module, path))
    return tuple(result)


def _ast_findings() -> list[_Finding]:
    findings: list[_Finding] = []
    pending: deque[tuple[str, Path]] = deque()
    seen: set[tuple[str, Path]] = set()

    for module in ENTRYPOINTS:
        path = _module_path(module)
        if path is None:
            findings.append(
                _Finding(
                    PACKAGE_ROOT / "__init__.py",
                    1,
                    "missing-entrypoint",
                    f"cannot resolve {module}",
                    module,
                )
            )
        else:
            pending.append((module, path))

    entrypoint_paths = {path.resolve() for _, path in _entrypoint_files()}

    while pending:
        module, path = pending.popleft()
        identity = (module, path.resolve())
        if identity in seen:
            continue
        seen.add(identity)
        try:
            tree = _parse(path)
        except (OSError, SyntaxError, UnicodeError) as exc:
            findings.append(
                _Finding(
                    path,
                    int(getattr(exc, "lineno", 1) or 1),
                    "parse-error",
                    str(exc),
                    module,
                )
            )
            continue

        migration = _is_migration_namespace(module)
        reported_imports: set[tuple[int, str]] = set()
        for reference in _static_import_references(tree, module, path):
            family = _forbidden_family(reference.module)
            if family and not migration:
                identity = (id(reference.node), family)
                if identity not in reported_imports:
                    reported_imports.add(identity)
                    findings.append(
                        _Finding(
                            path,
                            _line(reference.node),
                            "forbidden-import",
                            f"imports retired family {family} via {reference.module}",
                            module,
                        )
                    )
                # Do not descend into the retired implementation.  The
                # forbidden edge is the actionable closure boundary, and
                # scanning its internals would turn one offender into legacy
                # implementation noise.
                continue
            target_path = _module_path(reference.module)
            if target_path is not None and not _forbidden_family(reference.module):
                pending.append((reference.module, target_path))

        importlib_names, import_module_names, import_names, builtins_names = _dynamic_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kind = _dynamic_call_kind(
                node,
                importlib_names,
                import_module_names,
                import_names,
                builtins_names,
            )
            if kind is None:
                continue
            targets = _dynamic_targets(node, kind, module, path)
            target_text = ", ".join(targets) if targets else "non-literal target"
            if not migration:
                findings.append(
                    _Finding(
                        path,
                        _line(node),
                        "dynamic-import",
                        f"{kind} ({target_text})",
                        module,
                    )
                )
            for target in targets:
                target_path = _module_path(target)
                if target_path is not None and not _forbidden_family(target):
                    pending.append((target, target_path))

        if path.resolve() in entrypoint_paths:
            findings.extend(_legacy_dispatch_findings(tree, path, module))

    unique: dict[tuple[str, int, str, str], _Finding] = {}
    for finding in findings:
        key = (_display_path(finding.path), finding.line, finding.kind, finding.detail)
        unique[key] = finding
    return sorted(
        unique.values(),
        key=lambda item: (_display_path(item.path), item.line, item.kind, item.detail),
    )


def _subprocess_script() -> str:
    """Return a clean-process import probe with no project imports in parent."""

    return f'''
from __future__ import annotations

import builtins
import importlib
from pathlib import Path
import sys

SRC = Path({str(SRC)!r}).resolve()
ENTRYPOINTS = {ENTRYPOINTS!r}
FORBIDDEN = {tuple(sorted(FORBIDDEN_MODULES))!r}
MIGRATION = {MIGRATION_NAMESPACE!r}
violations = []
allowed_families = set()

def family(name):
    if not name.startswith("memoryguard."):
        return None
    tail = name[len("memoryguard."):]
    for item in FORBIDDEN:
        if tail == item or tail.startswith(item + "."):
            return "memoryguard." + item
    return None

def migration(name):
    return name == MIGRATION or name.startswith(MIGRATION + ".")

def source_location(globals_dict=None):
    source_module = (globals_dict or {{}}).get("__name__", "")
    frame = sys._getframe(2)
    while frame is not None:
        try:
            path = Path(frame.f_code.co_filename).resolve()
            path.relative_to(SRC)
        except (OSError, RuntimeError, ValueError):
            frame = frame.f_back
            continue
        if not source_module.startswith("memoryguard"):
            source_module = frame.f_globals.get("__name__", source_module)
        try:
            display = path.relative_to(SRC.parent).as_posix()
        except ValueError:
            display = path.as_posix()
        return source_module, display, frame.f_lineno
    return source_module or "<unknown>", "<unknown>", 0

def allowed_source(name):
    if migration(name):
        return True
    return any(name == item or name.startswith(item + ".") for item in allowed_families)

def check_targets(targets, globals_dict=None):
    source_module, display, line = source_location(globals_dict)
    for target in targets:
        item = family(target)
        if item is None:
            continue
        if allowed_source(source_module):
            allowed_families.add(item)
            continue
        record = f"{{display}}:{{line}}: runtime-forbidden-import: {{target}} (from {{source_module}})"
        if record not in violations:
            violations.append(record)
        raise ImportError(record)

def resolve(name, package=None, level=0, fromlist=()):
    if level:
        package_name = package or ""
        parts = package_name.split(".")
        if not package_name or level > len(parts):
            return ()
        base = parts[:len(parts) - level + 1]
        if name:
            base.extend(name.split("."))
        target = ".".join(base)
    else:
        target = name
    targets = [target]
    if target == "memoryguard" or target.startswith("memoryguard."):
        for item in fromlist or ():
            if item and item != "*":
                targets.append(target + "." + item)
    return tuple(targets)

real_import = builtins.__import__
def guarded_import(name, globals_dict=None, locals_dict=None, fromlist=(), level=0):
    package = (globals_dict or {{}}).get("__package__")
    check_targets(resolve(name, package, level, fromlist), globals_dict)
    return real_import(name, globals_dict, locals_dict, fromlist, level)

real_import_module = importlib.import_module
def guarded_import_module(name, package=None):
    resolved = name
    if isinstance(name, str) and name.startswith("."):
        resolved = importlib.util.resolve_name(name, package or "")
    check_targets((resolved,), None)
    return real_import_module(name, package)

builtins.__import__ = guarded_import
importlib.import_module = guarded_import_module
sys.path.insert(0, str(SRC))

for entrypoint in ENTRYPOINTS:
    try:
        real_import_module(entrypoint)
    except BaseException as exc:
        if not any(str(exc) in violation for violation in violations):
            print(f"entrypoint-error: {{entrypoint}}: {{type(exc).__name__}}: {{exc}}")

for loaded in sorted(sys.modules):
    item = family(loaded)
    if item is not None and item not in allowed_families:
        record = f"<runtime>:0: runtime-loaded-forbidden: {{loaded}}"
        if record not in violations:
            violations.append(record)

for violation in violations:
    print(violation)
sys.exit(1 if violations else 0)
'''


def _run_subprocess_import_probe() -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-I", "-c", _subprocess_script()],
        cwd=str(ROOT),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )


def test_v2_runtime_retirement_ast_gate() -> None:
    """The complete production import closure must be V1-store free."""

    findings = _ast_findings()
    assert not findings, "V1 runtime retirement AST gate offenders:\n" + "\n".join(
        finding.render() for finding in findings
    )


def test_v2_runtime_retirement_subprocess_import_closure() -> None:
    """A clean interpreter must not load a forbidden V1 module at import time."""

    result = _run_subprocess_import_probe()
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    assert result.returncode == 0, "V1 runtime retirement subprocess offenders:\n" + output


def test_ast_legacy_scan_ignores_comments_and_docstrings() -> None:
    """The executable-name rule must not be a raw-text grep in disguise."""

    tree = ast.parse(
        '''"""Legacy documentation is not a dispatch name."""\n'''
        "# Legacy comment is not executable.\n"
        "def safe_route():\n"
        "    \"\"\"Legacy function documentation.\"\"\"\n"
        "    return \"legacy-route\"\n"
    )
    findings = _legacy_dispatch_findings(tree, Path("fixture.py"), "fixture")
    assert [(finding.line, finding.detail) for finding in findings] == [
        (5, "string 'legacy-route'")
    ]
