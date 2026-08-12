"""V2-native automatic organizer compatibility surface.

The public class remains import-compatible for callers, but all persistence
now goes through ``MemoryAtomStore`` and the V2 governance boundary.  The
implementation lives in :mod:`memoryguard.runtime_v2.organizer` so native
ports have a small, explicit service seam to call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .memory import MemoryAtom, MemoryAtomStore
from .runtime_v2.dedup import V2DedupMatch
from .runtime_v2.organizer import V2MemoryOrganizer
from .runtime_v2.text_native import classify_kind
from .schema_v3 import MemoryEvent, MemoryKind
from .sensitive_content import SENSITIVE_PATTERNS


# Kept public because policy modules and hosts import this name.
SECRET_PATTERNS = list(SENSITIVE_PATTERNS)


class AutoOrganizer:
    """Thin V2 facade for automatic classification and organization."""

    def __init__(
        self,
        workspace: str | Path,
        share_group_id: str,
        enricher_mode: str | None = None,
        *,
        store: MemoryAtomStore | None = None,
        engine: Any | None = None,
        rule_store: Any | None = None,
        rule_reconciliation: Any | None = None,
        deduplicator: Any | None = None,
        threshold: float = 0.85,
    ) -> None:
        del enricher_mode  # V2 classification is deterministic and native.
        self._service = V2MemoryOrganizer(
            workspace,
            share_group_id,
            memory_store=store,
            governance=engine,
            rule_store=rule_store,
            rule_reconciliation=rule_reconciliation,
            deduplicator=deduplicator,
            threshold=threshold,
        )
        # The public facade may be constructed without injectable dependencies;
        # the official V2 service owns their native assembly.  Never fall back
        # to a retired V1 store or compatibility adapter here.
        self.store = self._service.store
        self.governance = self._service.governance
        self.semantic_dedup = self._service.deduplicator
        self.registry = None

    @property
    def service(self) -> V2MemoryOrganizer:
        """Return the injectable V2 service used by public memory writes."""

        return self._service

    @staticmethod
    def service_contract() -> dict[str, Any]:
        return V2MemoryOrganizer.service_contract()

    def write(self, payload: Mapping[str, Any] | None = None, *, context: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._service.write(payload, context=context, **kwargs)

    def organize(
        self,
        event: MemoryEvent,
        kind_override: str = "",
        write_policy: str = "auto_accept",
        *,
        context: Any | None = None,
    ) -> tuple[MemoryAtom, list[dict[str, Any]]]:
        return self._service.organize(
            event,
            kind_override=kind_override,
            write_policy=write_policy,
            context=context,
        )

    def plan_rule_create(
        self,
        event: MemoryEvent,
        kind_override: str = "",
        write_policy: str = "auto_accept",
        *,
        context: Any | None = None,
    ) -> tuple[MemoryAtom, list[dict[str, Any]], str]:
        return self._service.plan(
            event,
            kind_override=kind_override,
            write_policy=write_policy,
            context=context,
        )

    def reconcile_rules(self, *args: Any, **kwargs: Any) -> Any:
        return self._service.reconcile_rules(*args, **kwargs)

    # Small helper seams retained for integrations that used the old class.
    # None of them access a legacy record store.
    def _get_enricher(self) -> Any:
        raise RuntimeError("v2_native_organizer_has_no_legacy_enricher")

    def _classify(self, content: str) -> MemoryKind:
        return MemoryKind(classify_kind(content))

    def _confidence(self, content: str, kind: MemoryKind | str) -> float:
        return self._service._confidence(content, getattr(kind, "value", str(kind)))

    def _safe_kind(self, kind_str: str, original_content: str) -> MemoryKind:
        try:
            return MemoryKind(str(kind_str))
        except ValueError:
            return self._classify(original_content)

    @staticmethod
    def _safe_confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.5

    @staticmethod
    def _detect_secret(content: str) -> str:
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                return pattern.pattern[:80]
        return ""

    @staticmethod
    def _redact_for_enricher(content: str) -> str:
        result = str(content or "")
        for pattern in SECRET_PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result

    @staticmethod
    def _compress(content: str) -> str:
        return "\n".join(line.strip() for line in str(content or "").splitlines() if line.strip())

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {item for item in str(text or "").casefold().replace("，", " ").split() if item}

    @staticmethod
    def _jaccard(left: set[str], right: set[str]) -> float:
        union = left | right
        return len(left & right) / len(union) if union else 0.0

    def _find_duplicates(
        self,
        content: str,
        threshold: float = 0.85,
        *,
        injection_policy: str = "relevant",
        assignments: list[Any] | None = None,
    ) -> list[MemoryAtom]:
        del injection_policy, assignments
        return [item.atom for item in self.semantic_dedup.find(content, threshold=threshold)]

    def _is_correction(self, event: MemoryEvent, duplicates: list[MemoryAtom]) -> bool:
        del duplicates
        metadata = getattr(event, "metadata", {}) or {}
        return self._service._is_correction(
            getattr(event, "raw_content", ""), metadata, classify_kind(getattr(event, "raw_content", ""), metadata),
        )

    def _is_conflict(self, event: MemoryEvent, duplicates: list[MemoryAtom]) -> bool:
        if not duplicates:
            return False
        metadata = getattr(event, "metadata", {}) or {}
        body = getattr(event, "raw_content", "")
        kind = classify_kind(body, metadata)
        return self._service._is_conflict(body, duplicates[0], kind, metadata)

    @staticmethod
    def _has_kind_conflict(duplicates: list[MemoryAtom], new_kind: MemoryKind | str) -> bool:
        value = getattr(new_kind, "value", str(new_kind))
        return any(str(item.kind) != value for item in duplicates)

    @staticmethod
    def _explain_conflict(content: str, duplicates: list[MemoryAtom]) -> str:
        del content
        return "automatic semantic conflict: " + ",".join(item.memory_id for item in duplicates[:3])

    def _create_record(
        self,
        event: MemoryEvent,
        kind: MemoryKind | str,
        status: Any,
        supersedes: list[str] | None = None,
        confidence: float = 0.5,
    ) -> MemoryAtom:
        data = {
            "event_id": getattr(event, "event_id", ""),
            "agent_instance_id": getattr(event, "agent_instance_id", ""),
            "share_group_id": getattr(event, "share_group_id", ""),
            "body": getattr(event, "raw_content", ""),
            "metadata": getattr(event, "metadata", {}) or {},
            "kind": getattr(kind, "value", str(kind)),
            "confidence": confidence,
        }
        prepared = self._service._prepare(data)
        status_value = getattr(status, "value", str(status))
        return self._service._new_atom(prepared, status=status_value, supersedes=supersedes or ())

    def _append_atom(self, atom: MemoryAtom) -> MemoryAtom:
        evidence = [{"source_ref": "organizer:" + atom.memory_id, "digest": atom.canonical_hash}]
        return self.store.put_atom(
            atom,
            evidence=evidence,
            context=self._service._canonical_context,
        )


__all__ = ["AutoOrganizer", "MemoryEvent", "MemoryKind", "SECRET_PATTERNS"]
