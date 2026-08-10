from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "accept_v2_phase8.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("accept_v2_phase8_test_module", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _group(root: Path, name: str = "fixture-group") -> Path:
    path = root / ".memoryguard" / "shared-memory" / name / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "phase8 source body"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE records(memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, "
            "status TEXT, confidence REAL, locked INTEGER, injection_policy TEXT, "
            "priority INTEGER, supersedes TEXT, provenance TEXT, agent_instance_id TEXT, "
            "created_at TEXT, updated_at TEXT, canonical_hash TEXT, dedup_domain TEXT)"
        )
        conn.execute(
            "CREATE TABLE rule_assignments(memory_id TEXT, target_type TEXT, target_id TEXT, "
            "project_ref TEXT, effect TEXT, priority_override INTEGER, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "m1", body, "fact", "active", 0.9, 1, "always", 2, "[]", "[]",
                "agent-a", "t0", "t1", hashlib.sha256(body.encode()).hexdigest(), "relevant",
            ),
        )
        conn.execute(
            "INSERT INTO rule_assignments VALUES (?,?,?,?,?,?,?,?)",
            ("m1", "agent", "agent-a", "", "include", 2, "t0", "t1"),
        )
    return path


def _history(root: Path) -> Path:
    path = root / ".memoryguard" / "history" / "history.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversation_sessions(session_id TEXT PRIMARY KEY, external_id TEXT,
                title TEXT, provider TEXT, agent_instance_id TEXT, project_ref TEXT,
                share_group_id TEXT, created_at TEXT, imported_at TEXT);
            CREATE TABLE conversation_turns(turn_id TEXT PRIMARY KEY, session_id TEXT,
                ordinal INTEGER, role TEXT, content TEXT, created_at TEXT,
                event_key TEXT, content_hash TEXT);
            CREATE TABLE session_summaries(session_id TEXT PRIMARY KEY, summary TEXT,
                summary_kind TEXT, updated_at TEXT);
            """
        )
        conn.execute(
            "INSERT INTO conversation_sessions VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "external", "chat", "codex", "agent-a", "project-a", "fixture-group", "", ""),
        )
        content = "phase8 history"
        conn.execute(
            "INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)",
            ("t1", "s1", 0, "user", content, "", "event-1", hashlib.sha256(content.encode()).hexdigest()),
        )
        conn.execute("INSERT INTO session_summaries VALUES (?,?,?,?)", ("s1", "summary", "import", ""))
    return path


def _knowledge(data_home: Path) -> Path:
    path = data_home / "knowledge" / "knowledge.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE books(book_id TEXT PRIMARY KEY,title TEXT,root_path TEXT,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE documents(document_id TEXT PRIMARY KEY,book_id TEXT,relative_path TEXT,media_type TEXT,content_hash TEXT,status TEXT,updated_at TEXT);
            CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,document_id TEXT,book_id TEXT,ordinal INTEGER,text TEXT,text_hash TEXT,sensitivity TEXT,active INTEGER,created_at TEXT);
            CREATE TABLE memory_candidates(candidate_id TEXT PRIMARY KEY,book_id TEXT,chunk_id TEXT,content TEXT,source_text_hash TEXT,status TEXT,created_at TEXT);
            """
        )
        conn.execute("INSERT INTO books VALUES (?,?,?,?,?,?)", ("b1", "Book", "/book", "ready", "", ""))
        conn.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)", ("d1", "b1", "a.md", "text/plain", "h", "active", ""))
        conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)", ("c1", "d1", "b1", 0, "knowledge", "h", "normal", 1, ""))
    return path


def _ready_evidence(**kwargs: Any) -> dict[str, Any]:
    migration = kwargs["migration"]
    generation = kwargs["generation"]
    checkpoints = dict(migration["checkpoints"])
    source_hashes = dict(migration["source_hashes"])
    target_hashes = kwargs["target_hashes"]
    digest = lambda value: hashlib.sha256(repr(sorted(value.items())).encode()).hexdigest()
    return {
        "metrics": {
            "loss": 0,
            "orphan": 0,
            "outbox": {"pending": 0, "failed": 0},
            "scope": 0,
            "binding": 0,
            "leak": 0,
            "mandatory_equivalence": True,
            "recall_v2": 1,
            "recall_v1": 1,
            "tokens_v2": 1,
            "tokens_v1": 2,
        },
        "source_digest": digest(source_hashes),
        "target_digest": digest(target_hashes),
        "manifest_digest": hashlib.sha256(str(generation).encode()).hexdigest(),
        "checkpoints": checkpoints,
        "validator_passed": True,
        "migration_id": migration["migration_id"],
        "generation": generation,
    }


def test_default_synthetic_rehearsal_passes_and_real_preflight_is_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_script()
    control = tmp_path / "control"
    data_home = tmp_path / "source-data"
    sources = [_group(control), _history(control), _knowledge(data_home)]
    before = {str(path): (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()) for path in sources}
    fixture = tmp_path / "phase8-run"
    backed_up_sources: list[Path] = []
    online_backup = module._online_backup

    def tracked_backup(source: Path, target: Path) -> None:
        backed_up_sources.append(source.resolve())
        online_backup(source, target)

    monkeypatch.setattr(module, "_online_backup", tracked_backup)

    report = module.build_report(
        control,
        data_home=data_home,
        fixture_workspace=fixture,
        migration_id="phase8-test",
        readiness_assembler=_ready_evidence,
    )

    assert report["ok"] is True, report
    assert report["outcomes"]["synthetic_rehearsal"]["status"] == "PASS"
    assert report["outcomes"]["real_workspace_preflight"]["status"] == "BLOCKED"
    assert report["outcomes"]["real_workspace_preflight"]["unchanged"] is True
    assert report["source_mode"] == "synthetic"
    assert report["states"] == ["V2_BUILDING", "V2_READY", "V2_ACTIVE", "V1_ACTIVE"]
    assert report["checks"]["online_backup"] is True
    assert report["checks"]["readonly_validate"] is True
    assert report["checks"]["native_v2_smoke"] is True
    assert report["checks"]["control_unchanged"] is True
    assert report["checks"]["source_copies_unchanged"] is True
    assert report["checks"]["fixture_cleaned"] is True
    assert report["checks"]["source_fixture_cleaned"] is True
    assert not fixture.exists()
    assert not (control / ".memoryguard" / "system" / "manifest.db").exists()
    assert {str(path): (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()) for path in sources} == before
    assert backed_up_sources
    assert all(control.resolve() not in source.parents for source in backed_up_sources)


def test_explicit_source_fixture_uses_online_backup(tmp_path: Path) -> None:
    module = _load_script()
    control = tmp_path / "control"
    _group(control, "control-group")
    source_fixture = tmp_path / "source-fixture"
    source = _group(source_fixture, "copy-me")
    before = (source.stat().st_size, hashlib.sha256(source.read_bytes()).hexdigest())

    report = module.build_report(
        control,
        source_fixture=source_fixture,
        selected_sources=["memory:copy-me"],
        readiness_assembler=_ready_evidence,
    )

    assert report["ok"] is True, report
    assert report["source_mode"] == "explicit_source_fixture"
    assert report["source_keys"] == ["memory:copy-me"]
    assert report["checks"]["online_backup"] is True
    assert (source.stat().st_size, hashlib.sha256(source.read_bytes()).hexdigest()) == before


def test_real_workspace_target_is_rejected_before_manifest_write(tmp_path: Path) -> None:
    module = _load_script()
    control = tmp_path / "control"
    source = _group(control)
    before = source.read_bytes()

    report = module.build_report(
        control,
        fixture_workspace=control,
        readiness_assembler=_ready_evidence,
    )

    assert report["ok"] is False
    assert report["error_code"] == "unsafe_fixture_target"
    assert source.read_bytes() == before
    assert not (control / ".memoryguard" / "system" / "manifest.db").exists()


def test_blocked_readiness_never_transitions_and_fixture_is_cleaned(tmp_path: Path) -> None:
    module = _load_script()
    control = tmp_path / "control"
    _group(control)
    fixture = tmp_path / "phase8-blocked"

    def blocked(**kwargs: Any) -> dict[str, Any]:
        evidence = _ready_evidence(**kwargs)
        evidence["metrics"]["unknown_columns"] = 1
        return evidence

    report = module.build_report(
        control,
        fixture_workspace=fixture,
        readiness_assembler=blocked,
    )

    assert report["ok"] is False
    assert report["error_code"] == "readiness_blocked"
    assert "V2_READY" not in report["states"]
    assert report["checks"]["fixture_cleaned"] is True
    assert not fixture.exists()
    assert not (control / ".memoryguard" / "system" / "manifest.db").exists()


def test_control_workspace_cannot_be_copy_source_without_large_copy_opt_in(tmp_path: Path) -> None:
    module = _load_script()
    control = tmp_path / "control"
    _group(control)

    report = module.build_report(
        control,
        source_fixture=control,
        readiness_assembler=_ready_evidence,
    )

    assert report["ok"] is False
    assert report["error_code"] == "real_source_copy_requires_opt_in"
    assert report["outcomes"]["real_workspace_preflight"]["status"] == "BLOCKED"
    assert report["outcomes"]["real_workspace_preflight"]["unchanged"] is True


def test_default_real_assembler_preflight_blocks_but_synthetic_rehearsal_passes(tmp_path: Path) -> None:
    module = _load_script()
    control = tmp_path / "control"
    _group(control)

    report = module.build_report(control)

    assert report["ok"] is True, report
    assert report["outcomes"]["synthetic_rehearsal"]["status"] == "PASS"
    preflight = report["outcomes"]["real_workspace_preflight"]
    assert preflight["status"] == "BLOCKED"
    assert preflight["readiness_status"] == "BLOCKED"
    blocker_codes = [item["code"] for item in preflight["readiness"]["blockers"]]
    assert blocker_codes
    assert "readiness_assembler_unavailable" not in blocker_codes
    assert report["checks"]["fixture_cleaned"] is True
    assert report["checks"]["source_fixture_cleaned"] is True
    assert report["checks"]["control_unchanged"] is True
