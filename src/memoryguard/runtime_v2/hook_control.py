"""V2 GUI host-hook status/mode/uninstall orchestration.

Host hook configuration is an OS/user-profile side effect, while authority and
receipts live in the V2 system control plane.  The two stores cannot share one
SQLite transaction, so every mutation uses explicit compensation if the V2
receipt cannot be committed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..host_hook_executor import HostHookExecutor
from .group_native import GroupControlService, GroupControlError, SystemControlStore


class HookControlError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "hook_control_failed")
        super().__init__(self.code)


_PROVIDER_ALIASES = {
    "claude-code": "claude",
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor",
    "trae": "trae",
}


def _sanitize_status(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: item for key, item in value.items()
        if key not in {"config_file", "instruction_file", "mcp_config_file"}
    }
    capability = result.get("capability")
    if isinstance(capability, Mapping):
        result["capability"] = {
            key: item for key, item in capability.items()
            if key not in {"config_file", "path"}
        }
    return result


class HookControlService:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.groups = GroupControlService(self.workspace, write=False)
        self.system = SystemControlStore(self.workspace, write=False)
        self.manager = HostHookExecutor(self.workspace)

    def _product_map(self) -> dict[str, str]:
        try:
            from ..agent_locator import AgentLocator

            instances, _ = AgentLocator(self.workspace).detect_instances()
        except Exception:
            instances = []
        return {
            str(item.instance_id): _PROVIDER_ALIASES.get(
                str(item.product or "").strip().casefold(),
                str(item.product or "").strip().casefold(),
            )
            for item in instances
            if str(getattr(item, "instance_id", "") or "")
        }

    def _provider_for_agent(self, agent_id: str, requested: str = "") -> str:
        requested_provider = _PROVIDER_ALIASES.get(
            str(requested or "").strip().casefold(),
            str(requested or "").strip().casefold(),
        )
        discovered = self._product_map().get(str(agent_id), "")
        if requested_provider and discovered and requested_provider != discovered:
            raise HookControlError("hook_provider_agent_mismatch")
        provider = requested_provider or discovered
        if provider not in {"claude", "codex", "cursor", "trae"}:
            raise HookControlError("hook_provider_unknown")
        return provider

    def _binding(self, agent_id: str) -> dict[str, Any]:
        try:
            binding = self.groups.active_binding_for_agent(str(agent_id))
        except GroupControlError as exc:
            raise HookControlError(exc.code) from exc
        if binding is None:
            raise HookControlError("active_binding_required")
        return binding

    def status(self, *, provider: str = "", target_agent_id: str = "") -> dict[str, Any]:
        target = str(target_agent_id or "").strip()
        if target:
            self._binding(target)
            resolved_provider = self._provider_for_agent(target, provider)
            return {
                "ok": True,
                "status": "succeeded",
                **_sanitize_status(self.manager.status(resolved_provider, agent_instance_id=target)),
            }

        bindings = self.groups.list_bindings(include_inactive=False).get("bindings", [])
        products = self._product_map()
        agents: list[dict[str, Any]] = []
        for binding in bindings:
            agent_id = str(binding.get("agent_instance_id") or "")
            resolved_provider = products.get(agent_id, "")
            if resolved_provider not in {"claude", "codex", "cursor", "trae"}:
                continue
            item = _sanitize_status(
                self.manager.status(resolved_provider, agent_instance_id=agent_id)
            )
            item["agent_instance_id"] = agent_id
            item["share_group_id"] = str(binding.get("share_group_id") or "")
            agents.append(item)
        agents.sort(key=lambda item: (str(item.get("provider") or ""), str(item.get("agent_instance_id") or "")))
        return {
            "ok": True,
            "status": "succeeded",
            "agents": agents,
            "configured_count": sum(bool(item.get("configured")) for item in agents),
            "operational_count": sum(bool(item.get("runtime_verified")) for item in agents),
        }

    def set_mode(
        self,
        provider: str,
        target_agent_id: str,
        mode: str,
        *,
        admin: bool,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        if not admin:
            raise HookControlError("admin_capability_required")
        agent = str(target_agent_id or "").strip()
        if not agent:
            raise HookControlError("target_agent_id_required")
        binding = self._binding(agent)
        resolved_provider = self._provider_for_agent(agent, provider)
        before_status = self.manager.status(resolved_provider, agent_instance_id=agent)
        if not before_status.get("configured"):
            raise HookControlError("hook_not_configured")
        previous = self.manager.get_mode(resolved_provider, agent)
        requested = str(mode or "").strip().casefold()
        key = str(idempotency_key or f"hook-mode:{resolved_provider}:{agent}:{requested}")
        request = {
            "provider": resolved_provider,
            "agent": agent,
            "group": str(binding.get("share_group_id") or ""),
            "mode": requested,
        }
        changed_file = False

        def apply(_conn: Any):
            nonlocal changed_file
            result = self.manager.set_mode(resolved_provider, agent, requested)
            changed_file = previous != requested
            verified = self.manager.status(resolved_provider, agent_instance_id=agent)
            if not verified.get("configured") or str(verified.get("mode") or "") != requested:
                raise HookControlError("hook_mode_verification_failed")
            public = {
                "ok": True,
                "status": "succeeded",
                "provider": resolved_provider,
                "agent_instance_id": agent,
                "share_group_id": str(binding.get("share_group_id") or ""),
                "mode": requested,
                "changed": changed_file,
                "configured": True,
                "runtime_verified": bool(verified.get("runtime_verified")),
                "restart_required": bool(result.get("restart_required", False)),
            }
            return public, f"{resolved_provider}:{agent}"

        try:
            store = SystemControlStore(self.workspace, write=True)
            return store.mutate("host_hook_mode_set", key, request, apply)
        except Exception as exc:
            if changed_file:
                try:
                    self.manager.set_mode(resolved_provider, agent, previous)
                except Exception:
                    pass
            if isinstance(exc, HookControlError):
                raise
            raise HookControlError(str(getattr(exc, "code", "") or "hook_mode_update_failed")) from exc

    def uninstall(
        self,
        provider: str,
        *,
        admin: bool,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        if not admin:
            raise HookControlError("admin_capability_required")
        resolved_provider = _PROVIDER_ALIASES.get(
            str(provider or "").strip().casefold(),
            str(provider or "").strip().casefold(),
        )
        if resolved_provider not in {"claude", "codex", "cursor", "trae"}:
            raise HookControlError("hook_provider_unknown")
        products = self._product_map()
        bindings = [
            item for item in self.groups.list_bindings(include_inactive=False).get("bindings", [])
            if products.get(str(item.get("agent_instance_id") or "")) == resolved_provider
        ]
        before = self.manager.status(resolved_provider)
        key = str(idempotency_key or f"hook-uninstall:{resolved_provider}")
        request = {
            "provider": resolved_provider,
            "binding_ids": sorted(str(item.get("binding_id") or "") for item in bindings),
        }
        removed = False

        def apply(_conn: Any):
            nonlocal removed
            result = self.manager.uninstall(resolved_provider)
            removed = bool(before.get("configured") or before.get("drifted"))
            verified = self.manager.status(resolved_provider)
            if verified.get("configured"):
                raise HookControlError("hook_uninstall_verification_failed")
            return ({
                "ok": True,
                "status": "succeeded",
                "provider": resolved_provider,
                "configured": False,
                "changed": removed,
                "runtime_verified": False,
                "supported": bool(result.get("supported", True)),
            }, resolved_provider)

        try:
            store = SystemControlStore(self.workspace, write=True)
            return store.mutate("host_hook_uninstall", key, request, apply)
        except Exception as exc:
            if removed:
                for binding in bindings:
                    try:
                        self.manager.install(
                            resolved_provider,
                            agent_instance_id=str(binding.get("agent_instance_id") or ""),
                            share_group_id=str(binding.get("share_group_id") or ""),
                            mode=self.manager.get_mode(
                                resolved_provider,
                                str(binding.get("agent_instance_id") or ""),
                            ),
                        )
                    except Exception:
                        pass
            if isinstance(exc, HookControlError):
                raise
            raise HookControlError(str(getattr(exc, "code", "") or "hook_uninstall_failed")) from exc


__all__ = ["HookControlError", "HookControlService"]
