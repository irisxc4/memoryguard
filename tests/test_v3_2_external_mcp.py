"""v3.2 native external MCP detection and import checks."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memoryguard.access_context import AccessContext
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore
from memoryguard.runtime_v2.external_mcp_native import NativeExternalMCPService
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    msg = f"[{status}] {label}"
    if detail:
        msg += f" :: {detail}"
    print(msg)
    return ok


def _activate_v2(workspace: Path) -> None:
    initialize_all(WorkspaceV2Layout(workspace))
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="v3-2-external-mcp")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="v3-2-external-source",
        target_digest="v3-2-external-target",
        manifest_digest="v3-2-external-manifest",
        digests={"validator_passed": True, "checkpoints": {"external_mcp": True}},
    )
    manager.transition(ManifestState.V2_ACTIVE)
    GroupControlService(workspace, write=True).bind_agent(
        "external-agent", "external-team"
    )


def _context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="external-agent",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="external-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id="external-team",
        project_ref="v3-2-external",
        provider="codex",
        runtime_role="root",
    )


def main() -> int:
    all_pass = True
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _activate_v2(workspace)
        context = _context(workspace)
        service = NativeExternalMCPService(workspace)

        print("\n=== 1. L1 unknown tools are detected without invocation ===")
        l1_descriptor = {
            "display_name": "Unknown Tool Server",
            "tools": [{"name": "dangerous_export"}],
        }
        l1 = service.detect_external_mcp(
            "unknown-tools", l1_descriptor, context=context
        )
        all_pass &= _check(
            "L1 classification",
            l1.get("level") == "L1_unknown_tools",
            f"level={l1.get('level')}",
        )
        all_pass &= _check(
            "unknown tool not invoked",
            l1.get("safe_to_auto_call_tools") is False,
        )
        all_pass &= _check(
            "ephemeral detection does not write config",
            not (workspace / ".memoryguard" / "external-mcp" / "servers.json").exists(),
        )

        print("\n=== 2. L2 resources preview before native import ===")
        resource_descriptor = {
            "display_name": "Resource Server",
            "resources": [
                {
                    "name": "team-memory.md",
                    "uri": "memory://team",
                    "text": "Team preference: write backend acceptance first.",
                }
            ],
        }
        resource_detected = service.detect_external_mcp(
            "resource-server", resource_descriptor, context=context
        )
        all_pass &= _check(
            "L2 classification",
            resource_detected.get("level") == "L2_generic_resources",
            f"level={resource_detected.get('level')}",
        )
        imported_resource = service.import_external_mcp(
            {
                "server_id": "resource-server",
                "descriptor_json": json.dumps(resource_descriptor),
            },
            context=context,
        )
        all_pass &= _check(
            "L2 static import",
            imported_resource.get("ok") is True
            and imported_resource.get("imported") is True
            and imported_resource.get("unknown_tools_called") is False,
            f"status={imported_resource.get('status')}",
        )
        resource_ref = imported_resource.get("server_ref")
        resource_preview = service.preview_external_mcp_import(
            resource_ref, context=context
        )
        all_pass &= _check(
            "L2 preview entry",
            resource_preview.get("total") == 1
            and isinstance(
                resource_preview.get("preview_entries", [{}])[0].get("content_digest"),
                str,
            ),
            f"total={resource_preview.get('total')}",
        )

        print("\n=== 3. L3 known memory MCP remains read-only preview ===")
        memory_descriptor = {
            "display_name": "Known Memory",
            "tools": [{"name": "memory_search"}],
            "memory_entries": [
                {"body": "Project fact: the MCP source is external.", "metadata": {"kind": "fact"}}
            ],
        }
        memory_detected = service.detect_external_mcp(
            "known-memory", memory_descriptor, context=context
        )
        all_pass &= _check(
            "L3 classification",
            memory_detected.get("level") == "L3_known_memory_mcp",
            f"level={memory_detected.get('level')}",
        )
        memory_import = service.import_external_mcp(
            {
                "server_id": "known-memory",
                "descriptor_json": json.dumps(memory_descriptor),
            },
            context=context,
        )
        memory_ref = memory_import.get("server_ref")
        memory_preview = service.preview_external_mcp_import(
            memory_ref, context=context
        )
        all_pass &= _check(
            "L3 read-only preview",
            memory_preview.get("total") == 1
            and memory_preview.get("unknown_tools_called") is False,
            f"total={memory_preview.get('total')}",
        )

        print("\n=== 4. Native port route and servers.json persistence ===")
        port = NativeV2RuntimePort(
            workspace,
            state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
        )
        listed = port.dispatch_mcp(
            "memoryguard_external_mcp_list",
            {},
            context=context,
            generation=1,
            state="V2_ACTIVE",
        )
        all_pass &= _check(
            "native list route",
            listed.get("ok") is True and listed.get("data", {}).get("total") == 2,
            f"total={listed.get('data', {}).get('total')}",
        )
        gui_preview = port.dispatch_gui(
            "preview_external_mcp_import",
            [resource_ref],
            context=context,
            generation=1,
            state="V2_ACTIVE",
        )
        all_pass &= _check(
            "native GUI preview alias",
            gui_preview.get("ok") is True
            and gui_preview.get("data", {}).get("total") == 1,
        )
        servers_file = workspace / ".memoryguard" / "external-mcp" / "servers.json"
        all_pass &= _check("servers.json exists", servers_file.is_file(), str(servers_file))
        all_pass &= _check(
            "two static descriptors listed",
            listed.get("data", {}).get("total") == 2,
        )

    print("\n" + "=" * 50)
    if all_pass:
        print("All v3.2 External MCP tests PASSED")
        return 0
    print("Some External MCP tests FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
