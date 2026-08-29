from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.maintenance_v2.reference_audit import ReferenceAudit  # noqa: E402

from test_v2_reference_audit import _fixture  # noqa: E402


def test_reference_audit_exposes_scoped_health_evidence(tmp_path: Path) -> None:
    _fixture(tmp_path)

    public = ReferenceAudit(tmp_path).audit().to_public_dict()

    assert public["health_model"] == "v2_reference_integrity"
    assert public["health_model_version"] == 1
    assert public["health_scope"] == "reference_integrity"
    assert public["health_coverage"]["status"] == "complete"
    assert "runtime_leases" in public["health_coverage"]["out_of_scope"]
    assert public["health_available"] is True
    assert public["health_status"] == "available"
    assert public["health_score"] == 100.0
    assert all(item["status"] == "PASS" for item in public["health_components"].values())
    assert public["health_evidence"]["domain_count"] == len(public["domains"])
    assert public["health_evidence"]["blocker_count"] == 0


def test_reference_audit_does_not_score_incomplete_evidence(tmp_path: Path) -> None:
    public = ReferenceAudit(tmp_path).audit().to_public_dict()

    assert public["health_available"] is False
    assert public["health_status"] == "unavailable"
    assert public["health_score"] is None
    assert public["health_reason"] == "audit_evidence_incomplete"
    assert public["health_coverage"]["status"] == "inconclusive"
    assert public["health_evidence"]["audit_status"] == "BLOCKED"


def test_reference_defect_reduces_only_the_affected_component(tmp_path: Path) -> None:
    _fixture(tmp_path)
    skills = tmp_path / ".memoryguard" / "skills" / "skills.db"
    with sqlite3.connect(skills) as connection:
        connection.execute(
            "INSERT INTO skill_asset_refs(ref_id,version_id,asset_id,path,digest,asset_kind) "
            "VALUES('ref','version','missing-asset','','','')"
        )

    public = ReferenceAudit(tmp_path).audit().to_public_dict()

    assert public["health_available"] is True
    assert public["health_score"] == 75.0
    assert public["health_components"]["references"]["status"] == "BLOCKED"
    assert public["health_components"]["references"]["score"] == 0.0
    assert public["health_components"]["schema"]["score"] == 100.0
    assert public["health_evidence"]["inconclusive"] is False
