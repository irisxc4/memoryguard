"""Admin-only memory-plane safe atom supersede preview/execute.

Public MCP transport for folding one explicit same-group duplicate atom into
a stronger canonical atom. Mutation is always ``GovernanceV2.supersede``;
this module classifies, authorizes, and proves the pair. It does not invent
a second supersede algorithm or talk to SQLite directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..access_context import AccessContext
from ..memory.store import MemoryAtom, MemoryAtomStore, MemoryReadScope
from .governance_semantics import classify_governance_relation
from .native_ports import (
    NativeContextError,
    NativePortError,
    resolve_native_transport_context,
)


MEMORY_MERGE_OPERATIONS = ("merge_safe", "merge_safe_preview")
_READ_OPERATIONS = frozenset({"merge_safe_preview"})
_ALLOWED_RELATIONS = frozenset({"exact", "equivalent", "update"})
_POLICY_RANK = {"relevant": 0, "always": 1}
_IDENTITY_KEYS = frozenset(
    {
        "workspace_id", "workspace", "agent_instance_id", "agent_id", "agent",
        "trusted_agent_id", "trusted_agent", "share_group_id", "group_id", "group",
        "project_ref", "project_id", "project", "provider", "runtime_role", "runtime",
        "admin", "is_admin", "authority", "trusted_identity", "trusted_context", "identity",
    }
)
_ALIASES = {
    "merge_safe": "merge_safe",
    "merge_safe_preview": "merge_safe_preview",
    "memoryguard_memory_merge_safe": "merge_safe",
    "memoryguard_memory_merge_safe_preview": "merge_safe_preview",
    "memory_merge_safe": "merge_safe",
    "memory_merge_safe_preview": "merge_safe_preview",
    "safe": "merge_safe",
    "safe_preview": "merge_safe_preview",
}


class NativeMemoryMergeError(NativePortError):
    """Stable, non-leaking native memory-merge failure."""


@dataclass(frozen=True)
class _ResolvedPair:
    group: str
    canonical: MemoryAtom
    duplicate: MemoryAtom
    relation: str
    reason: str


def _text(value: Any, *, field: str = "", max_len: int = 256) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise NativeMemoryMergeError(f"invalid_{field or 'value'}")
    result = value.strip()
    if len(result) > max_len:
        raise NativeMemoryMergeError(f"invalid_{field or 'value'}")
    return result


def _error_code(error: Exception) -> str:
    if isinstance(error, NativeMemoryMergeError):
        return error.code
    if isinstance(error, NativeContextError):
        return error.code
    message = str(error or "").casefold()
    if "idempotency" in message or "request" in message:
        return "idempotency_conflict"
    if "supersession atom not found" in message:
        return "target_not_found"
    return "memory_merge_operation_failed"


def _atom_view(atom: MemoryAtom) -> dict[str, Any]:
    return {
        "atom_id": atom.atom_id,
        "memory_id": atom.memory_id,
        "revision": int(atom.revision or 0),
        "injection_policy": str(atom.injection_policy or ""),
        "priority": int(atom.priority or 0),
        "status": str(atom.status or ""),
    }


def _policy_rank(value: str) -> int:
    rank = _POLICY_RANK.get(str(value or "").strip().casefold())
    if rank is None:
        raise NativeMemoryMergeError("memory_merge_pair_not_mergeable")
    return rank


class NativeMemoryMergeService:
    """Strict native adapter for one explicit memory-atom supersede pair."""

    requires_state_provider = True

    def __init__(
        self,
        workspace: str | Path,
        *,
        memory_store: Any = None,
        governance: Any = None,
        state_provider: Any = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._memory_store = memory_store
        self._governance = governance
        self.state_provider = state_provider

    def _memory(self) -> MemoryAtomStore:
        if self._memory_store is not None:
            return self._memory_store
        return MemoryAtomStore(self.workspace)

    def _governance_boundary(self) -> Any:
        if self._governance is not None:
            return self._governance
        from ..governance_v2 import GovernanceV2

        return GovernanceV2(self.workspace, memory_store=self._memory())

    def _access_context(self, raw: Any) -> AccessContext:
        try:
            authority = resolve_native_transport_context(raw)
        except NativeContextError as exc:
            raise NativeMemoryMergeError("native_trusted_capability_required") from exc
        requested = Path(authority.workspace_id or self.workspace).expanduser()
        if requested.resolve() != self.workspace:
            raise NativeMemoryMergeError("context_workspace_mismatch")
        if not authority.agent_instance_id:
            raise NativeMemoryMergeError("context_identity_required")
        if not authority.share_group_id:
            raise NativeMemoryMergeError("context_group_required")
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
                raise NativeMemoryMergeError("native_trusted_session_required")
            raise NativeMemoryMergeError("native_admin_capability_required")
        return context

    def _authority(self, raw: Any) -> Any:
        self._access_context(raw)
        return resolve_native_transport_context(raw)

    def _scope(self, raw: Any) -> MemoryReadScope:
        authority = self._authority(raw)
        return MemoryReadScope(
            workspace_id=str(self.workspace),
            share_group_id=str(authority.share_group_id),
            admin=True,
        )

    def _mutation_context(self, raw: Any) -> dict[str, Any]:
        authority = self._authority(raw)
        return {
            "workspace_id": str(self.workspace),
            "share_group_id": str(authority.share_group_id),
            "agent_instance_id": str(authority.agent_instance_id),
            "project_ref": str(authority.project_ref or ""),
            "provider": str(authority.provider or ""),
            "runtime_role": str(authority.runtime_role or ""),
            "actor": str(authority.agent_instance_id),
            "admin": True,
            "authority": "admin",
        }

    @staticmethod
    def _payload(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise NativeMemoryMergeError("invalid_memory_merge_arguments")
        return {str(key): item for key, item in value.items() if str(key) not in _IDENTITY_KEYS}

    def _optional_id(self, payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if value in (None, ""):
            return ""
        return _text(value, field=key)

    def _lookup(
        self,
        store: MemoryAtomStore,
        scope: MemoryReadScope,
        *,
        memory_id: str,
        atom_id: str,
        allowed_status: frozenset[str] = frozenset({"active"}),
    ) -> MemoryAtom:
        if not memory_id and not atom_id:
            raise NativeMemoryMergeError("memory_merge_target_required")
        found: MemoryAtom | None = None
        if atom_id:
            found = store.get_atom(atom_id, scope=scope, atom_id=atom_id)
            if found is None:
                raise NativeMemoryMergeError("target_not_found")
        if memory_id:
            by_memory = store.get_atom(memory_id, scope=scope)
            if by_memory is None:
                raise NativeMemoryMergeError("target_not_found")
            if found is not None and found.atom_id != by_memory.atom_id:
                raise NativeMemoryMergeError("target_not_found")
            found = by_memory
        if found is None or str(found.status or "") not in allowed_status:
            raise NativeMemoryMergeError("target_not_found")
        return found

    def _plan(
        self,
        payload: Mapping[str, Any],
        raw_context: Any,
        *,
        allow_superseded_duplicate: bool = False,
    ) -> _ResolvedPair:
        store = self._memory()
        scope = self._scope(raw_context)
        canonical = self._lookup(
            store,
            scope,
            memory_id=self._optional_id(payload, "canonical_memory_id"),
            atom_id=self._optional_id(payload, "canonical_atom_id"),
        )
        duplicate_status = frozenset({"active", "superseded"}) if allow_superseded_duplicate else frozenset({"active"})
        duplicate = self._lookup(
            store,
            scope,
            memory_id=self._optional_id(payload, "duplicate_memory_id"),
            atom_id=self._optional_id(payload, "duplicate_atom_id"),
            allowed_status=duplicate_status,
        )
        if canonical.atom_id == duplicate.atom_id or canonical.memory_id == duplicate.memory_id:
            raise NativeMemoryMergeError("memory_merge_self_merge_rejected")
        if str(canonical.share_group_id or "") != str(duplicate.share_group_id or ""):
            raise NativeMemoryMergeError("target_not_found")
        relation = classify_governance_relation(canonical.body, duplicate.body)
        if relation.kind not in _ALLOWED_RELATIONS:
            raise NativeMemoryMergeError("memory_merge_pair_not_mergeable")
        if str(canonical.injection_policy or "").casefold() != "always":
            raise NativeMemoryMergeError("memory_merge_pair_not_mergeable")
        if _policy_rank(canonical.injection_policy) < _policy_rank(duplicate.injection_policy):
            raise NativeMemoryMergeError("memory_merge_pair_not_mergeable")
        if int(canonical.priority or 0) < int(duplicate.priority or 0):
            raise NativeMemoryMergeError("memory_merge_pair_not_mergeable")
        return _ResolvedPair(
            group=scope.share_group_id,
            canonical=canonical,
            duplicate=duplicate,
            relation=relation.kind,
            reason=relation.reason,
        )

    @staticmethod
    def _expected_revisions(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        expected = payload.get("expected_atom_revisions")
        if not isinstance(expected, Mapping) or not expected:
            raise NativeMemoryMergeError("atom_revision_required")
        return expected

    def _assert_cas(self, pair: _ResolvedPair, payload: Mapping[str, Any]) -> None:
        expected = self._expected_revisions(payload)
        aliases = {
            pair.canonical.atom_id: pair.canonical,
            pair.canonical.memory_id: pair.canonical,
            pair.duplicate.atom_id: pair.duplicate,
            pair.duplicate.memory_id: pair.duplicate,
        }
        mapped: dict[str, int] = {}
        for key, value in expected.items():
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise NativeMemoryMergeError("atom_revision_invalid")
            token = str(key or "").strip()
            atom = aliases.get(token)
            if atom is None:
                raise NativeMemoryMergeError("atom_revision_invalid")
            if atom.atom_id in mapped and mapped[atom.atom_id] != value:
                raise NativeMemoryMergeError("atom_revision_invalid")
            mapped[atom.atom_id] = value
        required = {pair.canonical.atom_id, pair.duplicate.atom_id}
        if set(mapped) != required:
            raise NativeMemoryMergeError("atom_revision_invalid")
        if mapped[pair.canonical.atom_id] != int(pair.canonical.revision or 0):
            raise NativeMemoryMergeError("atom_revision_mismatch")
        if mapped[pair.duplicate.atom_id] != int(pair.duplicate.revision or 0):
            raise NativeMemoryMergeError("atom_revision_mismatch")

    @staticmethod
    def _receipt_id(payload: Mapping[str, Any]) -> str:
        value = payload.get("mutation_receipt", payload.get("receipt"))
        if not isinstance(value, Mapping):
            raise NativeMemoryMergeError("mutation_receipt_required")
        receipt_id = value.get("receipt_id") or value.get("id")
        token = _text(receipt_id, field="mutation_receipt")
        if not token:
            raise NativeMemoryMergeError("mutation_receipt_required")
        return token

    @staticmethod
    def _idempotency_key(payload: Mapping[str, Any]) -> str:
        token = _text(payload.get("idempotency_key"), field="idempotency_key")
        if not token:
            raise NativeMemoryMergeError("idempotency_key_required")
        return token

    def _preview_body(self, pair: _ResolvedPair) -> dict[str, Any]:
        canonical = _atom_view(pair.canonical)
        duplicate = _atom_view(pair.duplicate)
        return {
            "canonical": canonical,
            "duplicate": duplicate,
            "relation": pair.relation,
            "reason": pair.reason,
            "expected_atom_revisions": {
                canonical["atom_id"]: canonical["revision"],
                duplicate["atom_id"]: duplicate["revision"],
            },
        }

    def _applied_supersede(self, governance: Any, context: Mapping[str, Any], key: str) -> Any:
        actor = str(context.get("actor") or "")
        for decision in governance.list_decisions():
            if (
                str(getattr(decision, "idempotency_key", "") or "") == key
                and str(getattr(decision, "operation", "") or "") == "supersede"
                and str(getattr(decision, "status", "") or "") == "applied"
                and str((getattr(decision, "context", {}) or {}).get("actor") or "") == actor
            ):
                return decision
        return None

    def _execute_result(self, pair: _ResolvedPair, decision: Any, *, replay: bool) -> dict[str, Any]:
        store = self._memory()
        scope = MemoryReadScope(
            workspace_id=str(self.workspace),
            share_group_id=pair.group,
            admin=True,
        )
        canonical = store.get_atom(pair.canonical.memory_id, scope=scope, atom_id=pair.canonical.atom_id) or pair.canonical
        duplicate = store.get_atom(
            pair.duplicate.memory_id,
            scope=scope,
            atom_id=pair.duplicate.atom_id,
            include_building=True,
        ) or pair.duplicate
        body = {
            "canonical": _atom_view(canonical),
            "duplicate": _atom_view(duplicate),
            "relation": pair.relation,
            "reason": pair.reason,
            "decision_id": str(getattr(decision, "decision_id", "") or ""),
            "undo_id": str(getattr(decision, "decision_id", "") or ""),
        }
        if replay:
            body["idempotent_replay"] = True
        return body

    def _preview(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        pair = self._plan(payload, context)
        return self._preview_body(pair)

    def _execute(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise NativeMemoryMergeError("confirmation_required")
        receipt_id = self._receipt_id(payload)
        key = self._idempotency_key(payload)
        pair = self._plan(payload, context, allow_superseded_duplicate=True)
        mutation = self._mutation_context(context)
        governance = self._governance_boundary()
        existing = self._applied_supersede(governance, mutation, key)
        replay = False
        if existing is not None:
            target = dict(getattr(existing, "target", {}) or {})
            if str(target.get("old") or "") != pair.duplicate.atom_id or str(target.get("new") or "") != pair.canonical.atom_id:
                raise NativeMemoryMergeError("idempotency_conflict")
            replay = True
        elif str(pair.duplicate.status or "") != "active":
            raise NativeMemoryMergeError("target_not_found")
        else:
            self._assert_cas(pair, payload)
        try:
            decision = governance.supersede(
                pair.duplicate.atom_id,
                pair.canonical.atom_id,
                context=mutation,
                reason="memory_merge_safe",
                confidence=1.0,
                source_ref=receipt_id,
                idempotency_key=key,
            )
        except NativeMemoryMergeError:
            raise
        except Exception as exc:
            raise NativeMemoryMergeError(_error_code(exc)) from exc
        if existing is not None and str(getattr(existing, "decision_id", "") or "") == str(getattr(decision, "decision_id", "") or ""):
            replay = True
        return self._execute_result(pair, decision, replay=replay)

    def dispatch(
        self,
        operation: str,
        payload: Any = None,
        *,
        context: Any = None,
        trusted_context: Any = None,
        generation: Any = None,
        state: Any = None,
        mutation: bool = True,
        **_: Any,
    ) -> dict[str, Any]:
        del generation, state, mutation
        name = _ALIASES.get(str(operation or "").strip().casefold())
        if name is None:
            return {
                "ok": False,
                "status": "error",
                "operation": str(operation or ""),
                "code": "unknown_memory_merge_operation",
                "error": "unknown_memory_merge_operation",
            }
        effective_context = context if context is not None else trusted_context
        try:
            data = self._payload(payload)
            if name in _READ_OPERATIONS:
                result = self._preview(data, context=effective_context)
            else:
                result = self._execute(data, context=effective_context)
            return {"ok": True, "status": "ok", "operation": name, "data": result}
        except Exception as exc:
            code = _error_code(exc)
            return {"ok": False, "status": "error", "operation": name, "code": code, "error": code}

    def merge_safe(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("merge_safe", payload, **kwargs)

    def merge_safe_preview(self, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.dispatch("merge_safe_preview", payload, **kwargs)

    call = dispatch


__all__ = [
    "MEMORY_MERGE_OPERATIONS",
    "NativeMemoryMergeError",
    "NativeMemoryMergeService",
]
