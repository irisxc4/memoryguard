"""v3.2 §5 SharedMemoryStore 共享记忆后端版本化存储（SQLite 底层）。

存储结构：
.memoryguard/shared-memory/<group-id>/
  memory.db                # SQLite 数据库（事实源）
    表：records / events / decisions / conflicts / quarantine / versions / active_version

关键约束：
- 覆盖不是删除：supersede 保留旧 memory_id，旧记录 status=SHADOWED
- locked=True 防止自动覆盖
- 所有写入可回滚到历史版本
- 版本快照保存全部 5 类数据（records + events + decisions + conflicts + quarantine）

并发与事务：
- WAL 模式 + busy_timeout=5000，多 Agent 并发写入由 SQLite 事务/锁保证
- 每个写操作用事务上下文（成功提交、异常回滚）

向后兼容：
- __init__ 检测旧 JSONL 文件时自动迁移到 SQLite，迁移后旧文件重命名为 .bak
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .schema_v3 import (
    ConflictGroup, ConflictResolution,
    DecisionEvent,
    MemoryEvent,
    QuarantineEntry,
    SharedMemoryRecord, SharedMemoryStatus,
    MemoryKind, MemoryStatus,
    Provenance,
    stable_hash, _now_iso,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    memory_id TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    conflict_group_id TEXT DEFAULT '',
    locked INTEGER NOT NULL DEFAULT 0,
    supersedes TEXT DEFAULT '[]',
    provenance TEXT DEFAULT '[]',
    agent_instance_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    canonical_hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_records_canonical_hash ON records(canonical_hash);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    agent_instance_id TEXT NOT NULL,
    share_group_id TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    auto_actions TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    event_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_ids TEXT DEFAULT '[]',
    before_hash TEXT DEFAULT '',
    after_hash TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conflicts (
    group_id TEXT PRIMARY KEY,
    member_ids TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    resolution TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    detected_pattern TEXT DEFAULT '',
    original_content TEXT DEFAULT '',
    released INTEGER NOT NULL DEFAULT 0,
    quarantined_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_version (
    id INTEGER PRIMARY KEY DEFAULT 1,
    version_id TEXT
);
-- B1: FTS5 全文索引虚拟表(BM25 排序)
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    memory_id UNINDEXED,
    body,
    content='records',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, memory_id, body) VALUES (new.rowid, new.memory_id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, memory_id, body) VALUES ('delete', old.rowid, old.memory_id, old.body);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, memory_id, body) VALUES ('delete', old.rowid, old.memory_id, old.body);
    INSERT INTO records_fts(rowid, memory_id, body) VALUES (new.rowid, new.memory_id, new.body);
END;
"""

# 允许通过 _update_record_field 更新的列（白名单，防 SQL 注入）
_RECORD_COLUMNS = {
    "body", "status", "kind", "confidence", "conflict_group_id",
    "locked", "supersedes", "provenance", "agent_instance_id",
    "created_at", "updated_at",
}


import re as _re

# S1.1: group_id 合法 slug 规则(防路径穿越)
_GROUP_ID_PATTERN = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _validate_group_id(group_id: str) -> str:
    """验证 group_id 是合法 slug,返回规范化后的值。

    规则:
    - 只允许 [a-z0-9_-]
    - 长度 1-64
    - 不允许 .. 或路径分隔符
    - 不允许 default 之外的保留词检查(后续可扩展)

    非法值抛 ValueError,不静默降级。
    """
    if not group_id or not isinstance(group_id, str):
        raise ValueError("share_group_id must be a non-empty string")
    if not _GROUP_ID_PATTERN.match(group_id):
        raise ValueError(
            f"invalid share_group_id: {group_id!r}; "
            "must match [a-z0-9][a-z0-9_-]{{0,63}}"
        )
    # 二次防御:解析后不能包含 .. 或绝对路径
    if ".." in group_id or "/" in group_id or "\\" in group_id:
        raise ValueError(f"share_group_id contains path separator: {group_id!r}")
    return group_id


def _check_containment(resolved_path: Path, base_dir: Path) -> None:
    """验证 resolved_path 在 base_dir 内(防路径穿越)。"""
    try:
        resolved_path.relative_to(base_dir.resolve())
    except ValueError:
        raise ValueError(
            f"path escapes shared-memory root: {resolved_path} not under {base_dir}"
        )


class SharedMemoryStore:
    """共享记忆后端版本化存储（SQLite 底层）。

    一个 share_group_id 对应一个独立的版本化存储。
    Agent 通过 MCP 写入 -> 自动整理 -> 写入 records 表。
    治理动作（编辑/合并/锁定/恢复/删除/回滚）记录为 DecisionEvent。

    安全:
    - group_id 必须是合法 slug(防路径穿越)
    - read_only=True 时不初始化目录/数据库；无 WAL 时用 immutable
    """

    def __init__(
        self,
        workspace: str | Path,
        share_group_id: str,
        *,
        read_only: bool = False,
        must_exist: bool = False,
    ):
        self.workspace = Path(workspace).resolve()
        # S1.1: group_id 规范化验证(防路径穿越)
        self.group_id = _validate_group_id(share_group_id)
        sm_base = self.workspace / ".memoryguard" / "shared-memory"
        self.root = sm_base / self.group_id
        # S1.1: containment 检查(二次防御)
        _check_containment(self.root.resolve(), sm_base)
        self.versions_dir = self.root / "versions"
        self.db_path = self.root / "memory.db"
        self.maintenance_marker = self.root / ".maintenance"
        self._maintenance_override = False
        self.read_only = read_only
        self.must_exist = must_exist
        # JSONL 备份路径
        self.records_bak_path = self.root / "records.jsonl"
        self.events_bak_path = self.root / "events.jsonl"
        self.decisions_bak_path = self.root / "decisions.jsonl"
        self.conflicts_bak_path = self.root / "conflicts.jsonl"
        self.quarantine_bak_path = self.root / "quarantine.jsonl"
        # S1.3: 只读模式不初始化目录/数据库
        if not read_only:
            if must_exist and not self.db_path.exists():
                raise FileNotFoundError(
                    f"shared memory group not found: {self.group_id}"
                )
            self._ensure_dirs()
            self._init_db()
            self._migrate_from_jsonl()
        else:
            # 只读:如果 DB 不存在直接报错,不创建
            if not self.db_path.exists():
                raise FileNotFoundError(
                    f"shared memory group not found: {self.group_id}"
                )

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _append_jsonl(self, path: Path, obj: Any) -> None:
        """追加一行 JSON 到 JSONL 备份文件。"""
        self._assert_write_allowed()
        line = json.dumps(obj.to_dict() if hasattr(obj, "to_dict") else obj,
                          ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def export_jsonl_backup(self) -> None:
        """全量导出所有 JSONL 备份（可从 memory.db 重建）。"""
        for path, items in [
            (self.records_bak_path, self.list_records()),
            (self.events_bak_path, self.list_events()),
            (self.decisions_bak_path, self.list_decisions()),
            (self.conflicts_bak_path, self.list_conflicts()),
            (self.quarantine_bak_path, self.list_quarantine()),
        ]:
            with open(path, "w", encoding="utf-8") as f:
                for item in items:
                    f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # 连接与建表
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            # SQLite 的普通 mode=ro 连接到 WAL 数据库时，可能创建 -shm/-wal
            # 侧车。干净数据库用 immutable 保证绝对无副作用；已经存在有效
            # WAL 时必须用普通只读模式，才能看见尚未 checkpoint 的最新提交。
            wal_path = Path(f"{self.db_path}-wal")
            has_live_wal = wal_path.exists() and wal_path.stat().st_size > 0
            immutable = "" if has_live_wal else "&immutable=1"
            uri = f"file:{self.db_path}?mode=ro{immutable}"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        elif self.must_exist:
            uri = f"file:{self.db_path}?mode=rw"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        else:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        if not self.read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextlib.contextmanager
    def _db(self):
        """只读连接：用完即关。"""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _tx(self):
        """写事务连接：成功提交、异常回滚，用完即关。"""
        self._assert_write_allowed()
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _assert_write_allowed(self) -> None:
        if (
            not self.read_only
            and self.maintenance_marker.exists()
            and not self._maintenance_override
        ):
            raise RuntimeError(
                f"memory group is in maintenance: {self.group_id}"
            )

    @contextlib.contextmanager
    def maintenance(self, reason: str):
        """阻止新写事务，用于导出后清空/归档的一致性窗口。"""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with self.maintenance_marker.open("x", encoding="utf-8") as marker:
                marker.write(json.dumps({
                    "reason": reason,
                    "started_at": _now_iso(),
                }, ensure_ascii=False))
        except FileExistsError as exc:
            raise RuntimeError(
                f"memory group already in maintenance: {self.group_id}"
            ) from exc
        self._maintenance_override = True
        try:
            yield self
        finally:
            self._maintenance_override = False
            try:
                self.maintenance_marker.unlink()
            except FileNotFoundError:
                pass

    def _init_db(self) -> None:
        with self._tx() as conn:
            conn.executescript(_SCHEMA)
            # S3.2: schema version 标记(在事务内提交)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '2')"
            )
            # 迁移:旧 DB 可能缺 canonical_hash 列
            cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
            if "canonical_hash" not in cols:
                conn.execute("ALTER TABLE records ADD COLUMN canonical_hash TEXT DEFAULT ''")
            # 补填已有记录的 canonical_hash
            rows = conn.execute(
                "SELECT memory_id, body FROM records WHERE canonical_hash = '' OR canonical_hash IS NULL"
            ).fetchall()
            for row in rows:
                c_hash = self._canonical_hash(row["body"])
                conn.execute(
                    "UPDATE records SET canonical_hash = ? WHERE memory_id = ?",
                    (c_hash, row["memory_id"]),
                )

    @staticmethod
    def _canonical_hash(body: str) -> str:
        """S2.2: 正文规范哈希,用于并发去重唯一索引。"""
        import hashlib
        normalized = " ".join(body.split()).lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------------
    # 行 <-> 对象 转换
    # ------------------------------------------------------------------

    def _insert_record(self, conn: sqlite3.Connection, record: SharedMemoryRecord) -> None:
        d = record.to_dict()
        # S2.2: 计算 canonical_hash
        c_hash = self._canonical_hash(d.get("body", ""))
        conn.execute(
            """INSERT OR REPLACE INTO records
               (memory_id, body, kind, status, confidence, conflict_group_id, locked,
                supersedes, provenance, agent_instance_id, created_at, updated_at, canonical_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d["memory_id"], d["body"], d["kind"], d["status"], d["confidence"],
             d.get("conflict_group_id", ""), 1 if d.get("locked") else 0,
             json.dumps(d.get("supersedes", []), ensure_ascii=False),
             json.dumps(d.get("provenance", []), ensure_ascii=False),
             d.get("agent_instance_id", ""), d.get("created_at", ""), d.get("updated_at", ""),
             c_hash),
        )

    def _row_to_record(self, row: sqlite3.Row) -> SharedMemoryRecord:
        d = {
            "memory_id": row["memory_id"],
            "body": row["body"],
            "kind": row["kind"],
            "status": row["status"],
            "confidence": row["confidence"],
            "conflict_group_id": row["conflict_group_id"] or "",
            "locked": bool(row["locked"]),
            "supersedes": json.loads(row["supersedes"] or "[]"),
            "provenance": json.loads(row["provenance"] or "[]"),
            "agent_instance_id": row["agent_instance_id"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return SharedMemoryRecord.from_dict(d)

    def _insert_event(self, conn: sqlite3.Connection, event: MemoryEvent) -> None:
        d = event.to_dict()
        conn.execute(
            """INSERT OR REPLACE INTO events
               (event_id, agent_instance_id, share_group_id, raw_content, metadata, auto_actions, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (d["event_id"], d["agent_instance_id"], d["share_group_id"], d["raw_content"],
             json.dumps(d.get("metadata", {}), ensure_ascii=False),
             json.dumps(d.get("auto_actions", []), ensure_ascii=False),
             d.get("created_at", "")),
        )

    def _row_to_event(self, row: sqlite3.Row) -> MemoryEvent:
        d = {
            "event_id": row["event_id"],
            "agent_instance_id": row["agent_instance_id"],
            "share_group_id": row["share_group_id"],
            "raw_content": row["raw_content"],
            "metadata": json.loads(row["metadata"] or "{}"),
            "auto_actions": json.loads(row["auto_actions"] or "[]"),
            "created_at": row["created_at"],
        }
        return MemoryEvent.from_dict(d)

    def _insert_decision(self, conn: sqlite3.Connection, decision: DecisionEvent) -> None:
        d = decision.to_dict()
        conn.execute(
            """INSERT OR REPLACE INTO decisions
               (event_id, actor, action, target_ids, before_hash, after_hash, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (d["event_id"], d["actor"], d["action"],
             json.dumps(d.get("target_ids", []), ensure_ascii=False),
             d.get("before_hash", ""), d.get("after_hash", ""),
             d.get("reason", ""), d.get("created_at", "")),
        )

    def _row_to_decision(self, row: sqlite3.Row) -> DecisionEvent:
        return DecisionEvent(
            event_id=row["event_id"], actor=row["actor"], action=row["action"],
            target_ids=json.loads(row["target_ids"] or "[]"),
            before_hash=row["before_hash"] or "",
            after_hash=row["after_hash"] or "",
            reason=row["reason"] or "",
            created_at=row["created_at"],
        )

    def _insert_conflict(self, conn: sqlite3.Connection, group: ConflictGroup) -> None:
        d = group.to_dict()
        conn.execute(
            """INSERT OR REPLACE INTO conflicts
               (group_id, member_ids, reason, status, resolution, created_at)
               VALUES (?,?,?,?,?,?)""",
            (d["group_id"], json.dumps(d.get("member_ids", []), ensure_ascii=False),
             d["reason"], d["status"], d.get("resolution", ""), d.get("created_at", "")),
        )

    def _row_to_conflict(self, row: sqlite3.Row) -> ConflictGroup:
        d = {
            "group_id": row["group_id"],
            "member_ids": json.loads(row["member_ids"] or "[]"),
            "reason": row["reason"],
            "status": row["status"],
            "resolution": row["resolution"] or "",
            "created_at": row["created_at"],
        }
        return ConflictGroup.from_dict(d)

    def _insert_quarantine(self, conn: sqlite3.Connection, entry: QuarantineEntry) -> None:
        d = entry.to_dict()
        conn.execute(
            """INSERT OR REPLACE INTO quarantine
               (quarantine_id, memory_id, reason, detected_pattern, original_content, released, quarantined_at)
               VALUES (?,?,?,?,?,?,?)""",
            (d["quarantine_id"], d["memory_id"], d["reason"],
             d.get("detected_pattern", ""), d.get("original_content", ""),
             1 if d.get("released") else 0, d.get("quarantined_at", "")),
        )

    def _row_to_quarantine(self, row: sqlite3.Row) -> QuarantineEntry:
        d = {
            "quarantine_id": row["quarantine_id"],
            "memory_id": row["memory_id"],
            "reason": row["reason"],
            "detected_pattern": row["detected_pattern"] or "",
            "original_content": row["original_content"] or "",
            "quarantined_at": row["quarantined_at"],
            "released": bool(row["released"]),
        }
        return QuarantineEntry.from_dict(d)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append_event(self, event: MemoryEvent) -> None:
        """追加原始写入事件。"""
        with self._tx() as conn:
            self._insert_event(conn, event)
        self._append_jsonl(self.events_bak_path, event)

    def update_event(self, event: MemoryEvent) -> None:
        """更新事件（回填 auto_actions）。仅在已存在时更新。"""
        d = event.to_dict()
        with self._tx() as conn:
            conn.execute(
                """UPDATE events SET agent_instance_id=?, share_group_id=?, raw_content=?,
                   metadata=?, auto_actions=?, created_at=? WHERE event_id=?""",
                (d["agent_instance_id"], d["share_group_id"], d["raw_content"],
                 json.dumps(d.get("metadata", {}), ensure_ascii=False),
                 json.dumps(d.get("auto_actions", []), ensure_ascii=False),
                 d.get("created_at", ""), d["event_id"]),
            )

    def append_record(self, record: SharedMemoryRecord) -> None:
        """P0-C: 追加记忆记录,带并发去重(BEGIN IMMEDIATE + canonical_hash)。

        如果 canonical_hash 已存在 active 记录,merge provenance 而非新建。
        """
        c_hash = self._canonical_hash(record.body)
        # P0-C: 手动事务(BEGIN IMMEDIATE),不走 _tx()(它会自动 BEGIN)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 查同 canonical_hash 的 active 记录
            existing = conn.execute(
                "SELECT memory_id, provenance FROM records "
                "WHERE canonical_hash = ? AND status = 'active' LIMIT 1",
                (c_hash,),
            ).fetchone()
            if existing:
                # P0-C: merge provenance(追加到现有记录,不新建)
                old_provs = json.loads(existing["provenance"] or "[]")
                new_provs = record.to_dict().get("provenance", [])
                merged = old_provs + [p for p in new_provs if p not in old_provs]
                conn.execute(
                    "UPDATE records SET provenance = ?, updated_at = ? WHERE memory_id = ?",
                    (json.dumps(merged, ensure_ascii=False),
                     record.to_dict().get("updated_at", ""),
                     existing["memory_id"]),
                )
                conn.commit()
                # 更新 record 的 memory_id 为已存在的(调用方能感知 merge)
                record.memory_id = existing["memory_id"]
                return
            # 无重复:插入新记录
            d = record.to_dict()
            conn.execute(
                """INSERT OR REPLACE INTO records
                   (memory_id, body, kind, status, confidence, conflict_group_id, locked,
                    supersedes, provenance, agent_instance_id, created_at, updated_at, canonical_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["memory_id"], d["body"], d["kind"], d["status"], d["confidence"],
                 d.get("conflict_group_id", ""), 1 if d.get("locked") else 0,
                 json.dumps(d.get("supersedes", []), ensure_ascii=False),
                 json.dumps(d.get("provenance", []), ensure_ascii=False),
                 d.get("agent_instance_id", ""), d.get("created_at", ""), d.get("updated_at", ""),
                 c_hash),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._append_jsonl(self.records_bak_path, record)

    def append_decision(self, decision: DecisionEvent) -> None:
        """追加决策事件。"""
        with self._tx() as conn:
            self._insert_decision(conn, decision)
        self._append_jsonl(self.decisions_bak_path, decision)

    def append_conflict(self, group: ConflictGroup) -> None:
        """追加冲突组。"""
        with self._tx() as conn:
            self._insert_conflict(conn, group)
        self._append_jsonl(self.conflicts_bak_path, group)

    def append_quarantine(self, entry: QuarantineEntry) -> None:
        """追加隔离条目。"""
        with self._tx() as conn:
            self._insert_quarantine(conn, entry)
        self._append_jsonl(self.quarantine_bak_path, entry)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def list_records(self, status: str | None = None,
                     kind: str | None = None) -> list[SharedMemoryRecord]:
        """列出记忆记录，可按 status/kind 过滤。"""
        sql = "SELECT * FROM records"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def search_fts(self, query: str, *, status: str | None = None,
                   kind: str | None = None, limit: int = 20) -> list[dict]:
        """B1: FTS5 全文搜索 + BM25 排序。

        返回 [{record, bm25_score}, ...],按相关性降序。
        每条结果含完整记录 + 元数据(B2)。
        """
        if not query or not query.strip():
            return []
        # FTS5 MATCH 查询:对查询词做简单分词后用 OR 连接
        # 避免 FTS5 语法错误(特殊字符需要转义)
        import re as _re
        tokens = _re.findall(r"[\w]+", query)
        if not tokens:
            return []
        match_expr = " OR ".join(tokens)
        sql = (
            "SELECT r.*, bm25(records_fts) AS score "
            "FROM records_fts JOIN records r ON r.rowid = records_fts.rowid "
            "WHERE records_fts MATCH ? "
        )
        params: list[Any] = [match_expr]
        clauses = []
        if status:
            clauses.append("r.status=?")
            params.append(status)
        if kind:
            clauses.append("r.kind=?")
            params.append(kind)
        if clauses:
            sql += " AND " + " AND ".join(clauses)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        try:
            with self._db() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception:
            # FTS5 查询失败(语法错误等),回退子串搜索
            return self._fallback_search(query, status=status, kind=kind, limit=limit)
        # 中文分词场景:FTS5 把整句当一个 token,MATCH 可能漏召回
        # FTS 结果为空时回退子串搜索
        if not rows:
            fallback = self._fallback_search(query, status=status, kind=kind, limit=limit)
            if fallback:
                return fallback
        results = []
        for row in rows:
            rec = self._row_to_record(row)
            results.append({
                "record": rec.to_dict(),
                "bm25_score": row["score"],
                "share_group_id": self.group_id,
                "agent_instance_id": rec.agent_instance_id,
                "kind": rec.kind.value,
                "provenance": rec.provenance,
                "confidence": rec.confidence,
            })
        return results

    def _fallback_search(self, query: str, *, status: str | None = None,
                         kind: str | None = None, limit: int = 20) -> list[dict]:
        """FTS5 不可用时按查询词做 OR 子串匹配。"""
        import re as _re

        records = self.list_records(status=status, kind=kind)
        tokens = list(dict.fromkeys(
            token.casefold() for token in _re.findall(r"[\w]+", query)
        ))
        if not tokens:
            return []
        scored = []
        for row_index, record in enumerate(records):
            body = record.body.casefold()
            hit_count = sum(token in body for token in tokens)
            if hit_count:
                scored.append((-hit_count, row_index, record))
        scored.sort(key=lambda item: (item[0], item[1]))
        matched = [item[2] for item in scored[:limit]]
        return [{
            "record": r.to_dict(),
            "bm25_score": 0.0,
            "share_group_id": self.group_id,
            "agent_instance_id": r.agent_instance_id,
            "kind": r.kind.value,
            "provenance": r.provenance,
            "confidence": r.confidence,
        } for r in matched]

    def list_events(self) -> list[MemoryEvent]:
        """列出所有写入事件。"""
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY rowid").fetchall()
        return [self._row_to_event(r) for r in rows]

    def list_decisions(self) -> list[DecisionEvent]:
        """列出所有决策事件。"""
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM decisions ORDER BY rowid").fetchall()
        return [self._row_to_decision(r) for r in rows]

    def list_conflicts(self) -> list[ConflictGroup]:
        """列出所有冲突组。"""
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM conflicts ORDER BY rowid").fetchall()
        return [self._row_to_conflict(r) for r in rows]

    def list_quarantine(self) -> list[QuarantineEntry]:
        """列出所有隔离条目。"""
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM quarantine ORDER BY rowid").fetchall()
        return [self._row_to_quarantine(r) for r in rows]

    def get_record(self, memory_id: str) -> SharedMemoryRecord | None:
        """获取单条记忆记录。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE memory_id=?", (memory_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def update_record(self, record: SharedMemoryRecord) -> None:
        """按 memory_id 覆盖写回单条记录。"""
        with self._tx() as conn:
            self._insert_record(conn, record)

    # ------------------------------------------------------------------
    # 治理动作
    # ------------------------------------------------------------------

    def supersede(self, old_id: str, new_id: str, reason: str,
                  actor: str = "auto") -> None:
        """覆盖旧记忆：old.status=SHADOWED, new.supersedes 追加 old_id。

        覆盖不是删除：旧记录保留为影子，可恢复。
        """
        now = _now_iso()
        with self._tx() as conn:
            conn.execute(
                "UPDATE records SET status=?, updated_at=? WHERE memory_id=?",
                (SharedMemoryStatus.SHADOWED.value, now, old_id),
            )
            row = conn.execute(
                "SELECT supersedes FROM records WHERE memory_id=?", (new_id,)).fetchone()
            if row is not None:
                try:
                    sup = json.loads(row["supersedes"] or "[]")
                except (ValueError, TypeError):
                    sup = []
                if old_id not in sup:
                    sup.append(old_id)
                conn.execute(
                    "UPDATE records SET supersedes=?, updated_at=? WHERE memory_id=?",
                    (json.dumps(sup, ensure_ascii=False), now, new_id),
                )
        # 记录 DecisionEvent
        decision = DecisionEvent(
            event_id=stable_hash("supersede", old_id, new_id, _now_iso()),
            actor=actor, action="auto_supersede",
            target_ids=[old_id, new_id],
            reason=reason, created_at=_now_iso(),
        )
        self.append_decision(decision)

    def quarantine_memory(
        self,
        memory_id: str,
        reason: str,
        pattern: str,
        original_content: str = "",
        *,
        actor: str = "auto",
        manual_override: bool = False,
    ) -> None:
        """Compatibility wrapper; policy lives in GovernanceEngine."""
        from .governance_engine import GovernanceEngine
        GovernanceEngine(
            self.workspace, self.group_id, store=self,
        ).quarantine(
            memory_id,
            reason=reason,
            pattern=pattern,
            original_content=original_content,
            actor=actor,
            manual_override=manual_override,
        )

    def conflict(self, member_ids: list[str], reason: str) -> str:
        """创建冲突组，返回 group_id。"""
        group_id = stable_hash("conflict", *member_ids, _now_iso())
        group = ConflictGroup(
            group_id=group_id, member_ids=member_ids,
            reason=reason, created_at=_now_iso(),
        )
        self.append_conflict(group)
        # 更新涉及记录的 status
        now = _now_iso()
        if member_ids:
            placeholders = ",".join("?" * len(member_ids))
            with self._tx() as conn:
                conn.execute(
                    f"UPDATE records SET status=?, conflict_group_id=?, updated_at=? "
                    f"WHERE memory_id IN ({placeholders})",
                    (SharedMemoryStatus.CONFLICTED.value, group_id, now, *member_ids),
                )
        # 记录 DecisionEvent
        decision = DecisionEvent(
            event_id=stable_hash("conflict_dec", group_id, _now_iso()),
            actor="auto", action="auto_conflict",
            target_ids=member_ids, reason=reason,
            created_at=_now_iso(),
        )
        self.append_decision(decision)
        return group_id

    def resolve_conflict_group(
        self,
        group_id: str,
        keep_memory_id: str,
        *,
        actor: str = "user",
    ) -> None:
        """Compatibility wrapper; policy lives in GovernanceEngine."""
        from .governance_engine import GovernanceEngine
        GovernanceEngine(
            self.workspace, self.group_id, store=self,
        ).resolve_conflict(group_id, keep_memory_id)

    def close_quarantine(
        self,
        quarantine_id: str,
        *,
        action: str,
        actor: str = "user",
    ) -> None:
        """Compatibility wrapper; policy lives in GovernanceEngine."""
        from .governance_engine import GovernanceEngine
        resolution = "release" if action == "release_quarantine" else "delete"
        GovernanceEngine(
            self.workspace, self.group_id, store=self,
        ).resolve_quarantine(quarantine_id, resolution=resolution)

    def lock(self, memory_id: str) -> None:
        """Compatibility wrapper; policy lives in GovernanceEngine."""
        from .governance_engine import GovernanceEngine
        GovernanceEngine(
            self.workspace, self.group_id, store=self,
        ).human_lock(memory_id)

    def unlock(self, memory_id: str) -> None:
        """Compatibility wrapper; policy lives in GovernanceEngine."""
        from .governance_engine import GovernanceEngine
        GovernanceEngine(
            self.workspace, self.group_id, store=self,
        ).human_unlock(memory_id)

    def restore(self, memory_id: str) -> None:
        """Compatibility wrapper; policy lives in GovernanceEngine."""
        from .governance_engine import GovernanceEngine
        GovernanceEngine(
            self.workspace, self.group_id, store=self,
        ).human_restore(memory_id)

    def delete(
        self,
        memory_id: str,
        *,
        actor: str = "user",
        manual_override: bool = True,
    ) -> None:
        """Compatibility wrapper; policy lives in GovernanceEngine."""
        from .governance_engine import GovernanceEngine
        engine = GovernanceEngine(
            self.workspace, self.group_id, store=self,
        )
        if manual_override:
            engine.human_delete(memory_id)
        else:
            engine.agent_delete(memory_id, actor=actor)

    def edit(self, memory_id: str, body: str) -> None:
        """Compatibility wrapper; policy lives in GovernanceEngine."""
        from .governance_engine import GovernanceEngine
        GovernanceEngine(
            self.workspace, self.group_id, store=self,
        ).human_edit(memory_id, body)

    def _update_record_field(self, memory_id: str, field: str, value: Any) -> None:
        """更新记录的某个字段（白名单列，防 SQL 注入）。"""
        if field not in _RECORD_COLUMNS:
            return
        if field == "locked":
            col_val: Any = 1 if value else 0
        elif field == "supersedes":
            col_val = json.dumps(list(value), ensure_ascii=False)
        elif field == "provenance":
            col_val = json.dumps(
                [p.to_dict() if hasattr(p, "to_dict") else p for p in value],
                ensure_ascii=False)
        else:
            col_val = value
        with self._tx() as conn:
            conn.execute(
                f"UPDATE records SET {field}=? WHERE memory_id=?",
                (col_val, memory_id),
            )

    # ------------------------------------------------------------------
    # 版本管理
    # ------------------------------------------------------------------

    def create_version_snapshot(self, reason: str = "") -> str:
        """创建版本快照（保存全部 5 类数据），返回 version_id。"""
        version_id = stable_hash("v", self.group_id, _now_iso())
        created_at = _now_iso()
        records = self.list_records()
        snapshot = {
            "records": [r.to_dict() for r in records],
            "events": [e.to_dict() for e in self.list_events()],
            "decisions": [d.to_dict() for d in self.list_decisions()],
            "conflicts": [c.to_dict() for c in self.list_conflicts()],
            "quarantine": [q.to_dict() for q in self.list_quarantine()],
        }
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO versions(version_id, reason, created_at, snapshot) VALUES (?,?,?,?)",
                (version_id, reason, created_at,
                 json.dumps(snapshot, ensure_ascii=False)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO active_version(id, version_id) VALUES(1,?)",
                (version_id,),
            )
        return version_id

    def _set_active_version(self, version_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO active_version(id, version_id) VALUES(1,?)",
                (version_id,),
            )

    def get_active_version_id(self) -> str | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT version_id FROM active_version WHERE id=1").fetchone()
        if row is None or not row["version_id"]:
            return None
        return row["version_id"]

    def rollback_to_version(self, version_id: str) -> None:
        """回滚到指定版本（恢复全部 5 类数据）。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT snapshot FROM versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"version not found: {version_id}")
        # 先备份当前状态
        self.create_version_snapshot(f"pre-rollback to {version_id}")
        # 恢复全部 5 类数据
        try:
            snapshot = json.loads(row["snapshot"])
        except (ValueError, TypeError):
            snapshot = {}
        self._restore_snapshot(snapshot)
        # 更新 active 指针
        self._set_active_version(version_id)
        # 记录 DecisionEvent
        decision = DecisionEvent(
            event_id=stable_hash("rollback", version_id, _now_iso()),
            actor="user", action="rollback",
            target_ids=[version_id], reason=f"rollback to {version_id}",
            created_at=_now_iso(),
        )
        self.append_decision(decision)

    def _restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        """用快照原子覆盖全部 5 类数据表。"""
        with self._tx() as conn:
            for table in ("records", "events", "decisions", "conflicts", "quarantine"):
                conn.execute(f"DELETE FROM {table}")
            for d in snapshot.get("records", []):
                try:
                    self._insert_record(conn, SharedMemoryRecord.from_dict(d))
                except (ValueError, KeyError):
                    continue
            for d in snapshot.get("events", []):
                try:
                    self._insert_event(conn, MemoryEvent.from_dict(d))
                except (ValueError, KeyError):
                    continue
            for d in snapshot.get("decisions", []):
                try:
                    self._insert_decision(conn, DecisionEvent(
                        event_id=d["event_id"], actor=d.get("actor", "user"),
                        action=d.get("action", ""),
                        target_ids=list(d.get("target_ids", [])),
                        before_hash=d.get("before_hash", ""),
                        after_hash=d.get("after_hash", ""),
                        reason=d.get("reason", ""),
                        created_at=d.get("created_at", ""),
                    ))
                except (ValueError, KeyError):
                    continue
            for d in snapshot.get("conflicts", []):
                try:
                    self._insert_conflict(conn, ConflictGroup.from_dict(d))
                except (ValueError, KeyError):
                    continue
            for d in snapshot.get("quarantine", []):
                try:
                    self._insert_quarantine(conn, QuarantineEntry.from_dict(d))
                except (ValueError, KeyError):
                    continue

    def list_versions(self) -> list[dict]:
        """列出所有版本。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT version_id, reason, created_at, snapshot FROM versions "
                "ORDER BY created_at").fetchall()
        versions: list[dict] = []
        for row in rows:
            try:
                snap = json.loads(row["snapshot"])
            except (ValueError, TypeError):
                snap = {}
            recs = snap.get("records", [])
            versions.append({
                "version_id": row["version_id"],
                "share_group_id": self.group_id,
                "created_at": row["created_at"],
                "reason": row["reason"],
                "record_count": len(recs),
                "active_count": sum(1 for r in recs if r.get("status") == "active"),
                "shadowed_count": sum(1 for r in recs if r.get("status") == "shadowed"),
                "quarantined_count": sum(1 for r in recs if r.get("status") == "quarantined"),
                "conflicted_count": sum(1 for r in recs if r.get("status") == "conflicted"),
            })
        return versions

    def list_version_snapshots(self) -> list[dict[str, Any]]:
        """导出完整版本快照；用于可恢复的记忆组备份。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT version_id, reason, created_at, snapshot FROM versions "
                "ORDER BY created_at"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot"])
            except (ValueError, TypeError):
                snapshot = {}
            result.append({
                "version_id": row["version_id"],
                "share_group_id": self.group_id,
                "reason": row["reason"],
                "created_at": row["created_at"],
                "snapshot": snapshot,
            })
        return result

    def export_state(self) -> dict[str, list[dict[str, Any]]]:
        """在同一 SQLite 读事务中取得完整可移植状态。"""
        with self._db() as conn:
            conn.execute("BEGIN")
            records = [
                self._row_to_record(row).to_dict()
                for row in conn.execute("SELECT * FROM records ORDER BY rowid")
            ]
            events = [
                self._row_to_event(row).to_dict()
                for row in conn.execute("SELECT * FROM events ORDER BY rowid")
            ]
            decisions = [
                self._row_to_decision(row).to_dict()
                for row in conn.execute("SELECT * FROM decisions ORDER BY rowid")
            ]
            conflicts = [
                self._row_to_conflict(row).to_dict()
                for row in conn.execute("SELECT * FROM conflicts ORDER BY rowid")
            ]
            quarantine = [
                self._row_to_quarantine(row).to_dict()
                for row in conn.execute("SELECT * FROM quarantine ORDER BY rowid")
            ]
            version_rows = conn.execute(
                "SELECT version_id, reason, created_at, snapshot FROM versions "
                "ORDER BY created_at"
            ).fetchall()
            versions: list[dict[str, Any]] = []
            for row in version_rows:
                try:
                    snapshot = json.loads(row["snapshot"])
                except (ValueError, TypeError):
                    snapshot = {}
                versions.append({
                    "version_id": row["version_id"],
                    "share_group_id": self.group_id,
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                    "snapshot": snapshot,
                })
        return {
            "records": records,
            "events": events,
            "decisions": decisions,
            "conflicts": conflicts,
            "quarantine": quarantine,
            "versions": versions,
        }

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """返回共享组状态统计。"""
        records = self.list_records()
        events = self.list_events()
        decisions = self.list_decisions()
        conflicts = self.list_conflicts()
        quarantine = self.list_quarantine()
        return {
            "share_group_id": self.group_id,
            "total_records": len(records),
            "active": sum(1 for r in records if r.status == SharedMemoryStatus.ACTIVE),
            "shadowed": sum(1 for r in records if r.status == SharedMemoryStatus.SHADOWED),
            "conflicted": sum(1 for r in records if r.status == SharedMemoryStatus.CONFLICTED),
            "quarantined": sum(1 for r in records if r.status == SharedMemoryStatus.QUARANTINED),
            "deleted": sum(1 for r in records if r.status == SharedMemoryStatus.DELETED),
            "total_events": len(events),
            "total_decisions": len(decisions),
            "total_conflicts": len(conflicts),
            "total_quarantine": len(quarantine),
            "active_version": self.get_active_version_id(),
        }

    def clear_all(self) -> dict[str, Any]:
        """清空当前组的全部运行时/历史记忆，保留数据库文件和组绑定。

        调用方必须先完成外部导出。这里同时移走 JSONL/旧 versions 侧车，
        避免空库下次打开时被兼容迁移逻辑重新灌回。
        """
        if self.read_only:
            raise PermissionError("read_only_store_cannot_be_cleared")
        before = self.status()
        token = stable_hash("clear", self.group_id, _now_iso())[:12]
        sidecars = [
            self.records_bak_path,
            self.events_bak_path,
            self.decisions_bak_path,
            self.conflicts_bak_path,
            self.quarantine_bak_path,
            self.versions_dir,
        ]
        staged: list[tuple[Path, Path]] = []
        try:
            for path in sidecars:
                if not path.exists():
                    continue
                staged_path = path.with_name(f"{path.name}.clearing-{token}")
                path.replace(staged_path)
                staged.append((path, staged_path))
            with self._tx() as conn:
                for table in (
                    "quarantine", "conflicts", "decisions", "events",
                    "records", "active_version", "versions",
                ):
                    conn.execute(f"DELETE FROM {table}")
        except Exception:
            for original, staged_path in reversed(staged):
                if staged_path.exists() and not original.exists():
                    staged_path.replace(original)
            raise

        warnings: list[str] = []
        for _, staged_path in staged:
            try:
                if staged_path.is_dir():
                    shutil.rmtree(staged_path)
                else:
                    staged_path.unlink()
            except OSError as exc:
                # 已改名的侧车不会被自动迁移回库；报告残留供后续清理。
                warnings.append(f"{staged_path}: {exc}")
        return {
            "share_group_id": self.group_id,
            "before": before,
            "after": self.status(),
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # 向后兼容：从旧 JSONL 迁移
    # ------------------------------------------------------------------

    def _migrate_from_jsonl(self) -> None:
        """检测旧 JSONL 文件并迁移到 SQLite，迁移后重命名为 .bak。

        仅在 memory.db 为空（首次创建）时执行迁移。
        如果 db 已有数据，JSONL 文件是备份格式，不迁移。
        """
        # 如果 db 已有 records，说明不是首次初始化，跳过迁移
        try:
            with self._db() as conn:
                count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                if count > 0:
                    return
        except Exception:
            pass

        jsonl_map = [
            ("records.jsonl", self._migrate_records_jsonl),
            ("events.jsonl", self._migrate_events_jsonl),
            ("decisions.jsonl", self._migrate_decisions_jsonl),
            ("conflicts.jsonl", self._migrate_conflicts_jsonl),
            ("quarantine.jsonl", self._migrate_quarantine_jsonl),
        ]
        for name, migrator in jsonl_map:
            path = self.root / name
            if path.exists():
                try:
                    migrator(path)
                except Exception:
                    # 迁移失败不阻塞初始化；旧文件保留以便重试
                    continue
                try:
                    path.rename(path.with_suffix(".bak"))
                except OSError:
                    pass
        # 迁移 active 指针
        active = self.root / "active.json"
        if active.exists():
            try:
                data = json.loads(active.read_text(encoding="utf-8"))
                vid = data.get("version_id")
                if vid:
                    self._set_active_version(vid)
            except (ValueError, OSError):
                pass
            try:
                active.rename(active.with_suffix(".bak"))
            except OSError:
                pass
        # 迁移旧 versions 目录
        self._migrate_versions_dir()

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _migrate_records_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_record(conn, SharedMemoryRecord.from_dict(d))
                except (ValueError, KeyError):
                    continue

    def _migrate_events_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_event(conn, MemoryEvent.from_dict(d))
                except (ValueError, KeyError):
                    continue

    def _migrate_decisions_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_decision(conn, DecisionEvent(
                        event_id=d["event_id"], actor=d.get("actor", "user"),
                        action=d.get("action", ""),
                        target_ids=list(d.get("target_ids", [])),
                        before_hash=d.get("before_hash", ""),
                        after_hash=d.get("after_hash", ""),
                        reason=d.get("reason", ""),
                        created_at=d.get("created_at", ""),
                    ))
                except (ValueError, KeyError):
                    continue

    def _migrate_conflicts_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_conflict(conn, ConflictGroup.from_dict(d))
                except (ValueError, KeyError):
                    continue

    def _migrate_quarantine_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_quarantine(conn, QuarantineEntry.from_dict(d))
                except (ValueError, KeyError):
                    continue

    def _migrate_versions_dir(self) -> None:
        if not self.versions_dir.exists():
            return
        for vdir in self.versions_dir.iterdir():
            if not vdir.is_dir():
                continue
            manifest: dict[str, Any] = {}
            mpath = vdir / "manifest.json"
            if mpath.exists():
                try:
                    manifest = json.loads(mpath.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    manifest = {}
            version_id = manifest.get("version_id") or vdir.name
            reason = manifest.get("reason", "")
            created_at = manifest.get("created_at", "")
            snapshot = {
                "records": self._read_jsonl(vdir / "records.jsonl"),
                "events": [],
                "decisions": self._read_jsonl(vdir / "decisions.jsonl"),
                "conflicts": [],
                "quarantine": [],
            }
            with self._tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO versions(version_id, reason, created_at, snapshot) "
                    "VALUES (?,?,?,?)",
                    (version_id, reason, created_at,
                     json.dumps(snapshot, ensure_ascii=False)),
                )
        try:
            self.versions_dir.rename(self.root / "versions.bak")
        except OSError:
            pass
