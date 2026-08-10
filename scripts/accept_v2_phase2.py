#!/usr/bin/env python3
"""Machine-readable V2 Phase 2 shadow-build acceptance gate.

Default mode is read-only validation.  ``--write-shadow`` is intended for an
isolated fixture/worktree and still never promotes the manifest or switches a
runtime read/write path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard.migration.v2_coordinator import V2MigrationCoordinator  # noqa: E402


def _domain_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    metrics = value.get("metrics") if isinstance(value.get("metrics"), Mapping) else {}
    return {
        "status": value.get("status", ""),
        "ok": bool(value.get("ok")),
        "counts": dict(metrics.get("counts") or value.get("counts") or {}),
        "loss": metrics.get("loss", value.get("loss", "NOT_EVALUATED")),
        "orphan": metrics.get("evidence_orphan", value.get("orphan", 0)),
        "outbox": {
            "memory": metrics.get("outbox_pending", value.get("outbox_pending", 0)),
            "rules": metrics.get("rule_evidence_outbox_pending", value.get("evidence_pending", 0)),
        },
        "binding_multiset_diff": metrics.get("binding_identity_multiset_diff", value.get("binding_multiset_diff", 0)),
        "auto_expansion": metrics.get("auto_scope_expansion", value.get("system_auto_expansion", 0)),
        "unknown_authoritative": metrics.get("unknown_authoritative", value.get("unknown_authoritative", 0)),
        "acl_digest": metrics.get("acl_digest", value.get("acl_digest", "")),
        "errors": list(value.get("errors") or []),
    }


def _output(result: Mapping[str, Any], *, dry_run: bool) -> dict[str, Any]:
    validation = result.get("validation") if isinstance(result.get("validation"), Mapping) else {}
    domains = validation.get("domains") if isinstance(validation.get("domains"), Mapping) else {}
    domain_summary = {str(name): _domain_summary(value) for name, value in domains.items() if isinstance(value, Mapping)}
    manifest_state = str(result.get("manifest_state") or "")
    checks = {
        "manifest_state": {"ok": dry_run or manifest_state == "V2_BUILDING", "value": manifest_state},
        "not_ready": {"ok": result.get("ready") is False and result.get("can_promote") is False},
        "validation": {"ok": dry_run or str(validation.get("status") or "") in {"PASS", "NO_SOURCE", "NOT_CONFIGURED"}},
    }
    errors: list[str] = []
    for item in list(result.get("errors") or []) + list(validation.get("errors") or []):
        if str(item) not in errors:
            errors.append(str(item))
    return {
        "status": result.get("status"),
        "ok": bool(result.get("ok")) and all(item["ok"] for item in checks.values()),
        "ready": False,
        "can_promote": False,
        "dry_run": dry_run,
        "manifest_state": manifest_state,
        "migration_id": result.get("migration_id", ""),
        "checkpoints": result.get("checkpoints", {}),
        "source_hashes": result.get("source_hashes", {}),
        "source_status": validation.get("source_status", {}),
        "domains": domain_summary,
        "validation_status": validation.get("status", "NOT_EVALUATED"),
        "errors": errors,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--data-home", type=Path)
    parser.add_argument("--global-source-pointer", type=Path)
    parser.add_argument("--migration-id", default="")
    parser.add_argument("--write-shadow", action="store_true", help="run isolated V2 shadow build; never promote")
    parser.add_argument("--strict", action="store_true", help="raise on the first failed domain")
    parser.add_argument("--json", action="store_true", help="emit JSON (default also emits JSON for automation)")
    args = parser.parse_args(argv)
    coordinator = V2MigrationCoordinator(
        args.workspace,
        data_home=args.data_home,
        global_source_pointer=args.global_source_pointer,
        migration_id=args.migration_id or None,
    )
    result = coordinator.run(dry_run=not args.write_shadow, strict=args.strict)
    payload = _output(result.to_dict(), dry_run=not args.write_shadow)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
