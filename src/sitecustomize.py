"""Self-retiring activation bridge for the isolated Codex lifecycle hotfix.

MemoryGuard already has editable .pth entries pointing at this ``src`` tree.
For the exact managed Codex host-hook process only, this module places the
clean compatibility wheel ahead of editable/source paths. Ordinary Python,
MemoryGuard MCP servers, and other providers are untouched. Installing the
same or a newer distribution makes the bridge inert automatically.
"""

from __future__ import annotations

import os
import sys


HOTFIX_VERSION = "0.7.1.post18"
HOTFIX_WHEEL = "agent_memguard-0.7.1.post18-py3-none-any.whl"
BRIDGEABLE_VERSIONS = frozenset({
    "0.7.1",
    "0.7.1.post1",
    "0.7.1.post2",
    "0.7.1.post3",
    "0.7.1.post4",
    "0.7.1.post5",
    "0.7.1.post6",
    "0.7.1.post7",
    "0.7.1.post8",
    "0.7.1.post9",
    "0.7.1.post10",
    "0.7.1.post11",
    "0.7.1.post12",
    "0.7.1.post13",
    "0.7.1.post14",
    "0.7.1.post15",
    "0.7.1.post16",
    "0.7.1.post17",
})


def _arg_value(name: str) -> str:
    try:
        index = sys.argv.index(name)
    except ValueError:
        return ""
    return str(sys.argv[index + 1]) if index + 1 < len(sys.argv) else ""


def _is_managed_codex_hook() -> bool:
    return (
        len(sys.argv) >= 2
        and sys.argv[0] == "-m"
        and sys.argv[1] == "run"
        and _arg_value("--provider").strip().lower() == "codex"
        and _arg_value("--managed-by").strip().lower() == "memoryguard"
        and bool(_arg_value("--event"))
        and bool(_arg_value("--workspace"))
        and bool(_arg_value("--agent-id"))
        and bool(_arg_value("--share-group-id"))
    )


def _wheel_is_expected(path: object) -> bool:
    """Verify version and required code inside the local wheel before loading."""
    try:
        import zipfile
        from pathlib import Path

        wheel = Path(path)
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                return False
            metadata = archive.read(metadata_names[0]).decode("utf-8", errors="strict")
            if f"Version: {HOTFIX_VERSION}" not in {
                line.strip() for line in metadata.splitlines()
            }:
                return False
            lifecycle_name = "memoryguard/codex_mcp_lifecycle.py"
            hook_name = "memoryguard/host_hooks.py"
            provider_name = "memoryguard/provider_adapters.py"
            subagent_name = "memoryguard/codex_subagent_reconcile.py"
            if (
                lifecycle_name not in names
                or hook_name not in names
                or provider_name not in names
                or subagent_name not in names
            ):
                return False
            lifecycle = archive.read(lifecycle_name).decode("utf-8", errors="strict")
            hooks = archive.read(hook_name).decode("utf-8", errors="strict")
            subagents = archive.read(subagent_name).decode("utf-8", errors="strict")
            return (
                'if mode != "force"' in lifecycle
                and 'allow_termination=selected_mode == "force"' in lifecycle
                and "_best_effort_codex_mcp_lifecycle" in hooks
                and "reconcile_closed_edge_rollout_activities" in hooks
                and "reconcile_closed_edge_rollout_activities" in subagents
                and 'terminal[child] = "missing_rollout"' in subagents
            )
    except Exception:
        return False


if _is_managed_codex_hook():
    try:
        import hashlib
        import importlib.metadata
        import importlib.util
        from pathlib import Path

        installed = importlib.metadata.version("agent-memguard")
        if installed in BRIDGEABLE_VERSIONS:
            repo_root = Path(__file__).resolve().parents[1]
            wheel = repo_root / "dist-hotfix-subagent-rollout-post18" / HOTFIX_WHEEL
            if wheel.is_file() and _wheel_is_expected(wheel):
                wheel_text = str(wheel)
                sys.path[:] = [item for item in sys.path if item != wheel_text]
                sys.path.insert(0, wheel_text)
                digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
                os.environ["MEMORYGUARD_CODEX_HOTFIX_ACTIVE"] = HOTFIX_VERSION
                os.environ["MEMORYGUARD_CODEX_HOTFIX_SHA256"] = digest
                if os.environ.get("MEMORYGUARD_SITECUSTOMIZE_PROBE") == "1":
                    spec = importlib.util.find_spec("memoryguard.host_hooks")
                    print(
                        "MEMORYGUARD_CODEX_HOTFIX_ACTIVE=" + HOTFIX_VERSION,
                        file=sys.stderr,
                    )
                    print(
                        "MEMORYGUARD_CODEX_HOTFIX_SHA256=" + digest,
                        file=sys.stderr,
                    )
                    print(
                        "MEMORYGUARD_CODEX_HOTFIX_HOST="
                        + str(getattr(spec, "origin", "")),
                        file=sys.stderr,
                    )
    except Exception:
        # Python startup and Codex Hooks must stay fail-open.
        pass
