"""Non-executing V2 skill runtime.

Phase 5 stores and validates skill declarations while the manifest remains
``V2_BUILDING``.  The runtime therefore exposes a deterministic planning
receipt and refuses every execution request.  There is intentionally no
``subprocess``, ``importlib`` or shell invocation in this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping

from .models import (
    ALLOWED_CAPABILITIES,
    SkillAuthorizationError,
    SkillExecutionReceipt,
    SkillMutationContext,
    SkillRuntimeError,
    SkillValidationError,
    stable_hash,
)
from .store import SkillStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SkillRuntime:
    """Safety boundary for a future runtime; never executes an entrypoint."""

    state = "V2_BUILDING"
    ready = False

    def __init__(self, store: SkillStore, *, state: str = "V2_BUILDING") -> None:
        self.store = store
        self.state = str(state or "V2_BUILDING")
        self.ready = self.state == "V2_ACTIVE"

    @staticmethod
    def _requested(values: Iterable[str] | None) -> tuple[str, ...]:
        result = tuple(dict.fromkeys(str(value or "").strip().casefold() for value in (values or ()) if str(value or "").strip()))
        unknown = sorted(set(result) - ALLOWED_CAPABILITIES)
        if unknown:
            raise SkillValidationError("unknown requested skill capability: " + ",".join(unknown))
        return result

    def plan(
        self,
        skill_id: str,
        *,
        context: SkillMutationContext,
        requested_capabilities: Iterable[str] | None = None,
    ) -> SkillExecutionReceipt:
        if not isinstance(context, SkillMutationContext) or type(context._trusted) is not bool or not context._trusted:
            raise SkillAuthorizationError("trusted SkillMutationContext is required for runtime planning")
        requested = self._requested(requested_capabilities)
        item = self.store.get(skill_id, scope=context, include_tombstoned=True)
        if item is None:
            raise KeyError(skill_id)
        allowed = set(item.execution_policy.capabilities) | {cap.capability for cap in item.capabilities}
        if not set(requested).issubset(allowed):
            missing = sorted(set(requested) - allowed)
            raise SkillAuthorizationError("skill capability is not granted: " + ",".join(missing))
        if item.state == "tombstoned":
            reason = "skill is tombstoned"
        elif item.state == "disabled":
            reason = "skill is disabled"
        elif self.state != "V2_ACTIVE" or not self.ready:
            reason = "skill execution is disabled while V2_BUILDING"
        else:
            # A future implementation may add a separate trusted activation
            # gate.  Keeping this branch blocked prevents accidental process
            # execution if a caller passes state=V2_ACTIVE today.
            reason = "skill execution is not implemented in the shadow runtime"
        receipt_id = stable_hash({"skill_id": item.stable_id, "version_id": item.version_id, "requested": requested, "state": self.state})[:40]
        return SkillExecutionReceipt(receipt_id=receipt_id, skill_id=item.stable_id, version_id=item.version_id, status="blocked", reason=reason, requested_capabilities=requested, created_at=_now())

    def execute(self, skill_id: str, *, context: SkillMutationContext, requested_capabilities: Iterable[str] | None = None, **_: object) -> SkillExecutionReceipt:
        receipt = self.plan(skill_id, context=context, requested_capabilities=requested_capabilities)
        raise SkillRuntimeError(receipt.reason or "skill execution is blocked")

    run = execute
    invoke = execute


# Compatibility aliases make the non-executing boundary discoverable without
# exposing any lower-level process API.
SafeSkillRuntime = SkillRuntime
SkillRuntimeBlocked = SkillRuntimeError


__all__ = ["SafeSkillRuntime", "SkillRuntime", "SkillRuntimeBlocked"]
