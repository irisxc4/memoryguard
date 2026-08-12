"""Import selected native-memory sources through the V2 group service."""
from __future__ import annotations

from typing import Any
from pathlib import Path

from .runtime_v2.group_native import GroupControlError, GroupControlService


def import_native_memories_to_group(
    workspace: str | Path,
    share_group_id: str,
    agent_instance_ids: list[str],
    *,
    context: Any = None,
) -> dict[str, Any]:
    """Import selected sources through the canonical V2 Content/Governance path.

    The former API did not carry a trusted transport context.  Such calls now
    fail closed; callers on a native surface must pass the bound context so the
    group service can enforce membership, source selection and governance.
    """
    if not isinstance(context, dict):
        return {
            "ok": False,
            "status": "blocked",
            "code": "trusted_v2_context_required",
            "share_group_id": str(share_group_id or ""),
        }
    required = ("workspace_id", "agent_instance_id", "share_group_id")
    if any(not str(context.get(key) or "").strip() for key in required):
        return {
            "ok": False,
            "status": "blocked",
            "code": "trusted_v2_context_required",
            "share_group_id": str(share_group_id or ""),
        }
    if str(context.get("share_group_id") or "") != str(share_group_id or ""):
        return {
            "ok": False,
            "status": "blocked",
            "code": "context_group_mismatch",
            "share_group_id": str(share_group_id or ""),
        }
    try:
        return GroupControlService(Path(workspace).resolve()).import_native_memories(
            str(share_group_id),
            agent_instance_ids=agent_instance_ids,
            trusted=context,
        )
    except GroupControlError as exc:
        return {
            "ok": False,
            "status": "blocked",
            "code": str(exc.code),
            "share_group_id": str(share_group_id or ""),
        }
