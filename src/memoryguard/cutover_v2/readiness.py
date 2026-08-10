"""Phase 1/2/4/5 acceptance aggregation for V2 activation.

The gate is deliberately conservative.  Missing, malformed, ``UNKNOWN``,
``NO_SOURCE`` or ``-1`` evidence is *not* converted to a passing zero.  A
ready result carries a stable digest so activation can prove that the exact
evidence it validated is the evidence it commits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Callable, Mapping


class ReadinessError(RuntimeError):
    """Readiness evidence is unavailable or activation is not safe."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten metric mappings while preserving their leaf aliases."""

    result: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = _text(key).casefold().replace("-", "_")
            name = f"{prefix}_{key_text}" if prefix else key_text
            result[name] = item
            result.update(_flatten(item, name))
    return result


def _first(metrics: Mapping[str, Any], names: tuple[str, ...]) -> tuple[Any, str]:
    flat = _flatten(metrics)
    for name in names:
        key = name.casefold().replace("-", "_")
        if key in flat:
            return flat[key], key
    return None, ""


def _metric_values(metrics: Mapping[str, Any], names: tuple[str, ...]) -> list[tuple[Any, str]]:
    """Find top-level or domain-nested aliases deterministically."""

    flat = _flatten(metrics)
    aliases = {name.casefold().replace("-", "_") for name in names}
    generic = {item for item in aliases if "_" not in item}
    found: list[tuple[Any, str]] = []
    for key, value in flat.items():
        leaf = key.rsplit("_", 1)[-1]
        versioned_generic = any(f"_{version}_" in key for version in ("v1", "v2"))
        if key in aliases or (leaf in aliases and not (leaf in generic and versioned_generic)) or any(
            "_" in alias and key.endswith("_" + alias) for alias in aliases
        ):
            found.append((value, key))
    # A direct key wins over nested domain copies; otherwise preserve stable
    # lexical order for an all-domain aggregation.
    found.sort(key=lambda item: (0 if item[1] in aliases else 1, item[1]))
    return found


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        marker = value.strip().upper()
        if marker in {"UNKNOWN", "UNAVAILABLE", "NOT_EVALUATED", "NO_SOURCE", "BLOCKED", "N/A"}:
            return -1
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    return None


def _zero(value: Any) -> bool:
    numeric = _numeric(value)
    return numeric == 0


def _unknown_value(value: Any) -> bool:
    """Return whether an ``unknown_*`` evidence field is unsafe.

    Unknown columns/authoritative reports are counters or collections in
    different producers.  Empty/zero/False means the producer explicitly
    reported none; every positive, malformed, or missing-like value blocks.
    """

    if isinstance(value, Mapping):
        return not value or any(_unknown_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return bool(value)
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    numeric = _numeric(value)
    if numeric is not None:
        return numeric != 0
    marker = _text(value).casefold()
    return marker not in {"", "none", "null", "false", "known", "resolved", "clear"}


@dataclass(frozen=True)
class ReadinessEvidence:
    """Immutable, normalized evidence snapshot used by one gate decision."""

    metrics: Mapping[str, Any] = field(default_factory=dict)
    source_digest: str = ""
    target_digest: str = ""
    manifest_digest: str = ""
    checkpoints: Mapping[str, Any] = field(default_factory=dict)
    validator_passed: bool = False
    migration_id: str = ""
    generation: int | None = None
    raw: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", _freeze(dict(self.metrics or {})))
        object.__setattr__(self, "checkpoints", _freeze(dict(self.checkpoints or {})))

    @classmethod
    def from_value(cls, value: Any, *, manifest: Any = None) -> "ReadinessEvidence":
        if isinstance(value, cls):
            return value
        data = _mapping(value)
        # Acceptance scripts often nest metrics under domains/checks/results.
        metrics = _mapping(data.get("metrics"))
        for key in ("checks", "domains", "validation", "acceptance", "phase1", "phase2", "phase4", "phase5"):
            nested = data.get(key)
            if isinstance(nested, Mapping):
                metrics = {**metrics, key: dict(nested)}
        if not metrics:
            metrics = dict(data)
        if isinstance(manifest, Mapping):
            manifest_data = manifest
        else:
            manifest_data = {name: getattr(manifest, name, None) for name in (
                "source_digest", "target_digest", "manifest_digest", "checkpoints", "migration_id", "generation",
            )} if manifest is not None else {}
        def value_of(name: str, default: Any = "") -> Any:
            return data.get(name, manifest_data.get(name, default))
        digests = _mapping(data.get("digests"))
        source_digest = _text(value_of("source_digest") or digests.get("source_digest"))
        target_digest = _text(value_of("target_digest") or digests.get("target_digest"))
        manifest_digest = _text(value_of("manifest_digest") or digests.get("manifest_digest"))
        checkpoints = _mapping(value_of("checkpoints") or digests.get("checkpoints"))
        validator = value_of("validator_passed", data.get("validated", False))
        if isinstance(validator, str):
            validator = validator.strip().casefold() in {"true", "1", "yes", "pass", "passed"}
        raw_generation = value_of("generation", None)
        generation = None
        if raw_generation is not None and str(raw_generation).strip():
            # Generation is a protocol value, not a coercible user field.
            # Reject bools (``True`` is an int subclass), floats, strings and
            # negative values so evidence cannot be laundered into a valid
            # activation generation.
            if isinstance(raw_generation, bool) or type(raw_generation) is not int or raw_generation < 0:
                raise ValueError("generation must be a non-negative integer")
            generation = raw_generation
        return cls(
            metrics=metrics,
            source_digest=source_digest,
            target_digest=target_digest,
            manifest_digest=manifest_digest,
            checkpoints=checkpoints,
            validator_passed=type(validator) is bool and validator,
            migration_id=_text(value_of("migration_id")),
            generation=generation,
            raw=value,
        )

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": _plain(self.metrics),
            "source_digest": self.source_digest,
            "target_digest": self.target_digest,
            "manifest_digest": self.manifest_digest,
            "checkpoints": _plain(self.checkpoints),
            "validator_passed": self.validator_passed,
            "migration_id": self.migration_id,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    evidence: ReadinessEvidence
    failures: tuple[str, ...] = ()
    checks: Mapping[str, Any] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", tuple(dict.fromkeys(str(item) for item in self.failures if str(item))))
        object.__setattr__(self, "checks", dict(self.checks))
        object.__setattr__(self, "digest", self.digest or self.evidence.digest)

    @property
    def ok(self) -> bool:
        return self.ready

    @property
    def status(self) -> str:
        return "READY" if self.ready else "BLOCKED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "ok": self.ready,
            "status": self.status,
            "failures": list(self.failures),
            "checks": dict(self.checks),
            "evidence": self.evidence.to_dict(),
            "evidence_digest": self.digest,
        }


class ReadinessGate:
    """Aggregate phase acceptance evidence and guard activation/rollback."""

    _ZERO_METRICS = {
        "loss": ("loss", "migration_loss", "loss_count"),
        "orphan": ("orphan", "evidence_orphan", "orphan_count"),
        "outbox": ("outbox", "outbox_pending", "outbox_total_pending", "outbox_failed"),
        "scope": ("scope", "scope_leak", "scope_leak_count", "auto_scope_expansion", "scope_anomaly"),
        "binding": ("binding", "binding_multiset_diff", "binding_identity_multiset_diff", "binding_diff", "binding_leak"),
        "leak": ("leak", "negative_evidence_leak", "runtime_leak", "v1_collision_runtime_leak", "v1_collision_binding_leak"),
    }

    def __init__(self, evidence: Any = None, *, provider: Any = None, manifest: Any = None) -> None:
        self.evidence = evidence
        self.provider = provider
        self.manifest = manifest
        self._last: ReadinessResult | None = None

    @property
    def last(self) -> ReadinessResult | None:
        return self._last

    @property
    def last_result(self) -> ReadinessResult | None:
        return self._last

    def _provided(self, evidence: Any = None) -> Any:
        if evidence is not None:
            return evidence
        provider = self.provider
        if provider is not None:
            for name in ("snapshot", "collect", "readiness", "evidence", "evaluate"):
                method = getattr(provider, name, None)
                if callable(method):
                    return method()
            if callable(provider):
                return provider()
        return self.evidence

    def evaluate(self, evidence: Any = None, *, manifest: Any = None) -> ReadinessResult:
        source = self._provided(evidence)
        try:
            evidence_obj = ReadinessEvidence.from_value(source, manifest=manifest or self.manifest)
        except Exception:
            evidence_obj = ReadinessEvidence(metrics={"unknown": -1})
        metrics = evidence_obj.metrics
        failures: list[str] = []
        checks: dict[str, Any] = {}

        # Any explicit unknown sentinel blocks the gate, even if a sibling
        # metric happens to be zero.
        flat = _flatten(metrics)
        unknown_keys = sorted(
            key
            for key, value in flat.items()
            if _numeric(value) == -1
            or _text(value).upper() in {"UNKNOWN", "UNAVAILABLE", "NOT_EVALUATED", "NO_SOURCE"}
            or ("unknown" in key.casefold() and _unknown_value(value))
        )
        if unknown_keys:
            failures.append("unknown_metric")
        checks["unknown"] = {"ok": not unknown_keys, "keys": unknown_keys}

        for check_name, aliases in self._ZERO_METRICS.items():
            matches = _metric_values(metrics, aliases)
            value, key = matches[0] if matches else (None, "")
            # A nested outbox object is valid only if every reported numeric
            # pending/failed/total counter is explicitly zero.
            if check_name == "outbox" and isinstance(value, Mapping):
                leaves = [item for item in _flatten(value).values() if isinstance(item, (int, float)) and not isinstance(item, bool)]
                passed = bool(leaves) and all(item == 0 for item in leaves)
            elif len(matches) > 1:
                # Domain reports may repeat the same metric.  All reported
                # values must be explicit zero; a single unknown/non-zero
                # domain blocks activation.
                numeric_values = [_numeric(item) for item, _ in matches]
                passed = all(item == 0 for item in numeric_values) and all(item is not None for item in numeric_values)
                value = {key_name: item for item, key_name in matches}
            else:
                passed = value is not None and _zero(value)
            checks[check_name] = {"ok": passed, "value": value, "key": key}
            if not passed:
                failures.append(f"{check_name}_nonzero" if value is not None else f"{check_name}_unavailable")

        mandatory_matches = _metric_values(metrics, ("mandatory_equivalence", "mandatory_equal", "mandatory_equivalent"))
        mandatory = mandatory_matches[0][0] if mandatory_matches else None
        if len(mandatory_matches) > 1:
            mandatory = all(item is True for item, _ in mandatory_matches)
        checks["mandatory_equivalence"] = {"ok": mandatory is True, "value": mandatory}
        if mandatory is not True:
            failures.append("mandatory_equivalence_failed")

        v2_matches = _metric_values(metrics, ("recall_v2", "recall_at_k", "v2_recall", "recall"))
        v1_matches = _metric_values(metrics, ("recall_v1", "v1_recall_at_k", "v1_recall", "baseline_recall"))
        recall_v2 = v2_matches[0][0] if v2_matches else None
        recall_v1 = v1_matches[0][0] if v1_matches else None
        if isinstance(recall_v2, Mapping):
            recall_v2 = recall_v2.get("at_k", recall_v2.get("value"))
        if isinstance(recall_v1, Mapping):
            recall_v1 = recall_v1.get("at_k", recall_v1.get("value"))
        r2, r1 = _numeric(recall_v2), _numeric(recall_v1)
        recall_ok = r2 is not None and r1 is not None and r2 >= r1 and r1 >= 0
        checks["recall"] = {"ok": recall_ok, "v2": recall_v2, "v1": recall_v1}
        if not recall_ok:
            failures.append("recall_below_v1")

        tokens_v2_matches = _metric_values(metrics, ("tokens_v2", "v2_tokens", "context_tokens_v2", "tokens"))
        tokens_v1_matches = _metric_values(metrics, ("tokens_v1", "v1_tokens", "context_tokens_v1", "baseline_tokens"))
        tokens_v2 = tokens_v2_matches[0][0] if tokens_v2_matches else None
        tokens_v1 = tokens_v1_matches[0][0] if tokens_v1_matches else None
        if isinstance(tokens_v2, Mapping):
            tokens_v2 = tokens_v2.get("v2", tokens_v2.get("current"))
        if isinstance(tokens_v1, Mapping):
            tokens_v1 = tokens_v1.get("v1", tokens_v1.get("baseline"))
        t2, t1 = _numeric(tokens_v2), _numeric(tokens_v1)
        tokens_ok = t2 is not None and t1 is not None and t1 >= 0 and t2 >= 0 and t2 < t1
        checks["tokens"] = {"ok": tokens_ok, "v2": tokens_v2, "v1": tokens_v1}
        if not tokens_ok:
            failures.append("tokens_not_lower")

        checks["digests"] = {
            "ok": bool(evidence_obj.source_digest and evidence_obj.target_digest and evidence_obj.manifest_digest),
            "source_digest": evidence_obj.source_digest,
            "target_digest": evidence_obj.target_digest,
            "manifest_digest": evidence_obj.manifest_digest,
        }
        if not checks["digests"]["ok"]:
            failures.append("immutable_digests_missing")
        checks["checkpoints"] = {"ok": bool(evidence_obj.checkpoints), "value": dict(evidence_obj.checkpoints)}
        if not evidence_obj.checkpoints:
            failures.append("checkpoints_missing")
        checks["validator"] = {"ok": evidence_obj.validator_passed}
        if not evidence_obj.validator_passed:
            failures.append("validator_not_passed")

        ready = not failures
        result = ReadinessResult(ready=ready, evidence=evidence_obj, failures=tuple(failures), checks=checks)
        self._last = result
        return result

    check = evaluate
    assess = evaluate

    def readiness(self, evidence: Any = None, **kwargs: Any) -> ReadinessResult:
        return self.evaluate(evidence, **kwargs)

    def activate(self, manifest: Any = None, result: ReadinessResult | None = None, *, expected_generation: int | None = None, snapshot: Any = None) -> Any:
        """Commit V2_ACTIVE only from an unchanged, ready evidence snapshot."""

        current_result = result or self._last or self.evaluate()
        if not current_result.ready:
            raise ReadinessError("v2_readiness_failed:" + ",".join(current_result.failures))
        # A ready result is a capability issued by this gate's most recent
        # evaluate call.  Matching digests alone are insufficient because an
        # attacker can replay a result produced by another gate instance.
        if self._last is None or current_result is not self._last:
            raise ReadinessError("readiness_evidence_not_issued_by_gate")
        target = manifest or self.manifest
        if target is None:
            raise ReadinessError("manifest_port_required")
        try:
            from .state import CutoverState, snapshot_from_port
            snapshot = snapshot or snapshot_from_port(target)
            if not snapshot.available or snapshot.state is not CutoverState.V2_READY:
                raise ReadinessError("activation_requires_v2_ready")
            if expected_generation is not None and snapshot.generation != expected_generation:
                raise ReadinessError("manifest_generation_conflict")
            evidence = current_result.evidence
            if evidence.generation is not None and evidence.generation != snapshot.generation:
                raise ReadinessError("readiness_generation_conflict")
            for field in ("source_digest", "target_digest", "manifest_digest"):
                expected = getattr(snapshot, field)
                supplied = getattr(evidence, field)
                if expected and supplied and expected != supplied:
                    raise ReadinessError("readiness_digest_conflict")
            expected_evidence_digest = snapshot.digests.get("evidence_digest") or snapshot.digests.get("readiness_digest")
            if expected_evidence_digest and expected_evidence_digest != current_result.digest:
                raise ReadinessError("readiness_evidence_digest_conflict")
            if snapshot.checkpoints and evidence.checkpoints and _plain(snapshot.checkpoints) != _plain(evidence.checkpoints):
                raise ReadinessError("readiness_checkpoint_conflict")
            transition = getattr(target, "activate_v2", None) or getattr(target, "transition", None)
            if not callable(transition):
                raise ReadinessError("manifest_transition_unavailable")
            kwargs: dict[str, Any] = {
                "source_digest": evidence.source_digest,
                "target_digest": evidence.target_digest,
                "manifest_digest": evidence.manifest_digest,
                "migration_id": evidence.migration_id,
                "expected_generation": snapshot.generation,
            }
            if getattr(transition, "__name__", "") == "transition":
                return transition(CutoverState.V2_ACTIVE, **kwargs)
            return transition(**kwargs)
        except ReadinessError:
            raise
        except Exception as exc:
            raise ReadinessError("activation_failed") from exc

    def rollback(self, manifest: Any = None, *, reason: str = "v2_cutover_rollback", expected_generation: int | None = None, snapshot: Any = None) -> Any:
        target = manifest or self.manifest
        if target is None:
            raise ReadinessError("manifest_port_required")
        from .state import CutoverState, snapshot_from_port
        snapshot = snapshot or snapshot_from_port(target)
        if not snapshot.available or snapshot.state not in {CutoverState.V2_BUILDING, CutoverState.V2_READY, CutoverState.V2_ACTIVE}:
            raise ReadinessError("rollback_state_unavailable")
        if expected_generation is not None and snapshot.generation != expected_generation:
            raise ReadinessError("manifest_generation_conflict")
        transition = getattr(target, "fail", None) or getattr(target, "transition", None)
        if not callable(transition):
            raise ReadinessError("manifest_transition_unavailable")
        try:
            reason_text = _text(reason) or "v2_cutover_rollback"
            audit = {
                "reason": reason_text,
                "generation": snapshot.generation,
                "source_digest": snapshot.source_digest,
                "target_digest": snapshot.target_digest,
                "manifest_digest": snapshot.manifest_digest,
                "digests": _plain(snapshot.digests),
                "checkpoints": _plain(snapshot.checkpoints),
            }
            audit["evidence_digest"] = stable_digest(audit)
            audit["digest"] = audit["evidence_digest"]
            kwargs = {
                "error": reason_text,
                "migration_id": snapshot.migration_id,
                "source_digest": snapshot.source_digest,
                "target_digest": snapshot.target_digest,
                "manifest_digest": snapshot.manifest_digest,
                "digests": _plain(snapshot.digests),
                "errors": audit,
                "expected_generation": snapshot.generation if expected_generation is None else expected_generation,
            }
            if getattr(transition, "__name__", "") == "transition":
                return transition(CutoverState.V1_ACTIVE, **kwargs)
            return transition(**kwargs)
        except Exception as exc:
            raise ReadinessError("rollback_failed") from exc


__all__ = ["ReadinessError", "ReadinessEvidence", "ReadinessResult", "ReadinessGate", "stable_digest"]
