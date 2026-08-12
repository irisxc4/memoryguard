"""V3.2 acceptance probe through the V2 public native surfaces.

The fixture deliberately exercises the same acceptance family as the original
V3.2 probe: transport authentication, automatic organization and
supersession, quarantine, agent discovery, group/mode persistence, the GUI
read boundary, external-MCP detection, and revision rollback.  Only V2
stores and the public ``NativeV2RuntimePort`` are used.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryguard.access_context import AccessContext  # noqa: E402
from memoryguard.cutover_v2.surfaces import GUI_OPERATION_SPECS  # noqa: E402
from memoryguard.evidence import EvidenceStore  # noqa: E402
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext  # noqa: E402
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope  # noqa: E402
from memoryguard.runtime_v2.group_native import GroupControlService  # noqa: E402
from memoryguard.runtime_v2.native_ports import (  # noqa: E402
    NativeV2RuntimePort,
    bind_native_transport_context,
)


GROUP = "test-group"


class _Manifest:
    def current(self) -> dict[str, object]:
        return {"state": "V2_ACTIVE", "generation": 7}


def _check(label: str, ok: bool, detail: str = "") -> bool:
    passed = bool(ok)
    suffix = f" :: {detail}" if detail else ""
    message = f"[{('PASS' if passed else 'FAIL')}] {label}{suffix}"
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"))
    return passed


def _context(workspace: Path, agent: str, group: str = GROUP, *, trusted: bool = True) -> Any:
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"accept-v3-{agent}",
            session_source="transport",
            session_trusted=trusted,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id=group,
        project_ref="",
        provider="codex",
        runtime_role="root",
        entrypoint="acceptance",
    )


def _seed(workspace: Path, group: str) -> tuple[MemoryAtomStore, EvidenceStore, GovernanceV2]:
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    governance = GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    context = V2MutationContext(
        workspace_id=str(workspace.resolve()), share_group_id=group,
        agent_instance_id="claude-code-1", actor="acceptance-seed",
        authority="manual", admin=True,
    )
    governance.put_atom(
        MemoryAtom(
            memory_id="seed-preference",
            body="Use concise code in the project",
            kind="preference",
            status="active",
            share_group_id=group,
            workspace_id=str(workspace.resolve()),
            agent_instance_id="claude-code-1",
        ),
        context=context,
        evidence=[{"source_ref": "acceptance:seed-preference"}],
        reason="v3.2 seed",
        idempotency_key="seed-preference",
    )
    while memory.pending_outbox(include_failed=True):
        memory.project_evidence(evidence)
    memory.set_visibility("active")
    return memory, evidence, governance


def _dispatch(
    port: NativeV2RuntimePort,
    name: str,
    payload: Any,
    context: Any,
    *,
    mutation: bool = False,
) -> dict[str, Any]:
    return port.dispatch_mcp(
        name, payload, context=context, generation=7,
        mutation=mutation, state="V2_ACTIVE",
    )


def _gui(
    port: NativeV2RuntimePort,
    name: str,
    payload: Any,
    context: Any,
    *,
    mutation: bool = False,
) -> dict[str, Any]:
    return port.dispatch_gui(
        name, payload, context=context, generation=7,
        mutation=mutation, state="V2_ACTIVE",
    )


def _data(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("data")
    return value if isinstance(value, dict) else {}


def _drain(memory: MemoryAtomStore, evidence: EvidenceStore) -> None:
    """Complete the V2 evidence barrier before checking public visibility."""
    while memory.pending_outbox(include_failed=True):
        memory.project_evidence(evidence)
    memory.set_visibility("active")


def _agent_fixture() -> tuple[Path, dict[str, str | None], Any]:
    """Create the declared Codex surface without depending on host installs."""
    fixture = Path(tempfile.mkdtemp(prefix="memoryguard-v3-agent-"))
    codex = fixture / ".codex" / "memories"
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "MEMORY.md").write_text("# acceptance fixture\n", encoding="utf-8")
    (fixture / "CLAUDE.md").write_text("# Claude acceptance fixture\n", encoding="utf-8")
    bin_dir = fixture / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "codex.exe").write_text("", encoding="ascii")
    keys = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PATH")
    previous = {key: os.environ.get(key) for key in keys}
    for key, value in {
        "HOME": str(fixture),
        "USERPROFILE": str(fixture),
        "APPDATA": str(fixture / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(fixture / "AppData" / "Local"),
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
    }.items():
        os.environ[key] = value
    original_home = Path.home
    Path.home = staticmethod(lambda: fixture)  # type: ignore[assignment]
    return fixture, previous, original_home


def _restore_agent_fixture(fixture: Path, previous: dict[str, str | None], original_home: Any) -> None:
    Path.home = original_home  # type: ignore[assignment]
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(fixture, ignore_errors=True)


def main() -> int:
    checks: list[bool] = []
    agent_fixture, previous_env, original_home = _agent_fixture()
    try:
        with tempfile.TemporaryDirectory(prefix="memoryguard-v3-2-") as temp:
            workspace = Path(temp)
            (workspace / "AGENTS.md").write_text("# Codex acceptance fixture\n", encoding="utf-8")
            (workspace / "CLAUDE.md").write_text("# Claude acceptance fixture\n", encoding="utf-8")
            fixture_marker = workspace / "fixture-agent.marker"
            fixture_marker.write_text("installed\n", encoding="utf-8")
            profile_dir = workspace / ".memoryguard" / "agent-profiles"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "acceptance_fixture.json").write_text(
                json.dumps({
                    "profile_id": "acceptance-fixture@v2",
                    "product": "acceptance-fixture",
                    "profile_version": "2",
                    "surfaces": [{
                        "surface_id": "fixture-install-marker",
                        "path_template": str(fixture_marker),
                        "surface_role": "fixture_install",
                        "scope": "workspace",
                        "category": "unknown",
                        "ingestion_policy": "extract_candidates",
                        "ownership": "external_read_only",
                        "target_role": "none",
                        "evidence_role": "content_source",
                    }],
                    "target_capability": "export_only",
                    "support_level": "C",
                }, sort_keys=True),
                encoding="utf-8",
            )
            groups = GroupControlService(workspace, write=True)
            binding = groups.bind_agents(
                ["claude-code-1", "codex-1", "test-agent"],
                share_group_id=GROUP,
                native_memory_modes={
                    "claude-code-1": "redirected",
                    "codex-1": "observed",
                    "test-agent": "observed",
                },
            )
            memory, evidence, governance = _seed(workspace, GROUP)
            port = NativeV2RuntimePort(workspace, state_provider=_Manifest())
            claude = _context(workspace, "claude-code-1")

            print("\n=== V2 transport and memory backend ===")
            anonymous = port.dispatch_mcp(
                "memoryguard_memory_write",
                {"memory_id": "anonymous", "body": "must be rejected", "idempotency_key": "anonymous"},
                context=None, generation=7, mutation=True, state="V2_ACTIVE",
            )
            checks.append(_check(
                "anonymous mutation is rejected",
                anonymous.get("ok") is False,
                str(anonymous),
            ))
            status = _dispatch(port, "memoryguard_memory_status", {}, claude)
            status_data = _data(status)
            checks.append(_check(
                "V2 memory backend is available",
                status.get("ok") is True and status_data.get("available") is True and status_data.get("total_records", 0) >= 1,
                str(status),
            ))
            names = {
                item["name"]
                for item in port.coverage()["surfaces"]["mcp"]["entries"]
            }
            required_tools = {
                "memoryguard_memory_read", "memoryguard_memory_search",
                "memoryguard_memory_write", "memoryguard_memory_update",
                "memoryguard_memory_delete", "memoryguard_memory_status",
            }
            checks.append(_check(
                "six MCP tools are native",
                required_tools <= names,
                str(sorted(required_tools & names)),
            ))

            print("\n=== automatic organization and supersession ===")
            written = _dispatch(
                port, "memoryguard_memory_write",
                {
                    "memory_id": "mcp-write",
                    "body": "The project uses Python for testing",
                    "kind": "preference",
                    "idempotency_key": "mcp-write",
                    "evidence": [{"source_ref": "mcp:mcp-write"}],
                }, claude, mutation=True,
            )
            written_data = _data(written)
            written_atom = written_data.get("atom", {})
            _drain(memory, evidence)
            checks.append(_check(
                "agent write creates an active atom",
                written.get("ok") is True and written_atom.get("status") == "active",
                str(written),
            ))
            checks.append(_check(
                "automatic organization executed",
                bool(written_data.get("actions")) and written_data.get("mutation_kind") in {"created", "updated"},
                str(written_data.get("actions")),
            ))

            original = _dispatch(
                port, "memoryguard_memory_write",
                {
                    "memory_id": "python-original",
                    "body": "The project uses Python 3.8",
                    "kind": "fact",
                    "idempotency_key": "python-original",
                    "evidence": [{"source_ref": "mcp:python-original"}],
                }, claude, mutation=True,
            )
            correction = _dispatch(
                port, "memoryguard_memory_write",
                {
                    "memory_id": "python-correction",
                    "body": "Correction: the project uses Python 3.12",
                    "kind": "correction",
                    "idempotency_key": "python-correction",
                    "evidence": [{"source_ref": "mcp:python-correction"}],
                }, _context(workspace, "codex-1"), mutation=True,
            )
            _drain(memory, evidence)
            correction_atom = _data(correction).get("atom", {})
            scoped = MemoryReadScope(
                workspace_id=str(workspace.resolve()), share_group_id=GROUP, admin=True,
            )
            shadowed = memory.list_atoms(scope=scoped, status="superseded")
            checks.append(_check(
                "old memory becomes superseded (V2 shadowed state)",
                bool(shadowed),
                f"count={len(shadowed)}",
            ))
            checks.append(_check(
                "new memory keeps supersedes",
                correction.get("ok") is True and bool(correction_atom.get("supersedes")),
                str(correction_atom.get("supersedes")),
            ))
            supersede_events = _gui(port, "get_supersede_decisions", {}, claude)
            supersede_data = _data(supersede_events)
            checks.append(_check(
                "supersession decision is recorded",
                supersede_events.get("ok") is True and bool(
                    supersede_data.get("decisions")
                    or supersede_data.get("events")
                    or supersede_data.get("receipts")
                ),
                str(supersede_events),
            ))

            print("\n=== quarantine and governance ===")
            secret = _dispatch(
                port, "memoryguard_memory_write",
                {
                    "memory_id": "secret",
                    "body": "API_KEY=sk-abc123def456ghi789",
                    "idempotency_key": "secret",
                    "evidence": [{"source_ref": "mcp:secret"}],
                }, _context(workspace, "test-agent"), mutation=True,
            )
            _drain(memory, evidence)
            secret_atom = _data(secret).get("atom", {})
            checks.append(_check(
                "secret is quarantined",
                secret.get("ok") is True and secret_atom.get("status") == "quarantined",
                str(secret),
            ))
            quarantine = _gui(port, "get_quarantine", {"operation": "quarantine"}, claude)
            quarantine_data = _data(quarantine)
            checks.append(_check(
                "quarantine queue is native and non-empty",
                quarantine.get("ok") is True and quarantine_data.get("total", 0) >= 1,
                str(quarantine),
            ))

            print("\n=== agent discovery and shared group ===")
            agents_result = _gui(port, "list_agents", {}, claude)
            agents_data = _data(agents_result)
            agents = agents_data.get("agents", [])
            checks.append(_check(
                "agent discovery returns a fixture agent",
                agents_result.get("ok") is True and bool(agents),
                str(agents_result),
            ))
            if agents:
                agent_id = str(agents[0].get("instance_id", ""))
                agent_data_result = _gui(port, "get_agent_data", {"instance_id": agent_id}, claude)
                agent_data = _data(agent_data_result)
                checks.append(_check(
                    "agent data view is available",
                    agent_data_result.get("ok") is True and bool(
                        agent_data.get("product") or agent_data.get("agent")
                    ),
                    str(agent_data_result),
                ))
            else:
                checks.append(_check("agent data view is available", False, "agent discovery returned no instances"))

            entered = _gui(port, "enter_multi_agent_mode", {}, claude, mutation=True)
            entered_data = _data(entered)
            checks.append(_check(
                "enter multi-agent shared MCP mode",
                entered.get("ok") is True and entered_data.get("mode") == "multi_agent_shared_mcp",
                str(entered),
            ))
            shared_status = _gui(port, "get_memory_status", {}, claude)
            shared_data = _data(shared_status)
            checks.append(_check(
                "shared memory group is visible",
                shared_status.get("ok") is True and shared_data.get("scope", {}).get("share_group_id") == GROUP,
                str(shared_status),
            ))

            binding_result = _gui(
                port, "bind_agents_to_shared_group",
                {
                    "target_agent_ids": ["agent-1", "agent-2"],
                    "target_group_id": GROUP,
                    "native_memory_modes": {"agent-1": "redirected", "agent-2": "observed"},
                }, claude, mutation=True,
            )
            binding_data = _data(binding_result)
            new_bindings = binding_data.get("bindings", [])
            checks.append(_check(
                "two GUI bindings target the same group",
                binding_result.get("ok") is True and len(new_bindings) == 2
                and all(item.get("share_group_id") == GROUP for item in new_bindings),
                str(binding_result),
            ))
            persisted = groups.list_bindings(include_inactive=False).get("bindings", [])
            persisted_group = [item for item in persisted if item.get("share_group_id") == GROUP]
            checks.append(_check(
                "V2 binding ledger is readable",
                len(persisted_group) >= 5,
                f"persisted={len(persisted_group)}",
            ))
            modes = {item.get("agent_instance_id"): item.get("native_memory_mode") for item in persisted_group}
            checks.append(_check(
                "agent 1 remains redirected",
                modes.get("agent-1") == "redirected",
                json.dumps(modes, sort_keys=True),
            ))
            checks.append(_check(
                "agent 2 remains observed",
                modes.get("agent-2") == "observed",
                json.dumps(modes, sort_keys=True),
            ))
            checks.append(_check(
                "native memory is not marked disabled",
                all(value != "disabled" for value in modes.values()),
                json.dumps(modes, sort_keys=True),
            ))
            checks.append(_check(
                "binding mutation receipt exists",
                bool(binding.get("binding", binding.get("bindings"))),
                str(binding),
            ))

            print("\n=== GUI read boundary and external MCP ===")
            list_spec = GUI_OPERATION_SPECS["list_memory"]
            checks.append(_check(
                "GUI memory read has no confirmation parameter",
                "confirmed" not in tuple(list_spec.parameters),
                str(list_spec.parameters),
            ))
            gui_names = set(GUI_OPERATION_SPECS)
            checks.append(_check(
                "GUI exposes no write-named memory method",
                not any("write" in name.casefold() for name in gui_names),
                str(sorted(name for name in gui_names if "write" in name.casefold())),
            ))
            descriptor = {
                "display_name": "Unknown Tool Server",
                "tools": [{"name": "dangerous_export"}],
            }
            detected = _gui(
                port, "detect_external_mcp",
                {"server_id": "unknown-tools", "descriptor": descriptor}, claude,
            )
            detected_data = _data(detected)
            checks.append(_check(
                "unknown tools classify as L1",
                detected.get("ok") is True and detected_data.get("level") == "L1_unknown_tools",
                str(detected),
            ))
            preview = _gui(
                port, "preview_external_mcp_import",
                {"server_id": "unknown-tools", "descriptor": descriptor}, claude,
            )
            preview_data = _data(preview)
            checks.append(_check(
                "L1 preview detects only and extracts nothing",
                preview.get("ok") is True and preview_data.get("unknown_tools_called") is False
                and preview_data.get("total") == 0,
                str(preview),
            ))

            print("\n=== revision rollback ===")
            first_version = _dispatch(
                port, "memoryguard_memory_write",
                {"memory_id": "versioned", "body": "revision one", "idempotency_key": "versioned-1", "evidence": [{"source_ref": "versioned-1"}]},
                claude, mutation=True,
            )
            _drain(memory, evidence)
            second_version = _dispatch(
                port, "memoryguard_memory_update",
                {"memory_id": "versioned", "body": "revision two", "idempotency_key": "versioned-2", "evidence": [{"source_ref": "versioned-2"}]},
                claude, mutation=True,
            )
            _drain(memory, evidence)
            versions = _gui(port, "list_memory_versions", {"memory_id": "versioned"}, claude)
            version_rows = _data(versions).get("versions", [])
            current_revision = _data(second_version).get("atom", {}).get("revision", 0)
            old_version = next(
                (row for row in version_rows if row.get("revision", 0) < current_revision),
                None,
            )
            checks.append(_check(
                "V2 revision snapshot is listed",
                first_version.get("ok") is True and second_version.get("ok") is True
                and versions.get("ok") is True and old_version is not None,
                str(versions),
            ))
            rollback = _gui(
                port, "rollback_memory",
                {"version_id": old_version.get("version_id", "") if old_version else ""},
                claude, mutation=True,
            )
            rollback_atom = _data(rollback).get("atom", {})
            checks.append(_check(
                "V2 revision rollback restores the prior body",
                rollback.get("ok") is True and rollback_atom.get("body") == "revision one"
                and bool(_data(rollback).get("receipt")),
                str(rollback),
            ))

            # Keep these concrete V2 objects alive through all assertions.
            checks.append(_check(
                "V2 governance objects are live",
                isinstance(memory, MemoryAtomStore)
                and isinstance(evidence, EvidenceStore)
                and isinstance(governance, GovernanceV2),
            ))
    finally:
        _restore_agent_fixture(agent_fixture, previous_env, original_home)

    passed = all(checks)
    print("\n" + "=" * 60)
    print(f"v3.2 V2_ACTIVE acceptance: {'PASS' if passed else 'FAIL'} ({sum(checks)}/{len(checks)})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
