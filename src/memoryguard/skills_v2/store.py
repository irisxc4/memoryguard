"""SQLite-backed V2 Skill Store.

The store is a declaration registry, not a plugin loader.  It persists
stable identifiers, relative entrypoint references and hashes, scopes,
capabilities and reference-only evidence/assets.  No method in this module
reads a skill body or starts a process.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from ..storage.database import connect_database
from ..storage.layout import WorkspaceV2Layout
from ..storage.transaction import transaction
from .models import (
    ALLOWED_CAPABILITIES,
    SkillAuthorizationError,
    SkillBinding,
    SkillConflictError,
    SkillDecision,
    SkillDefinition,
    SkillMutationContext,
    SkillMutationResult,
    SkillReadScope,
    SkillReceipt,
    SkillSchemaError,
    SkillValidationError,
    canonical_json,
    stable_hash,
)


SCHEMA_VERSION = 1
SCHEMA_MARKER = "memoryguard-v2-phase5-skills"
SCHEMA_DOMAIN = "skills"
SKILL_DB_NAME = "skills.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return canonical_json(value)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _source_ref(value: str | Path) -> str:
    text = _text(value).replace("\\", "/")
    if not text or "\x00" in text or text.startswith("/") or text.startswith("//"):
        raise SkillValidationError("source reference must be relative")
    if len(text) >= 2 and text[1] == ":":
        raise SkillValidationError("source reference cannot contain a drive")
    if "://" in text or text.casefold().startswith(("file:", "http:", "https:", "urn:")):
        raise SkillValidationError("source reference cannot be a URI")
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise SkillValidationError("source reference contains traversal")
    return "/".join(parts)


def _path_for(value: str | Path | WorkspaceV2Layout, path: str | Path | None = None) -> tuple[WorkspaceV2Layout, Path]:
    raw: Any = path if path is not None else value
    if isinstance(raw, WorkspaceV2Layout):
        layout = raw
        candidate = layout.root / "skills" / SKILL_DB_NAME
    else:
        candidate = Path(raw).expanduser()
        if candidate.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}:
            candidate = Path(os.path.abspath(os.fspath(candidate)))
            if candidate.name != SKILL_DB_NAME or candidate.parent.name != "skills" or candidate.parent.parent.name != WorkspaceV2Layout.ROOT_NAME:
                raise ValueError("skills database must be inside .memoryguard/skills/skills.db")
            layout = WorkspaceV2Layout(candidate.parent.parent.parent)
        else:
            layout = WorkspaceV2Layout(Path(raw))
            candidate = layout.root / "skills" / SKILL_DB_NAME
    return layout, candidate


def _assert_safe(path: Path, *, allow_missing: bool = True) -> None:
    """Reject symlink/reparse components at every writable boundary."""

    current = path
    components: list[Path] = []
    while current != current.parent:
        components.append(current)
        if current.name == WorkspaceV2Layout.ROOT_NAME:
            break
        current = current.parent
    for item in reversed(components):
        if not item.exists() and not item.is_symlink():
            if allow_missing:
                continue
            raise SkillSchemaError(f"required skills path is missing: {item}")
        if WorkspaceV2Layout._is_reparse_or_symlink(item):
            raise SkillSchemaError(f"skills path cannot be a symlink or reparse point: {item}")


class SkillStore:
    """Durable, ACL-checked skill declaration registry."""

    SCHEMA_VERSION = SCHEMA_VERSION
    SCHEMA_MARKER = SCHEMA_MARKER
    SCHEMA_META_TABLE = "schema_meta"
    DB_NAME = SKILL_DB_NAME
    ALLOWED_CAPABILITIES = ALLOWED_CAPABILITIES

    def __init__(
        self,
        workspace_or_path: str | Path | WorkspaceV2Layout,
        *,
        path: str | Path | None = None,
        readonly: bool = False,
        read_only: bool | None = None,
    ) -> None:
        if read_only is not None:
            readonly = bool(read_only)
        self.layout, self.db_path = _path_for(workspace_or_path, path)
        self.path = self.db_path
        self.workspace = self.layout.workspace
        self.readonly = bool(readonly)
        _assert_safe(self.workspace, allow_missing=False if self.workspace.exists() else True)
        if self.readonly:
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
            self._check_schema(readonly=True)
        else:
            self._ensure_dirs()
            self._preflight_existing()
            self._init_schema()

    def _ensure_dirs(self) -> None:
        root = self.layout.root
        skills_dir = root / "skills"
        _assert_safe(root)
        root.mkdir(parents=True, exist_ok=True)
        _assert_safe(root, allow_missing=False)
        _assert_safe(skills_dir)
        skills_dir.mkdir(parents=True, exist_ok=True)
        _assert_safe(skills_dir, allow_missing=False)

    def _preflight_existing(self) -> None:
        if not self.db_path.is_file():
            return
        _assert_safe(self.db_path)
        try:
            conn = connect_database(self.db_path, readonly=True)
            try:
                self._check_schema_connection(conn, allow_fresh=True)
            finally:
                conn.close()
        except FileNotFoundError:
            return
        except SkillSchemaError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise SkillSchemaError(f"cannot inspect existing skills database: {self.db_path}") from exc

    @contextmanager
    def _connection(self, *, readonly: bool | None = None) -> Iterator[sqlite3.Connection]:
        ro = self.readonly if readonly is None else bool(readonly)
        _assert_safe(self.db_path)
        conn = connect_database(self.db_path, readonly=ro)
        try:
            yield conn
        finally:
            conn.close()

    @classmethod
    def _schema_tables(cls, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {str(row[0]) for row in rows}

    @classmethod
    def _check_schema_connection(cls, conn: sqlite3.Connection, *, allow_fresh: bool = False) -> bool:
        tables = cls._schema_tables(conn)
        if "schema_meta" not in tables:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if tables or user_version:
                raise SkillSchemaError("skills schema metadata is missing")
            if not allow_fresh:
                raise SkillSchemaError("fresh skills database is not allowed in read-only mode")
            return True
        rows = conn.execute("SELECT domain,version,marker FROM schema_meta").fetchall()
        if len(rows) != 1:
            raise SkillSchemaError("skills schema metadata must contain exactly one row")
        domain, version, marker = str(rows[0][0]), int(rows[0][1]), str(rows[0][2])
        if domain != SCHEMA_DOMAIN:
            raise SkillSchemaError(f"skills schema domain mismatch: {domain!r}")
        if marker != SCHEMA_MARKER:
            raise SkillSchemaError(f"skills schema marker mismatch: {marker!r}")
        if version != SCHEMA_VERSION:
            direction = "future" if version > SCHEMA_VERSION else "unsupported"
            raise SkillSchemaError(f"{direction} skills schema version: {version}")
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version != SCHEMA_VERSION:
            direction = "future" if user_version > SCHEMA_VERSION else "unsupported"
            raise SkillSchemaError(f"{direction} skills user_version: {user_version}")
        return False

    @classmethod
    def _create_schema(cls, conn: sqlite3.Connection) -> None:
        now = _now()
        statements = (
            "CREATE TABLE IF NOT EXISTS schema_meta (domain TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>=1), marker TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS skill_definitions (skill_id TEXT PRIMARY KEY, stable_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, namespace TEXT NOT NULL, current_version INTEGER NOT NULL CHECK(current_version>=1), current_version_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','disabled','tombstoned')), stable_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS skill_versions (version_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>=1), description TEXT NOT NULL DEFAULT '', declaration_json TEXT NOT NULL DEFAULT '{}', entrypoint_ref TEXT NOT NULL, entrypoint_hash TEXT NOT NULL, content_hash TEXT NOT NULL, capabilities_json TEXT NOT NULL DEFAULT '[]', execution_policy_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(skill_id,version), UNIQUE(skill_id,content_hash), FOREIGN KEY(skill_id) REFERENCES skill_definitions(skill_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS skill_bindings (binding_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, version_id TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL DEFAULT '', project_ref TEXT NOT NULL DEFAULT '', share_group_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', runtime_role TEXT NOT NULL DEFAULT '', effect TEXT NOT NULL DEFAULT 'include', binding_hash TEXT NOT NULL, UNIQUE(version_id,binding_hash), FOREIGN KEY(skill_id) REFERENCES skill_definitions(skill_id) ON DELETE CASCADE, FOREIGN KEY(version_id) REFERENCES skill_versions(version_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS skill_capabilities (capability_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, capability TEXT NOT NULL, authority TEXT NOT NULL, constraints_json TEXT NOT NULL DEFAULT '{}', UNIQUE(version_id,capability,authority), FOREIGN KEY(version_id) REFERENCES skill_versions(version_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS skill_evidence_refs (ref_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, evidence_id TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL, revision TEXT NOT NULL DEFAULT '', authority TEXT NOT NULL, UNIQUE(version_id,evidence_id,source_ref,digest), FOREIGN KEY(version_id) REFERENCES skill_versions(version_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS skill_asset_refs (ref_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, asset_id TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL, asset_kind TEXT NOT NULL DEFAULT '', UNIQUE(version_id,asset_id,path,digest), FOREIGN KEY(version_id) REFERENCES skill_versions(version_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS execution_policies (policy_id TEXT PRIMARY KEY, version_id TEXT NOT NULL UNIQUE, policy_json TEXT NOT NULL, policy_hash TEXT NOT NULL, FOREIGN KEY(version_id) REFERENCES skill_versions(version_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS receipts (receipt_id TEXT PRIMARY KEY, operation TEXT NOT NULL, skill_id TEXT NOT NULL DEFAULT '', version_id TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL DEFAULT '', request_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'applied', result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(idempotency_key), FOREIGN KEY(skill_id) REFERENCES skill_definitions(skill_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS decisions (decision_id TEXT PRIMARY KEY, operation TEXT NOT NULL, skill_id TEXT NOT NULL DEFAULT '', before_hash TEXT NOT NULL DEFAULT '', after_hash TEXT NOT NULL DEFAULT '', expected_hash TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'applied' CHECK(status IN ('applied','compensated')), context_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, FOREIGN KEY(skill_id) REFERENCES skill_definitions(skill_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS domain_outbox (event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, event_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'projected' CHECK(status IN ('pending','projected','failed')), attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, projected_at TEXT NOT NULL DEFAULT '', error_json TEXT NOT NULL DEFAULT '{}')",
            "CREATE TABLE IF NOT EXISTS outbox_checkpoints (domain TEXT PRIMARY KEY, last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence>=0), updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS migration_map (map_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_hash TEXT NOT NULL, source_kind TEXT NOT NULL DEFAULT 'skill_manifest', skill_id TEXT NOT NULL, version_id TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(source_path), FOREIGN KEY(skill_id) REFERENCES skill_definitions(skill_id) ON DELETE CASCADE, FOREIGN KEY(version_id) REFERENCES skill_versions(version_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS unknown_ledger (unknown_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, field_name TEXT NOT NULL, value_hash TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(source_path,field_name,value_hash))",
            "CREATE INDEX IF NOT EXISTS idx_skill_bindings_scope ON skill_bindings(target_type,target_id,project_ref,share_group_id,provider,runtime_role)",
            "CREATE INDEX IF NOT EXISTS idx_skill_versions_skill ON skill_versions(skill_id,version)",
        )
        for statement in statements:
            conn.execute(statement)
        # PRAGMA does not accept bound parameters on the Python sqlite API.
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute(
            "INSERT INTO schema_meta(domain,version,marker,updated_at) VALUES(?,?,?,?) ON CONFLICT(domain) DO UPDATE SET version=excluded.version,marker=excluded.marker,updated_at=excluded.updated_at",
            (SCHEMA_DOMAIN, SCHEMA_VERSION, SCHEMA_MARKER, now),
        )
        conn.execute("INSERT INTO outbox_checkpoints(domain,last_sequence,updated_at) VALUES('skills',0,?) ON CONFLICT(domain) DO NOTHING", (now,))

    def _init_schema(self) -> None:
        conn = connect_database(self.db_path, readonly=False)
        try:
            with transaction(conn):
                self._check_schema_connection(conn, allow_fresh=True)
                self._create_schema(conn)
        finally:
            conn.close()

    def _check_schema(self, *, readonly: bool = False) -> None:
        with self._connection(readonly=readonly) as conn:
            self._check_schema_connection(conn, allow_fresh=False)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            raise PermissionError("skills store is read-only")
        self._check_schema(readonly=True)
        with self._connection(readonly=False) as conn:
            with transaction(conn):
                yield conn

    @staticmethod
    def _context(context: SkillMutationContext | Mapping[str, Any] | None) -> SkillMutationContext:
        if not isinstance(context, SkillMutationContext) or type(context._trusted) is not bool or not context._trusted:
            raise SkillAuthorizationError("trusted SkillMutationContext is required for writes")
        return context

    def _scope(self, scope: SkillReadScope | Mapping[str, Any] | None) -> SkillReadScope:
        if scope is None:
            raise SkillAuthorizationError("SkillReadScope is required for reads")
        resolved = SkillReadScope.from_value(scope)
        actual = os.path.abspath(os.fspath(self.workspace))
        requested = os.path.abspath(os.fspath(Path(resolved.workspace_id).expanduser()))
        if requested != actual:
            raise SkillAuthorizationError("skill read scope workspace mismatch")
        return resolved

    @staticmethod
    def _binding_matches(binding: SkillBinding, scope: SkillReadScope) -> bool:
        target = binding.target_type
        if scope.admin:
            # Admin is broad, but still honours explicit deny rows below.
            return True
        if target == "agent":
            return bool(scope.agent_instance_id and binding.target_id == scope.agent_instance_id)
        if target == "project":
            return bool(scope.project_ref and binding.target_id == scope.project_ref)
        if target == "agent_project":
            return bool(scope.agent_instance_id and scope.project_ref and binding.target_id == scope.agent_instance_id and binding.project_ref == scope.project_ref)
        if target == "group":
            return bool(scope.share_group_id and binding.target_id == scope.share_group_id)
        if target == "provider":
            return bool(scope.provider and binding.target_id == scope.provider)
        if target == "runtime":
            return bool(scope.runtime_role and binding.target_id == scope.runtime_role)
        if target == "system":
            return True
        return False

    @classmethod
    def _binding_allowed(cls, binding: SkillBinding, scope: SkillReadScope, *, mutation: bool = False) -> bool:
        return cls._binding_matches(binding, scope)

    @classmethod
    def _skill_visible(cls, bindings: tuple[SkillBinding, ...], scope: SkillReadScope) -> bool:
        """Evaluate the complete ACL: matching deny always wins."""

        matching = [binding for binding in bindings if cls._binding_matches(binding, scope)]
        if any(binding.effect in {"deny", "exclude"} for binding in matching):
            return False
        return any(binding.effect in {"include", "allow"} for binding in matching)

    @classmethod
    def _authorize_bindings(cls, definition: SkillDefinition, context: SkillMutationContext) -> None:
        if not definition.bindings:
            raise SkillAuthorizationError("skill requires at least one binding")
        if context.automatic:
            for binding in definition.bindings:
                if binding.target_type not in {"agent", "agent_project"}:
                    raise SkillAuthorizationError("automatic skill writes may target only own agent/agent_project")
                if binding.target_type == "agent" and binding.target_id != context.agent_instance_id:
                    raise SkillAuthorizationError("automatic skill write targets another agent")
                if binding.target_type == "agent_project" and (binding.target_id != context.agent_instance_id or binding.project_ref != context.project_ref):
                    raise SkillAuthorizationError("automatic skill write targets another agent/project")
            return
        for binding in definition.bindings:
            broad = binding.target_type in {"group", "provider", "runtime", "system"}
            if broad and not context.admin:
                raise SkillAuthorizationError("manual broad skill scope requires admin=true")
            if not context.admin and not cls._binding_allowed(binding, context):
                raise SkillAuthorizationError("skill binding is outside mutation context")

    @staticmethod
    def _request_hash(operation: str, payload: Any, context: SkillMutationContext) -> str:
        return stable_hash({"operation": operation, "payload": payload, "context": context.to_dict()})

    @staticmethod
    def _key(operation: str, payload_hash: str, idempotency_key: str | None) -> str:
        return _text(idempotency_key) or f"{operation}:{payload_hash}"

    @staticmethod
    def _existing_receipt(conn: sqlite3.Connection, key: str, request_hash: str) -> SkillMutationResult | None:
        row = conn.execute("SELECT * FROM receipts WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise SkillConflictError("idempotency key was reused with a different payload")
        result = json.loads(str(row["result_json"] or "{}"))
        definition = None
        skill_id = str(row["skill_id"] or "")
        if skill_id:
            definition = SkillStore._get_unscoped(conn, skill_id, include_tombstoned=True)
        decision_row = conn.execute("SELECT * FROM decisions WHERE decision_id=(SELECT json_extract(result_json,'$.decision_id') FROM receipts WHERE receipt_id=?)", (str(row["receipt_id"]),)).fetchone()
        if decision_row is None:
            decision_row = conn.execute("SELECT * FROM decisions WHERE skill_id=? ORDER BY created_at DESC,decision_id DESC LIMIT 1", (skill_id,)).fetchone()
        decision = SkillStore._decision_from_row(decision_row) if decision_row else SkillDecision("", "replay", skill_id)
        receipt = SkillStore._receipt_from_row(row)
        return SkillMutationResult(definition, receipt, decision)

    @staticmethod
    def _definition_payload(definition: SkillDefinition) -> dict[str, Any]:
        return definition.canonical_payload

    @staticmethod
    def _get_unscoped(conn: sqlite3.Connection, skill_id: str, *, include_tombstoned: bool = False) -> SkillDefinition | None:
        row = conn.execute("SELECT d.*,v.version_id,v.description,v.declaration_json,v.entrypoint_ref,v.entrypoint_hash,v.capabilities_json,v.execution_policy_json FROM skill_definitions d JOIN skill_versions v ON v.version_id=d.current_version_id WHERE d.skill_id=?", (skill_id,)).fetchone()
        if row is None or (not include_tombstoned and str(row["state"]) == "tombstoned"):
            return None
        return SkillStore._row_definition(conn, row)

    @staticmethod
    def _row_definition(conn: sqlite3.Connection, row: sqlite3.Row) -> SkillDefinition:
        skill_id = str(row["skill_id"])
        version_id = str(row["version_id"])
        bindings = [SkillBinding(target_type=r["target_type"], target_id=r["target_id"], project_ref=r["project_ref"], share_group_id=r["share_group_id"], provider=r["provider"], runtime_role=r["runtime_role"], effect=r["effect"]) for r in conn.execute("SELECT * FROM skill_bindings WHERE version_id=? ORDER BY binding_id", (version_id,))]
        caps = [json.loads(r["constraints_json"] or "{}") | {"capability": r["capability"], "authority": r["authority"]} for r in conn.execute("SELECT * FROM skill_capabilities WHERE version_id=? ORDER BY capability_id", (version_id,))]
        evidence = [{"evidence_id": r["evidence_id"], "source_ref": r["source_ref"], "digest": r["digest"], "revision": r["revision"], "authority": r["authority"]} for r in conn.execute("SELECT * FROM skill_evidence_refs WHERE version_id=? ORDER BY ref_id", (version_id,))]
        assets = [{"asset_id": r["asset_id"], "path": r["path"], "digest": r["digest"], "asset_kind": r["asset_kind"]} for r in conn.execute("SELECT * FROM skill_asset_refs WHERE version_id=? ORDER BY ref_id", (version_id,))]
        policy = json.loads(str(row["execution_policy_json"] or "{}"))
        version_value = row["version"] if "version" in row.keys() else row["current_version"]
        return SkillDefinition(name=row["name"], namespace=row["namespace"], version=int(version_value), skill_id=skill_id, description=row["description"], declaration=json.loads(row["declaration_json"] or "{}"), entrypoint_ref=row["entrypoint_ref"], entrypoint_hash=row["entrypoint_hash"], bindings=tuple(bindings), capabilities=tuple(caps), evidence_refs=tuple(evidence), asset_refs=tuple(assets), execution_policy=policy, state=row["state"])

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> SkillReceipt:
        return SkillReceipt(receipt_id=str(row["receipt_id"]), operation=str(row["operation"]), skill_id=str(row["skill_id"] or ""), version_id=str(row["version_id"] or ""), idempotency_key=str(row["idempotency_key"] or ""), request_hash=str(row["request_hash"]), status=str(row["status"]), result=json.loads(str(row["result_json"] or "{}")), created_at=str(row["created_at"]))

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> SkillDecision:
        return SkillDecision(decision_id=str(row["decision_id"]), operation=str(row["operation"]), skill_id=str(row["skill_id"] or ""), before_hash=str(row["before_hash"]), after_hash=str(row["after_hash"]), expected_hash=str(row["expected_hash"]), reason=str(row["reason"]), status=str(row["status"]), created_at=str(row["created_at"]), context=json.loads(str(row["context_json"] or "{}")))

    @staticmethod
    def _next_sequence(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM domain_outbox").fetchone()[0])

    def _event(self, conn: sqlite3.Connection, operation: str, skill_id: str, payload: Mapping[str, Any]) -> str:
        event_id = stable_hash({"operation": operation, "skill_id": skill_id, "payload": payload})
        now = _now()
        sequence = self._next_sequence(conn)
        conn.execute("INSERT INTO domain_outbox(event_id,sequence,event_type,aggregate_id,payload_json,status,attempts,created_at,projected_at) VALUES(?,?,?,?,?,'projected',1,?,?) ON CONFLICT(event_id) DO NOTHING", (event_id, sequence, operation, skill_id, _json(payload), now, now))
        row = conn.execute("SELECT sequence FROM domain_outbox WHERE event_id=?", (event_id,)).fetchone()
        if row:
            conn.execute("UPDATE outbox_checkpoints SET last_sequence=?,updated_at=? WHERE domain='skills' AND last_sequence<?", (int(row[0]), now, int(row[0])))
        return event_id

    def _record(self, conn: sqlite3.Connection, *, operation: str, skill_id: str, version_id: str = "", key: str, request_hash: str, before_hash: str, after_hash: str, expected_hash: str, reason: str, context: SkillMutationContext, result: Mapping[str, Any]) -> SkillMutationResult:
        now = _now()
        decision_id = stable_hash({"operation": operation, "skill_id": skill_id, "before": before_hash, "after": after_hash, "expected": expected_hash, "request": request_hash})[:40]
        receipt_id = stable_hash({"operation": operation, "skill_id": skill_id, "key": key, "request": request_hash})[:40]
        conn.execute("INSERT INTO decisions(decision_id,operation,skill_id,before_hash,after_hash,expected_hash,reason,status,context_json,created_at) VALUES(?,?,?,?,?,?,?,'applied',?,?)", (decision_id, operation, skill_id, before_hash, after_hash, expected_hash, reason, _json(context.to_dict()), now))
        body = dict(result)
        body["decision_id"] = decision_id
        conn.execute("INSERT INTO receipts(receipt_id,operation,skill_id,version_id,idempotency_key,request_hash,status,result_json,created_at) VALUES(?,?,?,?,?,?,?, ?,?)", (receipt_id, operation, skill_id, version_id, key, request_hash, "applied", _json(body), now))
        self._event(conn, operation, skill_id, {"decision_id": decision_id, "before_hash": before_hash, "after_hash": after_hash, "result": body})
        receipt_row = conn.execute("SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
        decision_row = conn.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        assert receipt_row is not None and decision_row is not None
        return SkillMutationResult(self._get_unscoped(conn, skill_id, include_tombstoned=True), self._receipt_from_row(receipt_row), self._decision_from_row(decision_row))

    def register(self, definition: SkillDefinition | Mapping[str, Any], *, context: SkillMutationContext | Mapping[str, Any] | None, idempotency_key: str | None = None, reason: str = "register skill", conn: sqlite3.Connection | None = None) -> SkillMutationResult:
        ctx = self._context(context)
        item = SkillDefinition.from_value(definition)
        self._authorize_bindings(item, ctx)
        stable_id = item.stable_id
        if item.skill_id and item.skill_id != stable_id:
            raise SkillValidationError("skill_id is not stable for this namespace/name")
        if item.stable_id != item.skill_id:
            item = SkillDefinition.from_value({**item.to_dict(), "skill_id": stable_id})
        payload = self._definition_payload(item)
        request_hash = self._request_hash("register", {"skill": payload}, ctx)
        key = self._key("register", request_hash, idempotency_key)
        manager = self._write() if conn is None else nullcontext(conn)
        with manager as conn:
            replay = self._existing_receipt(conn, key, request_hash)
            if replay is not None:
                return replay
            existing = conn.execute("SELECT * FROM skill_definitions WHERE skill_id=?", (stable_id,)).fetchone()
            if existing is not None:
                current = self._get_unscoped(conn, stable_id, include_tombstoned=True)
                assert current is not None
                if item.version <= int(existing["current_version"]):
                    current_hash = str(conn.execute("SELECT content_hash FROM skill_versions WHERE version_id=?", (existing["current_version_id"],)).fetchone()[0])
                    if current_hash == item.content_hash:
                        # Same immutable declaration is a deterministic replay even without a supplied key.
                        old = conn.execute("SELECT * FROM receipts WHERE skill_id=? AND operation='register' AND request_hash=? ORDER BY created_at DESC LIMIT 1", (stable_id, request_hash)).fetchone()
                        if old is not None:
                            return self._existing_receipt(conn, str(old["idempotency_key"]), request_hash)  # type: ignore[return-value]
                    raise SkillConflictError("skill version is immutable; later edits require a new version")
                if current.state == "tombstoned":
                    raise SkillConflictError("tombstoned skill cannot be edited")
            version_id = item.version_id
            # Foreign-key parent must exist before the immutable version row.
            if existing is None:
                now = _now()
                conn.execute("INSERT INTO skill_definitions(skill_id,stable_key,name,namespace,current_version,current_version_id,state,stable_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (stable_id, item.stable_key, item.name, item.namespace, item.version, version_id, item.state, stable_id, now, now))
            conn.execute("INSERT INTO skill_versions(version_id,skill_id,version,description,declaration_json,entrypoint_ref,entrypoint_hash,content_hash,capabilities_json,execution_policy_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (version_id, stable_id, item.version, item.description, _json(item.declaration), item.entrypoint_ref, item.entrypoint_hash, item.content_hash, _json([c.to_dict() for c in item.capabilities]), _json(item.execution_policy.to_dict()), _now()))
            for binding in item.bindings:
                binding_hash = stable_hash(binding.to_dict())
                conn.execute("INSERT INTO skill_bindings(binding_id,skill_id,version_id,target_type,target_id,project_ref,share_group_id,provider,runtime_role,effect,binding_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (stable_hash({"v": version_id, "b": binding_hash})[:40], stable_id, version_id, binding.target_type, binding.target_id, binding.project_ref, binding.share_group_id, binding.provider, binding.runtime_role, binding.effect, binding_hash))
            for cap in item.capabilities:
                conn.execute("INSERT INTO skill_capabilities(capability_id,version_id,capability,authority,constraints_json) VALUES(?,?,?,?,?)", (stable_hash({"v": version_id, "c": cap.to_dict()})[:40], version_id, cap.capability, cap.authority, _json(cap.constraints)))
            for ref in item.evidence_refs:
                conn.execute("INSERT INTO skill_evidence_refs(ref_id,version_id,evidence_id,source_ref,digest,revision,authority) VALUES(?,?,?,?,?,?,?)", (stable_hash({"v": version_id, "e": ref.to_dict()})[:40], version_id, ref.evidence_id, ref.source_ref, ref.digest, ref.revision, ref.authority))
            for ref in item.asset_refs:
                conn.execute("INSERT INTO skill_asset_refs(ref_id,version_id,asset_id,path,digest,asset_kind) VALUES(?,?,?,?,?,?)", (stable_hash({"v": version_id, "a": ref.to_dict()})[:40], version_id, ref.asset_id, ref.path, ref.digest, ref.asset_kind))
            policy_hash = stable_hash(item.execution_policy.to_dict())
            conn.execute("INSERT INTO execution_policies(policy_id,version_id,policy_json,policy_hash) VALUES(?,?,?,?)", (stable_hash({"v": version_id, "p": policy_hash})[:40], version_id, _json(item.execution_policy.to_dict()), policy_hash))
            if existing is not None:
                conn.execute("UPDATE skill_definitions SET current_version=?,current_version_id=?,state=?,updated_at=? WHERE skill_id=?", (item.version, version_id, item.state, _now(), stable_id))
            result = self._record(conn, operation="register", skill_id=stable_id, version_id=version_id, key=key, request_hash=request_hash, before_hash=(str(existing["stable_hash"]) if existing else ""), after_hash=item.content_hash, expected_hash="", reason=reason or "register skill", context=ctx, result={"skill_id": stable_id, "version_id": version_id})
            return result

    put = register
    create = register

    def register_skill(
        self,
        name: str | SkillDefinition | Mapping[str, Any] | None = None,
        *,
        definition: SkillDefinition | Mapping[str, Any] | None = None,
        namespace: str = "default",
        version: int = 1,
        skill_id: str = "",
        description: str = "",
        declaration: Mapping[str, Any] | None = None,
        entrypoint_ref: str = "entrypoint",
        entrypoint_hash: str = "",
        bindings: tuple[SkillBinding, ...] | list[SkillBinding] = (),
        capabilities: tuple[Any, ...] | list[Any] = (),
        evidence_refs: tuple[Any, ...] | list[Any] = (),
        asset_refs: tuple[Any, ...] | list[Any] = (),
        execution_policy: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        context: SkillMutationContext | Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        reason: str = "register skill",
    ) -> SkillMutationResult:
        if definition is None and isinstance(name, (SkillDefinition, Mapping)):
            definition = name
        if definition is None:
            definition = SkillDefinition(
                name=str(name or ""), namespace=namespace, version=version,
                skill_id=skill_id, description=description, declaration=declaration or {},
                entrypoint_ref=entrypoint_ref, entrypoint_hash=entrypoint_hash,
                bindings=tuple(bindings), capabilities=tuple(capabilities),
                evidence_refs=tuple(evidence_refs), asset_refs=tuple(asset_refs),
                execution_policy=execution_policy, metadata=metadata or {},
            )
        return self.register(definition, context=context, idempotency_key=idempotency_key, reason=reason)

    create_skill = register_skill

    def get(self, skill_id: str, *, scope: SkillReadScope | Mapping[str, Any], include_tombstoned: bool = False) -> SkillDefinition | None:
        resolved = self._scope(scope)
        with self._connection(readonly=True) as conn:
            item = self._get_unscoped(conn, str(skill_id), include_tombstoned=include_tombstoned)
            if item is None:
                return None
            if not self._skill_visible(item.bindings, resolved):
                return None
            return item

    read = get
    get_skill = get
    read_skill = get

    def list(self, *, scope: SkillReadScope | Mapping[str, Any], include_tombstoned: bool = False) -> list[SkillDefinition]:
        resolved = self._scope(scope)
        with self._connection(readonly=True) as conn:
            rows = conn.execute("SELECT skill_id FROM skill_definitions ORDER BY stable_key,skill_id").fetchall()
            result: list[SkillDefinition] = []
            for row in rows:
                item = self._get_unscoped(conn, str(row[0]), include_tombstoned=include_tombstoned)
                if item is not None and self._skill_visible(item.bindings, resolved):
                    result.append(item)
            return result

    list_skills = list

    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a physically read-only SQLite handle for inspection."""

        return self._connection(readonly=True)

    def get_version(self, skill_id: str, version: int, *, scope: SkillReadScope | Mapping[str, Any]) -> SkillDefinition | None:
        resolved = self._scope(scope)
        with self._connection(readonly=True) as conn:
            row = conn.execute("SELECT d.*,v.version_id,v.description,v.declaration_json,v.entrypoint_ref,v.entrypoint_hash,v.capabilities_json,v.execution_policy_json FROM skill_definitions d JOIN skill_versions v ON v.skill_id=d.skill_id AND v.version=? WHERE d.skill_id=?", (int(version), str(skill_id))).fetchone()
            if row is None:
                return None
            item = self._row_definition(conn, row)
            # Replace the current-version collections with the requested
            # version's rows; _row_definition is otherwise shared with get().
            if not resolved.admin and not any(self._binding_allowed(binding, resolved) for binding in item.bindings):
                return None
            return item

    def latest(self, skill_id: str, *, scope: SkillReadScope | Mapping[str, Any]) -> SkillDefinition | None:
        return self.get(skill_id, scope=scope)

    def _state_mutation(self, operation: str, skill_id: str, *, context: SkillMutationContext | Mapping[str, Any] | None, expected_hash: str | None = None, idempotency_key: str | None = None, reason: str = "") -> SkillMutationResult:
        ctx = self._context(context)
        request_hash = self._request_hash(operation, {"skill_id": str(skill_id), "expected_hash": expected_hash or ""}, ctx)
        key = self._key(operation, request_hash, idempotency_key)
        with self._write() as conn:
            replay = self._existing_receipt(conn, key, request_hash)
            if replay is not None:
                return replay
            current = self._get_unscoped(conn, str(skill_id), include_tombstoned=True)
            if current is None:
                raise KeyError(skill_id)
            self._authorize_bindings(current, ctx)
            current_hash = stable_hash({"state": current.state, "content_hash": current.content_hash})
            if expected_hash and expected_hash != current_hash and expected_hash != current.content_hash:
                raise SkillConflictError("expected skill hash does not match current state")
            if operation == "enable":
                new_state = "active"
            elif operation == "disable":
                new_state = "disabled"
            elif operation == "tombstone":
                new_state = "tombstoned"
            else:
                raise ValueError(operation)
            if current.state == new_state:
                return self._record(conn, operation=operation, skill_id=current.skill_id, key=key, request_hash=request_hash, before_hash=current_hash, after_hash=current_hash, expected_hash=expected_hash or current_hash, reason=reason or operation, context=ctx, result={"skill_id": current.skill_id, "state": new_state})
            conn.execute("UPDATE skill_definitions SET state=?,updated_at=? WHERE skill_id=?", (new_state, _now(), current.skill_id))
            after_hash = stable_hash({"state": new_state, "content_hash": current.content_hash})
            return self._record(conn, operation=operation, skill_id=current.skill_id, version_id=current.version_id, key=key, request_hash=request_hash, before_hash=current_hash, after_hash=after_hash, expected_hash=expected_hash or current_hash, reason=reason or operation, context=ctx, result={"skill_id": current.skill_id, "state": new_state})

    def enable(self, skill_id: str, *, context: SkillMutationContext | Mapping[str, Any] | None, expected_hash: str | None = None, idempotency_key: str | None = None, reason: str = "enable skill") -> SkillMutationResult:
        return self._state_mutation("enable", skill_id, context=context, expected_hash=expected_hash, idempotency_key=idempotency_key, reason=reason)

    def disable(self, skill_id: str, *, context: SkillMutationContext | Mapping[str, Any] | None, expected_hash: str | None = None, idempotency_key: str | None = None, reason: str = "disable skill") -> SkillMutationResult:
        return self._state_mutation("disable", skill_id, context=context, expected_hash=expected_hash, idempotency_key=idempotency_key, reason=reason)

    def tombstone(self, skill_id: str, *, context: SkillMutationContext | Mapping[str, Any] | None, expected_hash: str | None = None, idempotency_key: str | None = None, reason: str = "tombstone skill") -> SkillMutationResult:
        return self._state_mutation("tombstone", skill_id, context=context, expected_hash=expected_hash, idempotency_key=idempotency_key, reason=reason)

    delete = tombstone
    enable_skill = enable
    disable_skill = disable
    tombstone_skill = tombstone

    def undo(self, decision_id: str, *, context: SkillMutationContext | Mapping[str, Any] | None, reason: str = "undo skill decision", expected_hash: str | None = None, idempotency_key: str | None = None) -> SkillMutationResult:
        ctx = self._context(context)
        request_hash = self._request_hash("undo", {"decision_id": str(decision_id), "expected_hash": expected_hash or ""}, ctx)
        key = self._key("undo", request_hash, idempotency_key)
        with self._write() as conn:
            replay = self._existing_receipt(conn, key, request_hash)
            if replay is not None:
                return replay
            row = conn.execute("SELECT * FROM decisions WHERE decision_id=?", (str(decision_id),)).fetchone()
            if row is None:
                raise KeyError(decision_id)
            if str(row["status"]) != "applied":
                raise SkillConflictError("skill decision is already compensated")
            current = self._get_unscoped(conn, str(row["skill_id"]), include_tombstoned=True)
            if current is None:
                raise SkillConflictError("skill decision target no longer exists")
            self._authorize_bindings(current, ctx)
            actual = stable_hash({"state": current.state, "content_hash": current.content_hash})
            if expected_hash and expected_hash != actual:
                raise SkillConflictError("undo expected hash mismatch")
            if str(row["after_hash"]) and actual != str(row["after_hash"]):
                raise SkillConflictError("undo hash/state guard rejected")
            target = str(row["operation"])
            prior = {"enable": "disabled", "disable": "active", "tombstone": "active"}.get(target)
            if prior is None:
                raise SkillConflictError(f"undo is not supported for {target}")
            conn.execute("UPDATE skill_definitions SET state=?,updated_at=? WHERE skill_id=?", (prior, _now(), current.skill_id))
            conn.execute("UPDATE decisions SET status='compensated' WHERE decision_id=? AND status='applied'", (str(decision_id),))
            after = stable_hash({"state": prior, "content_hash": current.content_hash})
            return self._record(conn, operation="undo", skill_id=current.skill_id, version_id=current.version_id, key=key, request_hash=request_hash, before_hash=actual, after_hash=after, expected_hash=expected_hash or actual, reason=reason, context=ctx, result={"skill_id": current.skill_id, "state": prior, "compensates": str(decision_id)})

    def list_decisions(self, *, scope: SkillReadScope | Mapping[str, Any]) -> list[SkillDecision]:
        resolved = self._scope(scope)
        with self._connection(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM decisions ORDER BY created_at,decision_id").fetchall()
            result: list[SkillDecision] = []
            for row in rows:
                if resolved.admin:
                    result.append(self._decision_from_row(row))
                else:
                    item = self._get_unscoped(conn, str(row["skill_id"]), include_tombstoned=True)
                    if item and self._skill_visible(item.bindings, resolved):
                        result.append(self._decision_from_row(row))
            return result

    def record_migration_map(self, *, source_path: str | Path, source_hash: str, source_kind: str, skill_id: str, version_id: str, metadata: Mapping[str, Any] | None = None, conn: sqlite3.Connection | None = None) -> None:
        if self.readonly:
            raise PermissionError("skills store is read-only")
        source = _source_ref(source_path)
        if not source_hash or len(source_hash) != 64:
            raise SkillValidationError("migration source hash must be sha256")
        manager = self._write() if conn is None else nullcontext(conn)
        with manager as conn:
            row = conn.execute("SELECT source_hash,skill_id,version_id FROM migration_map WHERE source_path=?", (source,)).fetchone()
            if row is not None and tuple(row) != (source_hash, skill_id, version_id):
                raise SkillConflictError("migration source path/hash mapping is immutable")
            conn.execute("INSERT INTO migration_map(map_id,source_path,source_hash,source_kind,skill_id,version_id,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_path) DO NOTHING", (stable_hash({"source": source, "hash": source_hash})[:40], source, source_hash, source_kind, skill_id, version_id, _json(metadata or {}), _now()))

    def record_unknown(self, *, source_path: str | Path, field_name: str, value: Any, details: Mapping[str, Any] | None = None, conn: sqlite3.Connection | None = None) -> None:
        if self.readonly:
            raise PermissionError("skills store is read-only")
        source = _source_ref(source_path)
        value_hash = stable_hash(value)
        manager = self._write() if conn is None else nullcontext(conn)
        with manager as conn:
            conn.execute("INSERT INTO unknown_ledger(unknown_id,source_path,field_name,value_hash,details_json,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(source_path,field_name,value_hash) DO NOTHING", (stable_hash({"source": source, "field": field_name, "value": value_hash})[:40], source, str(field_name), value_hash, _json(details or {}), _now()))

    def integrity(self) -> dict[str, Any]:
        with self._connection(readonly=True) as conn:
            fk = [dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
            check = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            orphan = int(conn.execute("SELECT COUNT(*) FROM skill_bindings b LEFT JOIN skill_versions v ON v.version_id=b.version_id WHERE v.version_id IS NULL").fetchone()[0])
            return {"ok": check == "ok" and not fk and orphan == 0, "integrity_check": check, "foreign_key_errors": fk, "orphan_bindings": orphan}

    status = integrity

    def integrity_check(self) -> list[str]:
        result = self.integrity()
        errors: list[str] = []
        if result["integrity_check"] != "ok":
            errors.append(str(result["integrity_check"]))
        errors.extend(str(item) for item in result["foreign_key_errors"])
        if int(result["orphan_bindings"]):
            errors.append(f"orphan_bindings:{result['orphan_bindings']}")
        return ["ok"] if not errors else errors

    def counts(self) -> dict[str, int]:
        tables = (
            "skill_definitions", "skill_versions", "skill_bindings", "skill_capabilities",
            "skill_evidence_refs", "skill_asset_refs", "execution_policies", "receipts",
            "decisions", "domain_outbox", "migration_map", "unknown_ledger",
        )
        with self._connection(readonly=True) as conn:
            return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}

    def list_receipts(self) -> list[SkillReceipt]:
        with self._connection(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM receipts ORDER BY created_at,receipt_id").fetchall()
            return [self._receipt_from_row(row) for row in rows]

    def list_migration_map(self) -> list[dict[str, Any]]:
        with self._connection(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM migration_map ORDER BY source_path").fetchall()
            return [dict(row) for row in rows]

    def list_unknown_ledger(self) -> list[dict[str, Any]]:
        with self._connection(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM unknown_ledger ORDER BY source_path,field_name").fetchall()
            return [dict(row) for row in rows]


__all__ = ["SCHEMA_MARKER", "SCHEMA_VERSION", "SkillStore", "SKILL_DB_NAME"]
