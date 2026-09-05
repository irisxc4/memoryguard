"""Narrow regressions for the source-aware V2 rule-merge MCP path."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.cutover_v2.surfaces import (
    MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES,
    RULE_MUTATION_MCP_NAMES,
)
from memoryguard.mcp_server import (
    TOOL_DEFINITIONS,
    _MUTATING_TOOLS,
    _V2_RULE_MERGE_TOOLS,
    _validate_v2_mcp_arguments,
)
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.runtime_v2.rule_merge_native import NativeRuleMergeService


GROUP = "group-a"
OTHER_GROUP = "group-b"


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path, *, admin: bool = True, group: str = GROUP):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id="session-a",
            session_source="host",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id=group,
        project_ref="project-a",
        provider="codex",
    )


def _seed(
    store: RuleV2Store,
    text: str,
    *,
    kind: str,
    definition_id: str,
    source_id: str,
    group: str = GROUP,
    strength: str = "must",
):
    definition = build_definition(text, kind=kind, rule_strength=strength)
    definition = definition.__class__(
        **{**definition.to_dict(), "definition_id": definition_id}
    )
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id=group,
        target_type="group",
        target_id=group,
        binding_id=f"binding-{definition_id}",
    ))
    store.upsert_source_link(
        source_kind="native",
        share_group_id=group,
        memory_id=source_id,
        source_ref=source_id,
        source_revision="1",
        original_definition_id=definition.definition_id,
        canonical_definition_id=definition.definition_id,
        status="active",
    )
    store.record_evidence_ref({
        "evidence_id": f"evidence-{source_id}",
        "definition_id": definition.definition_id,
        "source_rule_id": source_id,
        "share_group_id": group,
        "evidence_ref": source_id,
    })
    store._write(lambda conn: conn.execute(
        "UPDATE rule_definitions SET text=? WHERE definition_id=?",
        (text, definition_id),
    ))
    return store.get_definition(definition_id)


def _counts(store: RuleV2Store) -> dict[str, int]:
    tables = (
        "rule_definitions",
        "rule_definition_versions",
        "rule_definition_aliases",
        "rule_source_links",
        "rule_evidence_refs",
        "rule_receipt_refs",
        "rule_decisions",
        "rule_bindings",
        "rule_merge_native_requests",
        "rule_canonical_state",
    )
    return store._read(lambda conn: {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in tables
    })


def _statuses(store: RuleV2Store) -> dict[str, str]:
    return {
        item.definition_id: str(item.status)
        for item in store.list_definitions()
    }


def _service(workspace: Path) -> NativeRuleMergeService:
    RuleV2Store(workspace)
    return NativeRuleMergeService(workspace, state_provider=_Manifest())


def _payload(
    *,
    canonical_source_id: str,
    duplicate_source_ids: list[str],
    revisions: dict[str, int],
    confirmed: bool = True,
    key: str = "merge-safe-1",
    receipt: str = "receipt-merge-safe-1",
    extra: dict | None = None,
) -> dict:
    payload = {
        "canonical_source_id": canonical_source_id,
        "duplicate_source_ids": duplicate_source_ids,
        "confirmed": confirmed,
        "expected_definition_revisions": revisions,
        "mutation_receipt": {"receipt_id": receipt},
        "idempotency_key": key,
    }
    if extra:
        payload.update(extra)
    return payload


def test_same_group_source_ids_merge_conserves_evidence_and_leaves_unrequested(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    winner = _seed(
        store,
        "Always use rtk for shell commands before every command.",
        kind="procedure",
        definition_id="def-canonical",
        source_id="3bf686a37a705d30",
    )
    loser = _seed(
        store,
        "Always use rtk for shell commands",
        kind="fact",
        definition_id="def-duplicate",
        source_id="5d1a9089cb7dd8ce",
    )
    extra = _seed(
        store,
        "Always use rtk for shell commands",
        kind="preference",
        definition_id="def-extra",
        source_id="source-unrequested",
    )
    before = _counts(store)
    service = _service(tmp_path)
    request = _payload(
        canonical_source_id="3bf686a37a705d30",
        duplicate_source_ids=["5d1a9089cb7dd8ce"],
        revisions={
            winner.definition_id: int(winner.revision or 0),
            loser.definition_id: int(loser.revision or 0),
        },
    )
    result = service.dispatch(
        "memoryguard_rule_merge_safe",
        request,
        context=_context(tmp_path),
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["canonical_definition_id"] == winner.definition_id
    assert data["merged_definition_ids"] == [loser.definition_id]
    assert data["decision_ids"]
    assert data["undo_ids"] == data["decision_ids"]
    assert set(data["source_ids"]) >= {"3bf686a37a705d30", "5d1a9089cb7dd8ce"}

    assert store.get_definition(loser.definition_id).status == "alias"
    assert store.get_definition(loser.definition_id).superseded_by == winner.definition_id
    assert store.get_definition(extra.definition_id).status == "active"
    assert store.resolve_canonical(loser.definition_id) == winner.definition_id

    links = store.list_source_links(share_group_id=GROUP, status="active")
    by_source = {link["memory_id"]: link["canonical_definition_id"] for link in links}
    assert by_source["3bf686a37a705d30"] == winner.definition_id
    assert by_source["5d1a9089cb7dd8ce"] == winner.definition_id
    assert by_source["source-unrequested"] == extra.definition_id

    evidence_ids = store._read(lambda conn: {
        row["evidence_id"]: row["definition_id"]
        for row in conn.execute("SELECT evidence_id, definition_id FROM rule_evidence_refs")
    })
    assert evidence_ids["evidence-3bf686a37a705d30"] == winner.definition_id
    assert evidence_ids["evidence-5d1a9089cb7dd8ce"] == winner.definition_id
    assert evidence_ids["evidence-source-unrequested"] == extra.definition_id

    active_bindings = store.list_bindings(share_group_id=GROUP, status="active")
    bound_defs = {binding.definition_id for binding in active_bindings}
    assert winner.definition_id in bound_defs
    assert extra.definition_id in bound_defs
    assert loser.definition_id not in bound_defs

    after = _counts(store)
    assert after["rule_source_links"] == before["rule_source_links"]
    assert after["rule_evidence_refs"] == before["rule_evidence_refs"]
    assert after["rule_definition_aliases"] == before["rule_definition_aliases"] + 1
    assert after["rule_definition_versions"] >= before["rule_definition_versions"] + 2
    assert after["rule_decisions"] == before["rule_decisions"] + 1
    decision = store._read(lambda conn: conn.execute(
        "SELECT decision_id, undo_id, action FROM rule_decisions WHERE decision_id=?",
        (data["decision_ids"][0],),
    ).fetchone())
    assert str(decision["action"]) == "historical_duplicate_fold"
    assert str(decision["undo_id"]) == data["undo_ids"][0]
    replay = service.dispatch(
        "memoryguard_rule_merge_safe",
        request,
        context=_context(tmp_path),
        generation=7,
        state="V2_ACTIVE",
    )
    assert replay["ok"] is True
    assert replay["data"]["idempotent_replay"] is True
    assert replay["data"]["canonical_definition_id"] == winner.definition_id
    assert _counts(store) == after


def test_merge_safe_folds_relevant_exact_or_equivalent_into_mandatory_canonical(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "exact",
            "Always use rtk for shell commands before every command.",
            "Always use rtk for shell commands before every command.",
        ),
        (
            "equivalent",
            "Always use rtk for shell commands before every command.",
            "Always use rtk for shell commands",
        ),
    )
    for label, canonical_text, duplicate_text in cases:
        workspace = tmp_path / label
        store = RuleV2Store(workspace)
        winner = _seed(
            store,
            canonical_text,
            kind="procedure",
            definition_id="def-canonical",
            source_id="source-canonical",
            strength="must",
        )
        loser = _seed(
            store,
            duplicate_text,
            kind="fact",
            definition_id="def-relevant",
            source_id="source-relevant",
            strength="observation",
        )
        assert winner.rule_strength == "must"
        assert loser.rule_strength == "observation"
        before = _counts(store)
        service = _service(workspace)
        request = _payload(
            canonical_source_id="source-canonical",
            duplicate_source_ids=["source-relevant"],
            revisions={
                winner.definition_id: int(winner.revision or 0),
                loser.definition_id: int(loser.revision or 0),
            },
            key=f"merge-safe-relevant-{label}",
            receipt=f"receipt-merge-safe-relevant-{label}",
        )
        result = service.dispatch(
            "memoryguard_rule_merge_safe",
            request,
            context=_context(workspace),
            generation=7,
            state="V2_ACTIVE",
        )
        assert result["ok"] is True, label
        data = result["data"]
        assert data["canonical_definition_id"] == winner.definition_id
        assert data["merged_definition_ids"] == [loser.definition_id]
        assert data["decision_ids"]
        assert data["undo_ids"] == data["decision_ids"]
        assert set(data["source_ids"]) >= {"source-canonical", "source-relevant"}

        canonical = store.get_definition(winner.definition_id)
        assert canonical.status == "active"
        assert canonical.rule_strength == "must"
        assert store.get_definition(loser.definition_id).status == "alias"
        assert store.get_definition(loser.definition_id).superseded_by == winner.definition_id
        assert store.resolve_canonical(loser.definition_id) == winner.definition_id

        body = store._read(lambda conn: conn.execute(
            "SELECT text FROM rule_definitions WHERE definition_id=?",
            (winner.definition_id,),
        ).fetchone()[0])
        assert "rtk" in str(body).casefold()
        if canonical_text != duplicate_text:
            assert str(body).strip() != duplicate_text.strip()
            assert "before every command" in str(body).casefold()

        links = store.list_source_links(share_group_id=GROUP, status="active")
        by_source = {link["memory_id"]: link["canonical_definition_id"] for link in links}
        assert by_source["source-canonical"] == winner.definition_id
        assert by_source["source-relevant"] == winner.definition_id

        evidence_ids = store._read(lambda conn: {
            row["evidence_id"]: row["definition_id"]
            for row in conn.execute(
                "SELECT evidence_id, definition_id FROM rule_evidence_refs"
            )
        })
        assert evidence_ids["evidence-source-canonical"] == winner.definition_id
        assert evidence_ids["evidence-source-relevant"] == winner.definition_id

        active_bindings = store.list_bindings(share_group_id=GROUP, status="active")
        bound_defs = {binding.definition_id for binding in active_bindings}
        assert winner.definition_id in bound_defs
        assert loser.definition_id not in bound_defs

        after = _counts(store)
        assert after["rule_source_links"] == before["rule_source_links"]
        assert after["rule_evidence_refs"] == before["rule_evidence_refs"]
        assert after["rule_definition_aliases"] == before["rule_definition_aliases"] + 1
        assert after["rule_definition_versions"] >= before["rule_definition_versions"] + 2
        assert after["rule_decisions"] == before["rule_decisions"] + 1
        decision = store._read(lambda conn: conn.execute(
            "SELECT decision_id, undo_id, action FROM rule_decisions WHERE decision_id=?",
            (data["decision_ids"][0],),
        ).fetchone())
        assert str(decision["action"]) == "historical_duplicate_fold"
        assert str(decision["undo_id"]) == data["undo_ids"][0]
        replay = service.dispatch(
            "memoryguard_rule_merge_safe",
            request,
            context=_context(workspace),
            generation=7,
            state="V2_ACTIVE",
        )
        assert replay["ok"] is True, label
        assert replay["data"]["idempotent_replay"] is True
        assert replay["data"]["canonical_definition_id"] == winner.definition_id
        assert _counts(store) == after


def test_merge_safe_rejects_relevant_canonical_with_mandatory_duplicate_zero_writes(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    relevant = _seed(
        store,
        "Always use rtk for shell commands before every command.",
        kind="procedure",
        definition_id="def-relevant",
        source_id="source-relevant",
        strength="observation",
    )
    mandatory = _seed(
        store,
        "Always use rtk for shell commands before every command.",
        kind="fact",
        definition_id="def-mandatory",
        source_id="source-mandatory",
        strength="must",
    )
    service = _service(tmp_path)
    before = _counts(store)
    before_status = _statuses(store)
    result = service.dispatch(
        "memoryguard_rule_merge_safe",
        _payload(
            canonical_source_id="source-relevant",
            duplicate_source_ids=["source-mandatory"],
            revisions={
                relevant.definition_id: int(relevant.revision or 0),
                mandatory.definition_id: int(mandatory.revision or 0),
            },
            key="merge-safe-reverse-relevant",
            receipt="receipt-merge-safe-reverse-relevant",
        ),
        context=_context(tmp_path),
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is False
    assert result["code"] == "rule_merge_pair_not_mergeable"
    dumped = json.dumps(result)
    assert str(tmp_path) not in dumped
    assert "rules.db" not in dumped
    assert _counts(store) == before
    assert _statuses(store) == before_status
    assert store.get_definition(relevant.definition_id).rule_strength == "observation"
    assert store.get_definition(mandatory.definition_id).rule_strength == "must"


def test_merge_safe_fail_closed_zero_writes(tmp_path: Path) -> None:
    store = RuleV2Store(tmp_path)
    canonical = _seed(
        store,
        "Always use rtk for shell commands",
        kind="procedure",
        definition_id="def-keep",
        source_id="source-keep",
    )
    duplicate = _seed(
        store,
        "Always use rtk for shell commands",
        kind="fact",
        definition_id="def-dup",
        source_id="source-dup",
    )
    unrelated = _seed(
        store,
        "Never share secrets in prompt text",
        kind="procedure",
        definition_id="def-unrelated",
        source_id="source-unrelated",
    )
    conflict = _seed(
        store,
        "Never use rtk for shell commands",
        kind="fact",
        definition_id="def-conflict",
        source_id="source-conflict",
    )
    foreign = _seed(
        store,
        "Always use rtk for shell commands",
        kind="fact",
        definition_id="def-foreign",
        source_id="source-foreign",
        group=OTHER_GROUP,
    )
    revisions = {
        canonical.definition_id: int(canonical.revision or 0),
        duplicate.definition_id: int(duplicate.revision or 0),
    }
    service = _service(tmp_path)
    before = _counts(store)
    before_status = _statuses(store)

    cases = [
        (
            "native_admin_capability_required",
            _payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-dup"],
                revisions=revisions,
            ),
            _context(tmp_path, admin=False),
        ),
        (
            "rule_merge_target_not_found",
            _payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-foreign"],
                revisions={
                    canonical.definition_id: int(canonical.revision or 0),
                    foreign.definition_id: int(foreign.revision or 0),
                },
            ),
            _context(tmp_path),
        ),
        (
            "rule_merge_pair_not_mergeable",
            _payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-unrelated"],
                revisions={
                    canonical.definition_id: int(canonical.revision or 0),
                    unrelated.definition_id: int(unrelated.revision or 0),
                },
            ),
            _context(tmp_path),
        ),
        (
            "rule_merge_pair_not_mergeable",
            _payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-conflict"],
                revisions={
                    canonical.definition_id: int(canonical.revision or 0),
                    conflict.definition_id: int(conflict.revision or 0),
                },
            ),
            _context(tmp_path),
        ),
        (
            "definition_revision_mismatch",
            _payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-dup"],
                revisions={
                    canonical.definition_id: int(canonical.revision or 0) + 9,
                    duplicate.definition_id: int(duplicate.revision or 0),
                },
            ),
            _context(tmp_path),
        ),
        (
            "confirmation_required",
            _payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-dup"],
                revisions=revisions,
                confirmed=False,
            ),
            _context(tmp_path),
        ),
    ]
    rendered = []
    for code, payload, context in cases:
        result = service.dispatch(
            "memoryguard_rule_merge_safe",
            payload,
            context=context,
            generation=7,
            state="V2_ACTIVE",
        )
        assert result["ok"] is False
        assert result["code"] == code
        dumped = json.dumps(result)
        assert str(tmp_path) not in dumped
        assert "rules.db" not in dumped
        rendered.append(dumped)
        assert _counts(store) == before
        assert _statuses(store) == before_status

    del rendered
    del foreign


def test_rule_merge_safe_public_mcp_cutover_contract(tmp_path: Path) -> None:
    name = "memoryguard_rule_merge_safe"
    assert name in MCP_TOOL_NAMES
    assert name in MCP_MUTATION_NAMES
    assert name in RULE_MUTATION_MCP_NAMES
    assert name in _MUTATING_TOOLS
    assert name in _V2_RULE_MERGE_TOOLS
    tools = TOOL_DEFINITIONS
    schema = tools[name]["inputSchema"]
    required = set(schema["required"])
    assert {
        "confirmed",
        "expected_definition_revisions",
        "mutation_receipt",
        "idempotency_key",
    } <= required
    properties = schema["properties"]
    assert "canonical_source_id" in properties
    assert "canonical_definition_id" in properties
    assert "duplicate_source_ids" in properties
    assert "duplicate_definition_ids" in properties
    assert "force" not in properties
    assert "bypass" not in properties
    _validate_v2_mcp_arguments(name, {
        "confirmed": True,
        "canonical_source_id": "source-keep",
        "duplicate_source_ids": ["source-dup"],
        "expected_definition_revisions": {"def-keep": 1, "def-dup": 1},
        "mutation_receipt": {"receipt_id": "merge-safe-schema-check"},
        "idempotency_key": "merge-safe-schema-check",
    })
    entries = {
        item["name"]: item
        for item in NativeV2RuntimePort(
            tmp_path, state_provider=_Manifest(),
        ).coverage()["surfaces"]["mcp"]["entries"]
    }
    assert entries[name]["status"] == "implemented"
    assert entries[name]["mutation"] is True
    assert entries[name]["handler"] == "rule_merge_safe"


def _preview_payload(
    *,
    canonical_source_id: str = "",
    duplicate_source_ids: list[str] | None = None,
    canonical_definition_id: str = "",
    duplicate_definition_ids: list[str] | None = None,
) -> dict:
    payload: dict = {}
    if canonical_source_id:
        payload["canonical_source_id"] = canonical_source_id
    if duplicate_source_ids:
        payload["duplicate_source_ids"] = duplicate_source_ids
    if canonical_definition_id:
        payload["canonical_definition_id"] = canonical_definition_id
    if duplicate_definition_ids:
        payload["duplicate_definition_ids"] = duplicate_definition_ids
    return payload


def test_merge_safe_preview_source_only_relevant_duplicate_resolves_definition_revision(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    winner = _seed(
        store,
        "Always use rtk for shell commands before every command.",
        kind="procedure",
        definition_id="def-canonical",
        source_id="source-canonical",
        strength="must",
    )
    loser = _seed(
        store,
        "Always use rtk for shell commands",
        kind="fact",
        definition_id="def-relevant",
        source_id="source-relevant",
        strength="observation",
    )
    service = _service(tmp_path)
    result = service.dispatch(
        "memoryguard_rule_merge_safe_preview",
        _preview_payload(
            canonical_source_id="source-canonical",
            duplicate_source_ids=["source-relevant"],
        ),
        context=_context(tmp_path),
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True
    data = result["data"]
    canonical = data["canonical"]
    duplicate = data["duplicates"][0]
    assert canonical["source_id"] == "source-canonical"
    assert canonical["definition_id"] == winner.definition_id
    assert canonical["revision"] == int(winner.revision or 0)
    assert canonical["strength"] == "must"
    assert canonical["layer"] == "mandatory"
    assert canonical["status"] == "active"
    assert duplicate["source_id"] == "source-relevant"
    assert duplicate["definition_id"] == loser.definition_id
    assert duplicate["revision"] == int(loser.revision or 0)
    assert duplicate["strength"] == "observation"
    assert duplicate["layer"] == "relevant"
    assert duplicate["status"] == "active"
    assert duplicate["relation"] in {"exact", "equivalent", "update"}
    assert duplicate["reason"]
    assert set(canonical) == {
        "source_id", "definition_id", "revision", "strength", "layer", "status",
    }
    assert set(duplicate) == {
        "source_id", "definition_id", "revision", "strength", "layer", "status",
        "relation", "reason",
    }
    assert data["expected_definition_revisions"] == {
        winner.definition_id: int(winner.revision or 0),
        loser.definition_id: int(loser.revision or 0),
    }
    dumped = json.dumps(result)
    assert str(tmp_path) not in dumped
    assert "rules.db" not in dumped
    assert OTHER_GROUP not in dumped
    assert "def-foreign" not in dumped


def test_merge_safe_preview_zero_writes(tmp_path: Path) -> None:
    store = RuleV2Store(tmp_path)
    winner = _seed(
        store,
        "Always use rtk for shell commands before every command.",
        kind="procedure",
        definition_id="def-canonical",
        source_id="source-canonical",
        strength="must",
    )
    loser = _seed(
        store,
        "Always use rtk for shell commands",
        kind="fact",
        definition_id="def-relevant",
        source_id="source-relevant",
        strength="observation",
    )
    extra = _seed(
        store,
        "Never share secrets in prompt text",
        kind="procedure",
        definition_id="def-extra",
        source_id="source-extra",
    )
    service = _service(tmp_path)
    before = _counts(store)
    before_status = _statuses(store)
    db_before = store.db_path.read_bytes()
    result = service.dispatch(
        "memoryguard_rule_merge_safe_preview",
        _preview_payload(
            canonical_source_id="source-canonical",
            duplicate_source_ids=["source-relevant"],
        ),
        context=_context(tmp_path),
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True
    assert result["data"]["canonical"]["definition_id"] == winner.definition_id
    assert result["data"]["duplicates"][0]["definition_id"] == loser.definition_id
    assert extra.definition_id not in json.dumps(result)
    assert _counts(store) == before
    assert _statuses(store) == before_status
    assert store.db_path.read_bytes() == db_before
    assert store.get_definition(winner.definition_id).status == "active"
    assert store.get_definition(loser.definition_id).status == "active"
    assert store.get_definition(extra.definition_id).status == "active"


def test_merge_safe_preview_fail_closed_zero_writes(tmp_path: Path) -> None:
    store = RuleV2Store(tmp_path)
    canonical = _seed(
        store,
        "Always use rtk for shell commands",
        kind="procedure",
        definition_id="def-keep",
        source_id="source-keep",
    )
    _seed(
        store,
        "Always use rtk for shell commands",
        kind="fact",
        definition_id="def-dup",
        source_id="source-dup",
    )
    _seed(
        store,
        "Never share secrets in prompt text",
        kind="procedure",
        definition_id="def-unrelated",
        source_id="source-unrelated",
    )
    _seed(
        store,
        "Never use rtk for shell commands",
        kind="fact",
        definition_id="def-conflict",
        source_id="source-conflict",
    )
    foreign = _seed(
        store,
        "Always use rtk for shell commands",
        kind="fact",
        definition_id="def-foreign",
        source_id="source-foreign",
        group=OTHER_GROUP,
    )
    relevant = _seed(
        store,
        "Always use rtk for shell commands before every command.",
        kind="procedure",
        definition_id="def-relevant",
        source_id="source-relevant",
        strength="observation",
    )
    mandatory = _seed(
        store,
        "Always use rtk for shell commands before every command.",
        kind="fact",
        definition_id="def-mandatory",
        source_id="source-mandatory",
        strength="must",
    )
    service = _service(tmp_path)
    before = _counts(store)
    before_status = _statuses(store)
    db_before = store.db_path.read_bytes()
    cases = [
        (
            "native_admin_capability_required",
            _preview_payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-dup"],
            ),
            _context(tmp_path, admin=False),
        ),
        (
            "rule_merge_target_not_found",
            _preview_payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-foreign"],
            ),
            _context(tmp_path),
        ),
        (
            "rule_merge_pair_not_mergeable",
            _preview_payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-unrelated"],
            ),
            _context(tmp_path),
        ),
        (
            "rule_merge_pair_not_mergeable",
            _preview_payload(
                canonical_source_id="source-keep",
                duplicate_source_ids=["source-conflict"],
            ),
            _context(tmp_path),
        ),
        (
            "rule_merge_pair_not_mergeable",
            _preview_payload(
                canonical_source_id="source-relevant",
                duplicate_source_ids=["source-mandatory"],
            ),
            _context(tmp_path),
        ),
    ]
    for code, payload, context in cases:
        result = service.dispatch(
            "memoryguard_rule_merge_safe_preview",
            payload,
            context=context,
            generation=7,
            state="V2_ACTIVE",
        )
        assert result["ok"] is False
        assert result["code"] == code
        dumped = json.dumps(result)
        assert str(tmp_path) not in dumped
        assert "rules.db" not in dumped
        assert foreign.definition_id not in dumped
        assert OTHER_GROUP not in dumped
        assert _counts(store) == before
        assert _statuses(store) == before_status
        assert store.db_path.read_bytes() == db_before
    assert store.get_definition(canonical.definition_id).status == "active"
    assert store.get_definition(relevant.definition_id).rule_strength == "observation"
    assert store.get_definition(mandatory.definition_id).rule_strength == "must"


def test_merge_safe_preview_revisions_fill_safe_merge(tmp_path: Path) -> None:
    store = RuleV2Store(tmp_path)
    winner = _seed(
        store,
        "Always use rtk for shell commands before every command.",
        kind="procedure",
        definition_id="def-canonical",
        source_id="source-canonical",
        strength="must",
    )
    loser = _seed(
        store,
        "Always use rtk for shell commands",
        kind="fact",
        definition_id="def-relevant",
        source_id="source-relevant",
        strength="observation",
    )
    service = _service(tmp_path)
    preview = service.dispatch(
        "memoryguard_rule_merge_safe_preview",
        _preview_payload(
            canonical_source_id="source-canonical",
            duplicate_source_ids=["source-relevant"],
        ),
        context=_context(tmp_path),
        generation=7,
        state="V2_ACTIVE",
    )
    assert preview["ok"] is True
    revisions = preview["data"]["expected_definition_revisions"]
    assert revisions == {
        winner.definition_id: int(winner.revision or 0),
        loser.definition_id: int(loser.revision or 0),
    }
    result = service.dispatch(
        "memoryguard_rule_merge_safe",
        _payload(
            canonical_source_id="source-canonical",
            duplicate_source_ids=["source-relevant"],
            revisions=revisions,
            key="merge-safe-from-preview",
            receipt="receipt-merge-safe-from-preview",
        ),
        context=_context(tmp_path),
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["canonical_definition_id"] == winner.definition_id
    assert data["merged_definition_ids"] == [loser.definition_id]
    assert store.get_definition(loser.definition_id).status == "alias"
    assert store.resolve_canonical(loser.definition_id) == winner.definition_id


def test_rule_merge_safe_preview_public_mcp_cutover_contract(tmp_path: Path) -> None:
    name = "memoryguard_rule_merge_safe_preview"
    assert name in MCP_TOOL_NAMES
    assert name not in MCP_MUTATION_NAMES
    assert name not in RULE_MUTATION_MCP_NAMES
    assert name not in _MUTATING_TOOLS
    assert name in _V2_RULE_MERGE_TOOLS
    tools = TOOL_DEFINITIONS
    schema = tools[name]["inputSchema"]
    properties = schema["properties"]
    required = set(schema.get("required") or ())
    assert "confirmed" not in properties
    assert "mutation_receipt" not in properties
    assert "idempotency_key" not in properties
    assert "expected_definition_revisions" not in properties
    assert "force" not in properties
    assert "bypass" not in properties
    assert required.isdisjoint({
        "confirmed",
        "expected_definition_revisions",
        "mutation_receipt",
        "idempotency_key",
    })
    assert "canonical_source_id" in properties
    assert "canonical_definition_id" in properties
    assert "duplicate_source_ids" in properties
    assert "duplicate_definition_ids" in properties
    assert "workspace" in properties
    description = str(tools[name].get("description") or "").casefold()
    assert "read-only" in description or "does not write" in description
    _validate_v2_mcp_arguments(name, {
        "canonical_source_id": "source-keep",
        "duplicate_source_ids": ["source-dup"],
    })
    entries = {
        item["name"]: item
        for item in NativeV2RuntimePort(
            tmp_path, state_provider=_Manifest(),
        ).coverage()["surfaces"]["mcp"]["entries"]
    }
    assert entries[name]["status"] == "implemented"
    assert entries[name]["mutation"] is False
    assert entries[name]["handler"] == "rule_merge_safe_preview"
