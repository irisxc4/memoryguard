"""V2-native automatic memory organization.

The service is deliberately group-bound.  It reads candidates through an
explicit V2 admin read scope and mutates only through ``GovernanceV2``.  The
admin context is an internal capability of this already-bound service; the
caller identity is retained in provenance, never inferred from another
group.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Iterable, Mapping, Sequence

from ..governance_v2 import GovernanceV2, V2MutationContext
from ..memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from ..memory.store import stable_digest
from ..sensitive_content import SENSITIVE_PATTERNS
from .dedup import V2DedupMatch, V2SemanticDeduplicator, canonical_hash, canonical_text
from .text_native import VALID_KINDS, classify_kind


_LOCK_GUARD = Lock()
_BODY_LOCKS: dict[str, RLock] = {}
_FORBIDDEN_METADATA_KEYS = frozenset({
    "body", "raw", "raw_content", "content", "text", "full_text",
    "document", "document_body", "document_text", "conversation",
    "conversation_body", "full_transcript", "raw_transcript", "transcript",
})


class OrganizationError(RuntimeError):
    """Stable failure code for the native organizer boundary."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "organization_failed")
        super().__init__(self.code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_for(workspace: Path, group: str, body: str) -> RLock:
    key = stable_digest({
        "workspace": str(workspace),
        "share_group_id": group,
        "body": canonical_text(body),
    })
    with _LOCK_GUARD:
        lock = _BODY_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _BODY_LOCKS[key] = lock
        return lock


def _clean_tree(value: Any, *, key: str = "") -> Any:
    """Keep metadata JSON-safe and prevent a second body storage channel."""

    if key.casefold() in _FORBIDDEN_METADATA_KEYS:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            name = str(raw_key)
            if name.casefold() in _FORBIDDEN_METADATA_KEYS:
                continue
            cleaned = _clean_tree(raw_value, key=name)
            if cleaned is not None:
                result[name] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        return [_clean_tree(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _provenance_key(value: Mapping[str, Any]) -> str:
    return stable_digest({
        "source_ref": value.get("source_ref", ""),
        "source_event_id": value.get("source_event_id", ""),
        "agent_instance_id": value.get("agent_instance_id", ""),
        "digest": value.get("digest", ""),
    })


class V2MemoryOrganizer:
    """Group-scoped automatic write service for V2 memory atoms."""

    def __init__(
        self,
        workspace: str | Path,
        share_group_id: str,
        *,
        memory_store: MemoryAtomStore | None = None,
        governance: Any | None = None,
        rule_store: Any | None = None,
        rule_reconciliation: Any | None = None,
        deduplicator: Any | None = None,
        threshold: float = 0.85,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.share_group_id = str(share_group_id or "").strip()
        if not self.share_group_id:
            raise OrganizationError("share_group_id_required")
        self.store = memory_store or MemoryAtomStore(self.workspace)
        if not isinstance(self.store, MemoryAtomStore):
            raise TypeError("v2 organizer requires MemoryAtomStore")
        self.governance = governance or GovernanceV2(self.workspace, memory_store=self.store)
        if not callable(getattr(self.governance, "put_atom", None)):
            raise TypeError("v2 organizer requires GovernanceV2.put_atom")
        if not callable(getattr(self.governance, "supersede", None)):
            raise TypeError("v2 organizer requires GovernanceV2.supersede")
        self.rule_store = rule_store
        self.rule_reconciliation = rule_reconciliation
        self.scope = MemoryReadScope(
            workspace_id=str(self.workspace),
            share_group_id=self.share_group_id,
            admin=True,
        )
        self.deduplicator = deduplicator or V2SemanticDeduplicator(
            self.store, self.scope, threshold=threshold,
        )
        self.threshold = float(threshold)
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("semantic dedup threshold must be between 0 and 1")
        self._canonical_context = V2MutationContext(
            workspace_id=str(self.workspace),
            share_group_id=self.share_group_id,
            actor="organizer:" + stable_digest(self.share_group_id)[:24],
            admin=True,
            authority="system",
        )

    @staticmethod
    def service_contract() -> dict[str, Any]:
        """Minimal contract for a public ``memory_write`` adapter."""

        return {
            "input": {
                "body": "required string",
                "event_id": "stable source event id; optional when idempotency_key is supplied",
                "agent_instance_id": "caller identity",
                "share_group_id": "must equal the bound service group",
                "metadata": "JSON object without raw body fields",
                "kind": "optional V2 kind",
                "idempotency_key": "optional durable retry key",
            },
            "context": "V2MutationContext or equivalent trusted bound context",
            "output": {
                "atom": "persisted MemoryAtom",
                "actions": "body-free action list",
                "mutation_kind": "created|deduplicated|superseded|conflicted|quarantined",
                "receipt": "V2Decision.to_dict()",
            },
        }

    def _context(
        self,
        value: Any | None,
        *,
        agent_instance_id: str,
        project_ref: str,
        provider: str,
        runtime_role: str,
    ) -> V2MutationContext:
        if value is not None:
            context = V2MutationContext.from_value(value)
            if context.workspace_id != str(self.workspace):
                raise OrganizationError("organization_workspace_forbidden")
            if context.share_group_id != self.share_group_id:
                raise OrganizationError("organization_group_forbidden")
            if agent_instance_id and not context.admin and agent_instance_id != context.agent_instance_id:
                raise OrganizationError("organization_agent_forbidden")
            return context
        agent = str(agent_instance_id or "").strip()
        return V2MutationContext(
            workspace_id=str(self.workspace),
            share_group_id=self.share_group_id,
            agent_instance_id=agent,
            project_ref=str(project_ref or ""),
            provider=str(provider or ""),
            runtime_role=str(runtime_role or ""),
            actor="organizer-caller:" + (agent or "anonymous"),
            admin=True,
            authority="system",
        )

    @staticmethod
    def _secret_match(body: str, metadata: Mapping[str, Any]) -> str:
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(body):
                return pattern.pattern[:80]
        marker = metadata.get("_secret_detected")
        return str(marker or "")[:80]

    @staticmethod
    def _confidence(body: str, kind: str) -> float:
        value = 0.50
        if len(body.strip()) >= 12:
            value += 0.10
        if len(body.strip()) >= 40:
            value += 0.10
        if kind in {"procedure", "project", "correction"}:
            value += 0.05
        if len(body.strip()) < 4:
            value -= 0.20
        return max(0.05, min(0.95, value))

    @staticmethod
    def _is_correction(body: str, metadata: Mapping[str, Any], kind: str) -> bool:
        text = body.casefold()
        markers = ("correction", "actually", "update", "wrong", "instead", "更正", "纠正", "不对", "应该")
        return kind == "correction" or metadata.get("type") == "correction" or any(item in text for item in markers)

    @staticmethod
    def _is_conflict(body: str, candidate: MemoryAtom, kind: str, metadata: Mapping[str, Any]) -> bool:
        if metadata.get("conflict") is True or metadata.get("type") == "conflict":
            return True
        if kind != candidate.kind and canonical_text(body) != canonical_text(candidate.body):
            return True
        text = canonical_text(body)
        old = canonical_text(candidate.body)
        preference_markers = ("prefer", "like", "preference", "偏好", "喜欢")
        return (
            any(item in text for item in preference_markers)
            and any(item in old for item in preference_markers)
            and text != old
        )

    @staticmethod
    def _source_ref(data: Mapping[str, Any], metadata: Mapping[str, Any], event_id: str) -> str:
        return str(
            data.get("source_ref")
            or metadata.get("source_ref")
            or metadata.get("source_id")
            or "event:" + event_id
        )[:512]

    def _prepare(self, data: Mapping[str, Any], *, kind_override: str = "") -> dict[str, Any]:
        body = str(data.get("body") or data.get("raw_content") or "").strip()
        if not body:
            raise OrganizationError("memory_body_required")
        group = str(data.get("share_group_id") or data.get("group_id") or "").strip()
        if group and group != self.share_group_id:
            raise OrganizationError("organization_group_forbidden")
        metadata = _clean_tree(data.get("metadata") or {})
        if not isinstance(metadata, Mapping):
            raise OrganizationError("memory_metadata_invalid")
        metadata = dict(metadata)
        agent = str(data.get("agent_instance_id") or data.get("agent") or "").strip()
        source_ref = self._source_ref(data, metadata, str(data.get("event_id") or ""))
        event_id = str(data.get("event_id") or "").strip()
        if not event_id:
            event_id = "event-" + stable_digest({"body": canonical_text(body), "agent": agent, "source_ref": source_ref})[:32]
        requested_kind = str(kind_override or data.get("kind") or "").strip().casefold()
        kind = requested_kind or classify_kind(body, metadata)
        if kind not in VALID_KINDS:
            # An untrusted/invalid hint must not erase the native classifier's
            # semantic result.  MemoryAtom persists the prepared kind as-is,
            # so the fallback belongs here at the V2 organizer boundary.
            kind = classify_kind(body, metadata)
            if kind not in VALID_KINDS:
                kind = "fact"
        policy = str(data.get("injection_policy") or metadata.get("injection_policy") or "relevant")
        priority = int(data.get("priority", metadata.get("priority", 0)) or 0)
        confidence = float(data.get("confidence") if data.get("confidence") is not None else self._confidence(body, kind))
        confidence = max(0.0, min(1.0, confidence))
        visibility = str(data.get("visibility") or "building").strip().casefold()
        if visibility not in {"building", "ready", "active", "hidden"}:
            raise OrganizationError("memory_visibility_invalid")
        digest = canonical_hash(body)
        provenance = {
            "source_ref": source_ref,
            "source_event_id": event_id,
            "agent_instance_id": agent,
            "share_group_id": self.share_group_id,
            "digest": digest,
            "locator": str(metadata.get("locator") or "event")[:256],
            "source_revision": str(metadata.get("source_revision") or ""),
        }
        evidence = list(data.get("evidence") or ())
        if not evidence and not data.get("evidence_ids"):
            evidence = [{
                "source_ref": source_ref,
                "digest": digest,
                "authority": "observed",
                "metadata": {
                    "source_event_id": event_id,
                    "agent_instance_id": agent,
                    "share_group_id": self.share_group_id,
                },
            }]
        mappings = list(data.get("source_mappings") or ())
        if not mappings:
            mappings = [{
                "source_domain": "auto_organizer",
                "source_ref": source_ref,
                "source_record_id": event_id,
                "source_revision": str(metadata.get("source_revision") or ""),
                "digest": digest,
                "metadata": {
                    "agent_instance_id": agent,
                    "share_group_id": self.share_group_id,
                },
            }]
        return {
            "body": body,
            "metadata": metadata,
            "agent_instance_id": agent,
            "project_ref": str(data.get("project_ref") or ""),
            "provider": str(data.get("provider") or ""),
            "runtime_role": str(data.get("runtime_role") or ""),
            "event_id": event_id,
            "source_ref": source_ref,
            "kind": kind,
            "injection_policy": policy,
            "priority": priority,
            "confidence": confidence,
            # Explicit GUI updates carry the complete atom through the native
            # boundary.  Keep lifecycle state distinct from the organizer's
            # create-time status so a partial update cannot silently reset it.
            "status": str(data.get("status") or ""),
            "locked": data.get("locked") if "locked" in data else None,
            "preserve_provenance": bool(data.get("_preserve_provenance")),
            "visibility": visibility,
            "digest": digest,
            "provenance": provenance,
            "evidence": evidence,
            "evidence_ids": list(data.get("evidence_ids") or ()),
            "source_mappings": mappings,
            "memory_id": str(data.get("memory_id") or "").strip(),
            "idempotency_key": str(data.get("idempotency_key") or "").strip(),
            "write_policy": str(data.get("write_policy") or "auto_accept"),
            "secret_match": self._secret_match(body, metadata),
        }

    def _new_atom(self, prepared: Mapping[str, Any], *, status: str, supersedes: Sequence[str] = (), metadata: Mapping[str, Any] | None = None) -> MemoryAtom:
        body = str(prepared["body"])
        memory_id = str(prepared.get("memory_id") or "")
        if not memory_id:
            memory_id = "memory-" + stable_digest({
                "group": self.share_group_id,
                "body": canonical_text(body),
                "kind": prepared["kind"],
            })[:40]
        atom_metadata = dict(prepared.get("metadata") or {})
        atom_metadata.update({
            "organizer_version": "v2",
            "source_event_id": prepared["event_id"],
            "source_ref": prepared["source_ref"],
        })
        if prepared.get("secret_match"):
            atom_metadata.update({
                "quarantine_id": "quarantine-" + stable_digest(prepared["event_id"])[:32],
                "quarantine_reason": "sensitive content detected",
                "detected_pattern": prepared["secret_match"],
                "quarantined_at": _now(),
            })
        if metadata:
            atom_metadata.update(dict(metadata))
        return MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind=str(prepared["kind"]),
            status=status,
            confidence=float(prepared["confidence"]),
            locked=bool(prepared.get("locked", False)),
            injection_policy=str(prepared["injection_policy"]),
            priority=int(prepared["priority"]),
            canonical_hash=str(prepared["digest"]),
            dedup_domain=str(prepared["injection_policy"]),
            supersedes=list(supersedes),
            provenance=[dict(prepared["provenance"])],
            # Keep the trusted writer scope on the atom for compatibility and
            # ownership checks.  Native audience metadata is the explicit
            # visibility expansion; provenance remains the complete list of
            # contributing agents after a cross-agent merge.
            agent_instance_id=str(prepared.get("agent_instance_id") or ""),
            share_group_id=self.share_group_id,
            project_ref=str(prepared.get("project_ref") or ""),
            provider=str(prepared.get("provider") or ""),
            runtime_role=str(prepared.get("runtime_role") or ""),
            workspace_id=str(self.workspace),
            visibility=str(prepared.get("visibility") or "building"),
            metadata=atom_metadata,
        )

    @staticmethod
    def _receipt(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if hasattr(value, "to_dict"):
            return dict(value.to_dict())
        if isinstance(value, Mapping):
            return dict(value)
        return {"value": str(value)}

    @staticmethod
    def _idempotency_request(prepared: Mapping[str, Any]) -> dict[str, Any]:
        """Return request intent, excluding organizer-owned post-state."""

        metadata = dict(prepared.get("metadata") or {})
        # These fields are added while materializing or merging an atom.  They
        # must not make a retry look like a new request.
        for key in (
            "organizer_version",
            "source_event_id",
            "last_source_event_id",
            "quarantine_id",
            "quarantine_reason",
            "detected_pattern",
            "quarantined_at",
            "conflict_group_id",
            "conflict_peer_ids",
            "conflict_reason",
            "proposal_target_id",
            "manual_override_target_id",
            "idempotency_key",
        ):
            metadata.pop(key, None)
        return {
            "memory_id": str(prepared.get("memory_id") or ""),
            "body": str(prepared.get("body") or ""),
            "kind": str(prepared.get("kind") or ""),
            "injection_policy": str(prepared.get("injection_policy") or ""),
            "priority": int(prepared.get("priority") or 0),
            "confidence": float(prepared.get("confidence") or 0.0),
            "visibility": str(prepared.get("visibility") or ""),
            "event_id": str(prepared.get("event_id") or ""),
            "source_ref": str(prepared.get("source_ref") or ""),
            "metadata": metadata,
            "evidence": deepcopy(prepared.get("evidence") or []),
            "evidence_ids": list(prepared.get("evidence_ids") or []),
            "source_mappings": deepcopy(prepared.get("source_mappings") or []),
            "write_policy": str(prepared.get("write_policy") or ""),
        }

    def _put(self, atom: MemoryAtom, prepared: Mapping[str, Any], *, reason: str, key: str) -> tuple[MemoryAtom, dict[str, Any] | None]:
        result = self.governance.put_atom(
            atom,
            context=self._canonical_context,
            evidence=prepared.get("evidence"),
            evidence_ids=prepared.get("evidence_ids"),
            source_mappings=prepared.get("source_mappings"),
            reason=reason,
            confidence=float(prepared["confidence"]),
            idempotency_key=key,
            request_payload=self._idempotency_request(prepared),
        )
        if isinstance(result, tuple):
            persisted = result[0]
            receipt = self._receipt(result[1] if len(result) > 1 else None)
        elif isinstance(result, Mapping):
            persisted = result.get("atom") or result.get("memory") or result
            receipt = self._receipt(result.get("receipt"))
        else:
            persisted, receipt = result, None
        if not isinstance(persisted, MemoryAtom):
            persisted = MemoryAtom.from_value(persisted)
        return persisted, receipt

    def _merge(self, candidate: MemoryAtom, prepared: Mapping[str, Any]) -> tuple[MemoryAtom, list[dict[str, Any]], dict[str, Any] | None, bool]:
        incoming = dict(prepared["provenance"])
        incoming_key = _provenance_key(incoming)
        existing = {_provenance_key(item) for item in candidate.provenance}
        # A native update carries the existing logical memory_id.  It must be
        # an explicit governed revision even when the body is unchanged: the
        # injection policy and priority are mutable V2 fields, not dedup
        # metadata that may be silently retained from the prior revision.
        explicit_update = str(prepared.get("memory_id") or "") == candidate.memory_id
        if explicit_update:
            provenance = list(candidate.provenance)
            if not prepared.get("preserve_provenance") and incoming_key not in existing:
                provenance.append(incoming)
            status = str(prepared.get("status") or candidate.status)
            locked = candidate.locked if prepared.get("locked") is None else bool(prepared.get("locked"))
            metadata = (
                dict(candidate.metadata)
                if prepared.get("preserve_provenance")
                else {**dict(candidate.metadata), **dict(prepared.get("metadata") or {})}
            )
            updated = replace(
                candidate,
                body=str(prepared["body"]),
                provenance=provenance,
                status=status,
                locked=locked,
                injection_policy=str(prepared["injection_policy"]),
                dedup_domain=str(prepared["injection_policy"]),
                priority=int(prepared["priority"]),
                confidence=max(float(candidate.confidence), float(prepared["confidence"])),
                canonical_hash=str(prepared["digest"]),
                metadata=metadata,
            )
            key = str(prepared.get("idempotency_key") or "") or (
                "update:" + stable_digest({"atom": candidate.atom_id, "event": prepared["event_id"]})
            )
            persisted, receipt = self._put(
                updated,
                prepared,
                reason="native memory update",
                key=key,
            )
            return persisted, [{"action": "update", "target_id": candidate.memory_id}], receipt, False
        if incoming_key in existing:
            # Replay the original governed put request so callers receive
            # the durable receipt generated for the same idempotency key.
            # This remains a GovernanceV2 mutation boundary; it is not a
            # direct store shortcut or an unrecorded deduplication.
            replay_key = str(prepared.get("idempotency_key") or "")
            if not replay_key:
                replay_key = "create:" + stable_digest({
                    "group": self.share_group_id,
                    "event": prepared["event_id"],
                    "digest": prepared["digest"],
                })
            persisted, receipt = self._put(
                candidate,
                prepared,
                reason="automatic memory organization",
                key=replay_key,
            )
            return candidate if persisted is None else persisted, [{"action": "idempotent_replay", "target_id": candidate.memory_id}], receipt, True
        provenance = list(candidate.provenance) + [incoming]
        updated = replace(
            candidate,
            provenance=provenance,
            confidence=max(float(candidate.confidence), float(prepared["confidence"])),
            priority=max(int(candidate.priority), int(prepared["priority"])),
            metadata={**dict(candidate.metadata), "last_source_event_id": prepared["event_id"]},
        )
        key = "merge:" + stable_digest({"atom": candidate.atom_id, "provenance": incoming_key})
        persisted, receipt = self._put(updated, prepared, reason="automatic canonical provenance merge", key=key)
        return persisted, [{"action": "merge_provenance", "target_id": candidate.memory_id}], receipt, False

    def _find(self, prepared: Mapping[str, Any]) -> list[V2DedupMatch]:
        threshold = 1.0 if prepared.get("secret_match") else self.threshold
        if self._is_correction(str(prepared["body"]), prepared["metadata"], str(prepared["kind"])):
            threshold = min(threshold, 0.60)
        statuses = {"active", "low_confidence", "conflicted", "deleted", "quarantined", "shadowed"}
        if prepared.get("secret_match"):
            statuses.add("quarantined")
        found = self.deduplicator.find(
            str(prepared["body"]), threshold=threshold, statuses=statuses,
        )
        if prepared.get("secret_match"):
            found = [item for item in found if item.exact]
        return found

    def write(self, payload: Mapping[str, Any] | None = None, *, context: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        # Organizer normalization is allowed to consume nested metadata,
        # evidence, and source mappings.  Keep caller-owned request data
        # immutable even when this service is called below NativePort.
        data = deepcopy(dict(payload or {}))
        data.update({
            key: deepcopy(value)
            for key, value in kwargs.items()
            if value is not None
        })
        prepared = self._prepare(data, kind_override=str(data.get("kind_override") or ""))
        caller = self._context(
            context,
            agent_instance_id=prepared["agent_instance_id"],
            project_ref=prepared["project_ref"],
            provider=prepared["provider"],
            runtime_role=prepared["runtime_role"],
        )
        # The service has a fixed group; caller context is checked above and
        # only contributes identity to provenance.  Mutations use the fixed
        # internal group capability, never a payload-selected group.
        del caller
        requested_memory_id = str(prepared.get("memory_id") or "").strip()
        explicit_update = False
        lock = _lock_for(self.workspace, self.share_group_id, str(prepared["body"]))
        with lock:
            matches = self._find(prepared)
            selected_match = matches[0] if matches else None
            candidate = selected_match.atom if selected_match else None
            if requested_memory_id:
                requested = self.store.get_atom(
                    requested_memory_id,
                    scope=self.scope,
                    include_building=True,
                )
                if requested is not None:
                    # An existing logical id is an explicit governed update.
                    # A new id must not hide an exact/semantic candidate found
                    # above; public writes may carry client ids and still
                    # deduplicate within the trusted group.
                    candidate = requested
                    selected_match = None
                    explicit_update = True
            elif candidate is None:
                candidate = None
            actions: list[dict[str, Any]] = [{
                "action": "classify",
                "kind": prepared["kind"],
                "confidence": prepared["confidence"],
            }]
            exact_candidate = bool(selected_match and selected_match.exact)
            if explicit_update and candidate is not None:
                persisted, merge_actions, receipt, replay = self._merge(candidate, prepared)
                actions.extend(merge_actions)
                return {
                    "ok": True,
                    "status": persisted.status,
                    "atom": persisted,
                    "memory_id": persisted.memory_id,
                    "actions": actions,
                    "mutation_kind": "deduplicated",
                    "receipt": receipt,
                    "idempotent_replay": replay,
                }
            is_correction = self._is_correction(
                str(prepared["body"]), prepared["metadata"], str(prepared["kind"]),
            )
            if (
                candidate is not None
                and not explicit_update
                and not is_correction
                and (candidate.locked or candidate.status in {"deleted", "quarantined", "shadowed"})
            ):
                suppression_key = "manual-suppressed:" + stable_digest({
                    "atom_id": candidate.atom_id,
                    "event_id": prepared["event_id"],
                    "body": prepared["body"],
                })
                decision = self.governance.record_deduplication(
                    candidate,
                    context=self._canonical_context,
                    request_payload={"event_id": prepared["event_id"], "body_digest": prepared["digest"]},
                    reason="manual override suppresses automatic duplicate",
                    confidence=float(prepared["confidence"]),
                    idempotency_key=suppression_key,
                )
                actions.append({"action": "manual_override_suppressed", "target_id": candidate.memory_id})
                return {
                    "ok": True,
                    "status": candidate.status,
                    "atom": candidate,
                    "memory_id": candidate.memory_id,
                    "actions": actions,
                    "mutation_kind": "deduplicated",
                    "receipt": self._receipt(decision),
                    "idempotent_replay": bool(decision.status == "applied" and decision.idempotency_key == suppression_key),
                }
            if candidate is not None and not is_correction:
                if not exact_candidate and self._is_conflict(str(prepared["body"]), candidate, str(prepared["kind"]), prepared["metadata"]):
                    conflict_id = "conflict-" + stable_digest({"group": self.share_group_id, "members": sorted([candidate.memory_id, str(prepared.get("memory_id") or prepared["digest"])])})[:32]
                    conflict_metadata = {
                        "conflict_group_id": conflict_id,
                        "conflict_peer_ids": [candidate.memory_id],
                        "conflict_reason": "automatic semantic conflict",
                    }
                    if candidate.status != "conflicted" and not candidate.locked:
                        candidate, receipt = self._put(
                            replace(candidate, status="conflicted", metadata={**dict(candidate.metadata), **conflict_metadata}),
                            prepared,
                            reason="automatic conflict marking",
                            key="conflict-peer:" + conflict_id,
                        )
                    atom = self._new_atom(prepared, status="conflicted", metadata=conflict_metadata)
                    persisted, receipt = self._put(atom, prepared, reason="automatic semantic conflict", key="conflict:" + stable_digest({"group": self.share_group_id, "event": prepared["event_id"]}))
                    actions.append({"action": "conflict", "conflict_group_id": conflict_id, "old_ids": [candidate.memory_id]})
                    return {"ok": True, "status": persisted.status, "atom": persisted, "memory_id": persisted.memory_id, "actions": actions, "mutation_kind": "conflicted", "receipt": receipt}
                persisted, merge_actions, receipt, replay = self._merge(candidate, prepared)
                actions.extend(merge_actions)
                return {"ok": True, "status": persisted.status, "atom": persisted, "memory_id": persisted.memory_id, "actions": actions, "mutation_kind": "deduplicated", "receipt": receipt, "idempotent_replay": replay}
            if candidate is not None and is_correction and candidate.locked:
                atom = self._new_atom(
                    prepared,
                    status="low_confidence",
                    metadata={"manual_override_target_id": candidate.memory_id},
                )
                persisted, receipt = self._put(
                    atom,
                    prepared,
                    reason="manual override conflict candidate",
                    key="manual-conflict:" + stable_digest({"group": self.share_group_id, "event": prepared["event_id"]}),
                )
                actions.append({"action": "manual_override_conflict_candidate", "target_id": candidate.memory_id})
                return {
                    "ok": True,
                    "status": persisted.status,
                    "atom": persisted,
                    "memory_id": persisted.memory_id,
                    "actions": actions,
                    "mutation_kind": "deduplicated",
                    "receipt": receipt,
                    "idempotent_replay": False,
                }
            if candidate is not None and is_correction and not candidate.locked:
                atom = self._new_atom(prepared, status="active", supersedes=[candidate.memory_id])
                persisted, receipt = self._put(atom, prepared, reason="automatic correction supersede", key="supersede-put:" + stable_digest({"group": self.share_group_id, "event": prepared["event_id"]}))
                self.governance.supersede(
                    candidate.atom_id,
                    persisted.atom_id,
                    context=self._canonical_context,
                    reason="automatic correction supersede",
                    source_ref=str(prepared["source_ref"]),
                    idempotency_key="supersede-edge:" + stable_digest({"old": candidate.atom_id, "new": persisted.atom_id}),
                )
                refreshed = self.store.get_atom(persisted.memory_id, scope=self.scope, include_building=True) or persisted
                actions.append({"action": "supersede", "old_id": candidate.memory_id})
                return {"ok": True, "status": refreshed.status, "atom": refreshed, "memory_id": refreshed.memory_id, "actions": actions, "mutation_kind": "superseded", "receipt": receipt}
            status = "quarantined" if prepared.get("secret_match") else ("low_confidence" if prepared["write_policy"] == "propose_only" or float(prepared["confidence"]) < 0.45 else "active")
            atom = self._new_atom(prepared, status=status)
            persisted, receipt = self._put(atom, prepared, reason="automatic memory organization", key=prepared["idempotency_key"] or "create:" + stable_digest({"group": self.share_group_id, "event": prepared["event_id"], "digest": prepared["digest"]}))
            actions.append({"action": "quarantine" if status == "quarantined" else ("create_low_confidence" if status == "low_confidence" else "create_active")})
            return {"ok": True, "status": persisted.status, "atom": persisted, "memory_id": persisted.memory_id, "actions": actions, "mutation_kind": "quarantined" if status == "quarantined" else "created", "receipt": receipt, "idempotent_replay": False}

    def organize(self, event: Any, *, kind_override: str = "", write_policy: str = "auto_accept", context: Any | None = None) -> tuple[MemoryAtom, list[dict[str, Any]]]:
        payload = {
            "event_id": getattr(event, "event_id", ""),
            "agent_instance_id": getattr(event, "agent_instance_id", ""),
            "share_group_id": getattr(event, "share_group_id", ""),
            "body": getattr(event, "raw_content", getattr(event, "body", "")),
            "metadata": getattr(event, "metadata", {}) or {},
            "injection_policy": getattr(event, "injection_policy", "relevant"),
            "priority": getattr(event, "priority", 0),
            "write_policy": write_policy,
            "kind_override": kind_override,
        }
        result = self.write(payload, context=context)
        if hasattr(event, "auto_actions"):
            event.auto_actions = list(result["actions"])
        return result["atom"], list(result["actions"])

    def plan(self, event: Any, *, kind_override: str = "", write_policy: str = "auto_accept", context: Any | None = None) -> tuple[MemoryAtom, list[dict[str, Any]], str]:
        payload = {
            "event_id": getattr(event, "event_id", ""),
            "agent_instance_id": getattr(event, "agent_instance_id", ""),
            "share_group_id": getattr(event, "share_group_id", ""),
            "body": getattr(event, "raw_content", getattr(event, "body", "")),
            "metadata": getattr(event, "metadata", {}) or {},
            "write_policy": write_policy,
        }
        prepared = self._prepare({**payload, "kind": kind_override})
        self._context(context, agent_instance_id=prepared["agent_instance_id"], project_ref="", provider="", runtime_role="")
        matches = self._find(prepared)
        if matches:
            target = matches[0].atom
            if self._is_correction(str(prepared["body"]), prepared["metadata"], str(prepared["kind"])):
                return self._new_atom(prepared, status="active", supersedes=[target.memory_id]), [{"action": "supersede", "old_id": target.memory_id}], "superseded"
            return self._new_atom(prepared, status="low_confidence", metadata={"proposal_target_id": target.memory_id}), [{"action": "duplicate_proposal", "target_id": target.memory_id}], "deduplicated"
        status = "quarantined" if prepared.get("secret_match") else ("low_confidence" if write_policy == "propose_only" or prepared["confidence"] < 0.45 else "active")
        action = "quarantine" if status == "quarantined" else ("create_low_confidence" if status == "low_confidence" else "create_active")
        return self._new_atom(prepared, status=status), [{"action": action}], "quarantined" if status == "quarantined" else "created"

    def reconcile_rules(self, *args: Any, **kwargs: Any) -> Any:
        if self.rule_reconciliation is None:
            return {"ok": True, "status": "not_configured", "share_group_id": self.share_group_id}
        method = getattr(self.rule_reconciliation, "reconcile", None) or getattr(self.rule_reconciliation, "run", None)
        if not callable(method):
            raise OrganizationError("rule_reconciliation_invalid")
        kwargs.setdefault("share_group_id", self.share_group_id)
        return method(*args, **kwargs)


V2Organizer = V2MemoryOrganizer

__all__ = ["OrganizationError", "V2MemoryOrganizer", "V2Organizer"]
