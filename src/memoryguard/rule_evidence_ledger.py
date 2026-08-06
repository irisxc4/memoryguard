"""Append-only evidence contributions and their effective winner projection.

The contribution table is the durable source of truth.  The effective table is
only a rebuildable projection, so deactivating a winner never removes the
receipt/feedback rows that can become the next winner.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .schema_v3 import _now_iso, stable_hash

POLARITY_POSITIVE = "positive"
POLARITY_NEGATIVE = "negative"
VALID_POLARITIES = frozenset({POLARITY_POSITIVE, POLARITY_NEGATIVE})

EVIDENCE_CONTRIBUTIONS_TABLE = "rule_evidence_contributions"
EVIDENCE_EFFECTIVE_TABLE = "rule_evidence_effective"

# Kept as a standalone schema fragment so the migration and future Store
# bootstrap can share exactly the same table/index contract.
EVIDENCE_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_evidence_contributions (
    contribution_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    independence_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    polarity TEXT NOT NULL CHECK (polarity IN ('positive', 'negative')),
    authority INTEGER NOT NULL DEFAULT 0 CHECK (authority >= 0),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    observed_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    receipt_id TEXT NOT NULL DEFAULT '',
    feedback_id TEXT NOT NULL DEFAULT '',
    source_rule_id TEXT NOT NULL DEFAULT '',
    source_evidence_id TEXT NOT NULL DEFAULT '',
    source_memory_id TEXT NOT NULL DEFAULT '',
    source_ids TEXT NOT NULL DEFAULT '{}',
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    source_root_id TEXT NOT NULL DEFAULT '',
    source_object_id TEXT NOT NULL DEFAULT '',
    session_trusted INTEGER NOT NULL DEFAULT 0 CHECK (session_trusted IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE (definition_id, independence_key, kind, polarity, contribution_id),
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
);
CREATE INDEX IF NOT EXISTS idx_rule_evidence_contributions_group
    ON rule_evidence_contributions(definition_id, independence_key, kind);
CREATE INDEX IF NOT EXISTS idx_rule_evidence_contributions_active_group
    ON rule_evidence_contributions(
        definition_id, independence_key, kind, active,
        authority, confidence, observed_at, contribution_id
    );
CREATE INDEX IF NOT EXISTS idx_rule_evidence_contributions_receipt
    ON rule_evidence_contributions(receipt_id);
CREATE INDEX IF NOT EXISTS idx_rule_evidence_contributions_feedback
    ON rule_evidence_contributions(feedback_id);
CREATE INDEX IF NOT EXISTS idx_rule_evidence_contributions_source
    ON rule_evidence_contributions(source_evidence_id, source_memory_id, source_rule_id);

CREATE TABLE IF NOT EXISTS rule_evidence_effective (
    definition_id TEXT NOT NULL,
    independence_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    winner_contribution_id TEXT NOT NULL,
    polarity TEXT NOT NULL CHECK (polarity IN ('positive', 'negative')),
    authority INTEGER NOT NULL DEFAULT 0 CHECK (authority >= 0),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (definition_id, independence_key, kind),
    UNIQUE (winner_contribution_id),
    FOREIGN KEY (winner_contribution_id)
        REFERENCES rule_evidence_contributions(contribution_id)
);
CREATE INDEX IF NOT EXISTS idx_rule_evidence_effective_winner
    ON rule_evidence_effective(winner_contribution_id);
CREATE INDEX IF NOT EXISTS idx_rule_evidence_effective_definition
    ON rule_evidence_effective(definition_id, kind);
"""


@dataclass(frozen=True)
class EvidenceContribution:
    """One receipt/feedback contribution, including inactive history."""

    contribution_id: str
    definition_id: str = ""
    independence_key: str = ""
    kind: str = "receipt"
    polarity: str = POLARITY_POSITIVE
    authority: int = 0
    confidence: float = 1.0
    observed_at: str = ""
    active: bool = True
    receipt_id: str = ""
    feedback_id: str = ""
    source_rule_id: str = ""
    source_evidence_id: str = ""
    source_memory_id: str = ""
    source_ids: Mapping[str, str] = field(default_factory=dict)
    agent_instance_id: str = ""
    project_ref: str = ""
    share_group_id: str = ""
    session_id: str = ""
    source_root_id: str = ""
    source_object_id: str = ""
    session_trusted: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "contribution_id": self.contribution_id,
            "definition_id": self.definition_id,
            "independence_key": self.independence_key,
            "kind": self.kind,
            "polarity": self.polarity,
            "authority": self.authority,
            "confidence": self.confidence,
            "observed_at": self.observed_at,
            "active": self.active,
            "receipt_id": self.receipt_id,
            "feedback_id": self.feedback_id,
            "source_rule_id": self.source_rule_id,
            "source_evidence_id": self.source_evidence_id,
            "source_memory_id": self.source_memory_id,
            "source_ids": dict(self.source_ids),
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "share_group_id": self.share_group_id,
            "session_id": self.session_id,
            "source_root_id": self.source_root_id,
            "source_object_id": self.source_object_id,
            "session_trusted": self.session_trusted,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceContribution":
        return cls(
            contribution_id=str(data["contribution_id"]),
            definition_id=str(data.get("definition_id", "") or ""),
            independence_key=str(data.get("independence_key", "") or ""),
            kind=str(data.get("kind", "receipt") or "receipt"),
            polarity=str(data.get("polarity", POLARITY_POSITIVE) or POLARITY_POSITIVE),
            authority=int(data.get("authority", 0) or 0),
            confidence=(
                float(data.get("confidence", 1.0))
                if data.get("confidence", 1.0) is not None else 0.0
            ),
            observed_at=str(data.get("observed_at", "") or ""),
            active=_as_bool(data.get("active", True)),
            receipt_id=str(data.get("receipt_id", "") or ""),
            feedback_id=str(data.get("feedback_id", "") or ""),
            source_rule_id=str(data.get("source_rule_id", "") or ""),
            source_evidence_id=str(data.get("source_evidence_id", "") or ""),
            source_memory_id=str(data.get("source_memory_id", "") or ""),
            source_ids=_parse_source_ids(data.get("source_ids", {})),
            agent_instance_id=str(data.get("agent_instance_id", "") or ""),
            project_ref=str(data.get("project_ref", "") or ""),
            share_group_id=str(data.get("share_group_id", "") or ""),
            session_id=str(data.get("session_id", "") or ""),
            source_root_id=str(data.get("source_root_id", "") or ""),
            source_object_id=str(data.get("source_object_id", "") or ""),
            session_trusted=_as_bool(data.get("session_trusted", False)),
            created_at=str(data.get("created_at", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )


@dataclass(frozen=True)
class EffectiveEvidence:
    """The current winner for one effective grouping key."""

    definition_id: str
    independence_key: str
    kind: str
    winner_contribution_id: str
    polarity: str
    authority: int
    confidence: float
    observed_at: str
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition_id": self.definition_id,
            "independence_key": self.independence_key,
            "kind": self.kind,
            "winner_contribution_id": self.winner_contribution_id,
            "polarity": self.polarity,
            "authority": self.authority,
            "confidence": self.confidence,
            "observed_at": self.observed_at,
            "updated_at": self.updated_at,
        }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _parse_source_ids(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item or "") for key, item in value.items()}


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return getattr(row, key, default)


def _coerce_contribution(value: EvidenceContribution | Mapping[str, Any]) -> EvidenceContribution:
    if isinstance(value, EvidenceContribution):
        return value
    return EvidenceContribution.from_dict(value)


def contribution_from_row(row: Any) -> EvidenceContribution:
    """Convert a sqlite row (or row-shaped mapping) to a contribution."""
    return EvidenceContribution.from_dict({
        key: _row_value(row, key)
        for key in (
            "contribution_id", "definition_id", "independence_key", "kind",
            "polarity", "authority", "confidence", "observed_at", "active",
            "receipt_id", "feedback_id", "source_rule_id", "source_evidence_id",
            "source_memory_id", "source_ids", "agent_instance_id", "project_ref",
            "share_group_id", "session_id", "source_root_id", "source_object_id",
            "session_trusted", "created_at", "updated_at",
        )
    })


def build_contribution(
    *,
    definition_id: str,
    independence_key: str,
    kind: str = "receipt",
    polarity: str = POLARITY_POSITIVE,
    authority: int = 0,
    confidence: float = 1.0,
    observed_at: str = "",
    contribution_id: str = "",
    active: bool = True,
    receipt_id: str = "",
    feedback_id: str = "",
    source_rule_id: str = "",
    source_evidence_id: str = "",
    source_memory_id: str = "",
    source_ids: Mapping[str, str] | None = None,
    agent_instance_id: str = "",
    project_ref: str = "",
    share_group_id: str = "",
    session_id: str = "",
    source_root_id: str = "",
    source_object_id: str = "",
    session_trusted: bool = False,
) -> EvidenceContribution:
    """Build a stable contribution identity from source context."""
    ids = {str(key): str(value or "") for key, value in (source_ids or {}).items()}
    for key, value in {
        "receipt_id": receipt_id,
        "feedback_id": feedback_id,
        "source_rule_id": source_rule_id,
        "source_evidence_id": source_evidence_id,
        "source_memory_id": source_memory_id,
    }.items():
        if value and key not in ids:
            ids[key] = value
    contribution_id = contribution_id or stable_hash(
        "rule-evidence-contribution", definition_id, independence_key,
        kind, polarity, json.dumps(ids, sort_keys=True),
    )
    return EvidenceContribution(
        contribution_id=contribution_id,
        definition_id=definition_id,
        independence_key=independence_key,
        kind=kind,
        polarity=polarity,
        authority=authority,
        confidence=confidence,
        observed_at=observed_at or _now_iso(),
        active=active,
        receipt_id=receipt_id,
        feedback_id=feedback_id,
        source_rule_id=source_rule_id,
        source_evidence_id=source_evidence_id,
        source_memory_id=source_memory_id,
        source_ids=ids,
        agent_instance_id=agent_instance_id,
        project_ref=project_ref,
        share_group_id=share_group_id,
        session_id=session_id,
        source_root_id=source_root_id,
        source_object_id=source_object_id,
        session_trusted=session_trusted,
    )


def contribution_sort_key(
    contribution: EvidenceContribution | Mapping[str, Any],
) -> tuple[int, float, str, str]:
    """Return the descending deterministic rank used by every winner path."""
    confidence = _row_value(contribution, "confidence", 0.0)
    return (
        int(_row_value(contribution, "authority", 0) or 0),
        float(confidence) if confidence is not None else 0.0,
        str(_row_value(contribution, "observed_at", "") or ""),
        str(_row_value(contribution, "contribution_id", "") or ""),
    )


winner_sort_key = contribution_sort_key
rank_contribution = contribution_sort_key


def choose_winner(
    contributions: Iterable[EvidenceContribution | Mapping[str, Any]],
) -> EvidenceContribution | None:
    """Choose one active contribution; negative polarity is not special-cased."""
    active = [
        _coerce_contribution(item)
        for item in contributions
        if _as_bool(_row_value(item, "active", True))
    ]
    return max(active, key=contribution_sort_key) if active else None


select_effective_winner = choose_winner


def effective_group_key(
    contribution: EvidenceContribution | Mapping[str, Any],
) -> tuple[str, str, str]:
    return (
        str(_row_value(contribution, "definition_id", "") or ""),
        str(_row_value(contribution, "independence_key", "") or ""),
        str(_row_value(contribution, "kind", "") or ""),
    )


def compute_effective_winners(
    contributions: Iterable[EvidenceContribution | Mapping[str, Any]],
) -> list[EvidenceContribution]:
    """Pure, query-order-independent winner computation for all groups."""
    grouped: dict[tuple[str, str, str], list[EvidenceContribution]] = defaultdict(list)
    for item in contributions:
        contribution = _coerce_contribution(item)
        if contribution.active:
            grouped[effective_group_key(contribution)].append(contribution)
    return [
        winner
        for key in sorted(grouped)
        if (winner := choose_winner(grouped[key])) is not None
    ]


def upsert_contribution(
    conn: sqlite3.Connection,
    contribution: EvidenceContribution | Mapping[str, Any],
) -> EvidenceContribution:
    """Insert/update one contribution without touching any other receipt."""
    item = _coerce_contribution(contribution)
    _validate_contribution(item)
    payload = item.to_dict()
    conn.execute(
        """
        INSERT INTO rule_evidence_contributions (
            contribution_id, definition_id, independence_key, kind, polarity,
            authority, confidence, observed_at, active, receipt_id, feedback_id,
            source_rule_id, source_evidence_id, source_memory_id, source_ids,
            agent_instance_id, project_ref, share_group_id, session_id,
            source_root_id, source_object_id, session_trusted, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(contribution_id) DO UPDATE SET
            definition_id=excluded.definition_id,
            independence_key=excluded.independence_key,
            kind=excluded.kind,
            polarity=excluded.polarity,
            authority=excluded.authority,
            confidence=excluded.confidence,
            observed_at=excluded.observed_at,
            active=excluded.active,
            receipt_id=excluded.receipt_id,
            feedback_id=excluded.feedback_id,
            source_rule_id=excluded.source_rule_id,
            source_evidence_id=excluded.source_evidence_id,
            source_memory_id=excluded.source_memory_id,
            source_ids=excluded.source_ids,
            agent_instance_id=excluded.agent_instance_id,
            project_ref=excluded.project_ref,
            share_group_id=excluded.share_group_id,
            session_id=excluded.session_id,
            source_root_id=excluded.source_root_id,
            source_object_id=excluded.source_object_id,
            session_trusted=excluded.session_trusted,
            updated_at=excluded.updated_at
        """,
        (
            item.contribution_id, item.definition_id, item.independence_key,
            item.kind, item.polarity, item.authority, item.confidence,
            item.observed_at, int(item.active), item.receipt_id, item.feedback_id,
            item.source_rule_id, item.source_evidence_id, item.source_memory_id,
            json.dumps(payload["source_ids"], ensure_ascii=False, sort_keys=True),
            item.agent_instance_id, item.project_ref, item.share_group_id,
            item.session_id, item.source_root_id, item.source_object_id,
            int(item.session_trusted), item.created_at or item.observed_at,
            item.updated_at or item.observed_at,
        ),
    )
    # A contribution can move to a new Definition when a canonical generation
    # rebuilds with the same evidence_id but a changed merged body.  Its old
    # effective-projection row is stale and would violate the global
    # winner_contribution_id uniqueness on the next rebuild.
    conn.execute(
        """
        DELETE FROM rule_evidence_effective
        WHERE winner_contribution_id=?
          AND (
              definition_id<>?
              OR independence_key<>?
              OR kind<>?
          )
        """,
        (
            item.contribution_id,
            item.definition_id,
            item.independence_key,
            item.kind,
        ),
    )
    return item


upsert_evidence_contribution = upsert_contribution


def deactivate_contribution(conn: sqlite3.Connection, contribution_id: str) -> bool:
    """Deactivate one contribution, preserving its row for fallback rebuilds."""
    cursor = conn.execute(
        "UPDATE rule_evidence_contributions SET active=0, updated_at=? "
        "WHERE contribution_id=? AND active=1",
        (_now_iso(), contribution_id),
    )
    return cursor.rowcount > 0


def _validate_contribution(contribution: EvidenceContribution) -> None:
    if not contribution.definition_id:
        raise ValueError("definition_id is required")
    if not contribution.independence_key:
        raise ValueError("independence_key is required")
    if not contribution.kind:
        raise ValueError("kind is required")
    if contribution.polarity not in VALID_POLARITIES:
        raise ValueError("polarity must be positive or negative")
    if contribution.authority < 0:
        raise ValueError("authority must be non-negative")
    if not 0.0 <= contribution.confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")


def _scope_sql(
    *, definition_id: str | None,
    independence_key: str | None,
    kind: str | None,
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for column, value in (
        ("definition_id", definition_id),
        ("independence_key", independence_key),
        ("kind", kind),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    return (" AND ".join(clauses) or "1=1", params)


def list_contributions(
    conn: sqlite3.Connection,
    *,
    definition_id: str | None = None,
    independence_key: str | None = None,
    kind: str | None = None,
    active: bool | None = None,
) -> list[EvidenceContribution]:
    clauses, params = _scope_sql(
        definition_id=definition_id,
        independence_key=independence_key,
        kind=kind,
    )
    if active is not None:
        clauses += " AND active=?"
        params.append("1" if active else "0")
    rows = conn.execute(
        "SELECT * FROM rule_evidence_contributions WHERE " + clauses +
        " ORDER BY definition_id, independence_key, kind, contribution_id",
        params,
    ).fetchall()
    return [contribution_from_row(row) for row in rows]


def _effective_from_contribution(
    contribution: EvidenceContribution, *, updated_at: str,
) -> EffectiveEvidence:
    return EffectiveEvidence(
        definition_id=contribution.definition_id,
        independence_key=contribution.independence_key,
        kind=contribution.kind,
        winner_contribution_id=contribution.contribution_id,
        polarity=contribution.polarity,
        authority=contribution.authority,
        confidence=contribution.confidence,
        observed_at=contribution.observed_at,
        updated_at=updated_at,
    )


def rebuild_effective(
    conn: sqlite3.Connection,
    *,
    definition_id: str | None = None,
    independence_key: str | None = None,
    kind: str | None = None,
    updated_at: str | None = None,
) -> list[EffectiveEvidence]:
    """Rebuild a full or scoped effective projection, idempotently.

    Only projection rows are deleted.  Inactive and runner-up contributions
    remain untouched and can win on the next rebuild.
    """
    contributions = list_contributions(
        conn,
        definition_id=definition_id,
        independence_key=independence_key,
        kind=kind,
        active=True,
    )
    winners = compute_effective_winners(contributions)
    scope, params = _scope_sql(
        definition_id=definition_id,
        independence_key=independence_key,
        kind=kind,
    )
    projection_time = updated_at or _now_iso()
    for winner in winners:
        effective = _effective_from_contribution(
            winner, updated_at=projection_time,
        )
        conn.execute(
            """
            INSERT INTO rule_evidence_effective (
                definition_id, independence_key, kind,
                winner_contribution_id, polarity, authority, confidence,
                observed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(definition_id, independence_key, kind) DO UPDATE SET
                winner_contribution_id=excluded.winner_contribution_id,
                polarity=excluded.polarity,
                authority=excluded.authority,
                confidence=excluded.confidence,
                observed_at=excluded.observed_at,
                updated_at=CASE
                    WHEN rule_evidence_effective.winner_contribution_id =
                         excluded.winner_contribution_id
                    THEN rule_evidence_effective.updated_at
                    ELSE excluded.updated_at
                END
            """,
            (
                effective.definition_id, effective.independence_key,
                effective.kind, effective.winner_contribution_id,
                effective.polarity, effective.authority, effective.confidence,
                effective.observed_at, effective.updated_at,
            ),
        )
    # Remove only stale projection keys.  This preserves the projection
    # timestamp when a repeated rebuild selects the same winner, while still
    # clearing a key whose last active contribution was deactivated.
    conn.execute(
        """
        DELETE FROM rule_evidence_effective
        WHERE {scope}
          AND NOT EXISTS (
              SELECT 1
              FROM rule_evidence_contributions AS c
              WHERE c.active=1
                AND c.definition_id=rule_evidence_effective.definition_id
                AND c.independence_key=rule_evidence_effective.independence_key
                AND c.kind=rule_evidence_effective.kind
          )
        """.format(scope=scope),
        params,
    )
    return list_effective(
        conn,
        definition_id=definition_id,
        independence_key=independence_key,
        kind=kind,
    )


rebuild_effective_winners = rebuild_effective
rebuild_effective_projection = rebuild_effective


RuleEvidenceContribution = EvidenceContribution
RuleEvidenceEffective = EffectiveEvidence


def list_effective(
    conn: sqlite3.Connection,
    *,
    definition_id: str | None = None,
    independence_key: str | None = None,
    kind: str | None = None,
) -> list[EffectiveEvidence]:
    scope, params = _scope_sql(
        definition_id=definition_id,
        independence_key=independence_key,
        kind=kind,
    )
    rows = conn.execute(
        "SELECT * FROM rule_evidence_effective WHERE " + scope +
        " ORDER BY definition_id, independence_key, kind",
        params,
    ).fetchall()
    return [
        EffectiveEvidence(
            definition_id=row["definition_id"],
            independence_key=row["independence_key"],
            kind=row["kind"],
            winner_contribution_id=row["winner_contribution_id"],
            polarity=row["polarity"],
            authority=int(row["authority"] or 0),
            confidence=(
                float(row["confidence"])
                if row["confidence"] is not None else 0.0
            ),
            observed_at=row["observed_at"] or "",
            updated_at=row["updated_at"] or "",
        )
        for row in rows
    ]


list_effective_evidence = list_effective
