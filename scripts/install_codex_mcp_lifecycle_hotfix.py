"""Install the isolated Codex MCP lifecycle hotfix wheel.

The installer verifies the wheel's version and required lifecycle safety code,
then installs it into the current user's Python. It never edits Codex hooks or
configuration; the existing trusted hook command imports the installed package
on its next process.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import subprocess
import sys
import zipfile
from pathlib import Path


EXPECTED_VERSION = "0.7.1.post18"
WHEEL_NAME = "agent_memguard-0.7.1.post18-py3-none-any.whl"


def _verify_wheel(wheel: Path) -> str:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit("hotfix wheel metadata is missing or ambiguous")
        metadata = archive.read(metadata_names[0]).decode("utf-8", errors="strict")
        if f"Version: {EXPECTED_VERSION}" not in {
            line.strip() for line in metadata.splitlines()
        }:
            raise SystemExit("hotfix wheel version mismatch")
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
            raise SystemExit("hotfix wheel is missing lifecycle, hook, provider, or subagent integration")
        lifecycle = archive.read(lifecycle_name).decode("utf-8", errors="strict")
        hooks = archive.read(hook_name).decode("utf-8", errors="strict")
        subagents = archive.read(subagent_name).decode("utf-8", errors="strict")
        if (
            'if mode != "force"' not in lifecycle
            or 'allow_termination=selected_mode == "force"' not in lifecycle
        ):
            raise SystemExit("hotfix wheel is missing the auto-mode termination gate")
        if (
            "_best_effort_codex_mcp_lifecycle" not in hooks
            or "reconcile_closed_edge_rollout_activities" not in hooks
        ):
            raise SystemExit("hotfix wheel is missing host-hook integration")
        if (
            "reconcile_closed_edge_rollout_activities" not in subagents
            or 'terminal[child] = "missing_rollout"' not in subagents
        ):
            raise SystemExit("hotfix wheel is missing subagent rollout reconciliation")
    return digest


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    wheel = repo / "dist-hotfix-subagent-rollout-post18" / WHEEL_NAME
    if not wheel.is_file():
        raise SystemExit(f"hotfix wheel missing: {wheel}")
    digest = _verify_wheel(wheel)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--user",
            "--upgrade",
            "--force-reinstall",
            "--no-deps",
            str(wheel),
        ],
        check=True,
    )

    verify = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            (
                "import importlib.metadata as m; "
                "import memoryguard.host_hooks as h; "
                "import memoryguard.codex_mcp_lifecycle as c; "
                "import memoryguard.codex_subagent_reconcile as s; "
                f"assert m.version('agent-memguard') == '{EXPECTED_VERSION}'; "
                "assert hasattr(h, '_best_effort_codex_mcp_lifecycle'); "
                "assert hasattr(s, 'reconcile_closed_edge_rollout_activities'); "
                "assert c.WindowsProcessController().allow_termination is False; "
                "print('memoryguard codex subagent rollout hotfix: installed')"
            ),
        ],
        check=False,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if verify.returncode != 0:
        detail = (verify.stderr or verify.stdout or "verification failed").strip()
        raise SystemExit(detail)
    print(verify.stdout.strip())
    print("sha256=" + digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
