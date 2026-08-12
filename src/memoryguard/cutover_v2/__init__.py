"""Phase 6-A V2 runtime cutover boundary."""

from .state import (
    CutoverError,
    CutoverState,
    GenerationConflict,
    KNOWN_STATES,
    ManifestUnavailable,
    RuntimeSnapshot,
    RuntimeState,
    ManifestState,
    StateSnapshot,
    CutoverSnapshot,
    UNKNOWN,
    V1_ACTIVE,
    V2_ACTIVE,
    V2_BUILDING,
    V2_READY,
    snapshot_from_port,
    state_snapshot,
)
from .ports import (
    DispatchPort,
    HookPort,
    ManifestPort,
    ReadinessPort,
    RuntimePorts,
    V2Port,
)
from .readiness import ReadinessError, ReadinessEvidence, ReadinessGate, ReadinessResult, stable_digest


def __getattr__(name: str):
    """Lazily expose implementation helpers to keep package imports acyclic."""
    if name == "V2RuntimeFacade":
        from .facade import V2RuntimeFacade
        return V2RuntimeFacade
    if name in {
        "EvidenceBlocker", "ReadinessEvidenceAssembler", "ReadinessEvidenceAssembly",
        "manifest_snapshot_digest", "source_set_digest", "target_snapshot_digest",
    }:
        from . import evidence_assembler
        return getattr(evidence_assembler, name)
    raise AttributeError(name)

__all__ = [
    "CutoverState", "RuntimeState", "ManifestState", "V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE", "UNKNOWN", "KNOWN_STATES",
    "CutoverError", "ManifestUnavailable", "GenerationConflict", "RuntimeSnapshot", "StateSnapshot", "CutoverSnapshot", "snapshot_from_port", "state_snapshot",
    "ManifestPort", "DispatchPort", "V2Port", "HookPort", "ReadinessPort", "RuntimePorts",
    "ReadinessError", "ReadinessEvidence", "ReadinessGate", "ReadinessResult", "stable_digest",
    "EvidenceBlocker", "ReadinessEvidenceAssembler", "ReadinessEvidenceAssembly",
    "source_set_digest", "target_snapshot_digest", "manifest_snapshot_digest",
    "V2RuntimeFacade",
]
