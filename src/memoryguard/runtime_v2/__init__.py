"""Phase 4-A scoped working memory and task canvas."""

from .task_canvas import TaskCanvas, TaskCanvasView
from .native_ports import (
    NativeBoundContext,
    NativeContextEnvelope,
    NativeContextError,
    NativePortError,
    NativeRuntimePort,
    NativeV2RuntimePort,
    SurfaceSpec,
    bind_native_transport_context,
    resolve_native_transport_context,
)
from .history_native import NativeHistoryService
from .safe_services import ImportPreviewService, PureSourceReadService, RuntimeDiagnosticsService
from .working_memory import (
    MutationContext,
    RuntimeMutationContext,
    RuntimeSchemaError,
    RuntimeScope,
    RuntimeScopeError,
    RuntimeStore,
    RuntimeV2Error,
    WorkingMemoryScope,
    WorkingMemoryStore,
    TaskEvent,
    TaskNode,
    TaskRun,
    ToolRef,
    WorkingCheckpoint,
    RUNTIME_V2_SCHEMA_MARKER,
    RUNTIME_V2_SCHEMA_VERSION,
)


def __getattr__(name: str):
    """Keep the native runtime package independent of the legacy facade.

    Importing ``runtime_v2.native_ports`` must not load ``compat_v2`` merely
    because the convenience facade is re-exported from this package.
    """
    if name == "V2RuntimeFacade":
        from ..cutover_v2 import V2RuntimeFacade
        return V2RuntimeFacade
    raise AttributeError(name)

__all__ = [
    "MutationContext", "RuntimeMutationContext", "RuntimeSchemaError", "RuntimeScope", "WorkingMemoryScope", "RuntimeScopeError",
    "RuntimeStore", "WorkingMemoryStore", "RuntimeV2Error", "TaskCanvas", "TaskCanvasView",
    "TaskEvent", "TaskNode", "TaskRun", "ToolRef", "WorkingCheckpoint",
    "RUNTIME_V2_SCHEMA_MARKER", "RUNTIME_V2_SCHEMA_VERSION",
    "V2RuntimeFacade",
    "NativeBoundContext", "NativeContextEnvelope", "NativeContextError", "NativePortError", "NativeRuntimePort", "NativeV2RuntimePort", "SurfaceSpec",
    "bind_native_transport_context", "resolve_native_transport_context",
    "NativeHistoryService", "PureSourceReadService", "ImportPreviewService", "RuntimeDiagnosticsService",
]
