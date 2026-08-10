"""Task Canvas façade over the runtime working-memory store."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..storage.layout import WorkspaceV2Layout
from .working_memory import (
    MutationContext,
    RuntimeScope,
    RuntimeStore,
    RuntimeV2Error,
    TaskEvent,
    TaskNode,
    TaskRun,
    ToolRef,
    WorkingCheckpoint,
)


@dataclass(frozen=True)
class TaskCanvasView:
    run: TaskRun
    nodes: tuple[TaskNode, ...]
    checkpoints: tuple[WorkingCheckpoint, ...]
    tool_refs: tuple[ToolRef, ...] = ()

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def status(self) -> str:
        return self.run.status

    @property
    def state(self) -> str:
        return self.run.status

    @property
    def goal(self) -> str:
        return self.run.goal

    def __iter__(self):
        yield self.run
        yield self.nodes
        yield self.checkpoints


class TaskCanvas:
    """Scoped, append-only task execution canvas.

    This is a derived runtime view.  It does not publish MemoryAtoms, Rules,
    Evidence, or Content and has no MCP/Hook/GUI integration.
    """

    def __init__(
        self,
        workspace: str | Path | WorkspaceV2Layout,
        *,
        scope: RuntimeScope | None = None,
        readonly: bool = False,
        read_only: bool | None = None,
        initialize: bool = True,
        store: RuntimeStore | None = None,
    ) -> None:
        if read_only is not None:
            readonly = bool(read_only)
        self.store = store or RuntimeStore(workspace, readonly=readonly, initialize=initialize)
        self.scope = scope or RuntimeScope(workspace_id=str(self.store.workspace))
        if self.scope.workspace_id != str(self.store.workspace):
            raise RuntimeV2Error("TaskCanvas scope workspace does not match store")

    def mutation(self, idempotency_key: str, *, actor: str = "") -> MutationContext:
        return MutationContext(self.scope, idempotency_key, actor=actor)

    def load(self, run_id: str, *, scope: RuntimeScope | None = None) -> TaskCanvasView | None:
        selected = scope or self.scope
        result = self.store.load(run_id, selected)
        if result is None:
            return None
        run, nodes, checkpoints = result
        return TaskCanvasView(run, nodes, checkpoints, self.store.list_tool_refs(run_id, selected))

    def create_run(
        self,
        run_id: str,
        *,
        task_type: str = "task",
        goal: str = "",
        importance: int = 0,
        mutation: MutationContext,
        requested_by: str = "",
        fail_at: str | None = None,
    ) -> TaskRun:
        return self.store.create_run(
            run_id, task_type=task_type, goal=goal, importance=importance,
            mutation=mutation, requested_by=requested_by, fail_at=fail_at,
        )

    def add_node(
        self,
        run_id: str,
        node_id: str,
        *,
        node_type: str = "task",
        goal: str = "",
        depends: Sequence[str] = (),
        dependencies: Sequence[str] | None = None,
        refs: Sequence[Mapping[str, Any]] = (),
        result_ref: Mapping[str, Any] | None = None,
        importance: int = 0,
        mutation: MutationContext,
        fail_at: str | None = None,
    ) -> TaskNode:
        return self.store.add_node(
            run_id, node_id, node_type=node_type, goal=goal, depends=depends,
            dependencies=dependencies,
            refs=refs, result_ref=result_ref, importance=importance,
            mutation=mutation, fail_at=fail_at,
        )

    def transition(
        self,
        run_id: str,
        state: str | None = None,
        *,
        status: str | None = None,
        node_id: str | None = None,
        mutation: MutationContext,
        result_ref: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        fail_at: str | None = None,
    ) -> TaskEvent:
        return self.store.transition(
            run_id, state, status=status, mutation=mutation, node_id=node_id,
            result_ref=result_ref, error=error, fail_at=fail_at,
        )

    def add_blocker(
        self,
        run_id: str,
        node_id: str,
        blocker: Mapping[str, Any],
        *,
        mutation: MutationContext,
        fail_at: str | None = None,
    ) -> TaskEvent:
        return self.store.add_blocker(run_id, node_id, blocker, mutation=mutation, fail_at=fail_at)

    def add_ref(
        self,
        run_id: str,
        node_id: str,
        ref: Mapping[str, Any],
        *,
        mutation: MutationContext,
        fail_at: str | None = None,
    ) -> TaskEvent:
        return self.store.add_ref(run_id, node_id, ref, mutation=mutation, fail_at=fail_at)

    def checkpoint(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        checkpoint_key: str = "default",
        node_id: str | None = None,
        mutation: MutationContext,
        fail_at: str | None = None,
    ) -> WorkingCheckpoint:
        return self.store.checkpoint(
            run_id, state, checkpoint_key=checkpoint_key, node_id=node_id,
            mutation=mutation, fail_at=fail_at,
        )

    def add_tool_ref(
        self,
        run_id: str,
        *,
        tool_name: str,
        provider: str,
        mutation: MutationContext,
        node_id: str | None = None,
        path_ref: str = "",
        output_hash: str = "",
        response_digest: str = "",
        summary_ref: str = "",
        request_digest: str = "",
        metadata: Mapping[str, Any] | None = None,
        raw_output: Any = None,
        fail_at: str | None = None,
    ) -> TaskEvent:
        return self.store.add_tool_ref(
            run_id, tool_name=tool_name, provider=provider, mutation=mutation,
            node_id=node_id, path_ref=path_ref,
            output_hash=output_hash or response_digest, summary_ref=summary_ref,
            request_digest=request_digest, metadata=metadata,
            raw_output=raw_output, fail_at=fail_at,
        )

    def list_nodes(
        self,
        run_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
        scope: RuntimeScope | None = None,
    ) -> tuple[tuple[TaskNode, ...], str | None]:
        return self.store.list_nodes(run_id, scope or self.scope, limit=limit, cursor=cursor)

    def counts(self) -> dict[str, int]:
        return self.store.counts()

    def integrity_check(self) -> list[str]:
        return self.store.integrity_check()

    def foreign_key_check(self) -> list[tuple[Any, ...]]:
        return self.store.foreign_key_check()

    def orphan_count(self) -> int:
        return self.store.orphan_count()

    create_task_run = create_run
    create_node = add_node
    save_checkpoint = checkpoint


__all__ = ["TaskCanvas", "TaskCanvasView"]
