"""Agent 记忆发现/勾选/IR 摄取端到端硬断言。"""
from __future__ import annotations

from pathlib import Path

import pytest

from memoryguard.agent_locator import AgentLocator
from memoryguard.agent_profiles import (
    AgentProfileRegistry, _claude_code_profile, _codex_profile,
    _cursor_profile, _trae_profile, expand_path,
)
from memoryguard.content_parsers import parse_file
from memoryguard.memory_ir import MemoryNormalizer
from memoryguard.schema_v3 import (
    CoverageLedger, IngestionPolicy, SourceCategory, SourceObject, SourceSnapshot,
    stable_hash,
)
from memoryguard.source_registry import DirectoryAdapter, META_EXTS, ScanBudget
from memoryguard.schema_v3 import SourceRoot, SourceRootType

FIXTURES = Path(__file__).parent / "fixtures" / "agent_memories"
HOME = FIXTURES / "home"
APPDATA = FIXTURES / "appdata"


@pytest.fixture(scope="module", autouse=True)
def _ensure_fixtures():
    from _build_agent_memory_fixtures import main
    main()


@pytest.fixture
def fake_home(monkeypatch):
    home = HOME.resolve()
    appdata = APPDATA.resolve()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setenv("APPDATA", str(appdata))
    return home


def test_claude_native_memory_import_verbatim_default(fake_home, tmp_path):
    profile = _claude_code_profile()
    mem = next(s for s in profile.surfaces if s.surface_id == "claude_project_native_memory")
    assert mem.ingestion_policy == IngestionPolicy.IMPORT_VERBATIM
    assert mem.category == SourceCategory.NATIVE_MEMORY

    locator = AgentLocator(tmp_path)
    # inject surfaces with fake home resolution
    instances, _ = locator.detect_instances()
    claude = next((i for i in instances if i.product == "claude-code"), None)
    assert claude is not None
    tree = locator.get_selection_tree(claude.instance_id)
    files = []
    for scope in tree.get("scopes", []):
        for proj in scope.get("projects", []):
            for cat in proj.get("categories", []):
                files.extend(cat.get("files", []))
        for cat in scope.get("categories", []):
            files.extend(cat.get("files", []))
    mem_files = [f for f in files if "memory" in f.get("path", "").replace("\\", "/") and f.get("path", "").endswith(".md")]
    assert mem_files, f"expected memory md files in tree, got {[f.get('path') for f in files][:20]}"
    assert any(f.get("default_selected") for f in mem_files if f.get("ingestion_policy") == "import_verbatim")


def test_selection_tree_lists_second_third_level_files(fake_home, tmp_path):
    locator = AgentLocator(tmp_path)
    instances, _ = locator.detect_instances()
    by_product = {i.product: i for i in instances}
    assert "claude-code" in by_product
    assert "cursor" in by_product
    assert "codex" in by_product
    assert "trae" in by_product

    def collect(instance_id: str) -> list[str]:
        tree = locator.get_selection_tree(instance_id)
        paths = []
        for scope in tree.get("scopes", []):
            for proj in scope.get("projects", []):
                for cat in proj.get("categories", []):
                    for f in cat.get("files", []):
                        paths.append(f.get("path", "").replace("\\", "/"))
            for cat in scope.get("categories", []):
                for f in cat.get("files", []):
                    paths.append(f.get("path", "").replace("\\", "/"))
        return paths

    claude_paths = collect(by_product["claude-code"].instance_id)
    assert any("/memory/user.md" in p for p in claude_paths)
    assert any("sess-1.jsonl" in p for p in claude_paths)
    assert any("subagents/agent-1.jsonl" in p for p in claude_paths)

    cursor_paths = collect(by_product["cursor"].instance_id)
    assert any("agent-transcripts" in p and p.endswith(".jsonl") for p in cursor_paths)

    def collect_files(instance_id: str) -> list[dict]:
        tree = locator.get_selection_tree(instance_id)
        files: list[dict] = []
        for scope in tree.get("scopes", []):
            for proj in scope.get("projects", []):
                for cat in proj.get("categories", []):
                    files.extend(cat.get("files", []))
            for cat in scope.get("categories", []):
                files.extend(cat.get("files", []))
        return files

    codex_paths = collect(by_product["codex"].instance_id)
    assert any("rollout-demo.jsonl" in p for p in codex_paths)
    assert any("/memories/preferences.md" in p.replace("\\", "/") for p in codex_paths)
    codex_files = collect_files(by_product["codex"].instance_id)
    assert any(
        f.get("default_selected")
        for f in codex_files
        if "memories" in f.get("path", "").replace("\\", "/")
        and f.get("ingestion_policy") == "import_verbatim"
    )

    trae_paths = collect(by_product["trae"].instance_id)
    assert any(p.endswith("project_memory.md") for p in trae_paths)
    assert any("session_memory_1.jsonl" in p for p in trae_paths)
    assert any(p.endswith("topics.md") for p in trae_paths)


def test_session_jsonl_extract_candidates_enters_ir(tmp_path):
    p = FIXTURES / "home/.claude/projects/demo-proj/sess-1.jsonl"
    content = p.read_text(encoding="utf-8")
    root_id = "sess-root"
    obj = SourceObject(
        source_object_id=stable_hash(root_id, "sess-1.jsonl"),
        source_root_id=root_id,
        relative_path="sess-1.jsonl",
        content_hash=stable_hash(content),
        media_type="application/x-jsonlines",
    )
    snap = SourceSnapshot(snapshot_id="s", created_at="", source_objects=[obj], coverage=CoverageLedger())
    ir = MemoryNormalizer(tmp_path).normalize(
        snap,
        root_map={root_id: str(p.parent)},
        root_policies={root_id: {
            "source_category": "conversation_history",
            "ingestion_policy": "extract_candidates",
        }},
    )
    assert ir.records, "high-signal session lines must enter IR"
    bodies = "\n".join(r.body for r in ir.records).lower()
    assert "prefer short" in bodies
    assert all("bash" not in r.body.lower() for r in ir.records)


def test_evidence_only_still_blocked(tmp_path):
    p = FIXTURES / "home/.claude/projects/demo-proj/sess-1.jsonl"
    content = p.read_text(encoding="utf-8")
    root_id = "ev"
    obj = SourceObject(
        source_object_id=stable_hash(root_id, "sess-1.jsonl"),
        source_root_id=root_id,
        relative_path="sess-1.jsonl",
        content_hash=stable_hash(content),
    )
    snap = SourceSnapshot(snapshot_id="s", created_at="", source_objects=[obj], coverage=CoverageLedger())
    ir = MemoryNormalizer(tmp_path).normalize(
        snap,
        root_map={root_id: str(p.parent)},
        root_policies={root_id: {
            "source_category": "conversation_history",
            "ingestion_policy": "evidence_only",
        }},
    )
    assert ir.records == []


def test_plan_docs_and_project_extract_do_not_auto_ingest(tmp_path):
    """项目目录 + extract_candidates 不得把 .plan.md / 任务台账灌进 IR。"""
    plans = tmp_path / ".cursor" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "ship.plan.md"
    plan.write_text("# Ship\n\nDo the thing.\n", encoding="utf-8")
    knowledge = tmp_path / "docs" / "notes.md"
    knowledge.parent.mkdir(parents=True)
    knowledge.write_text("# Notes\n\nUseful fact.\n", encoding="utf-8")

    root_id = "proj"
    objs = []
    for rel, path in (
        (".cursor/plans/ship.plan.md", plan),
        ("docs/notes.md", knowledge),
    ):
        content = path.read_text(encoding="utf-8")
        objs.append(SourceObject(
            source_object_id=stable_hash(root_id, rel),
            source_root_id=root_id,
            relative_path=rel,
            content_hash=stable_hash(content),
            media_type="text/markdown",
        ))
    snap = SourceSnapshot(snapshot_id="s", created_at="", source_objects=objs, coverage=CoverageLedger())
    ir = MemoryNormalizer(tmp_path).normalize(
        snap,
        root_map={root_id: str(tmp_path)},
        root_policies={root_id: {
            "source_category": "knowledge_source",
            "ingestion_policy": "extract_candidates",
        }},
    )
    assert ir.records == []


def test_frontmatter_kind_survives_normalize(tmp_path):
    p = FIXTURES / "home/.claude/projects/demo-proj/memory/user.md"
    content = p.read_text(encoding="utf-8")
    root_id = "mem"
    obj = SourceObject(
        source_object_id=stable_hash(root_id, "user.md"),
        source_root_id=root_id,
        relative_path="user.md",
        content_hash=stable_hash(content),
        media_type="text/markdown",
    )
    snap = SourceSnapshot(snapshot_id="s", created_at="", source_objects=[obj], coverage=CoverageLedger())
    ir = MemoryNormalizer(tmp_path).normalize(
        snap,
        root_map={root_id: str(p.parent)},
        root_policies={root_id: {
            "source_category": "native_memory",
            "ingestion_policy": "import_verbatim",
        }},
    )
    assert ir.records
    assert any(r.kind.value == "preference" for r in ir.records)


def test_sqlite_meta_read_via_adapter(tmp_path):
    db = FIXTURES / "home/.codex/state_5.sqlite"
    root = SourceRoot(
        root_id="sqlite-root",
        type=SourceRootType.SELECTED_FILE,
        display_name="state",
        path=str(db),
        enabled=True,
    )
    from memoryguard.source_registry import SelectedFileAdapter
    adapter = SelectedFileAdapter(root)
    objs, entry = adapter.read(db, db)
    assert objs is not None
    assert objs.read_status == "meta"
    assert objs.media_type.endswith("sqlite3")
    segs = parse_file(db, media_type=objs.media_type)
    assert segs[0].signal_level == "meta"


def test_trae_policies():
    profile = _trae_profile()
    by_id = {s.surface_id: s for s in profile.surfaces}
    assert by_id["trae_user_profile"].ingestion_policy == IngestionPolicy.IMPORT_VERBATIM
    assert by_id["trae_project_memory"].ingestion_policy == IngestionPolicy.IMPORT_VERBATIM
    assert by_id["trae_session_memory"].ingestion_policy == IngestionPolicy.EXTRACT_CANDIDATES
    assert by_id["trae_topics"].category == SourceCategory.CONVERSATION_HISTORY
    assert by_id["trae_topics"].ingestion_policy == IngestionPolicy.EXTRACT_CANDIDATES


def test_codex_native_memories_surface():
    profile = _codex_profile()
    by_id = {s.surface_id: s for s in profile.surfaces}
    mem = by_id["codex_native_memories"]
    assert mem.category == SourceCategory.NATIVE_MEMORY
    assert mem.ingestion_policy == IngestionPolicy.IMPORT_VERBATIM
    assert "MEMORY.md" in (mem.file_globs or [])
    assert any("rollout_summaries" in g for g in (mem.file_globs or []))


def test_empty_native_memory_dir_still_selectable(tmp_path, monkeypatch):
    """专用记忆目录存在但 glob 无匹配时，仍应露出可勾选目录节点。"""
    from memoryguard.schema_v3 import AgentInstance, DiscoveryLedger, TargetCapability

    home = tmp_path / "home"
    mem = home / ".codex" / "memories"
    mem.mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        "[memories]\ngenerate_memories = false\nuse_memories = false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    mem_info = {
        "surface_id": "codex_native_memories",
        "resolved_path": str(mem),
        "status": "found",
        "scope": "user",
        "category": "native_memory",
        "ingestion_policy": "import_verbatim",
        "ownership": "agent_managed",
        "target_role": "takeover_input",
        "classification_confidence": 0.95,
        "file_globs": ["MEMORY.md", "memory_summary.md", "raw_memories.md", "*.md"],
    }
    inst = AgentInstance(
        instance_id="codex-test",
        profile_id="codex@profile-1",
        product="codex",
        profile_version="2",
        surfaces=[mem_info],
        target_capability=TargetCapability.EXPORT_ONLY,
    )
    locator = AgentLocator(tmp_path)
    monkeypatch.setattr(
        AgentLocator,
        "detect_instances",
        lambda self: ([inst], {"codex-test": DiscoveryLedger(instance_id="codex-test")}),
    )
    tree = locator.get_selection_tree("codex-test")
    files = []
    for scope in tree.get("scopes", []):
        for cat in scope.get("categories", []):
            files.extend(cat.get("files", []))
    assert files, "empty memories dir must still appear"
    assert any(f.get("empty_glob_match") for f in files)
    assert any(f.get("selectable") for f in files)
    notes = tree.get("discovery_notes", [])
    assert any(n.get("code") == "codex_memories_disabled" for n in notes)
    assert any(n.get("code") == "codex_memories_empty" for n in notes)


def test_empty_file_globs_do_not_fallback_to_directory(fake_home, tmp_path):
    """声明 file_globs 但无匹配时，不得回退授权整目录。"""
    locator = AgentLocator(tmp_path)
    instances, _ = locator.detect_instances()
    # 构造假 surface：指向 projects 但 glob 无匹配
    surface = {
        "surface_id": "empty_glob",
        "resolved_path": str(HOME / ".claude" / "projects"),
        "status": "found",
        "scope": "user",
        "category": "native_memory",
        "ingestion_policy": "import_verbatim",
        "file_globs": ["memory/DOES_NOT_EXIST_*.md"],
        "ownership": "agent_managed",
        "target_role": "takeover_input",
        "classification_confidence": 0.9,
    }
    expanded = locator._expand_project_root(surface["resolved_path"], surface)
    assert expanded == []
    # get_selection_tree 路径：has_globs 时 effective=expanded=[]，不暴露目录
    effective = expanded if surface.get("file_globs") else [surface]
    assert effective == []


def test_import_verbatim_preserves_body(tmp_path):
    p = FIXTURES / "home/.claude/projects/demo-proj/memory/user.md"
    content = p.read_text(encoding="utf-8")
    # 取 frontmatter 后正文
    body_only = content.split("---", 2)[-1].strip()
    root_id = "mem"
    obj = SourceObject(
        source_object_id=stable_hash(root_id, "user.md"),
        source_root_id=root_id,
        relative_path="user.md",
        content_hash=stable_hash(content),
        media_type="text/markdown",
    )
    snap = SourceSnapshot(snapshot_id="s", created_at="", source_objects=[obj], coverage=CoverageLedger())
    ir = MemoryNormalizer(tmp_path).normalize(
        snap,
        root_map={root_id: str(p.parent)},
        root_policies={root_id: {
            "source_category": "native_memory",
            "ingestion_policy": "import_verbatim",
        }},
    )
    assert ir.records
    assert ir.records[0].body == body_only
    assert ir.records[0].original_body == body_only


def test_codex_sessions_not_year_as_project(fake_home, tmp_path):
    locator = AgentLocator(tmp_path)
    instances, _ = locator.detect_instances()
    codex = next(i for i in instances if i.product == "codex")
    tree = locator.get_selection_tree(codex.instance_id)
    refs = []
    for scope in tree.get("scopes", []):
        for proj in scope.get("projects", []):
            refs.append(proj.get("project_ref"))
        for cat in scope.get("categories", []):
            for f in cat.get("files", []):
                if "rollout" in f.get("path", ""):
                    refs.append(f.get("project_ref", ""))
    assert "2026" not in refs

def test_profile_versions_bumped():
    assert _claude_code_profile().profile_version == "2"
    assert _codex_profile().profile_version == "2"
    assert _cursor_profile().profile_version == "2"
    assert _trae_profile().profile_version == "2"


def test_interactive_renders_user_scope_projects():
    src = Path(__file__).resolve().parents[1] / "src" / "memoryguard" / "interactive.py"
    text = src.read_text(encoding="utf-8")
    assert "MEMORY_SELECT_CATS" in text
    assert "renderScopeCategories" in text
    assert "for (const proj of projects)" in text
