"""V2 host-enrichment facade.

Enrichment tasks live in the V2 Content Plane.  This module intentionally has
no file-backed queue or IR serializer; callers without a trusted V2 context
receive an empty/blocked result instead of reopening retired state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .runtime_v2.extraction_native import NativeExtractionEnrichmentService
from .storage.layout import WorkspaceV2Layout


def _pending_path(workspace: str | Path) -> Path:
    """Return the native task database path for diagnostics only."""
    return WorkspaceV2Layout(Path(workspace).expanduser().resolve()).content_db


def _service(workspace: str | Path) -> NativeExtractionEnrichmentService | None:
    try:
        return NativeExtractionEnrichmentService(workspace)
    except Exception:
        return None


def _context(value: Any) -> Any:
    if isinstance(value, Mapping) and str(value.get("workspace_id") or value.get("workspace") or ""):
        return value
    return None


def enqueue_from_ir(
    workspace: str | Path,
    ir: Any,
    scope: dict | None = None,
    *,
    reason: str = "post_normalize",
) -> int:
    """Queue enrichment for already-persisted V2 atoms.

    ``ir`` and ``reason`` are retained as compatibility-shaped inputs only;
    no V1 IR is read or written.
    """
    del ir, reason
    context = _context(scope)
    service = _service(workspace) if context is not None else None
    if service is None:
        return 0
    try:
        result = service.build_and_enrich({}, context=context)
        return int(result.get("queued_or_pending", 0) or 0)
    except Exception:
        return 0


def enqueue_from_shared_store(
    workspace: str | Path,
    share_group_id: str,
    *,
    reason: str = "share_group_rebuild",
    context: Any = None,
) -> int:
    """Queue V2 enrichment under an already trusted group context."""
    del reason
    if not isinstance(context, Mapping) or str(context.get("share_group_id") or "") != str(share_group_id or ""):
        return 0
    return enqueue_from_ir(workspace, None, dict(context))


def list_pending(
    workspace: str | Path,
    limit: int = 50,
    agent_instance_id: str = "",
    share_group_id: str = "",
    *,
    context: Any = None,
) -> list[dict[str, Any]]:
    """List only tasks visible through the trusted V2 context."""
    del agent_instance_id, share_group_id
    service = _service(workspace) if _context(context) is not None else None
    if service is None:
        return []
    try:
        result = service.list_pending({"limit": int(limit)}, context=context)
        return [dict(item) for item in result.get("tasks", []) if isinstance(item, Mapping)]
    except Exception:
        return []


def apply_results(
    workspace: str | Path,
    results: list[dict[str, Any]],
    agent_instance_id: str = "",
    share_group_id: str = "",
    *,
    context: Any = None,
) -> dict[str, Any]:
    """Apply host results through the V2 governance boundary."""
    del agent_instance_id, share_group_id
    service = _service(workspace) if _context(context) is not None else None
    if service is None:
        return {
            "applied": 0,
            "rejected": len(results or []),
            "errors": ["trusted_v2_context_required"],
            "rebuild_suggested": False,
        }
    try:
        result = service.apply_enrichments({"results": list(results or [])}, context=context)
        return dict(result)
    except Exception as exc:
        return {
            "applied": 0,
            "rejected": len(results or []),
            "errors": [str(getattr(exc, "code", "v2_enrichment_apply_failed"))],
            "rebuild_suggested": False,
        }


def get_status(
    workspace: str | Path,
    agent_instance_id: str = "",
    share_group_id: str = "",
    *,
    context: Any = None,
) -> dict[str, Any]:
    """Return V2 task counts; no context means a neutral result."""
    del agent_instance_id, share_group_id
    service = _service(workspace) if _context(context) is not None else None
    if service is None:
        return {"pending": 0, "applied": 0, "total": 0, "status": "blocked"}
    try:
        result = service.enrichment_status({}, context=context)
        return {
            "pending": int(result.get("pending", 0) or 0),
            "applied": int(result.get("applied", 0) or 0),
            "total": int(result.get("total", 0) or 0),
            "status": "ready",
        }
    except Exception:
        return {"pending": 0, "applied": 0, "total": 0, "status": "blocked"}


__all__ = [
    "_pending_path",
    "enqueue_from_ir",
    "enqueue_from_shared_store",
    "list_pending",
    "apply_results",
    "get_status",
]
