"""Emit one machine-readable, read-only Phase 8 readiness report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


def _read_json(path: Path | None, *, label: str) -> Mapping[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}_json_unreadable:{type(exc).__name__}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}_json_must_be_object")
    return value


def _blocked(code: str) -> dict[str, Any]:
    return {
        "schema": "memoryguard-v2-readiness-evidence-1",
        "status": "BLOCKED",
        "ok": False,
        "ready": False,
        "blockers": [{"code": code, "component": "acceptance", "status": "BLOCKED", "detail": {}}],
        "evidence": {
            "metrics": {"unknown": "NOT_EVALUATED"},
            "source_digest": "",
            "target_digest": "",
            "manifest_digest": "",
            "checkpoints": {},
            "validator_passed": False,
            "migration_id": "",
            "generation": None,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--data-home", type=Path)
    parser.add_argument("--phase4-json", type=Path)
    parser.add_argument("--native-coverage-json", type=Path)
    parser.add_argument("--expected-source-hashes-json", type=Path)
    parser.add_argument("--expected-native-registry-digest", default="")
    parser.add_argument("--page-size", type=int, default=256)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    try:
        from memoryguard.cutover_v2.evidence_assembler import ReadinessEvidenceAssembler
        from memoryguard.cutover_v2.facade import get_v2_runtime_facade
        from memoryguard.runtime_v2.phase4_acceptance import phase4_acceptance_evidence

        phase4 = (
            _read_json(args.phase4_json, label="phase4")
            if args.phase4_json is not None
            else phase4_acceptance_evidence()
        )
        if args.native_coverage_json is None:
            # Construction is lazy/read-only.  Pass the in-process capability,
            # not its self-reported JSON, so readiness can verify provenance.
            native = get_v2_runtime_facade(str(args.workspace)).ports.v2
        else:
            # External JSON remains useful diagnostics, but the assembler
            # deliberately marks it untrusted and cannot become READY from it.
            native = _read_json(args.native_coverage_json, label="native_coverage")
        expected = _read_json(args.expected_source_hashes_json, label="expected_source_hashes")
        assembler = ReadinessEvidenceAssembler(
            args.workspace,
            data_home=args.data_home,
            phase4_evidence=phase4,
            native_coverage=native,
            expected_source_hashes=expected,
            expected_native_registry_digest=args.expected_native_registry_digest,
            page_size=args.page_size,
            require_frozen_sources=True,
        )
        report = assembler.assemble().to_public_dict()
    except Exception as exc:
        report = _blocked(f"assembler_error:{type(exc).__name__}")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("ready") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
