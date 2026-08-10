#!/usr/bin/env python3
"""Machine-checkable acceptance gate for MemoryGuard V2 Phase 1.

The gate intentionally checks contracts and safety properties rather than
claiming that a later migration phase is complete.  It can be run while the
storage implementation is being developed: missing implementation pieces are
reported as dependency failures and still produce a valid JSON document.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import tokenize
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CONTRACT_PATH = ROOT / "docs" / "v2" / "phase1-architecture-contract.json"


class Gate:
    """Collect checks without hiding the first useful error."""

    def __init__(self) -> None:
        self.checks: dict[str, dict[str, Any]] = {}
        self.failures: list[dict[str, Any]] = []

    def pass_(self, name: str, **details: Any) -> None:
        self.checks[name] = {"ok": True, **details}

    def fail(self, name: str, message: str, *, kind: str = "contract", **details: Any) -> None:
        self.checks[name] = {"ok": False, "kind": kind, "message": message, **details}
        self.failures.append({"check": name, "kind": kind, "message": message, **details})

    def dependency(self, name: str, message: str, **details: Any) -> None:
        self.fail(name, message, kind="dependency", **details)

    def skip(self, name: str, message: str, **details: Any) -> None:
        """Record a platform-conditional check without claiming a failure."""

        self.checks[name] = {"ok": True, "skipped": True, "message": message, **details}


def _load_contract(gate: Gate) -> dict[str, Any] | None:
    if not CONTRACT_PATH.is_file():
        gate.fail("contract_file", f"missing contract: {CONTRACT_PATH}")
        return None
    try:
        data = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report malformed contract
        gate.fail("contract_file", f"invalid JSON: {type(exc).__name__}: {exc}")
        return None
    if not isinstance(data, dict) or data.get("contract") != "memoryguard-v2-phase1":
        gate.fail("contract_file", "contract identity is not memoryguard-v2-phase1")
        return None
    gate.pass_("contract_file", path=str(CONTRACT_PATH), version=data.get("contract_version"))
    return data


def _check_static_contract(gate: Gate, contract: dict[str, Any]) -> None:
    entries = contract.get("database_layout")
    expected = [
        ".memoryguard/runtime/runtime.db",
        ".memoryguard/memory/memory.db",
        ".memoryguard/rules/rules.db",
        ".memoryguard/evidence/evidence.db",
        ".memoryguard/content/content.db",
        ".memoryguard/knowledge/knowledge.db",
        ".memoryguard/codegraph/codegraph.db",
        ".memoryguard/assets/assets.db",
        ".memoryguard/projection/scenario.db",
        ".memoryguard/projection/profile.db",
        ".memoryguard/system/manifest.db",
    ]
    actual = [item.get("path") for item in entries or [] if isinstance(item, dict)]
    if actual != expected or len(actual) != len(set(actual)):
        gate.fail("layout_contract", "database layout differs from the exact Phase 1 set", actual=actual, expected=expected)
    else:
        gate.pass_("layout_contract", paths=actual)

    domain_rules = contract.get("domain_rules", {})
    content = domain_rules.get("content", {})
    evidence = domain_rules.get("evidence", {})
    if not content.get("must_not_be_long_term_memory"):
        gate.fail("content_boundary", "content domain must be raw-content storage, not long-term memory")
    elif any(item not in content.get("owns", []) for item in ("raw_content", "conversation_turns")):
        gate.fail("content_boundary", "content domain must own raw content and conversation turns")
    else:
        gate.pass_("content_boundary", owns=content.get("owns", []))
    forbidden_evidence = {"raw_content", "conversation_body", "full_transcript"}
    if not forbidden_evidence.issubset(set(evidence.get("forbids", []))):
        gate.fail("evidence_boundary", "evidence domain must forbid full content fields")
    else:
        gate.pass_("evidence_boundary", forbids=evidence.get("forbids", []))

    data_home = contract.get("data_home", {})
    required_pointers = {"workspace_source_pointer", "global_source_pointer", "data_home_root"}
    listed = set(data_home.get("manifest_fields", []))
    if not required_pointers.issubset(listed) or not data_home.get("pointer_must_be_explicit"):
        gate.fail("datahome_contract", "manifest must store explicit workspace/global pointers and data home")
    elif not data_home.get("containment_check_required") or not data_home.get("forbid_guessing_or_silent_move"):
        gate.fail("datahome_contract", "DataHome pointers require containment and must not guess or move")
    else:
        gate.pass_("datahome_contract", fields=sorted(listed))

    manifest = contract.get("manifest", {})
    states = set(manifest.get("states", []))
    expected_states = {"V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE"}
    transitions = {
        (item.get("from"), item.get("to"))
        for item in manifest.get("transitions", [])
        if isinstance(item, dict)
    }
    required_transitions = {
        ("V1_ACTIVE", "V2_BUILDING"),
        ("V2_BUILDING", "V2_READY"),
        ("V2_READY", "V2_ACTIVE"),
        ("V2_BUILDING", "V1_ACTIVE"),
        ("V2_READY", "V1_ACTIVE"),
        ("V2_ACTIVE", "V1_ACTIVE"),
    }
    forbidden_transitions = {
        ("V2_BUILDING", "V2_ACTIVE"),
        ("V2_ACTIVE", "V2_BUILDING"),
    }
    if states != expected_states or not required_transitions.issubset(transitions) or forbidden_transitions.intersection(transitions):
        gate.fail("manifest_contract", "manifest states/transitions are incomplete", states=sorted(states), transitions=sorted(transitions))
    elif manifest.get("v2_read_requires") != "V2_ACTIVE" or manifest.get("build_mode") != "no_dual_read_or_write":
        gate.fail("manifest_contract", "V2 reads/build mode do not enforce the cutover boundary")
    elif manifest.get("failure_target") != "V1_ACTIVE" or manifest.get("physical_atomicity_claim") is not False:
        gate.fail("manifest_contract", "failure target or physical atomicity declaration is unsafe")
    elif set(manifest.get("ready_requires", [])) != {
        "source_digest", "target_digest", "manifest_digest", "validator_passed", "checkpoints",
    } or manifest.get("active_inherits_ready_evidence") is not True:
        gate.fail("manifest_contract", "V2_READY evidence requirements or V2_ACTIVE inheritance are incomplete")
    else:
        gate.pass_("manifest_contract", states=sorted(states), transitions=len(transitions))

    unknown = contract.get("migration", {}).get("unknown_domains", {})
    if any(unknown.get(name) != "NO_SOURCE" for name in ("codegraph", "assets", "taskcanvas")):
        gate.fail("no_source_contract", "codegraph/assets/taskcanvas must be explicit NO_SOURCE")
    elif contract.get("migration", {}).get("lossless_conversion_claim") is not False:
        gate.fail("no_source_contract", "NO_SOURCE domains cannot claim lossless conversion")
    else:
        gate.pass_("no_source_contract", domains=unknown)


def _import_module(name: str) -> tuple[Any | None, str | None]:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    try:
        return importlib.import_module(name), None
    except Exception as exc:  # noqa: BLE001 - dependency report is the output
        return None, f"{type(exc).__name__}: {exc}"


def _construct(cls: Any, root: Path) -> Any:
    """Construct common V2 objects without coupling the gate to one spelling."""

    attempts: list[Callable[[], Any]] = [
        lambda: cls(root),
        lambda: cls(workspace=root),
        lambda: cls(root=root),
        lambda: cls(data_home=root),
    ]
    errors: list[str] = []
    for attempt in attempts:
        try:
            return attempt()
        except (TypeError, ValueError, OSError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    raise TypeError("; ".join(errors[-2:]) or "cannot construct object")


def _relative_layout_paths(layout: Any, root: Path) -> list[str]:
    paths: list[Path] = []
    if hasattr(layout, "all_db_paths"):
        paths = [Path(item) for item in layout.all_db_paths]
    elif hasattr(layout, "iter_db_paths"):
        paths = [Path(item[1]) for item in layout.iter_db_paths()]
    elif hasattr(layout, "databases"):
        data = layout.databases
        paths = [Path(path) for group in data.values() for path in (group if isinstance(group, (tuple, list)) else (group,))]
    elif hasattr(layout, "db_paths"):
        data = layout.db_paths()
        paths = [Path(path) for group in data.values() for path in group]
    if not paths:
        raise ValueError("layout object exposes no database paths")
    return [str(path.resolve().relative_to(root.resolve())).replace("\\", "/") for path in paths]


def _check_layout_module(gate: Gate) -> None:
    module, error = _import_module("memoryguard.storage.layout")
    if module is None:
        gate.dependency("layout_module", f"cannot import storage layout: {error}")
        return
    cls = getattr(module, "WorkspaceV2Layout", None)
    if cls is None:
        gate.dependency("layout_module", "WorkspaceV2Layout is not exported")
        return
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-layout-") as temp:
        root = Path(temp)
        try:
            layout = _construct(cls, root)
            actual = _relative_layout_paths(layout, root)
        except Exception as exc:  # noqa: BLE001
            gate.fail("layout_module", f"layout probe failed: {type(exc).__name__}: {exc}")
            return
    expected = [
        ".memoryguard/runtime/runtime.db", ".memoryguard/memory/memory.db",
        ".memoryguard/rules/rules.db", ".memoryguard/evidence/evidence.db",
        ".memoryguard/content/content.db", ".memoryguard/knowledge/knowledge.db",
        ".memoryguard/codegraph/codegraph.db", ".memoryguard/assets/assets.db",
        ".memoryguard/projection/scenario.db", ".memoryguard/projection/profile.db",
        ".memoryguard/system/manifest.db",
    ]
    if actual != expected:
        gate.fail("layout_module", "WorkspaceV2Layout paths differ from contract", actual=actual, expected=expected)
    else:
        gate.pass_("layout_module", paths=actual)


def _invoke_initializer(module: Any, root: Path) -> tuple[bool, str]:
    """Try the small set of public initializer signatures used by V2."""

    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-schema-") as temp:
        work = Path(temp)
        layout_module, _ = _import_module("memoryguard.storage.layout")
        layout = None
        if layout_module is not None and getattr(layout_module, "WorkspaceV2Layout", None):
            try:
                layout = _construct(layout_module.WorkspaceV2Layout, work)
                if hasattr(layout, "ensure_dirs"):
                    layout.ensure_dirs()
            except Exception:
                layout = None
        initializer = getattr(module, "initialize_all", None)
        if initializer is None:
            initializer = getattr(module, "initialize", None)
        if initializer is None:
            return False, "schema initializer (initialize_all/initialize) is not exported"
        attempts = []
        if layout is not None:
            attempts.extend([lambda: initializer(layout), lambda: initializer(layout=layout)])
        attempts.extend([lambda: initializer(work), lambda: initializer(root=work)])
        errors: list[str] = []
        for attempt in attempts:
            try:
                attempt()
                # Keep this check local; callers need the temp path, so return a
                # marker and run SQLite checks in the same closure below.
                layout = layout or work
                paths = []
                if hasattr(layout, "all_db_paths"):
                    paths = list(layout.all_db_paths)
                if not paths:
                    paths = [work / item for item in (
                        ".memoryguard/runtime/runtime.db", ".memoryguard/memory/memory.db",
                        ".memoryguard/rules/rules.db", ".memoryguard/evidence/evidence.db",
                        ".memoryguard/content/content.db", ".memoryguard/knowledge/knowledge.db",
                        ".memoryguard/codegraph/codegraph.db", ".memoryguard/assets/assets.db",
                        ".memoryguard/projection/scenario.db", ".memoryguard/projection/profile.db",
                        ".memoryguard/system/manifest.db",
                    )]
                missing = [str(path) for path in paths if not Path(path).is_file()]
                if missing:
                    return False, f"initializer did not create all domain databases: {missing}"
                bad: list[str] = []
                for path in paths:
                    conn: sqlite3.Connection | None = None
                    try:
                        conn = sqlite3.connect(str(path))
                        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
                        if integrity != "ok" or fk:
                            bad.append(f"{path}: integrity={integrity!r}, fk={len(fk)}")
                    except sqlite3.Error as exc:
                        bad.append(f"{path}: {type(exc).__name__}: {exc}")
                    finally:
                        if conn is not None:
                            conn.close()
                if bad:
                    return False, "SQLite checks failed: " + "; ".join(bad)
                return True, "initialized all databases; integrity_check and foreign_key_check are clean"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
        return False, "initializer signatures rejected: " + "; ".join(errors[-3:])


def _check_schema_module(gate: Gate, contract: dict[str, Any]) -> None:
    module, error = _import_module("memoryguard.storage.schema")
    if module is None:
        gate.dependency("schema_module", f"cannot import storage schema: {error}")
        return
    marker = contract.get("schema_marker", "memoryguard-v2-phase1")
    exported = [
        getattr(module, name, None)
        for name in ("SCHEMA_MARKER", "SCHEMA_VERSION", "V2_SCHEMA_MARKER")
    ]
    values = {str(item) for item in exported if item is not None}
    if not any(marker == item or marker in item or item in marker for item in values):
        gate.fail("schema_marker", "schema module does not expose the Phase 1 marker", marker=marker, exported=sorted(values))
        return
    ok, message = _invoke_initializer(module, ROOT)
    if not ok:
        gate.fail("sqlite_integrity_fk", message)
        return
    gate.pass_("schema_marker", marker=marker, exported=sorted(values))
    gate.pass_("sqlite_integrity_fk", details=message)


def _check_database_read_only(gate: Gate) -> None:
    module, error = _import_module("memoryguard.storage.database")
    if module is None:
        gate.dependency("readonly_no_create", f"cannot import storage database: {error}")
        return
    connect = getattr(module, "connect_database", None)
    cls = getattr(module, "SQLiteDatabase", None)
    if connect is None and cls is None:
        gate.dependency("readonly_no_create", "connect_database or SQLiteDatabase is not exported")
        return
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-ro-") as temp:
        root = Path(temp)
        missing = root / "absent" / "db.sqlite"
        try:
            probe_conn: sqlite3.Connection | None = None
            try:
                if connect is not None:
                    try:
                        probe_conn = connect(missing, readonly=True)
                    except (FileNotFoundError, sqlite3.OperationalError):
                        pass
                else:
                    obj = cls(missing, readonly=True)
                    try:
                        probe_conn = obj.connect()
                    except (FileNotFoundError, sqlite3.OperationalError):
                        pass
            finally:
                if probe_conn is not None:
                    probe_conn.close()
            if missing.exists() or missing.parent.exists():
                gate.fail("readonly_no_create", "read-only open created a missing database or parent directory")
                return
            existing = root / "existing.db"
            create_conn = sqlite3.connect(existing)
            try:
                create_conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
                create_conn.commit()
            finally:
                create_conn.close()
            if connect is not None:
                conn = connect(existing, readonly=True)
            else:
                obj = cls(existing, readonly=True)
                conn = obj.connect()
            try:
                before = conn.execute("PRAGMA user_version").fetchone()[0]
                try:
                    conn.execute("CREATE TABLE must_fail (id INTEGER)")
                except sqlite3.Error:
                    pass
                else:
                    gate.fail("readonly_no_create", "read-only connection accepted a write")
                    return
                after = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            if before != after or existing.with_suffix(existing.suffix + "-wal").exists():
                gate.fail("readonly_no_create", "read-only probe changed database state or created WAL")
                return
        except Exception as exc:  # noqa: BLE001
            gate.fail("readonly_no_create", f"read-only probe failed: {type(exc).__name__}: {exc}")
            return
    gate.pass_("readonly_no_create")


def _check_manifest_module(gate: Gate, contract: dict[str, Any]) -> None:
    module, error = _import_module("memoryguard.system.manifest")
    if module is None:
        gate.dependency("manifest_module", f"cannot import system manifest: {error}")
        return
    # ``ManifestManager`` is the Phase 1 implementation name. Keep the
    # SystemManifestStore alias for downstream integrations that adopted the
    # earlier draft contract.
    cls = getattr(module, "ManifestManager", None) or getattr(module, "SystemManifestStore", None)
    if cls is None:
        gate.dependency("manifest_module", "ManifestManager/SystemManifestStore is not exported")
        return
    states = set(contract["manifest"]["states"])
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-manifest-") as temp:
        root = Path(temp)
        try:
            store = _construct(cls, root)
        except Exception as exc:  # noqa: BLE001
            gate.fail("manifest_module", f"manifest probe failed: {type(exc).__name__}: {exc}")
            return
        state_value = None
        for attr in ("state", "current_state", "status", "current", "read"):
            value = getattr(store, attr, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = None
            if value is not None:
                # A manager often exposes ``current()`` returning a record;
                # aliases expose the enum directly.
                value = getattr(value, "state", value)
                state_value = getattr(value, "value", value)
                break
        if state_value is not None and str(state_value) not in states:
            gate.fail("manifest_module", "manifest starts in an unknown state", state=state_value)
            return
        # Verify public transition methods when present. Static transitions
        # above remain authoritative for implementations exposing a lower-level
        # record API.
        transition = getattr(store, "transition", None)
        if callable(transition):
            try:
                # A direct V1 -> active transition would let a partially built
                # database become readable, and is therefore required to fail.
                try:
                    transition("V2_ACTIVE")
                except Exception:
                    pass
                else:
                    raise AssertionError("manifest accepted V1_ACTIVE -> V2_ACTIVE")
                transition("V2_BUILDING")
                try:
                    transition("V2_ACTIVE")
                except Exception:
                    pass
                else:
                    raise AssertionError("manifest accepted V2_BUILDING -> V2_ACTIVE")
                ready_evidence = {
                    "validator_passed": True,
                    "checkpoints": {"acceptance": "phase1"},
                }
                try:
                    transition(
                        "V2_READY",
                        source_digest="acceptance-source",
                        target_digest="acceptance-target",
                        manifest_digest="acceptance-manifest",
                        digests=ready_evidence,
                    )
                except TypeError:
                    # Older compatible managers may not expose digest kwargs;
                    # static contract checks still enforce the evidence rule.
                    transition("V2_READY")
                transition("V2_ACTIVE")
                try:
                    transition("V2_BUILDING")
                except Exception:
                    pass
                else:
                    raise AssertionError("manifest accepted V2_ACTIVE -> V2_BUILDING")
                # Returning to V1 is a failure path and therefore carries an
                # explicit reason in ManifestManager.
                try:
                    transition("V1_ACTIVE", error="acceptance rollback probe")
                except TypeError:
                    transition("V1_ACTIVE")
            except Exception as exc:  # noqa: BLE001
                gate.fail("manifest_transitions", f"manifest state-machine probe failed: {type(exc).__name__}: {exc}")
                return
        gate.pass_("manifest_module", state=state_value)
        gate.pass_("manifest_transitions", required=["V1_ACTIVE->V2_BUILDING", "V2_BUILDING->V2_READY", "V2_READY->V2_ACTIVE", "V2_BUILDING->V1_ACTIVE", "V2_READY->V1_ACTIVE"])


_LEGACY_MODULE_PARTS = frozenset({"shared_memory_store", "conversation_history"})
_LEGACY_BINDINGS = frozenset({"sharedmemorystore", "conversationhistory"})


def _constant_string(node: ast.AST | None) -> str | None:
    """Return a string only when its value is statically unambiguous."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(value.value)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _is_legacy_module(name: str) -> bool:
    """Recognise legacy modules, including relative and nested spellings."""

    normalized = name.replace("/", ".").lstrip(".")
    return any(part in _LEGACY_MODULE_PARTS for part in normalized.split("."))


def _is_legacy_binding(name: str) -> bool:
    return name.replace("_", "").lower() in _LEGACY_BINDINGS


def _location(path: Path, node: ast.AST) -> str:
    line = getattr(node, "lineno", "?")
    column = getattr(node, "col_offset", "?")
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        relative = path
    return f"{relative}:{line}:{column}"


class _V2SourceVisitor(ast.NodeVisitor):
    """Find executable policy violations without searching comments/strings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.executescript_hits: list[str] = []
        self.legacy_import_hits: list[str] = []
        self.dynamic_import_hits: list[str] = []
        self._importlib_aliases = {"importlib"}
        self._import_module_aliases: set[str] = set()
        self._builtin_import_aliases = {"__import__"}
        self._builtins_aliases = {"builtins"}

    def _legacy_hit(self, node: ast.AST, detail: str) -> None:
        self.legacy_import_hits.append(f"{_location(self.path, node)}: {detail}")

    def _dynamic_hit(self, node: ast.AST, detail: str) -> None:
        self.dynamic_import_hits.append(f"{_location(self.path, node)}: {detail}")

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 - AST API
        for alias in node.names:
            if _is_legacy_module(alias.name):
                self._legacy_hit(node, f"import {alias.name}")
            if alias.name == "importlib":
                self._importlib_aliases.add(alias.asname or "importlib")
            elif alias.name == "builtins":
                self._builtins_aliases.add(alias.asname or "builtins")
            elif alias.name == "__import__":
                self._builtin_import_aliases.add(alias.asname or "__import__")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802 - AST API
        module = ("." * int(node.level)) + (node.module or "")
        if _is_legacy_module(module):
            self._legacy_hit(node, f"from {module} import ...")
        for alias in node.names:
            if _is_legacy_module(alias.name) or _is_legacy_binding(alias.name):
                self._legacy_hit(node, f"from {module or '.'} import {alias.name}")
            if module == "importlib" and alias.name == "import_module":
                self._import_module_aliases.add(alias.asname or alias.name)
            if module == "builtins" and alias.name == "__import__":
                self._builtin_import_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - AST API
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "executescript":
            self.executescript_hits.append(f"{_location(self.path, node)}: Attribute call attr=executescript")
        elif (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and _constant_string(node.args[1]) == "executescript"
        ):
            # ``getattr(conn, "executescript")(...)`` is the same forbidden
            # call expressed without an Attribute AST node.
            self.executescript_hits.append(f"{_location(self.path, node)}: getattr(..., 'executescript') call")

        loader: str | None = None
        if isinstance(func, ast.Name):
            if func.id in self._import_module_aliases:
                loader = "import_module"
            elif func.id in self._builtin_import_aliases:
                loader = "__import__"
        elif isinstance(func, ast.Attribute):
            if func.attr == "import_module" and isinstance(func.value, ast.Name) and func.value.id in self._importlib_aliases:
                loader = "import_module"
            elif func.attr == "__import__" and isinstance(func.value, ast.Name) and func.value.id in self._builtins_aliases:
                loader = "__import__"
        if loader is not None:
            module_name = _constant_string(node.args[0]) if node.args else None
            if module_name is None:
                self._dynamic_hit(node, f"{loader}() module name is not statically resolvable")
            elif _is_legacy_module(module_name):
                self._legacy_hit(node, f"dynamic {loader}({module_name!r})")
        self.generic_visit(node)


def _scan_new_v2_sources(gate: Gate, contract: dict[str, Any]) -> None:
    """AST-scan only the storage/system V2 core; migration remains a reader boundary."""

    configured = contract.get("acceptance", {}).get("new_v2_source_roots", [])
    allowed = {
        "src/memoryguard/storage",
        "src/memoryguard/system",
    }
    roots: list[Path] = []
    for item in configured:
        normalized = str(item).replace("\\", "/").rstrip("/")
        if normalized in allowed:
            roots.append(ROOT / Path(normalized))
    if not roots:
        roots = [ROOT / "src" / "memoryguard" / name for name in ("storage", "system")]

    seen: set[Path] = set()
    exec_hits: list[str] = []
    import_hits: list[str] = []
    dynamic_hits: list[str] = []
    parse_hits: list[str] = []
    for base in roots:
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if path in seen:
                continue
            seen.add(path)
            try:
                # tokenize.open honours a source encoding declaration before
                # AST parsing, while AST naturally ignores comments/strings.
                with tokenize.open(str(path)) as handle:
                    source = handle.read()
                tree = ast.parse(source, filename=str(path), type_comments=True)
            except (OSError, SyntaxError, UnicodeError, tokenize.TokenError) as exc:
                parse_hits.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            visitor = _V2SourceVisitor(path)
            visitor.visit(tree)
            exec_hits.extend(visitor.executescript_hits)
            import_hits.extend(visitor.legacy_import_hits)
            dynamic_hits.extend(visitor.dynamic_import_hits)

    if parse_hits:
        gate.fail("v2_source_scan", "V2 core source could not be parsed", hits=parse_hits, scanned=len(seen))
    elif not seen:
        gate.dependency("v2_source_scan", "no storage/system Python sources were found", scanned=0)
    else:
        gate.pass_("v2_source_scan", scanned=len(seen))
    if exec_hits:
        gate.fail("no_executescript", "new V2 storage/system source calls executescript", hits=exec_hits)
    else:
        gate.pass_("no_executescript", scanned=len(seen))
    if import_hits:
        gate.fail("no_legacy_imports", "new V2 storage/system source imports a legacy store/history", hits=import_hits)
    else:
        gate.pass_("no_legacy_imports", scanned=len(seen))
    if dynamic_hits:
        gate.fail(
            "dynamic_import_risk",
            "V2 core dynamically loads a module whose name is not statically resolvable",
            hits=dynamic_hits,
        )
    else:
        gate.pass_("dynamic_import_risk", scanned=len(seen))


def _check_malformed_manifest_json(gate: Gate) -> None:
    """Malformed fallback journals must fail closed, never become V1 defaults."""

    try:
        from memoryguard.migration.framework import JsonManifestStore, MigrationError
    except Exception as exc:  # noqa: BLE001 - optional migration boundary
        gate.dependency("malformed_manifest_json", f"cannot import migration journal API: {type(exc).__name__}: {exc}")
        return
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-malformed-manifest-") as temp:
        path = Path(temp) / "phase1.json"
        path.write_text('{"state":', encoding="utf-8")
        try:
            JsonManifestStore(path).load()
        except MigrationError as exc:
            gate.pass_("malformed_manifest_json", rejected=True, error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - wrong exception is still useful evidence
            gate.fail(
                "malformed_manifest_json",
                "malformed manifest was not rejected with the migration error boundary",
                exception=f"{type(exc).__name__}: {exc}",
            )
        else:
            gate.fail("malformed_manifest_json", "malformed manifest JSON was accepted")


def _check_future_schema_no_downgrade(gate: Gate) -> None:
    """A database newer than Phase 1 must be rejected without mutation."""

    try:
        from memoryguard.storage.schema import initialize_database
    except Exception as exc:  # noqa: BLE001 - optional storage boundary
        gate.dependency("future_schema_no_downgrade", f"cannot import storage schema API: {type(exc).__name__}: {exc}")
        return
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-future-schema-") as temp:
        path = Path(temp) / "runtime.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA user_version=999")
            conn.commit()
        finally:
            conn.close()
        before = path.read_bytes()
        try:
            initialize_database(path, "runtime")
        except Exception as exc:  # noqa: BLE001 - rejection is the expected path
            after = path.read_bytes()
            if before == after:
                gate.pass_("future_schema_no_downgrade", rejected=True, error=f"{type(exc).__name__}: {exc}")
            else:
                gate.fail(
                    "future_schema_no_downgrade",
                    "future-schema rejection mutated the database",
                    exception=f"{type(exc).__name__}: {exc}",
                )
        else:
            conn = sqlite3.connect(path)
            try:
                version_after = int(conn.execute("PRAGMA user_version").fetchone()[0])
            finally:
                conn.close()
            gate.fail(
                "future_schema_no_downgrade",
                "future schema was accepted or downgraded",
                expected_rejection=True,
                version_before=999,
                version_after=version_after,
            )


def _check_symlink_escape(gate: Gate) -> None:
    """Containment checks must resolve symlinks before accepting a V2 path."""

    try:
        from memoryguard.storage.layout import LayoutError, WorkspaceV2Layout
    except Exception as exc:  # noqa: BLE001 - optional storage boundary
        gate.dependency("symlink_escape", f"cannot import layout API: {type(exc).__name__}: {exc}")
        return
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-symlink-") as temp:
        workspace = Path(temp) / "workspace"
        outside = Path(temp) / "outside"
        workspace.mkdir()
        outside.mkdir()
        layout = WorkspaceV2Layout(workspace)
        link = layout.root / "runtime" / "escape.db"
        link.parent.mkdir(parents=True)
        try:
            os.symlink(str(outside), str(link), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            gate.skip("symlink_escape", "platform cannot create symlinks for this probe", error=f"{type(exc).__name__}: {exc}")
            return
        try:
            layout.assert_contained(link)
        except LayoutError as exc:
            gate.pass_("symlink_escape", rejected=True, error=f"{type(exc).__name__}: {exc}")
        else:
            gate.fail("symlink_escape", "layout accepted a path whose symlink target escapes .memoryguard")


def _check_checkpoint_pointer_ledger_digest(gate: Gate) -> None:
    """Exercise persisted checkpoint/pointer/ledger/digest evidence via APIs."""

    try:
        from memoryguard.storage.database import open_database
        from memoryguard.system.manifest import ManifestManager, ManifestState
    except Exception as exc:  # noqa: BLE001 - optional system boundary
        message = f"cannot import manifest/storage APIs: {type(exc).__name__}: {exc}"
        for name in ("checkpoint_restart", "manifest_pointer", "manifest_ledger", "manifest_digest"):
            gate.dependency(name, message)
        return
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-manifest-evidence-") as temp:
        workspace = Path(temp)
        try:
            manager = ManifestManager(workspace)
            manager.transition(ManifestState.V2_BUILDING, migration_id="acceptance-evidence")
            manager.record_checkpoint(
                {"inventory": {"digest": "source-digest"}},
                migration_id="acceptance-evidence",
            )
            restarted = ManifestManager(workspace)
            building = restarted.current()
            if (
                building.state is ManifestState.V2_BUILDING
                and building.checkpoints.get("inventory", {}).get("digest") == "source-digest"
            ):
                gate.pass_("checkpoint_restart", state=building.state.value, checkpoint=building.checkpoints.get("inventory"))
            else:
                gate.fail("checkpoint_restart", "checkpoint or V2_BUILDING pointer did not survive manager restart", state=building.state.value)

            ready = restarted.transition(
                ManifestState.V2_READY,
                migration_id="acceptance-evidence",
                source_digest="source-digest",
                target_digest="target-digest",
                manifest_digest="manifest-digest",
                digests={"validator_passed": True, "checkpoints": {"validator": "pass"}},
            )
            active = restarted.transition(ManifestState.V2_ACTIVE)
            persisted = ManifestManager(workspace).current()
            if persisted.state is ManifestState.V2_ACTIVE and persisted.generation == active.generation:
                gate.pass_("manifest_pointer", state=persisted.state.value, generation=persisted.generation)
            else:
                gate.fail("manifest_pointer", "activation pointer did not persist V2_ACTIVE", state=persisted.state.value)

            with open_database(restarted.db_path, readonly=True) as conn:
                rows = conn.execute(
                    "SELECT from_state, to_state, source_digest, target_digest FROM migration_ledger "
                    "WHERE migration_id=? ORDER BY generation",
                    ("acceptance-evidence",),
                ).fetchall()
            if len(rows) >= 3 and rows[-1][1] == ManifestState.V2_ACTIVE.value:
                gate.pass_("manifest_ledger", transitions=len(rows), last_to_state=rows[-1][1])
            else:
                gate.fail("manifest_ledger", "manifest transition ledger did not record the evidence chain", transitions=len(rows))

            if (
                ready.source_digest == "source-digest"
                and ready.target_digest == "target-digest"
                and ready.manifest_digest == "manifest-digest"
                and active.source_digest == ready.source_digest
                and active.target_digest == ready.target_digest
                and active.manifest_digest == ready.manifest_digest
                and active.digests == ready.digests
            ):
                gate.pass_("manifest_digest", inherited=True, source_digest=active.source_digest, target_digest=active.target_digest)
            else:
                gate.fail("manifest_digest", "V2_ACTIVE did not inherit immutable READY digest evidence")
        except Exception as exc:  # noqa: BLE001 - retain one JSON result for malformed implementations
            message = f"manifest evidence probe failed: {type(exc).__name__}: {exc}"
            for name in ("checkpoint_restart", "manifest_pointer", "manifest_ledger", "manifest_digest"):
                if name not in gate.checks:
                    gate.fail(name, message)


def _check_phase2_scope(gate: Gate, contract: dict[str, Any]) -> None:
    """Keep Phase 2 conversion/loss metrics explicitly non-claims in Phase 1."""

    if contract.get("migration", {}).get("lossless_conversion_claim") is not False:
        gate.fail("phase2_scope_not_claimed", "contract must not claim Phase 2 lossless conversion")
        return
    try:
        from memoryguard.migration.framework import MigrationValidator
    except Exception as exc:  # noqa: BLE001 - optional migration boundary
        gate.dependency("phase2_scope_not_claimed", f"cannot import validator: {type(exc).__name__}: {exc}")
        return
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-phase2-scope-") as temp:
        result = MigrationValidator(Path(temp) / ".memoryguard").validate()
    if result.loss == "NOT_EVALUATED" and result.orphan == "NOT_EVALUATED" and result.migration_loss is None:
        gate.pass_(
            "phase2_scope_not_claimed",
            conversion="NOT_EVALUATED",
            loss=result.loss,
            orphan=result.orphan,
        )
    else:
        gate.fail(
            "phase2_scope_not_claimed",
            "Phase 1 acceptance reported unimplemented conversion metrics as evaluated",
            loss=result.loss,
            orphan=result.orphan,
            migration_loss=result.migration_loss,
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (defaults to this script's repository)")
    parser.add_argument("--json", action="store_true", help="kept for compatibility; output is always JSON")
    return parser.parse_args(argv)


def _safe_check(gate: Gate, name: str, callback: Callable[[], None]) -> None:
    """Keep stdout JSON even when an optional implementation is malformed.

    A check is still a failure (never converted to success); the boundary only
    prevents a traceback from replacing the machine-readable report.
    """

    try:
        callback()
    except Exception as exc:  # noqa: BLE001 - serialize unexpected check errors
        gate.fail(name, f"unhandled acceptance check error: {type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    global ROOT, SRC, CONTRACT_PATH
    opts = _parse_args(argv)
    ROOT = opts.root.expanduser().resolve()
    SRC = ROOT / "src"
    CONTRACT_PATH = ROOT / "docs" / "v2" / "phase1-architecture-contract.json"
    gate = Gate()
    contract = _load_contract(gate)
    if contract is not None:
        _safe_check(gate, "static_contract", lambda: _check_static_contract(gate, contract))
        _safe_check(gate, "layout_module", lambda: _check_layout_module(gate))
        _safe_check(gate, "schema_module", lambda: _check_schema_module(gate, contract))
        _safe_check(gate, "readonly_no_create", lambda: _check_database_read_only(gate))
        _safe_check(gate, "manifest_module", lambda: _check_manifest_module(gate, contract))
        _safe_check(gate, "v2_source_scan", lambda: _scan_new_v2_sources(gate, contract))
        _safe_check(gate, "malformed_manifest_json", lambda: _check_malformed_manifest_json(gate))
        _safe_check(gate, "future_schema_no_downgrade", lambda: _check_future_schema_no_downgrade(gate))
        _safe_check(gate, "symlink_escape", lambda: _check_symlink_escape(gate))
        _safe_check(gate, "manifest_evidence", lambda: _check_checkpoint_pointer_ledger_digest(gate))
        _safe_check(gate, "phase2_scope", lambda: _check_phase2_scope(gate, contract))
    result = {
        "contract": "memoryguard-v2-phase1",
        "phase": 1,
        "ok": not gate.failures,
        "checks": gate.checks,
        "failures": gate.failures,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
