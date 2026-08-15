"""Reconcile only MemoryGuard-owned Codex hook trust and enablement.

This maintenance entrypoint delegates to ``memoryguard.codex_hook_trust`` so
all writes use Codex's official app-server ``config/batchWrite`` API with
optimistic concurrency.  It never parses or rewrites ``config.toml`` itself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryguard.codex_hook_trust import reconcile_codex_memoryguard_hooks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", choices=("on", "off", "ensure"))
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--codex-cli", default="")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.state == "ensure":
        from memoryguard.codex_hook_trust import ensure_existing_codex_memoryguard_hooks

        result = ensure_existing_codex_memoryguard_hooks(
            codex_cli=args.codex_cli or None,
            timeout_seconds=args.timeout,
        )
    else:
        result = reconcile_codex_memoryguard_hooks(
            cwd=args.cwd,
            enabled=args.state == "on",
            codex_cli=args.codex_cli or None,
            timeout_seconds=args.timeout,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("verified") else 1


if __name__ == "__main__":
    raise SystemExit(main())
