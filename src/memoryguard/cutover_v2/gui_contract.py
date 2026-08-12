"""Static embedded-GUI operation discovery used by readiness and tests.

This is intentionally small and deterministic.  It extracts literal API method
names from the product's embedded JavaScript without interpreting or executing
that JavaScript.  Graphify Phase 9 later provides the richer control/handler
semantic graph; this scanner is the fail-closed Phase-0 gate that prevents a
visible literal call from silently escaping the canonical GUI registry.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping

from .surfaces import GUI_OPERATION_SPECS


_PRODUCT_GUI_FILES = (
    "interactive.py",
    "knowledge_gui.py",
    "gui.py",
)
_CALL_PATTERNS = (
    re.compile(r"\bcallApi\s*\(\s*(['\"])([A-Za-z_][A-Za-z0-9_-]*)\1"),
    re.compile(r"(?<![A-Za-z0-9_])api\s*\(\s*(['\"])([A-Za-z_][A-Za-z0-9_-]*)\1"),
    re.compile(r"\bdetailApi\s*\(\s*(['\"])([A-Za-z_][A-Za-z0-9_-]*)\1"),
    re.compile(r"\bhandle_knowledge_api\s*\(\s*(['\"])([A-Za-z_][A-Za-z0-9_-]*)\1"),
)


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def discover_embedded_gui_methods(root: str | Path | None = None) -> dict[str, tuple[str, ...]]:
    """Return literal GUI API methods grouped by production source file."""

    base = Path(root).expanduser().resolve() if root is not None else _package_root()
    result: dict[str, tuple[str, ...]] = {}
    for name in _PRODUCT_GUI_FILES:
        path = base / name
        if not path.is_file():
            result[name] = ()
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            result[name] = ()
            continue
        methods: set[str] = set()
        for pattern in _CALL_PATTERNS:
            methods.update(match.group(2) for match in pattern.finditer(source))
        result[name] = tuple(sorted(methods))
    return result


def visible_gui_methods(root: str | Path | None = None) -> frozenset[str]:
    discovered = discover_embedded_gui_methods(root)
    return frozenset(name for methods in discovered.values() for name in methods)


def codegraph_gui_issues(
    store: Any,
    scope: Any,
    *,
    root: str | Path | None = None,
) -> tuple[dict[str, str], ...]:
    """Cross-check visible GUI methods against production CodeGraph chains.

    A passing method has a production-only semantic chain:
    ``control -> handler -> api -> surface -> native`` using the canonical
    Graphify contexts ``control_handler``, ``handler_api`` and ``api_surface``.
    Test/fixture
    nodes never satisfy this gate.  The function consumes graph metadata only.
    """

    try:
        files = tuple(store.list_source_files(scope=scope, active_only=True))
        production_files = {
            item.file_id for item in files
            if getattr(item, "provenance", "") == "production"
            and getattr(item, "source_role", "") == "production"
        }
        symbols: dict[str, Any] = {}
        for source in files:
            if source.file_id not in production_files:
                continue
            for symbol in store.get_symbols(source.file_id, scope=scope):
                if getattr(symbol, "provenance", "") == "production":
                    symbols[symbol.symbol_id] = symbol
        edges = tuple(
            edge for edge in store.list_edges(scope=scope)
            if getattr(edge, "provenance", "") == "production"
            and edge.from_id in symbols and edge.to_id in symbols
        )
    except Exception:
        return ({"name": "*", "code": "codegraph_gui_capability_unavailable"},)

    by_name: dict[str, list[Any]] = {}
    for symbol in symbols.values():
        by_name.setdefault(str(getattr(symbol, "name", "")), []).append(symbol)
    incoming: dict[tuple[str, str], list[Any]] = {}
    outgoing: dict[tuple[str, str], list[Any]] = {}
    for edge in edges:
        incoming.setdefault((edge.to_id, str(getattr(edge, "context", ""))), []).append(edge)
        outgoing.setdefault((edge.from_id, str(getattr(edge, "context", ""))), []).append(edge)

    def api_chain_complete(api_symbol: Any, native_ids: set[str]) -> bool:
        for surface_edge in outgoing.get((api_symbol.symbol_id, "api_surface"), ()):
            for native_edge in outgoing.get((surface_edge.to_id, "surface_native"), ()):
                if native_edge.to_id in native_ids:
                    return True
        return False

    issues: list[dict[str, str]] = []
    visible_methods = sorted(visible_gui_methods(root))
    api_chain_status: dict[str, bool] = {}
    for name in visible_methods:
        spec = GUI_OPERATION_SPECS.get(name)
        if spec is None:
            issues.append({"name": name, "code": "visible_gui_method_unknown"})
            api_chain_status[name] = False
            continue
        api_candidates = [
            item for item in by_name.get(name, ())
            if str(getattr(item, "metadata", {}).get("semantic_kind", "")) == "api_method"
        ]
        native_ids = {
            item.symbol_id for item in by_name.get(spec.native_handler, ())
            if str(getattr(item, "metadata", {}).get("semantic_kind", "")) == "native_handler"
        }
        if not api_candidates:
            issues.append({"name": name, "code": "codegraph_gui_api_missing"})
            api_chain_status[name] = False
            continue
        if not native_ids:
            issues.append({"name": name, "code": "codegraph_gui_native_missing"})
            api_chain_status[name] = False
            continue
        complete = any(api_chain_complete(api_symbol, native_ids) for api_symbol in api_candidates)
        api_chain_status[name] = complete
        if not complete:
            issues.append({"name": name, "code": "codegraph_gui_semantic_chain_missing"})

    # Controls are a separate product invariant from API literals. Page init,
    # polling and recovery helpers legitimately call APIs without being backed
    # by a button. Conversely, every rendered production control must resolve
    # to a production handler; when that handler calls APIs, every such API must
    # reach its registered surface/native handler through production edges.
    controls = sorted(
        (
            item for item in symbols.values()
            if str(getattr(item, "metadata", {}).get("semantic_kind", "")) == "gui_control"
        ),
        key=lambda item: (str(getattr(item, "name", "")), item.symbol_id),
    )
    for control in controls:
        handler_edges = outgoing.get((control.symbol_id, "control_handler"), ())
        if not handler_edges:
            issues.append(
                {
                    "name": f"control:{control.symbol_id}",
                    "code": "codegraph_gui_control_handler_missing",
                    "label": str(getattr(control, "name", ""))[:128],
                }
            )
            continue
        control_ok = False
        for handler_edge in handler_edges:
            handler_id = handler_edge.to_id
            api_edges = outgoing.get((handler_id, "handler_api"), ())
            # Pure local UI handlers (navigation, modal state, filtering) are
            # mapped once their real production handler is known.
            if not api_edges:
                control_ok = True
                break
            handler_ok = True
            for api_edge in api_edges:
                api_symbol = symbols.get(api_edge.to_id)
                if api_symbol is None:
                    handler_ok = False
                    break
                api_name = str(getattr(api_symbol, "name", ""))
                spec = GUI_OPERATION_SPECS.get(api_name)
                if spec is None:
                    handler_ok = False
                    break
                native_ids = {
                    item.symbol_id for item in by_name.get(spec.native_handler, ())
                    if str(getattr(item, "metadata", {}).get("semantic_kind", "")) == "native_handler"
                }
                if not native_ids or not api_chain_complete(api_symbol, native_ids):
                    handler_ok = False
                    break
            if handler_ok:
                control_ok = True
                break
        if not control_ok:
            issues.append(
                {
                    "name": f"control:{control.symbol_id}",
                    "code": "codegraph_gui_control_api_chain_missing",
                    "label": str(getattr(control, "name", ""))[:128],
                }
            )
    return tuple(issues)


def codegraph_gui_coverage(store: Any, scope: Any, *, root: str | Path | None = None) -> dict[str, Any]:
    visible = visible_gui_methods(root)
    issues = codegraph_gui_issues(store, scope, root=root)
    method_failures = {
        item["name"] for item in issues
        if item.get("name") not in {None, "*"} and not str(item.get("name") or "").startswith("control:")
    }
    control_issues = [item for item in issues if str(item.get("name") or "").startswith("control:")]
    capability_error = any(item.get("name") == "*" for item in issues)
    mapped = 0 if capability_error else len(visible - method_failures)
    controls_total = 0
    if not capability_error:
        try:
            for source in store.list_source_files(scope=scope, active_only=True):
                if getattr(source, "provenance", "") != "production" or getattr(source, "source_role", "") != "production":
                    continue
                controls_total += sum(
                    1 for symbol in store.get_symbols(source.file_id, scope=scope)
                    if getattr(symbol, "provenance", "") == "production"
                    and str(getattr(symbol, "metadata", {}).get("semantic_kind", "")) == "gui_control"
                )
        except Exception:
            capability_error = True
    controls_mapped = 0 if capability_error else max(0, controls_total - len(control_issues))
    return {
        "total": len(visible),
        "mapped": mapped,
        "unmapped": len(visible) - mapped,
        "controls_total": controls_total,
        "controls_mapped": controls_mapped,
        "controls_unmapped": max(0, controls_total - controls_mapped),
        "complete": bool(visible) and not issues and not capability_error,
        "issues": list(issues),
    }


def visible_registry_issues(
    coverage_entries: Mapping[str, Mapping[str, object]] | None = None,
    *,
    root: str | Path | None = None,
) -> tuple[dict[str, str], ...]:
    """Return unknown/non-implemented visible literal operations.

    ``coverage_entries`` is keyed by public method and may be omitted to check
    registry existence only.  The function returns bounded metadata, never
    JavaScript/source snippets.
    """

    issues: list[dict[str, str]] = []
    for name in sorted(visible_gui_methods(root)):
        spec = GUI_OPERATION_SPECS.get(name)
        if spec is None:
            issues.append({"name": name, "code": "visible_gui_method_unknown"})
            continue
        if coverage_entries is None:
            continue
        entry = coverage_entries.get(name)
        if not isinstance(entry, Mapping):
            issues.append({"name": name, "code": "visible_gui_method_missing"})
            continue
        status = str(entry.get("status") or "").casefold()
        if status != "implemented":
            issues.append(
                {
                    "name": name,
                    "code": "visible_gui_method_not_implemented",
                    "status": status or "unknown",
                }
            )
    return tuple(issues)


__all__ = [
    "codegraph_gui_coverage",
    "codegraph_gui_issues",
    "discover_embedded_gui_methods",
    "visible_gui_methods",
    "visible_registry_issues",
]
