#!/usr/bin/env python3
"""Machine-readable V2 Phase 5 shadow acceptance gate.

The default is a read-only inventory.  It never creates ``.memoryguard``
directories/databases and never opens a V1 store through a writable handle.
``--write-shadow`` may project an explicitly supplied legacy CodeGraph source
into the workspace V2 database, but the report remains ``V2_BUILDING`` with
``ready=false`` and ``can_promote=false``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard.codegraph_v2 import CodeGraphScope  # noqa: E402
from memoryguard.migration.codegraph import V1CodeGraphMigrator  # noqa: E402
from memoryguard.storage.database import open_database  # noqa: E402
from memoryguard.storage.layout import WorkspaceV2Layout  # noqa: E402


def _empty(status: str = "NO_SOURCE") -> dict[str, Any]:
    return {
        "status": status,
        "counts": {},
        # ``-1`` means unassessed (missing/incomplete source), never a clean
        # zero.  Acceptance must not convert unknown state into readiness.
        "orphan": -1,
        "loss": 0,
        "outbox": {"total": 0, "pending": 0, "failed": 0},
        "acl": {"unknown": 0, "anomalies": 0},
        "unknown": {"total": 0, "blocked": 0},
        "errors": [],
    }


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _count(conn: sqlite3.Connection, table: str, where: str = "", params: Iterable[Any] = ()) -> int:
    try:
        if " " in table:
            base, alias = table.split(None, 1)
            sql = f'SELECT COUNT(*) FROM "{base.replace(chr(34), chr(34) * 2)}" {alias}'
        else:
            sql = f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
        if where:
            sql += " WHERE " + where
        return int(conn.execute(sql, tuple(params)).fetchone()[0])
    except sqlite3.Error:
        return -1


def _inspect_db(path: Path, *, domain: str) -> dict[str, Any]:
    if not path.is_file():
        return _empty("NO_SOURCE")
    result = _empty("READY")
    try:
        with open_database(path, readonly=True) as conn:
            tables = _tables(conn)
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
            foreign = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
            result["integrity"] = integrity
            result["foreign_keys"] = foreign
            if any(value.lower() != "ok" for value in integrity) or foreign:
                result["status"] = "FAIL"
                result["errors"].append("integrity_or_foreign_key_failure")
            result["counts"] = {table: _count(conn, table) for table in sorted(tables) if not table.startswith("sqlite_") and not table.endswith(("_data", "_idx", "_content", "_docsize", "_config"))}
            if domain == "content":
                result["orphan"] = _count(conn, "content_occurrences o", "o.active=1 AND (o.blob_id NOT IN (SELECT b.blob_id FROM content_blobs b))")
                result["loss"] = _count(conn, "migration_map", "status IN ('blocked','lost','loss')")
                result["acl"] = {
                    "unknown": _count(conn, "content_occurrences", "provider='__UNKNOWN__' OR sensitivity='__UNKNOWN__' OR policy_class='__UNKNOWN__'"),
                    "anomalies": _count(conn, "content_acl_anomalies"),
                }
                result["unknown"] = {
                    "total": _count(conn, "source_sync_anomalies", "error_code LIKE '%unknown%'"),
                    "blocked": _count(conn, "migration_map", "status='blocked'"),
                }
            elif domain == "knowledge":
                result["orphan"] = _count(conn, "knowledge_documents d", "d.active=1 AND (d.asset_id NOT IN (SELECT a.asset_id FROM knowledge_assets a WHERE a.active=1))")
                result["loss"] = _count(conn, "migration_map", "status IN ('blocked','lost','loss')")
                result["unknown"] = {"total": _count(conn, "migration_map", "status='blocked'"), "blocked": _count(conn, "migration_map", "status='blocked'")}
            elif domain == "codegraph":
                # Scope and active predicates are mandatory: an inactive or
                # cross-tenant historical row is not a live orphan, while an
                # unknown/missing schema remains ``-1`` (unassessed).
                result["orphan"] = _count(
                    conn,
                    "edges e",
                    "e.active=1 AND ("
                    "NOT EXISTS (SELECT 1 FROM revisions r WHERE r.revision_id=e.revision_id AND r.scope_id=e.scope_id) "
                    "OR "
                    "NOT EXISTS (SELECT 1 FROM symbols s1 WHERE s1.symbol_id=e.from_id AND s1.scope_id=e.scope_id AND s1.active=1) "
                    "OR NOT EXISTS (SELECT 1 FROM symbols s2 WHERE s2.symbol_id=e.to_id AND s2.scope_id=e.scope_id AND s2.active=1)"
                    ")",
                )
                result["loss"] = _count(conn, "migration_map", "status IN ('blocked','lost','loss')")
                result["outbox"] = {
                    "total": _count(conn, "outbox"),
                    "pending": _count(conn, "outbox", "status='pending'"),
                    "failed": _count(conn, "outbox", "status='failed'"),
                }
                result["unknown"] = {
                    "total": _count(conn, "unknown_ledger"),
                    "blocked": _count(conn, "unknown_ledger", "status='BLOCKED'"),
                }
            elif domain == "assets":
                result["orphan"] = _count(conn, "asset_registry", "state='missing'")
                result["loss"] = _count(conn, "asset_registry", "state IN ('deleted','quarantined')")
            elif domain == "skills":
                result["orphan"] = _count(conn, "skill_bindings", "version_id NOT IN (SELECT version_id FROM skill_versions)")
                result["loss"] = _count(conn, "migration_map", "status IN ('blocked','lost','loss')")
                result["outbox"] = {
                    "total": _count(conn, "domain_outbox"),
                    "pending": _count(conn, "domain_outbox", "status='pending'"),
                    "failed": _count(conn, "domain_outbox", "status='failed'"),
                }
                result["unknown"] = {
                    "total": _count(conn, "unknown_ledger"),
                    "blocked": _count(conn, "unknown_ledger"),
                }
            return result
    except (sqlite3.Error, OSError, ValueError) as exc:
        result["status"] = "FAIL"
        result["errors"].append(f"{type(exc).__name__}:{exc}")
        return result


def _inspect_skills(workspace: Path) -> dict[str, Any]:
    result = _empty("NO_SOURCE")
    roots = [workspace / ".agents" / "skills", workspace / ".codex" / "skills", workspace / ".claude" / "skills"]
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        result["status"] = "READY"
        try:
            files.extend(item for item in root.rglob("*") if item.is_file())
        except OSError as exc:
            result["status"] = "FAIL"; result["errors"].append(f"{type(exc).__name__}:{exc}")
    result["counts"] = {"roots": sum(1 for root in roots if root.is_dir()), "files": len(files), "skills": len({item.parent for item in files if item.name.lower() == "skill.md"})}
    # Skills V2 owns a separate metadata-only database under the fixed V2
    # root.  Inspect it read-only when present; a missing DB is not inferred
    # or created by dry-run acceptance.
    skills_db = workspace / ".memoryguard" / "skills" / "skills.db"
    if skills_db.is_file():
        db_result = _inspect_db(skills_db, domain="skills")
        result["status"] = "FAIL" if db_result.get("status") == "FAIL" else "READY"
        result["counts"]["db"] = db_result.get("counts", {})
        result["orphan"] += int(db_result.get("orphan", 0))
        result["loss"] += int(db_result.get("loss", 0))
        result["outbox"] = db_result.get("outbox", result["outbox"])
        result["unknown"] = db_result.get("unknown", result["unknown"])
        result["errors"].extend(db_result.get("errors", []))
    return result


def _storage_summary(layout: WorkspaceV2Layout) -> dict[str, Any]:
    result = _empty("READY")
    dbs = 0; present = 0
    errors: list[str] = []
    for domain, paths in layout.databases.items():
        for path in paths:
            dbs += 1
            if path.is_file():
                present += 1
                checked = _inspect_db(path, domain=domain)
                errors.extend(f"{domain}:{item}" for item in checked.get("errors", []))
    result["counts"] = {"databases": dbs, "present": present, "missing": dbs - present}
    result["errors"] = errors
    result["status"] = "FAIL" if errors else ("READY" if present else "NO_SOURCE")
    return result


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve()
    layout = WorkspaceV2Layout(workspace)
    dry_run = not bool(args.write_shadow)
    migration: dict[str, Any] | None = None
    if args.write_shadow and (args.source or args.codegraph_source or args.knowledge_source):
        source = args.source or args.codegraph_source or args.knowledge_source
        scope = CodeGraphScope(
            workspace_id=str(workspace),
            agent_instance_id=str(args.agent or "phase5"),
            project_ref=str(args.project or "phase5"),
            provider=str(args.provider or "phase5"),
            share_group_id=str(args.group or "phase5"),
            runtime_role=str(args.runtime or "acceptance"),
        )
        try:
            migration = V1CodeGraphMigrator(workspace, source_path=source, scope=scope).migrate(write_shadow=True).to_dict()
        except Exception as exc:
            migration = {"status": "FAILED", "errors": [f"{type(exc).__name__}:{exc}"], "ready": False, "can_promote": False}
    domains = {
        "storage": _storage_summary(layout),
        "content": _inspect_db(layout.content_db, domain="content"),
        "assets": _inspect_db(layout.assets_db, domain="assets"),
        "skills": _inspect_skills(workspace),
        "knowledge": _inspect_db(layout.knowledge_db, domain="knowledge"),
        "codegraph": _inspect_db(layout.codegraph_db, domain="codegraph"),
    }
    if migration is not None:
        domains["codegraph"]["migration"] = migration
    errors: list[str] = []
    for name, value in domains.items():
        errors.extend(f"{name}:{item}" for item in value.get("errors", []))
    if migration:
        errors.extend(str(item) for item in migration.get("errors", []))
    ok = not errors
    return {
        "ok": ok,
        # Phase 5 is a permanent shadow gate; failures are reported in
        # ``errors`` but never promoted to a terminal state.
        "status": "V2_BUILDING",
        "state": "V2_BUILDING",
        "manifest_state": "V2_BUILDING",
        "ready": False,
        "can_promote": False,
        "dry_run": dry_run,
        "workspace": str(workspace),
        "domains": domains,
        "migration": migration or {"status": "NOT_CONFIGURED", "ready": False, "can_promote": False},
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--source", type=Path, help="explicit read-only legacy CodeGraph/Knowledge SQLite source")
    parser.add_argument("--codegraph-source", type=Path)
    parser.add_argument("--knowledge-source", type=Path)
    parser.add_argument("--write-shadow", action="store_true")
    parser.add_argument("--agent", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--group", default="")
    parser.add_argument("--runtime", default="")
    parser.add_argument("--json", action="store_true", help="accepted for automation; output is always JSON")
    args = parser.parse_args(argv)
    report = build_report(args)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
