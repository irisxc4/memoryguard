from __future__ import annotations

import pytest

from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort


GROUP = "shared-native-canonical-status"


def _record(store: RuleV2Store, *, scope_id: str, read_path: str, updated_at: str, digest: str) -> None:
    store.record_canonical_state(
        {
            "scope_id": scope_id,
            "share_group_id": GROUP,
            "activation_status": "active",
            "read_path": read_path,
            "canonical_digest": digest,
            "updated_at": updated_at,
        }
    )


def test_canonical_status_skips_newer_invalid_historical_row(tmp_path):
    store = RuleV2Store(tmp_path)
    _record(
        store,
        scope_id="valid-history",
        read_path="native-v2",
        updated_at="2026-01-01T00:00:00Z",
        digest="valid-digest",
    )
    _record(
        store,
        scope_id="invalid-residue",
        read_path="retired-path",
        updated_at="2026-02-01T00:00:00Z",
        digest="invalid-digest",
    )

    result = NativeV2RuntimePort(tmp_path)._canonical_status(
        {}, {"share_group_id": GROUP}
    )

    assert result["status"] == "READY"
    assert result["canonical_digest"] == "valid-digest"
    assert result["read_path"] == "rule-intelligence"
    assert result["observed_read_path"] == "retired-path"
    assert result["reason"] == "newer_invalid_canonical_state_ignored"


@pytest.mark.parametrize(
    "historical_path",
    ["native-v2", "native_v2", "rule_intelligence", "rule-intelligence", "v2", "native"],
)
def test_canonical_status_normalizes_historical_read_path(tmp_path, historical_path):
    store = RuleV2Store(tmp_path)
    _record(
        store,
        scope_id="historical-path",
        read_path=historical_path,
        updated_at="2026-01-01T00:00:00Z",
        digest=historical_path,
    )

    result = NativeV2RuntimePort(tmp_path)._canonical_status(
        {}, {"share_group_id": GROUP}
    )

    assert result["status"] == "READY"
    assert result["read_path"] == "rule-intelligence"


def test_canonical_status_fails_closed_with_observed_invalid_path(tmp_path):
    store = RuleV2Store(tmp_path)
    _record(
        store,
        scope_id="invalid-only",
        read_path="retired-path",
        updated_at="2026-02-01T00:00:00Z",
        digest="invalid-digest",
    )

    result = NativeV2RuntimePort(tmp_path)._canonical_status(
        {}, {"share_group_id": GROUP}
    )

    assert result["status"] == "BLOCKED"
    assert result["read_path"] == "unknown"
    assert result["observed_read_path"] == "retired-path"
    assert result["reason"] == "v2_canonical_read_path_unavailable"
