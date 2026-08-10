"""Phase4-C isolated acceptance fixture.

Prints exactly one JSON object.  It never opens a V1 store, Hook, MCP server,
or GUI; the baseline is an in-memory fixture representing the old packet.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys


def main() -> int:
    # Keep src-layout checkout runnable without installation.
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from memoryguard.runtime_v2.phase4_acceptance import phase4_acceptance_evidence

    report = phase4_acceptance_evidence()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
