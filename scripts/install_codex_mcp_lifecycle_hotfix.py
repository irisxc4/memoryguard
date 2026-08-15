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


EXPECTED_VERSION = "0.7.1.post6"
WHEEL_NAME = "agent_memguard-0.7.1.post6-py3-none-any.whl"


def _verify_wheel(wheel: Path) -> str:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit("hotfix wheel metadata is missing or ambiguous")
        metadata = archive.read(metadata_names[0]).decode("utf-8", errors="strict")
        if f"\nVersion: {EXPECTED_VERSION}\n" not in "\n" + metadata:
            raise SystemExit("hotfix wheel version mismatch")
        lifecycle_name = "memoryguard/codex_mcp_lifecycle.py"
        hook_name = "memoryguard/host_hooks.py"
        provider_name = "memoryguard/provider_adapters.py"
        if (
            lifecycle_name not in names
            or hook_name not in names
            or provider_name not in names
        ):
            raise SystemExit("hotfix wheel is missing lifecycle, hook, or provider integration")
        lifecycle = archive.read(lifecycle_name).decode("utf-8", errors="strict")
        hooks = archive.read(hook_name).decode("utf-8", errors="strict")
        providers = archive.read(provider_name).decode("utf-8", errors="strict")
        if f'SHIM_VERSION = "{EXPECTED_VERSION}"' not in lifecycle:
            raise SystemExit("hotfix wheel lifecycle version mismatch")
        if 'PROTECTED_ROOT_NAMES = frozenset({"python.exe"})' not in lifecycle:
            raise SystemExit("hotfix wheel does not protect Python stdio transports")
        if "_best_effort_codex_mcp_lifecycle" not in hooks:
            raise SystemExit("hotfix wheel is missing host-hook integration")
        if '["-X", "utf8", "-m", "memoryguard.mcp_server"]' not in providers:
            raise SystemExit("hotfix wheel would reinstall the unsafe MCP launch arguments")
    return digest


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    wheel = repo / "dist-hotfix-final13" / WHEEL_NAME
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
                f"assert m.version('agent-memguard') == '{EXPECTED_VERSION}'; "
                "assert hasattr(h, '_best_effort_codex_mcp_lifecycle'); "
                f"assert c.SHIM_VERSION == '{EXPECTED_VERSION}'; "
                "assert 'python.exe' in c.PROTECTED_ROOT_NAMES; "
                "print('memoryguard codex lifecycle hotfix: installed')"
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
