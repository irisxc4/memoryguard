"""Desktop capability facade for host hook side effects.

Runtime V2 control services must not import host integration implementations
directly.  This module is the process-local capability boundary that owns OS
and user-profile hook reads/writes while the V2 runtime owns authorization and
receipts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .host_hooks import HostHookManager, get_hook_mode, set_hook_mode


class HostHookExecutor:
    """Execute host-hook side effects outside the V2 data-plane package."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._manager = HostHookManager(self.workspace)

    def status(self, provider: str = "", *, agent_instance_id: str = "") -> dict[str, Any]:
        return self._manager.status(provider, agent_instance_id=agent_instance_id)

    def install(
        self,
        provider: str,
        *,
        agent_instance_id: str,
        share_group_id: str,
        mode: str = "enforce",
    ) -> dict[str, Any]:
        return self._manager.install(
            provider,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            mode=mode,
        )

    def uninstall(self, provider: str) -> dict[str, Any]:
        return self._manager.uninstall(provider)

    def get_mode(self, provider: str, agent_instance_id: str) -> str:
        return get_hook_mode(self.workspace, provider, agent_instance_id)

    def set_mode(self, provider: str, agent_instance_id: str, mode: str) -> dict[str, Any]:
        return set_hook_mode(self.workspace, provider, agent_instance_id, mode)


__all__ = ["HostHookExecutor"]
