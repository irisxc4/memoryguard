import sqlite3
import threading

import pytest

from memoryguard.access_context import load_access_context
from memoryguard.governance_capability import (
    CAPABILITY_TABLE,
    CapabilityIssueError,
    CapabilityRejected,
    GOVERNANCE_CAPABILITY_SCHEMA,
    RULE_MERGE_APPROVE_SCOPE,
    consume_capability,
    initialize_capability_schema,
    issue_capability,
)


def _admin_context(monkeypatch, principal="admin-agent"):
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", principal)
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    return load_access_context()


def _memory_db():
    conn = sqlite3.connect(":memory:")
    initialize_capability_schema(conn)
    return conn


def test_issue_requires_trusted_admin_and_stores_only_hash(monkeypatch):
    conn = _memory_db()
    try:
        monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "admin-agent")
        monkeypatch.setenv("MEMORYGUARD_ADMIN", "")
        non_admin = load_access_context()
        with pytest.raises(CapabilityIssueError):
            issue_capability(conn, non_admin, "proposal-1")
        with pytest.raises(CapabilityIssueError):
            issue_capability(conn, object(), "proposal-1")

        admin = _admin_context(monkeypatch)
        token = issue_capability(conn, admin, "proposal-1")
        conn.commit()
        row = conn.execute(
            f"SELECT token_hash, principal, scope, proposal_id, nonce "
            f"FROM {CAPABILITY_TABLE}"
        ).fetchone()
        assert row is not None
        assert token not in row
        assert len(row[0]) == 64
        assert row[1] == "admin-agent"
        assert row[2] == RULE_MERGE_APPROVE_SCOPE
        assert row[3] == "proposal-1"
        assert row[4]
    finally:
        conn.close()


def test_forged_admin_prefix_and_ordinary_string_are_rejected(monkeypatch):
    conn = _memory_db()
    try:
        token = issue_capability(conn, _admin_context(monkeypatch), "proposal-1")
        conn.commit()
        for forged in ("admin:forged", "ordinary-string", "A" * 43):
            with pytest.raises(CapabilityRejected):
                consume_capability(
                    conn, forged, principal="admin-agent", proposal_id="proposal-1"
                )
        conn.rollback()
        assert conn.execute(
            f"SELECT consumed FROM {CAPABILITY_TABLE}"
        ).fetchone()[0] == 0
        assert token
    finally:
        conn.close()


def test_consume_rejects_replay_expiry_cross_proposal_scope_and_principal(monkeypatch):
    conn = _memory_db()
    try:
        admin = _admin_context(monkeypatch)
        token = issue_capability(
            conn, admin, "proposal-1", issued_at=100, expires_at=200
        )
        conn.commit()

        with pytest.raises(CapabilityRejected):
            consume_capability(
                conn, token, principal="admin-agent", proposal_id="proposal-2", now=150
            )
        with pytest.raises(CapabilityRejected):
            consume_capability(
                conn, token, principal="other-agent", proposal_id="proposal-1", now=150
            )
        with pytest.raises(CapabilityRejected):
            consume_capability(
                conn, token, principal="admin-agent", proposal_id="proposal-1",
                scope="wrong-scope", now=150,
            )
        with pytest.raises(CapabilityRejected):
            consume_capability(
                conn, token, principal="admin-agent", proposal_id="proposal-1", now=201
            )

        assert consume_capability(
            conn, token, principal="admin-agent", proposal_id="proposal-1", now=150
        )
        conn.commit()
        with pytest.raises(CapabilityRejected):
            consume_capability(
                conn, token, principal="admin-agent", proposal_id="proposal-1", now=150
            )
    finally:
        conn.close()


def test_concurrent_consume_only_one_connection_wins(tmp_path, monkeypatch):
    db_path = tmp_path / "capabilities.sqlite3"
    setup = sqlite3.connect(str(db_path))
    initialize_capability_schema(setup)
    token = issue_capability(setup, _admin_context(monkeypatch), "proposal-1")
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def consume_once():
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            barrier.wait()
            try:
                ok = consume_capability(
                    conn, token, principal="admin-agent", proposal_id="proposal-1"
                )
                conn.commit()
                results.append(ok)
            except CapabilityRejected:
                conn.rollback()
        except Exception as exc:  # pragma: no cover - diagnostic only
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=consume_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert results == [True]
    verify = sqlite3.connect(str(db_path))
    try:
        assert verify.execute(
            f"SELECT consumed FROM {CAPABILITY_TABLE}"
        ).fetchone()[0] == 1
    finally:
        verify.close()


def test_schema_is_public_and_scope_is_fixed():
    assert CAPABILITY_TABLE in GOVERNANCE_CAPABILITY_SCHEMA
    assert RULE_MERGE_APPROVE_SCOPE in GOVERNANCE_CAPABILITY_SCHEMA
    assert "token_hash" in GOVERNANCE_CAPABILITY_SCHEMA
    assert "consumed" in GOVERNANCE_CAPABILITY_SCHEMA
