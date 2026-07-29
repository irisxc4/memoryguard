"""content_parsers 统一收口硬断言。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memoryguard.content_parsers import parse_content, parse_file
from memoryguard.adapters import GenericImportAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "agent_memories"


@pytest.fixture(scope="module", autouse=True)
def _ensure_fixtures():
    from _build_agent_memory_fixtures import main
    main()


def test_frontmatter_claude_memory_kind():
    p = FIXTURES / "home/.claude/projects/demo-proj/memory/user.md"
    segs = parse_file(p)
    assert len(segs) == 1
    assert segs[0].kind_hint == "preference"
    assert segs[0].locator.startswith("frontmatter:")
    assert "concise" in segs[0].body.lower() or "简洁" in segs[0].body or "short" in segs[0].body.lower()


def test_frontmatter_project_kind():
    p = FIXTURES / "home/.claude/projects/demo-proj/memory/project.md"
    segs = parse_file(p)
    assert segs[0].kind_hint == "project"


def test_jsonl_high_signal_drops_tool_calls():
    p = FIXTURES / "home/.claude/projects/demo-proj/sess-1.jsonl"
    segs = parse_file(p)
    assert segs, "expected at least one high-signal segment"
    assert all(s.locator.startswith("jsonl:line:") for s in segs)
    bodies = "\n".join(s.body for s in segs).lower()
    assert "prefer short" in bodies
    assert "bash" not in bodies  # tool_call dropped


def test_cursor_transcript_jsonl_locator():
    p = FIXTURES / "home/.cursor/projects/encoded-path/agent-transcripts/uuid-a/uuid-a.jsonl"
    segs = parse_file(p)
    assert any("typescript" in s.body.lower() for s in segs)
    assert all(s.locator.startswith("jsonl:line:") for s in segs)


def test_codex_rollout_high_signal():
    p = FIXTURES / "home/.codex/sessions/2026/07/28/rollout-demo.jsonl"
    segs = parse_file(p)
    assert any("pytest" in s.body.lower() for s in segs)
    assert not any("shell" == s.body.strip().lower() for s in segs)


def test_sqlite_meta_only_no_blob_body():
    p = FIXTURES / "home/.codex/state_5.sqlite"
    segs = parse_file(p)
    assert len(segs) == 1
    assert segs[0].signal_level == "meta"
    assert "sessions" in segs[0].body
    assert b"\x00" not in segs[0].body.encode("utf-8")


def test_trae_topics_session_blocks():
    p = FIXTURES / "home/.trae-cn/memory/projects/enc-proj/2026-07-28/topics.md"
    segs = parse_file(p)
    assert len(segs) == 2
    assert segs[0].locator == "topics:session:abc"
    assert segs[1].locator == "topics:session:def"


def test_trae_session_learned_signal():
    p = FIXTURES / "home/.trae-cn/memory/projects/enc-proj/2026-07-28/session_memory_1.jsonl"
    segs = parse_file(p)
    assert len(segs) == 1
    assert "uv" in segs[0].body.lower()


def test_import_and_parser_segment_count_consistent():
    p = FIXTURES / "home/.claude/projects/demo-proj/sess-1.jsonl"
    direct = [s for s in parse_file(p) if s.signal_level != "meta"]
    adapter = GenericImportAdapter()
    convs = adapter.parse(p)
    assert len(convs) == 1
    assert len(convs[0].messages) == len(direct)
