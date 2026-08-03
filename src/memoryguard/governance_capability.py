"""Server-issued, single-use capabilities for rule-merge governance.

The bearer token is returned only from :func:`issue_capability`; the database
stores its SHA-256 digest and never the opaque token itself.  Consumption is
connection-owned: the caller supplies the SQLite connection and owns the
surrounding transaction.  This lets a later Store atomically consume a
capability together with approval, first-merge acknowledgment, and cooldown
updates.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .access_context import AccessContext


RULE_MERGE_APPROVE_SCOPE = "rule_merge_approve"
CAPABILITY_SCOPE = RULE_MERGE_APPROVE_SCOPE
CAPABILITY_TABLE = "governance_capabilities"

# Keep the token opaque and reject legacy values such as ``admin:...`` or a
# short ordinary string before looking anything up.  The lower bound is
# intentionally format-only; the database digest remains the authority.
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")
_TOKEN_BYTES = 32


GOVERNANCE_CAPABILITY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {CAPABILITY_TABLE} (
    token_hash TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope = '{RULE_MERGE_APPROVE_SCOPE}'),
    proposal_id TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0, 1)),
    consumed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_governance_capabilities_proposal
    ON {CAPABILITY_TABLE}(proposal_id);
CREATE INDEX IF NOT EXISTS idx_governance_capabilities_principal_scope
    ON {CAPABILITY_TABLE}(principal, scope);
""".strip()

# Public alias for Store/migration code that prefers a shorter name.
CAPABILITY_SCHEMA = GOVERNANCE_CAPABILITY_SCHEMA


class CapabilityError(ValueError):
    """Base class for fail-closed capability issuance/consumption errors."""


class CapabilityIssueError(CapabilityError):
    """The trusted admin context or issue request is invalid."""


class CapabilityRejected(CapabilityError):
    """The presented capability is forged, mismatched, expired, or consumed."""


@dataclass(frozen=True)
class CapabilityRecord:
    """Persisted capability metadata; it deliberately contains no raw token."""

    token_hash: str
    principal: str
    scope: str
    proposal_id: str
    nonce: str
    issued_at: float
    expires_at: float
    consumed: bool
    consumed_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> "CapabilityRecord":
        def value(name: str, index: int) -> Any:
            try:
                return row[name]  # type: ignore[index]
            except (IndexError, TypeError):
                return row[index]  # type: ignore[index]

        return cls(
            token_hash=str(value("token_hash", 0)),
            principal=str(value("principal", 1)),
            scope=str(value("scope", 2)),
            proposal_id=str(value("proposal_id", 3)),
            nonce=str(value("nonce", 4)),
            issued_at=float(value("issued_at", 5)),
            expires_at=float(value("expires_at", 6)),
            consumed=bool(value("consumed", 7)),
            consumed_at=(
                None
                if value("consumed_at", 8) is None
                else float(value("consumed_at", 8))
            ),
        )


def initialize_capability_schema(conn: sqlite3.Connection) -> None:
    """Create the capability table/indexes without changing Store data."""
    conn.executescript(GOVERNANCE_CAPABILITY_SCHEMA)


def _connection(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("a caller-owned sqlite3.Connection is required")
    return conn


def _epoch(value: float | int | datetime | None, *, default: float) -> float:
    if value is None:
        return float(default)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("capability timestamps must be timezone-aware")
        return float(value.timestamp())
    return float(value)


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError(f"{field} is required")
    return value


def _require_scope(scope: str) -> None:
    if scope != RULE_MERGE_APPROVE_SCOPE:
        raise CapabilityRejected("capability scope rejected")


def _require_token(token: Any) -> str:
    if not isinstance(token, str) or not _OPAQUE_TOKEN_RE.fullmatch(token):
        raise CapabilityRejected("opaque capability token rejected")
    if token.startswith("admin:"):
        raise CapabilityRejected("legacy admin capability rejected")
    return token


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _principal_from_context(access_context: AccessContext) -> str:
    if not isinstance(access_context, AccessContext):
        raise CapabilityIssueError("trusted AccessContext required")
    checker = getattr(access_context, "require_capability_issue", None)
    if callable(checker):
        ok, error = checker()
    else:  # Compatible with older AccessContext implementations.
        ok, error = access_context.require_admin()
        if ok and not access_context.trusted_agent_id:
            ok, error = False, "trusted principal required for capability issuance"
    if not ok:
        raise CapabilityIssueError(error)
    return _require_text(access_context.principal, "principal")


def issue_capability(
    conn: sqlite3.Connection,
    access_context: AccessContext,
    proposal_id: str,
    *,
    scope: str = RULE_MERGE_APPROVE_SCOPE,
    principal: str | None = None,
    ttl_seconds: float = 300.0,
    issued_at: float | int | datetime | None = None,
    expires_at: float | int | datetime | None = None,
) -> str:
    """Issue and persist one opaque capability, returning its raw token once.

    The principal is derived from the trusted context.  If supplied by a
    caller for convenience, it must match that derived principal exactly.
    The operation participates in the caller's current SQLite transaction and
    does not commit it.
    """
    connection = _connection(conn)
    _require_scope(scope)
    trusted_principal = _principal_from_context(access_context)
    if principal is not None and principal != trusted_principal:
        raise CapabilityIssueError("capability principal must match AccessContext")
    proposal = _require_text(proposal_id, "proposal_id")
    if ttl_seconds <= 0 and expires_at is None:
        raise CapabilityIssueError("capability ttl_seconds must be > 0")

    issued = _epoch(issued_at, default=time.time())
    expiry = _epoch(
        expires_at,
        default=issued + float(ttl_seconds),
    )
    if expiry <= issued:
        raise CapabilityIssueError("capability expiry must be after issued_at")

    # A collision is cryptographically negligible; retrying keeps the API
    # correct even under a deliberately patched RNG in tests.
    for _ in range(3):
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        digest = _token_hash(token)
        nonce = secrets.token_urlsafe(16)
        try:
            connection.execute(
                f"""
                INSERT INTO {CAPABILITY_TABLE} (
                    token_hash, principal, scope, proposal_id, nonce,
                    issued_at, expires_at, consumed, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (digest, trusted_principal, RULE_MERGE_APPROVE_SCOPE,
                 proposal, nonce, issued, expiry),
            )
        except sqlite3.IntegrityError as exc:
            if "token_hash" in str(exc) or "nonce" in str(exc):
                continue
            raise
        return token
    raise CapabilityIssueError("could not allocate a unique capability")


def _consume_record(
    conn: sqlite3.Connection,
    token: str,
    *,
    principal: str,
    proposal_id: str,
    scope: str,
    now: float | int | datetime | None,
) -> CapabilityRecord:
    connection = _connection(conn)
    opaque_token = _require_token(token)
    expected_principal = _require_text(principal, "principal")
    proposal = _require_text(proposal_id, "proposal_id")
    _require_scope(scope)
    current_time = _epoch(now, default=time.time())
    digest = _token_hash(opaque_token)

    # This conditional UPDATE is the consume gate.  SQLite serializes writers,
    # so concurrent callers can observe at most one rowcount == 1.  No commit
    # occurs here: the supplied connection owns the larger governance tx.
    updated = connection.execute(
        f"""
        UPDATE {CAPABILITY_TABLE}
        SET consumed = 1, consumed_at = ?
        WHERE token_hash = ?
          AND principal = ?
          AND scope = ?
          AND proposal_id = ?
          AND expires_at > ?
          AND consumed = 0
        """,
        (current_time, digest, expected_principal, RULE_MERGE_APPROVE_SCOPE,
         proposal, current_time),
    )
    if updated.rowcount != 1:
        raise CapabilityRejected(
            "capability rejected: invalid, expired, mismatched, or consumed"
        )

    row = connection.execute(
        f"""
        SELECT token_hash, principal, scope, proposal_id, nonce,
               issued_at, expires_at, consumed, consumed_at
        FROM {CAPABILITY_TABLE}
        WHERE token_hash = ?
        """,
        (digest,),
    ).fetchone()
    if row is None:  # Defensive: the successful UPDATE must have a row.
        raise CapabilityRejected("capability record disappeared")
    return CapabilityRecord.from_row(row)


def consume_capability(
    conn: sqlite3.Connection,
    token: str,
    *,
    principal: str,
    proposal_id: str,
    scope: str = RULE_MERGE_APPROVE_SCOPE,
    now: float | int | datetime | None = None,
) -> bool:
    """Atomically validate and consume a capability on the owned connection."""
    _consume_record(
        conn, token, principal=principal, proposal_id=proposal_id,
        scope=scope, now=now,
    )
    return True


def consume_capability_record(
    conn: sqlite3.Connection,
    token: str,
    *,
    principal: str,
    proposal_id: str,
    scope: str = RULE_MERGE_APPROVE_SCOPE,
    now: float | int | datetime | None = None,
) -> CapabilityRecord:
    """Variant returning consumed metadata for Store audit/transaction code."""
    return _consume_record(
        conn, token, principal=principal, proposal_id=proposal_id,
        scope=scope, now=now,
    )


class CapabilityStore:
    """Convenience facade over the explicit connection-owned API."""

    def initialize_schema(self, conn: sqlite3.Connection) -> None:
        initialize_capability_schema(conn)

    def issue(self, conn: sqlite3.Connection, access_context: AccessContext,
              proposal_id: str, **kwargs: Any) -> str:
        return issue_capability(conn, access_context, proposal_id, **kwargs)

    def consume(self, conn: sqlite3.Connection, token: str, **kwargs: Any) -> bool:
        return consume_capability(conn, token, **kwargs)

    def consume_record(self, conn: sqlite3.Connection, token: str,
                       **kwargs: Any) -> CapabilityRecord:
        return consume_capability_record(conn, token, **kwargs)


# Explicit names for future Store integration and callers that prefer the
# governance-specific class name.
GovernanceCapabilityStore = CapabilityStore
issue_server_capability = issue_capability
consume_server_capability = consume_capability


__all__ = [
    "CAPABILITY_SCHEMA",
    "CAPABILITY_SCOPE",
    "CAPABILITY_TABLE",
    "CapabilityError",
    "CapabilityIssueError",
    "CapabilityRecord",
    "CapabilityRejected",
    "CapabilityStore",
    "GovernanceCapabilityStore",
    "GOVERNANCE_CAPABILITY_SCHEMA",
    "RULE_MERGE_APPROVE_SCOPE",
    "consume_capability",
    "consume_capability_record",
    "consume_server_capability",
    "initialize_capability_schema",
    "issue_capability",
    "issue_server_capability",
]
