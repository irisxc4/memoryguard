"""知识书库 KB1 核心功能测试。

验证：添加文件夹 → 自动分章 → 切片 → FTS5 检索 → MCP 工具。
"""

import tempfile
from pathlib import Path

import pytest

from memoryguard.data_home import resolve_data_home
from memoryguard.knowledge_store import KnowledgeStore, Book, Chunk
from memoryguard.knowledge_parser import parse_file
from memoryguard.knowledge_chunker import chunk_document
from memoryguard.knowledge_ingestion import create_book, ingest_book
from memoryguard.knowledge_retriever import search, read_chunk, list_books, get_book_info
from memoryguard.knowledge_mcp import handle_knowledge_tool


@pytest.fixture
def tmp_book_dir():
    """创建临时文件夹模拟一本书。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 创建子文件夹和文件
        (root / "combat").mkdir()
        (root / "combat" / "attributes.md").write_text(
            "# 战斗属性\n\n"
            "角色拥有力量、敏捷、智力三种基础属性。\n\n"
            "力量影响物理攻击和生命值。\n"
            "敏捷影响闪避和暴击率。\n"
            "智力影响魔法攻击和法力值。\n",
            encoding="utf-8",
        )
        (root / "combat" / "damage.md").write_text(
            "# 伤害计算\n\n"
            "物理伤害 = 攻击力 - 护甲值。\n"
            "魔法伤害 = 法术强度 * (1 - 魔法抗性)。\n",
            encoding="utf-8",
        )
        (root / "skills").mkdir()
        (root / "skills" / "fusion.md").write_text(
            "# 技能融合\n\n"
            "三个同品质的技能可以合成为一个更高品质的技能。\n"
            "融合成功率受技能等级影响。\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text(
            "# 游戏设计文档\n\n"
            "本文档包含游戏核心系统的设计说明。\n",
            encoding="utf-8",
        )
        yield root


@pytest.fixture
def store(tmp_path):
    """使用临时 data_home 的 KnowledgeStore。"""
    s = KnowledgeStore(tmp_path)
    yield s
    s.close()


class TestParser:
    """测试文档解析。"""

    def test_parse_markdown(self, tmp_book_dir):
        """Markdown 解析：识别标题、段落。"""
        f = tmp_book_dir / "combat" / "attributes.md"
        doc = parse_file(f, tmp_book_dir)
        assert doc is not None
        assert doc.media_type == "text/markdown"
        assert doc.relative_path == "combat/attributes.md"
        # 应该有标题块和段落块
        headings = [b for b in doc.blocks if b.block_type == "heading"]
        assert len(headings) >= 1
        assert headings[0].heading_text == "战斗属性"
        assert headings[0].heading_level == 1

    def test_parse_unsupported(self, tmp_book_dir):
        """不支持的文件类型返回 None。"""
        f = tmp_book_dir / "test.bin"
        f.write_bytes(b"\x00\x01")
        doc = parse_file(f, tmp_book_dir)
        assert doc is None


class TestChunker:
    """测试切片。"""

    def test_chunk_has_chapter(self, tmp_book_dir):
        """切片包含章节信息。"""
        f = tmp_book_dir / "combat" / "attributes.md"
        doc = parse_file(f, tmp_book_dir)
        chunks = chunk_document(doc, "book1", "doc1")
        assert len(chunks) > 0
        # 第一个 chunk 应该有 chapter="战斗属性"
        assert any(c.chapter == "战斗属性" for c in chunks)
        # 所有 chunk 应该有 text_hash
        assert all(c.text_hash for c in chunks)

    def test_chunk_line_numbers(self, tmp_book_dir):
        """切片包含行号。"""
        f = tmp_book_dir / "combat" / "attributes.md"
        doc = parse_file(f, tmp_book_dir)
        chunks = chunk_document(doc, "book1", "doc1")
        assert all(c.line_start > 0 for c in chunks)
        assert all(c.line_end >= c.line_start for c in chunks)


class TestIngestion:
    """测试入库和增量更新。"""

    def test_create_and_ingest(self, store, tmp_book_dir):
        """创建书并入库。"""
        book = create_book(store, str(tmp_book_dir), title="游戏设计")
        assert book.title == "游戏设计"
        assert book.book_id

        result = ingest_book(store, book.book_id)
        assert result.error == ""
        assert result.files_total == 4
        assert result.files_processed == 4
        assert result.chunks_created > 0
        assert "战斗属性" in result.chapters or "伤害计算" in result.chapters

    def test_incremental_skip_unchanged(self, store, tmp_book_dir):
        """未变化文件不重复处理。"""
        book = create_book(store, str(tmp_book_dir))
        ingest_book(store, book.book_id)
        # 第二次入库应该全部跳过
        result = ingest_book(store, book.book_id)
        assert result.files_processed == 0
        assert result.files_skipped == 4

    def test_incremental_update_changed(self, store, tmp_book_dir):
        """修改文件后只重建该文件。"""
        book = create_book(store, str(tmp_book_dir))
        ingest_book(store, book.book_id)
        # 修改一个文件
        f = tmp_book_dir / "combat" / "attributes.md"
        f.write_text("# 战斗属性更新\n\n新增内容。\n", encoding="utf-8")
        result = ingest_book(store, book.book_id)
        assert result.files_processed == 1
        assert result.files_skipped == 3

    def test_original_files_unchanged(self, store, tmp_book_dir):
        """原始文件零修改。"""
        original_content = (tmp_book_dir / "combat" / "attributes.md").read_text()
        book = create_book(store, str(tmp_book_dir))
        ingest_book(store, book.book_id)
        assert (tmp_book_dir / "combat" / "attributes.md").read_text() == original_content


class TestSearch:
    """测试 FTS5 检索。"""

    def test_search_chinese(self, store, tmp_book_dir):
        """中文搜索。"""
        book = create_book(store, str(tmp_book_dir))
        ingest_book(store, book.book_id)
        results = search(store, "技能融合")
        assert len(results) > 0
        assert any("技能" in r.get("text", "") for r in results)

    def test_search_with_book_filter(self, store, tmp_book_dir):
        """按 book_id 过滤搜索。"""
        book = create_book(store, str(tmp_book_dir))
        ingest_book(store, book.book_id)
        results = search(store, "属性", book_ids=[book.book_id])
        assert len(results) > 0
        results_empty = search(store, "属性", book_ids=["nonexistent"])
        assert len(results_empty) == 0

    def test_search_returns_source(self, store, tmp_book_dir):
        """结果包含来源信息。"""
        book = create_book(store, str(tmp_book_dir))
        ingest_book(store, book.book_id)
        results = search(store, "伤害")
        assert len(results) > 0
        r = results[0]
        assert r.get("book_title") == tmp_book_dir.name or r.get("book_title")
        assert r.get("relative_path")
        assert r.get("line_start", 0) > 0
        assert r.get("chunk_id")

    def test_read_chunk(self, store, tmp_book_dir):
        """读取单个 chunk 及相邻上下文。"""
        book = create_book(store, str(tmp_book_dir))
        ingest_book(store, book.book_id)
        results = search(store, "属性")
        assert len(results) > 0
        chunk_id = results[0]["chunk_id"]
        detail = read_chunk(store, chunk_id)
        assert detail is not None
        assert detail["text"]
        assert detail["book_title"]
        assert detail["relative_path"]


class TestMCP:
    """测试 MCP 工具。"""

    def test_mcp_list(self, store, tmp_book_dir):
        """MCP knowledge_list。"""
        book = create_book(store, str(tmp_book_dir))
        ingest_book(store, book.book_id)
        result = handle_knowledge_tool(
            "memoryguard_knowledge_list", {}, Path(store._data_home or ".")
        )
        assert result is not None
        assert "isError" not in result or not result["isError"]
        text = result["content"][0]["text"]
        assert "游戏" in text or tmp_book_dir.name in text

    def test_mcp_search(self, store, tmp_book_dir):
        """MCP knowledge_search。"""
        book = create_book(store, str(tmp_book_dir), title="游戏设计")
        ingest_book(store, book.book_id)
        result = handle_knowledge_tool(
            "memoryguard_knowledge_search",
            {"query": "技能融合"},
            Path(store._data_home or "."),
        )
        assert result is not None
        text = result["content"][0]["text"]
        assert "技能" in text

    def test_mcp_unknown_tool(self, store, tmp_book_dir):
        """未知工具名返回 None。"""
        result = handle_knowledge_tool(
            "memoryguard_knowledge_unknown", {}, Path(store._data_home or ".")
        )
        assert result is None


class TestStoreCRUD:
    """测试存储 CRUD。"""

    def test_book_lifecycle(self, store):
        """书的增删查。"""
        book = Book(book_id="test-1", title="测试书", root_path="/tmp/test")
        store.add_book(book)
        assert store.get_book("test-1") is not None
        assert len(store.list_books()) >= 1
        store.update_book_status("test-1", "ready", file_count=10)
        b = store.get_book("test-1")
        assert b.status == "ready"
        assert b.file_count == 10
        store.remove_book("test-1")
        assert store.get_book("test-1") is None
