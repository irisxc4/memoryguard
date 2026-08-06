"""Quick GUI launcher - delegates to the secure localhost server.

This file previously had its own unsecured HTTP server with wildcard CORS
and no session token. It now redirects to the hardened open_localhost_window
which includes session tokens, API whitelist, and sandbox mutation deferral.

Runnable directly from a source checkout without installing the package:
the repo's ``src/`` directory is prepended to ``sys.path`` before the
package import below.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from memoryguard.gui import open_localhost_window  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    workspace = args[0] if args else "."
    rc, url = open_localhost_window(workspace, auto_open=True)
    if rc != 0:
        print(f"Failed to start GUI (exit code {rc})", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
