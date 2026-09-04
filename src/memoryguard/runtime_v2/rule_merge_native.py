"""Native V2 transport service for the four rule-merge governance writes.

This module is deliberately a transport boundary. Production merge policy and
transactions are owned by the V2 Rules database adapter in this module; no
retired merge store participates in the runtime path. Native authority,
manifest CAS, receipts and idempotency gates remain at this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import stat
from typing import Any, Mapping, Sequence

from ..access_context import AccessContext
from ..rules.v2_store import (
    RuleV2Store,
    RULES_SCHEMA_MARKER,
    RULES_SCHEMA_VERSION,
    stable_digest,
    _canonical_relation_kind,
    _runtime_injection_layer,
)
from ..governance_capability import (
    CAPABILITY_SCOPE,
    CapabilityIssueError,
    capability_recovery_token,
    recovery_secret_proof,
)
from .native_ports import (
    NativeContextError,
    NativePortError,
    _NATIVE_INJECTION_CAPABILITY,
    _NativeInjectionCapability,
    bind_native_test_capability,
    resolve_native_transport_context,
)


RULE_MERGE_OPERATIONS = (
    "capability_issue",
    "approve",
    "acknowledge",
    "cooldown_clear",
    "merge_safe",
    "merge_safe_preview",
)
_READ_OPERATIONS = frozenset({"merge_safe_preview"})

_ALIASES = {
    **{name: name for name in RULE_MERGE_OPERATIONS},
    **{f"memoryguard_rule_merge_{name}": name for name in RULE_MERGE_OPERATIONS},
    "issue": "capability_issue",
    "safe": "merge_safe",
    "safe_preview": "merge_safe_preview",
    "rule_merge_capability_issue": "capability_issue",
    "rule_merge_approve": "approve",
    "rule_merge_acknowledge": "acknowledge",
    "rule_merge_cooldown_clear": "cooldown_clear",
    "rule_merge_safe": "merge_safe",
    "rule_merge_safe_preview": "merge_safe_preview",
    "memoryguard_rule_merge_safe": "merge_safe",
    "memoryguard_rule_merge_safe_preview": "merge_safe_preview",
}

_IDENTITY_KEYS = frozenset(
    {
        "workspace_id", "workspace", "agent_instance_id", "agent_id", "agent",
        "trusted_agent_id", "trusted_agent", "share_group_id", "group_id", "group",
        "project_ref", "project_id", "project", "provider", "runtime_role", "runtime",
        "admin", "is_admin", "authority", "trusted_identity", "trusted_context", "identity",
    }
)
_V2_REQUIRED_TABLES: dict[str, frozenset[str]] = {
    "rules_schema_meta": frozenset({"schema_id", "version", "marker"}),
    "rule_definitions": frozenset({"definition_id", "revision", "status"}),
    "rule_merge_proposals": frozenset({"proposal_id", "definition_ids_json", "status", "metadata_json"}),
    "rule_merge_approvals": frozenset({"approval_id", "proposal_id", "capability_id", "expected_revisions_json"}),
    "rule_governance_capabilities": frozenset({"capability_id", "proposal_id", "principal", "scope_json", "expires_at", "consumed_at", "token_digest", "metadata_json"}),
    "rule_governance_capability_consumptions": frozenset({"consumption_id", "capability_id", "proposal_id", "consumed_by", "consumed_at"}),
    "rule_merge_native_requests": frozenset({"request_key", "request_fingerprint", "operation", "schema_version", "status", "result_json"}),
}
_NATIVE_REQUEST_SCHEMA_VERSION = 2
CAPABILITY_ISSUE_REPLAY_SAFE = True
_REPARSE_POINT = 0x0400


class NativeRuleMergeError(NativePortError):
    """Stable, non-leaking native rule-merge failure."""


@dataclass(frozen=True)
class _MutationReceipt:
    receipt_id: str
    values: Mapping[str, Any]


@dataclass(frozen=True)
class _SafeMergePlan:
    group: str
    canonical_id: str
    ordered_dups: tuple[str, ...]
    by_source: Mapping[str, frozenset[str]]
    source_by_definition: Mapping[str, str]


def _text(value: Any, *, field: str = "", max_len: int = 512) -> str:
    if not isinstance(value, str):
        if field:
            raise NativeRuleMergeError(f"invalid_{field}")
        return ""
    result = value.strip()
    if len(result) > max_len:
        raise NativeRuleMergeError(f"invalid_{field or 'value'}")
    return result


def _lexical_absolute(value: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise NativeRuleMergeError("rule_merge_path_unavailable") from exc
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _assert_safe_path(value: str | Path, *, allow_missing: bool = True) -> Path:
    path = _lexical_absolute(value)
    cursor = path
    while True:
        try:
            cursor.lstat()
        except FileNotFoundError:
            if not allow_missing and cursor == path:
                raise NativeRuleMergeError("rule_merge_path_missing")
        except OSError as exc:
            raise NativeRuleMergeError("rule_merge_path_unavailable") from exc
        else:
            if _is_reparse_or_symlink(cursor):
                raise NativeRuleMergeError("rule_merge_path_reparse_or_symlink")
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    return path


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _error_code(error: Exception) -> str:
    """Map dependency exceptions to a bounded stable code set."""

    if isinstance(error, NativeRuleMergeError):
        return error.code
    if isinstance(error, NativeContextError):
        return error.code
    raw = str(error or "")
    token = raw.split(":", 1)[0].strip()
    coded = {
        "rule_merge_definition_not_found": "rule_merge_definition_not_found",
        "rule_merge_canonical_mismatch": "rule_merge_canonical_mismatch",
        "rule_merge_definition_ids_required": "rule_merge_definition_ids_required",
        "rule_merge_native_store_required": "rule_merge_native_store_required",
        "share_group_id_required": "share_group_id_required",
        "canonical_composition_rejected": "canonical_composition_rejected",
        "canonical_snapshot_not_settleable": "canonical_snapshot_not_settleable",
        "canonical_snapshot_settle_failed": "canonical_snapshot_settle_failed",
    }
    if token in coded:
        return coded[token]
    message = raw.casefold()
    exact = {
        "trusted accesscontext required": "native_admin_capability_required",
        "admin capability required (set memoryguard_admin=1)": "native_admin_capability_required",
        "trusted session context required": "native_trusted_session_required",
        "trusted principal required for capability issuance": "native_admin_capability_required",
        "rule_merge_approval_capability_required": "capability_token_required",
        "rule_merge_approval_capability_mismatch": "capability_token_mismatch",
        "rule_merge_approval_principal_mismatch": "native_admin_capability_required",
        "rule_merge_proposal_not_found": "rule_merge_proposal_not_found",
        "rule_merge_proposal_not_approvable": "rule_merge_proposal_not_approvable",
        "rule_merge_definition_revision_drift": "proposal_revision_conflict",
        "rule_merge_similarity_gate_failed": "proposal_revision_conflict",
        "rule_merge_proposal_must_pair_two_definitions": "proposal_revision_invalid",
        "capability rejected: invalid, expired, mismatched, or consumed": "capability_rejected",
        "capability rejected: capability scope rejected": "capability_rejected",
        "opaque capability token rejected": "capability_rejected",
        "recovery_secret_required": "recovery_secret_required",
        "recovery_secret_invalid": "recovery_secret_invalid",
        "recovery_binding_required": "recovery_binding_required",
        "capability_replay_unavailable": "capability_replay_unavailable",
    }
    if message in exact:
        return exact[message]
    if "capability rejected" in message or "capability" in message and "expired" in message:
        return "capability_rejected"
    if isinstance(error, sqlite3.DatabaseError):
        return "rule_merge_store_unavailable"
    if isinstance(error, (OSError, PermissionError)):
        return "rule_merge_store_unavailable"
    if isinstance(error, (TypeError, ValueError)):
        return "rule_merge_operation_rejected"
    return "rule_merge_operation_failed"


def bind_native_rule_merge_test_capability(store: Any) -> _NativeInjectionCapability:
    """Return an explicit process-local test DI capability for one RuleMergeStore."""

    return bind_native_test_capability(stores={"rule_merge": store})


def _parse_iso_epoch(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class _V2RuleMergeStore:
    """Minimal native governance transaction over the canonical V2 rules DB.

    This adapter deliberately does not reuse :class:`RuleMergeStore`: doing so
    would route an apparently-native V2 mutation back into
    ``.memoryguard/rule-intelligence/memory.db``.  Only the four transport
    governance operations live here; proposal generation/merge policy remains
    a separate V2 concern.
    """

    read_only = False

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._store = RuleV2Store(self.workspace)
        self.db_path = self._store.db_path

    def _write_conn(self):
        return self._store.transaction()

    def _conn(self) -> sqlite3.Connection:
        conn = self._store._active()
        if conn is None:
            raise NativeRuleMergeError("rule_merge_transaction_required")
        return conn

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
        return dict(value) if isinstance(value, Mapping) else {}

    @classmethod
    def _proposal(cls, row: sqlite3.Row) -> dict[str, Any]:
        try:
            definition_ids = json.loads(str(row["definition_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            definition_ids = []
        metadata = cls._metadata(row)
        return {
            "proposal_id": str(row["proposal_id"] or ""),
            "definition_ids": list(definition_ids) if isinstance(definition_ids, list) else [],
            "status": str(row["status"] or ""),
            "definition_revision_a": int(metadata.get("definition_revision_a", 0) or 0),
            "definition_revision_b": int(metadata.get("definition_revision_b", 0) or 0),
            "metadata": metadata,
        }

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM rule_merge_proposals WHERE proposal_id=?", (proposal_id,),
        ).fetchone()
        return None if row is None else self._proposal(row)

    @staticmethod
    def _principal(access_context: AccessContext) -> str:
        if not isinstance(access_context, AccessContext):
            raise ValueError("trusted AccessContext required")
        ok, error = access_context.require_capability_issue()
        if not ok:
            raise ValueError(error)
        principal = str(access_context.principal or "").strip()
        if not principal:
            raise ValueError("trusted principal required")
        return principal

    @staticmethod
    def _capability_metadata(row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
        return dict(value) if isinstance(value, Mapping) else {}

    def issue_merge_capability(
        self,
        proposal_id: str,
        access_context: AccessContext,
        *,
        ttl_seconds: float = 300.0,
        recovery_secret: Any = None,
        workspace: str = "",
        request_key: str = "",
        manifest_generation: int = 0,
        **_: Any,
    ) -> str:
        principal = self._principal(access_context)
        conn = self._conn()
        proposal = conn.execute(
            "SELECT status FROM rule_merge_proposals WHERE proposal_id=?", (proposal_id,),
        ).fetchone()
        if proposal is None:
            raise ValueError("rule_merge_proposal_not_found")
        if str(proposal["status"] or "") != "candidate":
            raise ValueError("rule_merge_proposal_not_approvable")
        token = capability_recovery_token(
            recovery_secret,
            workspace=str(workspace or self.workspace),
            principal=principal,
            proposal_id=proposal_id,
            request_key=request_key,
            manifest_generation=manifest_generation,
            scope=CAPABILITY_SCOPE,
        )
        token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        proof = recovery_secret_proof(recovery_secret)
        now = datetime.now(timezone.utc)
        expires = now.timestamp() + float(ttl_seconds)
        if not math.isfinite(expires) or expires <= now.timestamp():
            raise ValueError("invalid_ttl_seconds")
        metadata = {
            "recovery_proof_hash": proof,
            "token_version": "v2",
            "revoked": False,
            "request_key": request_key,
            "manifest_generation": manifest_generation,
        }
        conn.execute(
            "INSERT INTO rule_governance_capabilities(capability_id,proposal_id,principal,scope_json,issued_at,expires_at,consumed_at,token_digest,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                token_digest, proposal_id, principal,
                json.dumps({"scope": CAPABILITY_SCOPE}, ensure_ascii=False, sort_keys=True),
                now.isoformat(), datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
                "", token_digest, json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        return token

    def _consume_capability(self, token: str, *, principal: str, proposal_id: str) -> tuple[str, str]:
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("opaque capability token rejected")
        conn = self._conn()
        token_digest = hashlib.sha256(token.encode("ascii", "strict")).hexdigest()
        row = conn.execute(
            "SELECT * FROM rule_governance_capabilities WHERE token_digest=? AND proposal_id=?",
            (token_digest, proposal_id),
        ).fetchone()
        if row is None or str(row["principal"] or "") != principal:
            raise ValueError("capability rejected")
        metadata = self._capability_metadata(row)
        try:
            scope = json.loads(str(row["scope_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            scope = {}
        if not isinstance(scope, Mapping) or str(scope.get("scope") or "") != CAPABILITY_SCOPE:
            raise ValueError("capability rejected")
        if bool(metadata.get("revoked")) or str(row["consumed_at"] or ""):
            raise ValueError("capability rejected")
        if _parse_iso_epoch(row["expires_at"]) <= datetime.now(timezone.utc).timestamp():
            raise ValueError("capability rejected")
        consumed_at = datetime.now(timezone.utc).isoformat()
        changed = conn.execute(
            "UPDATE rule_governance_capabilities SET consumed_at=? WHERE capability_id=? AND consumed_at=''",
            (consumed_at, str(row["capability_id"])),
        ).rowcount
        if changed != 1:
            raise ValueError("capability rejected")
        consumption_id = stable_digest(("rule-merge-capability-consumption", token_digest, proposal_id, consumed_at))
        conn.execute(
            "INSERT INTO rule_governance_capability_consumptions(consumption_id,capability_id,proposal_id,consumed_by,consumed_at,metadata_json) VALUES(?,?,?,?,?,?)",
            (consumption_id, token_digest, proposal_id, principal, consumed_at, "{}"),
        )
        return token_digest, str(row["expires_at"] or "")

    def _definition_revisions(self, proposal: Mapping[str, Any]) -> dict[str, int]:
        definition_ids = proposal.get("definition_ids")
        if not isinstance(definition_ids, list) or len(definition_ids) != 2:
            raise ValueError("rule_merge_proposal_must_pair_two_definitions")
        conn = self._conn()
        result: dict[str, int] = {}
        for definition_id in definition_ids:
            row = conn.execute(
                "SELECT revision FROM rule_definitions WHERE definition_id=?", (str(definition_id),),
            ).fetchone()
            if row is None:
                raise ValueError("rule_merge_definition_not_found")
            result[str(definition_id)] = int(row["revision"] or 0)
        return result

    def approve_proposal(
        self,
        proposal_id: str,
        *,
        capability_token: str,
        expected_definition_revisions: Mapping[str, int],
        access_context: AccessContext,
        approved_by: str = "",
        approval_scope: str = "merge",
        **_: Any,
    ) -> dict[str, Any]:
        principal = self._principal(access_context)
        if approved_by and approved_by != principal:
            raise ValueError("rule_merge_approval_principal_mismatch")
        if approval_scope != "merge":
            raise ValueError("rule_merge_approval_scope_invalid")
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError("rule_merge_proposal_not_found")
        if proposal["status"] != "candidate":
            raise ValueError("rule_merge_proposal_not_approvable")
        revisions = self._definition_revisions(proposal)
        normalized = {str(key): int(value) for key, value in expected_definition_revisions.items()}
        if normalized != revisions:
            raise RuntimeError("rule_merge_definition_revision_drift")
        token_digest, expires_at = self._consume_capability(
            capability_token, principal=principal, proposal_id=proposal_id,
        )
        approval_id = stable_digest(("rule-merge-approval-v2", proposal_id, token_digest))
        now = datetime.now(timezone.utc).isoformat()
        self._conn().execute(
            "INSERT INTO rule_merge_approvals(approval_id,proposal_id,approved_by,capability_id,expected_revisions_json,approval_scope,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (approval_id, proposal_id, principal, token_digest, json.dumps(revisions, ensure_ascii=False, sort_keys=True), "merge", now, expires_at),
        )
        metadata = dict(proposal.get("metadata") or {})
        metadata["first_merge_acknowledged"] = True
        metadata["cooldown_until"] = ""
        changed = self._conn().execute(
            "UPDATE rule_merge_proposals SET status='approved', metadata_json=?, updated_at=? WHERE proposal_id=? AND status='candidate'",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, proposal_id),
        ).rowcount
        if changed != 1:
            raise ValueError("rule_merge_proposal_not_approvable")
        return {
            "approval_id": approval_id,
            "proposal_id": proposal_id,
            "approved_by": principal,
            "capability_id": token_digest,
            "expected_definition_revisions": revisions,
            "approval_scope": "merge",
            "created_at": now,
            "expires_at": expires_at,
        }

    def acknowledge_first_merge(
        self,
        proposal_id: str,
        actor: str = "human",
        *,
        capability_token: str,
        access_context: AccessContext,
        **_: Any,
    ) -> dict[str, Any]:
        principal = self._principal(access_context)
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError("rule_merge_proposal_not_found")
        self._consume_capability(capability_token, principal=principal, proposal_id=proposal_id)
        metadata = dict(proposal.get("metadata") or {})
        metadata["first_merge_acknowledged"] = True
        metadata["first_merge_acknowledged_by"] = str(actor or principal)
        now = datetime.now(timezone.utc).isoformat()
        self._conn().execute(
            "UPDATE rule_merge_proposals SET metadata_json=?, updated_at=? WHERE proposal_id=?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, proposal_id),
        )
        return self.get_proposal(proposal_id) or {}

    def clear_proposal_cooldown(
        self,
        proposal_id: str,
        *,
        capability_token: str,
        access_context: AccessContext,
        **_: Any,
    ) -> dict[str, Any]:
        principal = self._principal(access_context)
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError("rule_merge_proposal_not_found")
        self._consume_capability(capability_token, principal=principal, proposal_id=proposal_id)
        metadata = dict(proposal.get("metadata") or {})
        metadata["cooldown_until"] = ""
        now = datetime.now(timezone.utc).isoformat()
        self._conn().execute(
            "UPDATE rule_merge_proposals SET metadata_json=?, updated_at=? WHERE proposal_id=?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, proposal_id),
        )
        return self.get_proposal(proposal_id) or {}


class NativeRuleMergeService:
    """Strict native adapter for capability issue/approve/ack/clear operations."""

    requires_state_provider = True
    supports_durable_idempotency = True
    production_blockers: tuple[str, ...] = ()

    def __init__(
        self,
        workspace: str | Path,
        *,
        rule_store: Any = None,
        merge_store: Any = None,
        state_provider: Any = None,
    ) -> None:
        self.workspace = _assert_safe_path(workspace)
        self.db_path = self.workspace / ".memoryguard" / "rules" / "rules.db"
        self._rule_store = self._unwrap_store(
            rule_store if rule_store is not None else merge_store,
        )
        self.state_provider = state_provider
        # The request ledger lives in the same SQLite file as capabilities and
        # proposals.  Every operation is reserved/committed while the Store's
        # own re-entrant write transaction is held.

    def _unwrap_store(self, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, _NativeInjectionCapability) or value.token is not _NATIVE_INJECTION_CAPABILITY:
            raise NativeRuleMergeError("native_test_capability_required")
        store = value.stores.get("rule_merge") or value.stores.get("rules")
        if store is None:
            raise NativeRuleMergeError("native_test_store_required")
        # DI is a capability, not an authorization bypass.  Keep the identity
        # and workspace checks even for in-process fakes.
        try:
            store_workspace = _assert_safe_path(getattr(store, "workspace"), allow_missing=False)
            store_db = _assert_safe_path(getattr(store, "db_path"), allow_missing=True)
        except (AttributeError, TypeError, NativeRuleMergeError) as exc:
            if isinstance(exc, NativeRuleMergeError):
                raise NativeRuleMergeError("native_test_store_identity_required") from exc
            raise NativeRuleMergeError("native_test_store_identity_required") from exc
        if store_workspace != self.workspace or store_db != self.db_path:
            raise NativeRuleMergeError("injected_store_identity_mismatch")
        if getattr(store, "read_only", False) is True or getattr(store, "readonly", False) is True:
            raise NativeRuleMergeError("writable_rule_store_required")
        return store

    # ---- authority / manifest -------------------------------------------
    def _access_context(self, raw: Any) -> AccessContext:
        try:
            authority = resolve_native_transport_context(raw)
        except NativeContextError as exc:
            raise NativeRuleMergeError("native_trusted_capability_required") from exc
        workspace = _assert_safe_path(authority.workspace_id or self.workspace, allow_missing=True)
        if workspace != self.workspace:
            raise NativeRuleMergeError("context_workspace_mismatch")
        if not authority.agent_instance_id:
            raise NativeRuleMergeError("context_identity_required")
        context = AccessContext(
            trusted_agent_id=authority.agent_instance_id,
            is_admin=bool(authority.admin),
            strict_binding=True,
            allow_anon=False,
            session_id=authority.session_id,
            session_source=authority.session_source,
            session_trusted=authority.session_trusted,
        )
        ok, reason = context.require_capability_issue()
        if not ok:
            if "session" in reason.casefold():
                raise NativeRuleMergeError("native_trusted_session_required")
            raise NativeRuleMergeError("native_admin_capability_required")
        return context

    @staticmethod
    def _provider_snapshot(provider: Any) -> tuple[str, int]:
        try:
            value = provider.current() if callable(getattr(provider, "current", None)) else (
                provider() if callable(provider) else provider
            )
            if isinstance(value, Mapping):
                state, generation = value.get("state"), value.get("generation")
            else:
                state, generation = getattr(value, "state", None), getattr(value, "generation", None)
            state_text = str(getattr(state, "value", state) or "").strip().upper()
            if type(generation) is not int or generation < 0:
                raise ValueError
            return state_text, generation
        except Exception as exc:
            raise NativeRuleMergeError("v2_manifest_state_unavailable") from exc

    def _state_gate(self, generation: Any, state: Any) -> tuple[str, int]:
        if type(generation) is not int or generation < 0:
            raise NativeRuleMergeError("invalid_manifest_generation")
        if self.state_provider is None:
            raise NativeRuleMergeError("v2_state_provider_required")
        provider_state, provider_generation = self._provider_snapshot(self.state_provider)
        supplied_state = str(getattr(state, "value", state) or "").strip().upper()
        if supplied_state and supplied_state != provider_state:
            raise NativeRuleMergeError("manifest_state_mismatch")
        if provider_generation != generation:
            raise NativeRuleMergeError("manifest_generation_mismatch")
        if provider_state != "V2_ACTIVE":
            raise NativeRuleMergeError("v2_not_active")
        return provider_state, provider_generation

    def _assert_state_unchanged(self, snapshot: tuple[str, int]) -> None:
        current = self._provider_snapshot(self.state_provider)
        if current != snapshot:
            raise NativeRuleMergeError("manifest_generation_conflict")

    # ---- safe store lifecycle -------------------------------------------
    def _preflight_schema(self) -> None:
        db_path = _assert_safe_path(self.db_path, allow_missing=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            sidecar = Path(str(db_path) + suffix)
            if _is_reparse_or_symlink(sidecar):
                raise NativeRuleMergeError("rule_merge_path_reparse_or_symlink")
        if not db_path.is_file() or db_path.stat().st_size == 0:
            raise NativeRuleMergeError("rule_merge_schema_missing")
        uri = "file:" + str(db_path).replace("\\", "/") + "?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if type(version) is not int or version < 0:
                raise NativeRuleMergeError("rule_merge_schema_invalid")
            tables = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required_tables = _V2_REQUIRED_TABLES
            marker = conn.execute(
                "SELECT version,marker FROM rules_schema_meta WHERE schema_id='rules'"
            ).fetchone() if "rules_schema_meta" in tables else None
            if marker is None:
                raise NativeRuleMergeError("rule_merge_schema_invalid")
            marker_version = int(marker["version"] or 0)
            if marker_version > RULES_SCHEMA_VERSION:
                raise NativeRuleMergeError("rule_merge_schema_future")
            if str(marker["marker"] or "") != RULES_SCHEMA_MARKER or marker_version != RULES_SCHEMA_VERSION:
                raise NativeRuleMergeError("rule_merge_schema_invalid")
            for table, required in required_tables.items():
                if table not in tables:
                    raise NativeRuleMergeError("rule_merge_schema_partial")
                columns = {
                    str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
                }
                if not required <= columns:
                    raise NativeRuleMergeError("rule_merge_schema_partial")
        except NativeRuleMergeError:
            raise
        except (sqlite3.DatabaseError, OSError, ValueError, TypeError) as exc:
            raise NativeRuleMergeError("rule_merge_schema_invalid") from exc
        finally:
            if conn is not None:
                conn.close()

    def _store(self) -> Any:
        if self._rule_store is not None:
            self._preflight_schema()
            return self._rule_store
        self._preflight_schema()
        try:
            return _V2RuleMergeStore(self.workspace)
        except NativeRuleMergeError:
            raise
        except (sqlite3.DatabaseError, OSError, ValueError, TypeError, RuntimeError) as exc:
            raise NativeRuleMergeError("rule_merge_store_unavailable") from exc

    # ---- payload / idempotency ------------------------------------------
    @staticmethod
    def _payload(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise NativeRuleMergeError("invalid_rule_merge_arguments")
        return {str(key): item for key, item in value.items() if str(key) not in _IDENTITY_KEYS}

    @staticmethod
    def _receipt(payload: Mapping[str, Any], supplied: Any) -> _MutationReceipt:
        value = supplied if supplied is not None else payload.get("mutation_receipt", payload.get("receipt"))
        if not isinstance(value, Mapping):
            raise NativeRuleMergeError("mutation_receipt_required")
        values = {str(key): item for key, item in value.items()}
        receipt_id = values.get("receipt_id") or values.get("id")
        if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id.strip()) > 256:
            raise NativeRuleMergeError("mutation_receipt_required")
        return _MutationReceipt(receipt_id.strip(), values)

    @staticmethod
    def _idempotency_key(payload: Mapping[str, Any], supplied: Any) -> str:
        value = supplied if supplied not in (None, "") else payload.get("idempotency_key")
        return _text(value, field="idempotency_key", max_len=256)

    @staticmethod
    def _proposal_id(payload: Mapping[str, Any]) -> str:
        proposal_id = _text(payload.get("proposal_id"), field="proposal_id", max_len=256)
        if not proposal_id:
            raise NativeRuleMergeError("proposal_id_required")
        return proposal_id

    def _fingerprint(
        self,
        operation: str,
        payload: Mapping[str, Any],
        receipt: _MutationReceipt,
        context: AccessContext,
        generation: int,
    ) -> str:
        clean = dict(payload)
        token = clean.get("capability_token")
        if isinstance(token, str):
            clean["capability_token"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if operation == "capability_issue":
            secret = clean.pop("recovery_secret", None)
            if secret is None:
                raise NativeRuleMergeError("recovery_secret_required")
            try:
                clean["recovery_secret_proof"] = recovery_secret_proof(secret)
            except CapabilityIssueError as exc:
                raise NativeRuleMergeError("recovery_secret_invalid") from exc
            clean.setdefault("scope", CAPABILITY_SCOPE)
        clean.pop("mutation_receipt", None)
        clean.pop("receipt", None)
        clean.pop("idempotency_key", None)
        facts = {
            "workspace": self.workspace,
            "operation": operation,
            "payload": clean,
            "receipt": receipt.values,
            "receipt_id": receipt.receipt_id,
            "principal": context.principal,
            "generation": generation,
            "scope": str(clean.get("scope") or CAPABILITY_SCOPE),
        }
        return hashlib.sha256(_canonical_json(facts).encode("utf-8")).hexdigest()

    @staticmethod
    def _ledger_result(result: Mapping[str, Any], operation: str) -> str:
        # A capability issue result contains the one-time bearer token.  Never
        # persist that token; restart replays return a safe receipt projection
        # without pretending a new token was issued.
        projection = dict(result)
        if operation == "capability_issue":
            projection.pop("capability_token", None)
        return _canonical_json(projection)

    @staticmethod
    def _ledger_replay(row: sqlite3.Row, operation: str) -> dict[str, Any]:
        try:
            result = json.loads(str(row["result_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NativeRuleMergeError("idempotency_ledger_invalid") from exc
        if not isinstance(result, dict):
            raise NativeRuleMergeError("idempotency_ledger_invalid")
        if operation == "capability_issue":
            # Never persist/return the raw bearer token.  A successful replay
            # without it strands a caller after a lost response, while minting
            # another token would accumulate unreconciled grants.  Keep this
            # operation an explicit registry blocker until a workspace key
            # envelope/revocation protocol exists.
            raise NativeRuleMergeError("capability_replay_unavailable")
        result["idempotent_replay"] = True
        return result

    def _replay_capability_issue(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        payload: Mapping[str, Any],
        *,
        access: AccessContext,
        generation: int,
        request_key: str,
    ) -> dict[str, Any]:
        """Recover the original deterministic bearer without minting again."""

        secret = payload.get("recovery_secret")
        proposal_id = self._proposal_id(payload)
        try:
            token = capability_recovery_token(
                secret,
                workspace=str(self.workspace),
                principal=access.principal,
                proposal_id=proposal_id,
                request_key=request_key,
                manifest_generation=generation,
                scope=str(payload.get("scope") or CAPABILITY_SCOPE),
            )
            proof = recovery_secret_proof(secret)
        except CapabilityIssueError as exc:
            raise NativeRuleMergeError("recovery_secret_invalid") from exc
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        tables = {
            str(item[0]) for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "rule_governance_capabilities" in tables:
            capability = conn.execute(
                "SELECT capability_id,principal,scope_json,proposal_id,expires_at,consumed_at,token_digest,metadata_json FROM rule_governance_capabilities WHERE token_digest=?",
                (token_hash,),
            ).fetchone()
            if capability is None:
                raise NativeRuleMergeError("capability_replay_unavailable")
            try:
                scope_data = json.loads(str(capability["scope_json"] or "{}"))
                metadata = json.loads(str(capability["metadata_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise NativeRuleMergeError("capability_replay_unavailable") from exc
            if not isinstance(scope_data, Mapping) or not isinstance(metadata, Mapping):
                raise NativeRuleMergeError("capability_replay_unavailable")
            if (
                str(metadata.get("token_version") or "") != "v2"
                or str(metadata.get("recovery_proof_hash") or "") == ""
                or not hmac.compare_digest(str(metadata.get("recovery_proof_hash") or ""), proof)
                or str(capability["principal"] or "") != access.principal
                or str(scope_data.get("scope") or "") != str(payload.get("scope") or CAPABILITY_SCOPE)
                or str(capability["proposal_id"] or "") != proposal_id
                or bool(str(capability["consumed_at"] or ""))
                or bool(metadata.get("revoked"))
                or _parse_iso_epoch(capability["expires_at"]) <= datetime.now(timezone.utc).timestamp()
                or not hmac.compare_digest(str(capability["token_digest"] or ""), token_hash)
            ):
                raise NativeRuleMergeError("capability_replay_unavailable")
        else:
            capability = conn.execute(
                "SELECT token_hash, principal, scope, proposal_id, expires_at, consumed, revoked, "
                "recovery_proof_hash, token_version FROM governance_capabilities "
                "WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if capability is None:
                raise NativeRuleMergeError("capability_replay_unavailable")
            if (
                str(capability["token_version"] or "") != "v2"
                or str(capability["recovery_proof_hash"] or "") == ""
                or not hmac.compare_digest(str(capability["recovery_proof_hash"] or ""), proof)
                or str(capability["principal"] or "") != access.principal
                or str(capability["scope"] or "") != str(payload.get("scope") or CAPABILITY_SCOPE)
                or str(capability["proposal_id"] or "") != proposal_id
                or bool(capability["consumed"])
                or bool(capability["revoked"])
                or float(capability["expires_at"] or 0.0) <= datetime.now(timezone.utc).timestamp()
                or not hmac.compare_digest(str(capability["token_hash"] or ""), token_hash)
            ):
                raise NativeRuleMergeError("capability_replay_unavailable")
        return {
            "proposal_id": proposal_id,
            "capability_token": token,
            "token_persistence": "sha256_only",
            "idempotent_replay": True,
        }

    @staticmethod
    def _ledger_reserve(conn: sqlite3.Connection, key: str, fingerprint: str, operation: str) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT request_key, request_fingerprint, operation, schema_version, status, result_json "
            "FROM rule_merge_native_requests WHERE request_key=?",
            (key,),
        ).fetchone()
        if row is not None:
            schema_version = int(row["schema_version"] or 0)
            if schema_version != _NATIVE_REQUEST_SCHEMA_VERSION:
                raise NativeRuleMergeError("idempotency_ledger_invalid")
            if str(row["request_fingerprint"] or "") != fingerprint:
                raise NativeRuleMergeError("idempotency_conflict")
            if str(row["operation"] or "") != operation:
                raise NativeRuleMergeError("idempotency_conflict")
            status = str(row["status"] or "")
            if status == "committed":
                return row
            if status != "pending" or str(row["result_json"] or ""):
                raise NativeRuleMergeError("idempotency_ledger_invalid")
            # A pending row cannot be safely guessed to be a replay.  Because
            # writes share this transaction, it normally only appears after a
            # process crash; fail closed and let maintenance clean it up.
            raise NativeRuleMergeError("idempotency_in_progress")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO rule_merge_native_requests "
            "(request_key, request_fingerprint, operation, schema_version, status, result_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', '', ?, ?)",
            (key, fingerprint, operation, _NATIVE_REQUEST_SCHEMA_VERSION, now, now),
        )
        return None

    @staticmethod
    def _expected_revisions(store: Any, proposal_id: str, payload: Mapping[str, Any]) -> dict[str, int]:
        expected = payload.get("expected_definition_revisions")
        if not isinstance(expected, Mapping) or not expected:
            raise NativeRuleMergeError("proposal_revision_required")
        proposal = store.get_proposal(proposal_id)
        if not proposal:
            raise NativeRuleMergeError("rule_merge_proposal_not_found")
        definition_ids = proposal.get("definition_ids")
        if not isinstance(definition_ids, (list, tuple)) or len(definition_ids) != 2:
            raise NativeRuleMergeError("proposal_revision_invalid")
        normalized: dict[str, int] = {}
        for definition_id in definition_ids:
            value = expected.get(definition_id)
            if type(value) is not int or value < 0:
                raise NativeRuleMergeError("proposal_revision_invalid")
            normalized[str(definition_id)] = value
        if set(str(key) for key in expected) != set(normalized):
            raise NativeRuleMergeError("proposal_revision_invalid")
        return normalized

    @staticmethod
    def _id_list(value: Any, *, field: str) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = _text(value, field=field, max_len=256)
            return [text] if text else []
        if not isinstance(value, list):
            raise NativeRuleMergeError(f"invalid_{field}")
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            text = _text(item, field=field, max_len=256)
            if not text:
                raise NativeRuleMergeError(f"invalid_{field}")
            if text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _rules_store(self, store: Any) -> Any:
        rules = getattr(store, "_store", store)
        if not hasattr(rules, "reconcile_historical_duplicates"):
            raise NativeRuleMergeError("rule_merge_store_unavailable")
        return rules

    def _readonly_rules(self) -> Any:
        self._preflight_schema()
        try:
            return RuleV2Store(self.workspace, read_only=True)
        except NativeRuleMergeError:
            raise
        except (sqlite3.DatabaseError, OSError, ValueError, TypeError, RuntimeError) as exc:
            raise NativeRuleMergeError("rule_merge_store_unavailable") from exc

    def _plan_merge_safe(
        self,
        rules: Any,
        payload: Mapping[str, Any],
        raw_context: Any,
    ) -> _SafeMergePlan:
        authority = resolve_native_transport_context(raw_context)
        group = str(authority.share_group_id or "").strip()
        if not group:
            raise NativeRuleMergeError("context_group_required")
        links = rules.list_source_links(share_group_id=group, status="active")
        by_source: dict[str, set[str]] = {}
        reachable: set[str] = set()
        for link in links:
            source = str(link.get("memory_id") or "").strip()
            definition = str(
                link.get("canonical_definition_id")
                or link.get("original_definition_id")
                or ""
            ).strip()
            if source and definition:
                by_source.setdefault(source, set()).add(definition)
                reachable.add(definition)
        for binding in rules.list_bindings(share_group_id=group, status="active"):
            if str(getattr(binding, "effect", "") or "include") != "include":
                continue
            definition = str(getattr(binding, "definition_id", "") or "").strip()
            if definition:
                reachable.add(definition)
        source_by_definition: dict[str, str] = {}

        def resolve(source_id: str, definition_id: str, *, field: str) -> str:
            source_token = _text(source_id, field=field, max_len=256)
            definition_token = _text(definition_id, field=field, max_len=256)
            resolved = ""
            if source_token:
                matched = by_source.get(source_token) or set()
                if len(matched) != 1:
                    raise NativeRuleMergeError("rule_merge_target_not_found")
                resolved = next(iter(matched))
                source_by_definition.setdefault(resolved, source_token)
            if definition_token:
                if definition_token not in reachable:
                    raise NativeRuleMergeError("rule_merge_target_not_found")
                if resolved and resolved != definition_token:
                    raise NativeRuleMergeError("rule_merge_target_not_found")
                resolved = definition_token
            if not resolved:
                raise NativeRuleMergeError("rule_merge_target_required")
            definition = rules.get_definition(resolved)
            if (
                definition is None
                or str(definition.status or "") != "active"
                or resolved not in reachable
            ):
                raise NativeRuleMergeError("rule_merge_target_not_found")
            if resolved not in source_by_definition:
                matches = [
                    source for source, defs in by_source.items()
                    if resolved in defs
                ]
                if len(matches) == 1:
                    source_by_definition[resolved] = matches[0]
            return resolved

        canonical_source = payload.get("canonical_source_id")
        canonical_definition = payload.get("canonical_definition_id")
        if canonical_source not in (None, "") and not isinstance(canonical_source, str):
            raise NativeRuleMergeError("invalid_canonical_source_id")
        if canonical_definition not in (None, "") and not isinstance(canonical_definition, str):
            raise NativeRuleMergeError("invalid_canonical_definition_id")
        canonical_id = resolve(
            str(canonical_source or ""),
            str(canonical_definition or ""),
            field=(
                "canonical_source_id"
                if str(canonical_source or "").strip()
                else "canonical_definition_id"
            ),
        )
        duplicate_sources = self._id_list(
            payload.get("duplicate_source_ids"), field="duplicate_source_ids",
        )
        duplicate_definitions = self._id_list(
            payload.get("duplicate_definition_ids"), field="duplicate_definition_ids",
        )
        if not duplicate_sources and not duplicate_definitions:
            raise NativeRuleMergeError("rule_merge_duplicate_ids_required")
        ordered_dups: list[str] = []
        seen_dups: set[str] = set()
        for source_id in duplicate_sources:
            item = resolve(source_id, "", field="duplicate_source_ids")
            if item in seen_dups:
                continue
            seen_dups.add(item)
            ordered_dups.append(item)
        for definition_id in duplicate_definitions:
            item = resolve("", definition_id, field="duplicate_definition_ids")
            if item in seen_dups:
                continue
            seen_dups.add(item)
            ordered_dups.append(item)
        if canonical_id in seen_dups:
            raise NativeRuleMergeError("rule_merge_self_merge_rejected")
        if not ordered_dups:
            raise NativeRuleMergeError("rule_merge_duplicate_ids_required")
        return _SafeMergePlan(
            group=group,
            canonical_id=canonical_id,
            ordered_dups=tuple(ordered_dups),
            by_source={key: frozenset(value) for key, value in by_source.items()},
            source_by_definition=dict(source_by_definition),
        )

    @staticmethod
    def _assert_requested_pairs_mergeable(
        merged: Mapping[str, Any],
        ordered_dups: Sequence[str],
    ) -> list[Mapping[str, Any]]:
        details = [
            item for item in (merged.get("details") or ())
            if isinstance(item, Mapping)
        ]
        merged_ids = [
            str(item.get("merged") or "")
            for item in details
            if str(item.get("status") or "") == "merged"
        ]
        if set(merged_ids) != set(ordered_dups):
            raise NativeRuleMergeError("rule_merge_pair_not_mergeable")
        return details

    def _evaluate_merge_pairs(
        self,
        rules: Any,
        plan: _SafeMergePlan,
        *,
        actor: str,
        dry_run: bool,
    ) -> list[Mapping[str, Any]]:
        merged = rules.reconcile_historical_duplicates(
            plan.group,
            actor=actor,
            definition_ids=[plan.canonical_id, *plan.ordered_dups],
            canonical_definition_id=plan.canonical_id,
            dry_run=dry_run,
        )
        return self._assert_requested_pairs_mergeable(merged, plan.ordered_dups)

    def _candidate_record(
        self,
        rules: Any,
        plan: _SafeMergePlan,
        definition_id: str,
        *,
        pair: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        definition = rules.get_definition(definition_id)
        if definition is None:
            raise NativeRuleMergeError("rule_merge_target_not_found")
        record: dict[str, Any] = {
            "source_id": str(plan.source_by_definition.get(definition_id) or ""),
            "definition_id": definition_id,
            "revision": int(definition.revision or 0),
            "strength": str(definition.rule_strength or ""),
            "layer": _runtime_injection_layer(definition),
            "status": str(definition.status or ""),
        }
        if pair is not None:
            canonical = rules.get_definition(plan.canonical_id)
            relation = str(pair.get("relation") or "")
            if not relation and canonical is not None:
                relation = _canonical_relation_kind(
                    str(canonical.canonical_text or ""),
                    str(definition.canonical_text or ""),
            )
            record["relation"] = relation
            record["reason"] = str(pair.get("reason") or "historical_duplicate_fold")
        return record

    def _merge_safe_preview(
        self,
        rules: Any,
        payload: Mapping[str, Any],
        *,
        access: AccessContext,
        raw_context: Any,
    ) -> dict[str, Any]:
        plan = self._plan_merge_safe(rules, payload, raw_context)
        details = self._evaluate_merge_pairs(
            rules, plan, actor=access.principal, dry_run=True,
        )
        pair_by_dup = {
            str(item.get("merged") or ""): item
            for item in details
            if str(item.get("status") or "") == "merged"
        }
        canonical = self._candidate_record(rules, plan, plan.canonical_id)
        duplicates = [
            self._candidate_record(rules, plan, definition_id, pair=pair_by_dup.get(definition_id))
            for definition_id in plan.ordered_dups
        ]
        expected = {
            canonical["definition_id"]: canonical["revision"],
            **{item["definition_id"]: item["revision"] for item in duplicates},
        }
        return {
            "canonical": canonical,
            "duplicates": duplicates,
            "expected_definition_revisions": expected,
        }

    def _merge_safe(
        self,
        store: Any,
        payload: Mapping[str, Any],
        *,
        access: AccessContext,
        raw_context: Any,
    ) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise NativeRuleMergeError("confirmation_required")
        rules = self._rules_store(store)
        plan = self._plan_merge_safe(rules, payload, raw_context)
        canonical_id = plan.canonical_id
        ordered_dups = list(plan.ordered_dups)
        by_source = {key: set(value) for key, value in plan.by_source.items()}

        expected = payload.get("expected_definition_revisions")
        if not isinstance(expected, Mapping) or not expected:
            raise NativeRuleMergeError("definition_revision_required")
        involved = [canonical_id, *ordered_dups]
        normalized: dict[str, int] = {}
        for key, value in expected.items():
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise NativeRuleMergeError("definition_revision_invalid")
            token = str(key or "").strip()
            if not token:
                raise NativeRuleMergeError("definition_revision_invalid")
            if token in involved:
                mapped = token
            else:
                matched = by_source.get(token) or set()
                if len(matched) != 1:
                    raise NativeRuleMergeError("definition_revision_invalid")
                mapped = next(iter(matched))
            if mapped in normalized and normalized[mapped] != value:
                raise NativeRuleMergeError("definition_revision_invalid")
            normalized[mapped] = value
        if set(normalized) != set(involved):
            raise NativeRuleMergeError("definition_revision_invalid")
        for definition_id, revision in normalized.items():
            current = rules.get_definition(definition_id)
            if current is None or int(current.revision or 0) != revision:
                raise NativeRuleMergeError("definition_revision_mismatch")

        details = self._evaluate_merge_pairs(
            rules, plan, actor=access.principal, dry_run=False,
        )

        from ..rule_reconciliation import settle_native_canonical_snapshot

        try:
            settle_native_canonical_snapshot(
                self.workspace, plan.group, store=rules, reconcile=False,
            )
        except RuntimeError as exc:
            raise NativeRuleMergeError(_error_code(exc)) from exc

        decision_ids = [
            str(item.get("decision_id") or "")
            for item in details
            if str(item.get("status") or "") == "merged" and item.get("decision_id")
        ]
        undo_ids = [
            str(item.get("undo_id") or item.get("decision_id") or "")
            for item in details
            if str(item.get("status") or "") == "merged"
        ]
        requested_sources: list[str] = []
        canonical_source = payload.get("canonical_source_id")
        if isinstance(canonical_source, str) and canonical_source.strip():
            requested_sources.append(canonical_source.strip())
        requested_sources.extend(self._id_list(
            payload.get("duplicate_source_ids"), field="duplicate_source_ids",
        ))
        source_ids: list[str] = []
        seen_sources: set[str] = set()
        for item in requested_sources:
            if item and item not in seen_sources:
                seen_sources.add(item)
                source_ids.append(item)
        involved_set = {canonical_id, *ordered_dups}
        for link in rules.list_source_links(share_group_id=plan.group, status="active"):
            definition = str(link.get("canonical_definition_id") or "")
            original = str(link.get("original_definition_id") or "")
            if definition not in involved_set and original not in involved_set:
                continue
            memory_id = str(link.get("memory_id") or "").strip()
            if memory_id and memory_id not in seen_sources:
                seen_sources.add(memory_id)
                source_ids.append(memory_id)
        return {
            "canonical_definition_id": canonical_id,
            "merged_definition_ids": ordered_dups,
            "decision_ids": decision_ids,
            "undo_ids": undo_ids,
            "source_ids": source_ids,
        }

    def _preview(
        self,
        payload: Mapping[str, Any],
        *,
        context: Any,
        generation: Any,
        state: Any,
    ) -> dict[str, Any]:
        try:
            access = self._access_context(context)
            state_snapshot = self._state_gate(generation, state)
            rules = self._readonly_rules()
            result = self._merge_safe_preview(
                rules, payload, access=access, raw_context=context,
            )
            self._assert_state_unchanged(state_snapshot)
            return result
        except NativeRuleMergeError:
            raise
        except Exception as exc:
            raise NativeRuleMergeError(_error_code(exc)) from exc

    # ---- operations -----------------------------------------------------
    def _execute(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        context: Any,
        generation: Any,
        state: Any,
        mutation_receipt: Any,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        if operation in _READ_OPERATIONS:
            raise NativeRuleMergeError("rule_merge_operation_rejected")
        access = self._access_context(context)
        state_snapshot = self._state_gate(generation, state)
        receipt = self._receipt(payload, mutation_receipt)
        key = self._idempotency_key(payload, idempotency_key)
        fingerprint = self._fingerprint(operation, payload, receipt, access, state_snapshot[1])
        store = self._store()
        write_conn_factory = getattr(store, "_write_conn", None)
        if not callable(write_conn_factory):
            raise NativeRuleMergeError("durable_idempotency_unavailable")
        try:
            with write_conn_factory() as conn:
                existing = self._ledger_reserve(conn, key, fingerprint, operation)
                if existing is not None:
                    if operation == "capability_issue":
                        return self._replay_capability_issue(
                            conn,
                            existing,
                            payload,
                            access=access,
                            generation=state_snapshot[1],
                            request_key=key,
                        )
                    return self._ledger_replay(existing, operation)
                if operation == "merge_safe":
                    result = self._merge_safe(
                        store, payload, access=access, raw_context=context,
                    )
                    self._assert_state_unchanged(state_snapshot)
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        "UPDATE rule_merge_native_requests SET status='committed', result_json=?, updated_at=? WHERE request_key=?",
                        (self._ledger_result(result, operation), now, key),
                    )
                    return result
                proposal_id = self._proposal_id(payload)
                if operation == "capability_issue":
                    kwargs: dict[str, Any] = {}
                    if "ttl_seconds" in payload:
                        ttl = payload.get("ttl_seconds")
                        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(float(ttl)) or float(ttl) <= 0:
                            raise NativeRuleMergeError("invalid_ttl_seconds")
                        kwargs["ttl_seconds"] = float(ttl)
                    secret = payload.get("recovery_secret")
                    if secret is None:
                        raise NativeRuleMergeError("recovery_secret_required")
                    kwargs.update(
                        recovery_secret=secret,
                        workspace=str(self.workspace),
                        request_key=key,
                        manifest_generation=state_snapshot[1],
                    )
                    token = store.issue_merge_capability(proposal_id, access, **kwargs)
                    result: dict[str, Any] = {
                        "proposal_id": proposal_id,
                        "capability_token": str(token),
                        "token_persistence": "sha256_only",
                    }
                elif operation == "approve":
                    token = _text(payload.get("capability_token"), field="capability_token", max_len=512)
                    if not token:
                        raise NativeRuleMergeError("capability_token_required")
                    revisions = self._expected_revisions(store, proposal_id, payload)
                    result = dict(store.approve_proposal(
                        proposal_id,
                        capability_token=token,
                        expected_definition_revisions=revisions,
                        access_context=access,
                    ))
                elif operation == "acknowledge":
                    token = _text(payload.get("capability_token"), field="capability_token", max_len=512)
                    if not token:
                        raise NativeRuleMergeError("capability_token_required")
                    if store.get_proposal(proposal_id) is None:
                        raise NativeRuleMergeError("rule_merge_proposal_not_found")
                    result = dict(store.acknowledge_first_merge(
                        proposal_id,
                        actor=access.principal,
                        capability_token=token,
                        access_context=access,
                    ) or {})
                else:
                    token = _text(payload.get("capability_token"), field="capability_token", max_len=512)
                    if not token:
                        raise NativeRuleMergeError("capability_token_required")
                    if store.get_proposal(proposal_id) is None:
                        raise NativeRuleMergeError("rule_merge_proposal_not_found")
                    result = dict(store.clear_proposal_cooldown(
                        proposal_id,
                        capability_token=token,
                        access_context=access,
                    ) or {})
                self._assert_state_unchanged(state_snapshot)
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE rule_merge_native_requests SET status='committed', result_json=?, updated_at=? WHERE request_key=?",
                    (self._ledger_result(result, operation), now, key),
                )
        except NativeRuleMergeError:
            raise
        except sqlite3.IntegrityError as exc:
            # Concurrent reservation of the same key is handled as a stable
            # replay/conflict on the next transaction; do not expose SQLite.
            raise NativeRuleMergeError("idempotency_conflict") from exc
        except Exception as exc:
            raise NativeRuleMergeError(_error_code(exc)) from exc
        return result

    def dispatch(
        self,
        operation: str,
        payload: Any = None,
        *,
        context: Any = None,
        trusted_context: Any = None,
        generation: Any = None,
        state: Any = None,
        mutation_receipt: Any = None,
        idempotency_key: Any = None,
        mutation: bool = True,
    ) -> dict[str, Any]:
        del mutation
        name = _ALIASES.get(str(operation or "").strip().casefold())
        if name is None:
            return {"ok": False, "status": "error", "operation": str(operation or ""), "code": "unknown_rule_merge_operation", "error": "unknown_rule_merge_operation"}
        effective_context = context if context is not None else trusted_context
        try:
            data = self._payload(payload)
            if name in _READ_OPERATIONS:
                result = self._preview(
                    data, context=effective_context, generation=generation, state=state,
                )
            else:
                result = self._execute(
                    name, data, context=effective_context, generation=generation, state=state,
                    mutation_receipt=mutation_receipt, idempotency_key=idempotency_key,
                )
            return {"ok": True, "status": "ok", "operation": name, "data": result}
        except Exception as exc:
            code = _error_code(exc)
            return {"ok": False, "status": "error", "operation": name, "code": code, "error": code}

    # Explicit operation spellings keep registry adapters simple while all
    # calls still pass through the one canonical dispatch gate.
    def capability_issue(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("capability_issue", payload, **kwargs)

    issue = capability_issue

    def approve(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("approve", payload, **kwargs)

    def acknowledge(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("acknowledge", payload, **kwargs)

    def cooldown_clear(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("cooldown_clear", payload, **kwargs)

    def merge_safe(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("merge_safe", payload, **kwargs)

    def merge_safe_preview(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("merge_safe_preview", payload, **kwargs)

    call = dispatch


__all__ = [
    "RULE_MERGE_OPERATIONS",
    "CAPABILITY_ISSUE_REPLAY_SAFE",
    "NativeRuleMergeError",
    "NativeRuleMergeService",
    "bind_native_rule_merge_test_capability",
]
