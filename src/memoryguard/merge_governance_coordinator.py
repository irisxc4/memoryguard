"""Workspace-serialized P2 outbox drain and P3 merge barrier.

The coordinator is deliberately orchestration-only.  Legacy SQLite databases
and the P3 SQLite database remain separate transactions; the workspace lock
serializes their observable mutation window.  The injected P3 merge callback
must keep its own database operation atomic and idempotent.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .governance_lock import WorkspaceGovernanceLock


class ProjectionBarrierState(str, Enum):
    """Terminal result states for one in-memory barrier attempt."""

    COMMITTED = "committed"
    BLOCKED = "blocked"
    FAILED = "failed"


class ProjectionBarrierError(RuntimeError):
    """Raised when a caller requests exception propagation for a failed barrier."""

    def __init__(self, result: "ProjectionBarrierResult") -> None:
        self.result = result
        message = result.error or "projection barrier failed"
        super().__init__(message)


@dataclass(frozen=True)
class ProjectionBarrierSnapshot:
    """Observable state captured while the workspace lock is held."""

    committed_high_water: dict[str, dict[str, Any]]
    projected_high_water: dict[str, dict[str, Any]]
    pending: int
    projection_lag: int
    projection_error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "committed_high_water": self.committed_high_water,
            "projected_high_water": self.projected_high_water,
            "pending": self.pending,
            "projection_lag": self.projection_lag,
            "projection_error": self.projection_error,
        }


@dataclass(frozen=True)
class ProjectionBarrierResult:
    """Explicit outcome of one drain/check/merge/final-check attempt."""

    state: ProjectionBarrierState
    phase: str
    error: str = ""
    exception_type: str = ""
    drain_result: Any = None
    merge_result: Any = None
    before: ProjectionBarrierSnapshot | None = None
    after: ProjectionBarrierSnapshot | None = None

    @property
    def ok(self) -> bool:
        return self.state is ProjectionBarrierState.COMMITTED

    @property
    def status(self) -> str:
        return self.state.value

    @property
    def committed_high_water(self) -> dict[str, dict[str, Any]]:
        return self.before.committed_high_water if self.before else {}

    @property
    def projected_high_water(self) -> dict[str, dict[str, Any]]:
        return self.before.projected_high_water if self.before else {}

    @property
    def final_committed_high_water(self) -> dict[str, dict[str, Any]]:
        return self.after.committed_high_water if self.after else {}

    @property
    def final_projected_high_water(self) -> dict[str, dict[str, Any]]:
        return self.after.projected_high_water if self.after else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "phase": self.phase,
            "ok": self.ok,
            "error": self.error,
            "exception_type": self.exception_type,
            "drain_result": self.drain_result,
            "merge_result": self.merge_result,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class MergeGovernanceCoordinator:
    """Serialize legacy projection and one injected P3 merge callback.

    ``drain_callback`` must consume and checkpoint all legacy events it sees.
    It runs under the same re-entrant workspace lock as producer mutations.
    ``projection_status`` may be a callable or a P3 store exposing
    ``projection_status()``.  A status must expose ``projection_lag`` and
    ``projection_error`` (empty/zero means ready).
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        legacy_stores: Iterable[Any] | Callable[[], Iterable[Any]] | None = None,
        drain_callback: Callable[[], Any] | None = None,
        projection_status: Callable[[], Mapping[str, Any]] | Any | None = None,
        p3_store: Any | None = None,
        lock: WorkspaceGovernanceLock | None = None,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._legacy_stores_source = legacy_stores
        self._drain_callback = drain_callback
        status_source = projection_status if projection_status is not None else p3_store
        if status_source is None:
            self._projection_status_callback = None
        elif callable(status_source):
            self._projection_status_callback = status_source
        elif isinstance(status_source, Mapping):
            self._projection_status_callback = lambda: status_source
        else:
            method = getattr(status_source, "projection_status", None)
            if not callable(method):
                raise TypeError("projection_status must be callable or expose projection_status()")
            self._projection_status_callback = method
        self._lock = lock or WorkspaceGovernanceLock(
            self.workspace,
            timeout=(WorkspaceGovernanceLock.DEFAULT_TIMEOUT if timeout is None else timeout),
            poll_interval=(
                WorkspaceGovernanceLock.DEFAULT_POLL_INTERVAL
                if poll_interval is None else poll_interval
            ),
        )
        self.last_result: ProjectionBarrierResult | None = None

    @property
    def lock(self) -> WorkspaceGovernanceLock:
        """Return lock used by producer, drain and merge operations."""
        return self._lock

    def run_merge(
        self,
        merge_callback: Callable[..., Any],
        *,
        drain_callback: Callable[[], Any] | None = None,
        raise_on_error: bool = False,
    ) -> ProjectionBarrierResult:
        """Run one complete barrier attempt.

        The callback is not called unless the drain and ready checks pass.  A
        callback exception or final high-water drift yields ``FAILED`` and
        releases the lock.  ``raise_on_error`` re-raises such failures as
        ``ProjectionBarrierError`` while retaining ``last_result``.
        """
        if not callable(merge_callback):
            raise TypeError("merge_callback must be callable")
        drain = drain_callback if drain_callback is not None else self._drain_callback
        phase = "draining"
        drain_result: Any = None
        merge_result: Any = None
        before: ProjectionBarrierSnapshot | None = None
        after: ProjectionBarrierSnapshot | None = None
        result: ProjectionBarrierResult | None = None
        try:
            with self._lock:
                if drain is not None:
                    drain_result = self._invoke_noarg(drain)
                phase = "precheck"
                stores = self._legacy_stores()
                before = self._capture(stores)
                reason = self._ready_error(before)
                if reason:
                    result = ProjectionBarrierResult(
                        ProjectionBarrierState.BLOCKED,
                        phase,
                        error=reason,
                        drain_result=drain_result,
                        before=before,
                    )
                else:
                    phase = "merging"
                    merge_result = self._invoke_with_snapshot(merge_callback, before)
                    callback_error = self._merge_result_error(merge_result)
                    if callback_error:
                        result = ProjectionBarrierResult(
                            ProjectionBarrierState.BLOCKED,
                            phase,
                            error=callback_error,
                            drain_result=drain_result,
                            merge_result=merge_result,
                            before=before,
                        )
                    else:
                        phase = "final_recheck"
                        # Re-discover dynamic stores so a same-thread producer
                        # cannot create an unseen group inside the merge window.
                        after = self._capture(self._legacy_stores())
                        reason = self._drift_error(before, after)
                        if reason:
                            result = ProjectionBarrierResult(
                                ProjectionBarrierState.FAILED,
                                phase,
                                error=reason,
                                drain_result=drain_result,
                                merge_result=merge_result,
                                before=before,
                                after=after,
                            )
                        else:
                            result = ProjectionBarrierResult(
                                ProjectionBarrierState.COMMITTED,
                                "complete",
                                drain_result=drain_result,
                                merge_result=merge_result,
                                before=before,
                                after=after,
                            )
        except Exception as exc:
            result = ProjectionBarrierResult(
                ProjectionBarrierState.FAILED,
                phase,
                error=f"{type(exc).__name__}: {exc}",
                exception_type=type(exc).__name__,
                drain_result=drain_result,
                merge_result=merge_result,
                before=before,
                after=after,
            )
        assert result is not None
        self.last_result = result
        if raise_on_error and not result.ok:
            raise ProjectionBarrierError(result)
        return result

    # Small aliases keep future RuleMergeService wiring readable.
    coordinate_merge = run_merge
    execute_merge = run_merge

    def _legacy_stores(self) -> list[Any]:
        source = self._legacy_stores_source
        if callable(source):
            values = source()
        elif source is not None:
            values = [source] if hasattr(source, "group_id") else source
        else:
            values = self._discover_legacy_stores()
        stores: list[Any] = []
        seen: set[str] = set()
        for store in values or ():
            group_id = str(getattr(store, "group_id", ""))
            if not group_id:
                raise TypeError("legacy store must expose group_id")
            if group_id in seen:
                continue
            high_water = getattr(store, "rule_event_high_water", None)
            if not callable(high_water):
                high_water = getattr(store, "outbox_high_water", None)
            if not callable(high_water):
                raise TypeError("legacy store must expose rule_event_high_water()")
            seen.add(group_id)
            stores.append(store)
        return stores

    def _discover_legacy_stores(self) -> list[Any]:
        base = self.workspace / ".memoryguard" / "shared-memory"
        if not base.exists():
            return []
        from .shared_memory_store import SharedMemoryStore
        from .rule_merge_store import iter_legacy_groups

        return [
            SharedMemoryStore(self.workspace, group_id, must_exist=True)
            for group_id, _db_path in iter_legacy_groups(self.workspace)
        ]

    def _capture(self, stores: Iterable[Any]) -> ProjectionBarrierSnapshot:
        committed: dict[str, dict[str, Any]] = {}
        pending = 0
        for store in stores:
            item = dict(store.rule_event_high_water())
            group_id = str(item.get("share_group_id") or store.group_id)
            item["share_group_id"] = group_id
            item["pending"] = int(item.get("pending", 0) or 0)
            committed[group_id] = item
            pending += item["pending"]

        status = self._projection_status()
        lag = self._status_int(status, "projection_lag", "lag")
        error = str(status.get("projection_error", status.get("error", "")) or "")
        projected = self._projected_high_water(status)
        return ProjectionBarrierSnapshot(
            committed_high_water=committed,
            projected_high_water=projected,
            pending=pending,
            projection_lag=lag,
            projection_error=error,
        )

    def _projection_status(self) -> Mapping[str, Any]:
        if self._projection_status_callback is None:
            raise RuntimeError("projection_status_unavailable")
        status = self._projection_status_callback()
        if not isinstance(status, Mapping):
            raise TypeError("projection_status must return a mapping")
        return status

    @staticmethod
    def _status_int(status: Mapping[str, Any], *keys: str) -> int:
        for key in keys:
            if key in status:
                try:
                    return max(0, int(status[key] or 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid projection status {key}") from exc
        raise KeyError("projection_lag")

    @staticmethod
    def _projected_high_water(status: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        explicit = status.get("projected_high_water")
        if isinstance(explicit, Mapping):
            return {str(key): dict(value) for key, value in explicit.items() if isinstance(value, Mapping)}
        scopes = status.get("scopes", ())
        if not isinstance(scopes, Iterable) or isinstance(scopes, (str, bytes, Mapping)):
            return {}
        projected: dict[str, dict[str, Any]] = {}
        for raw in scopes:
            if not isinstance(raw, Mapping):
                continue
            scope_id = str(raw.get("scope_id") or raw.get("group_id") or "")
            if scope_id:
                projected[scope_id] = {
                    "last_outbox_event_id": str(raw.get("last_outbox_event_id") or ""),
                    "last_projected_event_id": str(raw.get("last_projected_event_id") or ""),
                }
        return projected

    @staticmethod
    def _ready_error(snapshot: ProjectionBarrierSnapshot) -> str:
        if snapshot.pending:
            return f"projection_barrier_outbox_not_drained: {snapshot.pending}"
        if snapshot.projection_lag:
            return f"projection_barrier_lag: {snapshot.projection_lag}"
        if snapshot.projection_error:
            return f"projection_barrier_error: {snapshot.projection_error}"
        return ""

    @staticmethod
    def _drift_error(
        before: ProjectionBarrierSnapshot,
        after: ProjectionBarrierSnapshot,
    ) -> str:
        ready_error = MergeGovernanceCoordinator._ready_error(after)
        if ready_error:
            return ready_error
        if before.committed_high_water != after.committed_high_water:
            return "projection_barrier_committed_high_water_drift"
        if before.projected_high_water != after.projected_high_water:
            return "projection_barrier_projected_high_water_drift"
        return ""

    @staticmethod
    def _merge_result_error(result: Any) -> str:
        if result is False:
            return "projection_barrier_merge_not_committed"
        if not isinstance(result, Mapping):
            return ""
        if result.get("ok") is False:
            return str(
                result.get("blocked_reason")
                or result.get("error")
                or "projection_barrier_merge_not_committed"
            )
        state = str(result.get("state") or result.get("status") or "").casefold()
        if state in {"blocked", "failed", "rejected"}:
            return str(
                result.get("blocked_reason")
                or result.get("error")
                or f"projection_barrier_merge_{state}"
            )
        return ""

    @staticmethod
    def _invoke_noarg(callback: Callable[[], Any]) -> Any:
        return callback()

    @staticmethod
    def _invoke_with_snapshot(
        callback: Callable[..., Any], snapshot: ProjectionBarrierSnapshot,
    ) -> Any:
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return callback()
        parameters = list(signature.parameters.values())
        accepts_argument = any(
            item.kind is inspect.Parameter.VAR_POSITIONAL
            or item.kind is inspect.Parameter.POSITIONAL_ONLY
            or item.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            for item in parameters
        )
        return callback(snapshot) if accepts_argument else callback()


# Naming aliases for the independent barrier concept.
ProjectionBarrierCoordinator = MergeGovernanceCoordinator
MergeProjectionBarrier = MergeGovernanceCoordinator


__all__ = [
    "MergeGovernanceCoordinator",
    "MergeProjectionBarrier",
    "ProjectionBarrierCoordinator",
    "ProjectionBarrierError",
    "ProjectionBarrierResult",
    "ProjectionBarrierSnapshot",
    "ProjectionBarrierState",
]
