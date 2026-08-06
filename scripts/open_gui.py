"""Quick GUI launcher - delegates to the secure localhost server.

This file previously had its own unsecured HTTP server with wildcard CORS
and no session token. It now redirects to the hardened open_localhost_window
which includes session tokens, API whitelist, and sandbox mutation deferral.
"""
import sys
from pathlib import Path

# Determine workspace from command line or default
workspace = sys.argv[1] if len(sys.argv) > 1 else "."

from memoryguard.gui import open_localhost_window

rc, url = open_localhost_window(workspace, auto_open=True)
if rc != 0:
    print(f"Failed to start GUI (exit code {rc})")
    sys.exit(rc)
