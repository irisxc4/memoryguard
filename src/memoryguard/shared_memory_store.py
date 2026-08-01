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
    EffectiveAgentContext, SharedMemoryRecord, SharedMemoryStatus,
    MemoryKind, MemoryStatus,
    Provenance,
    stable_hash, _now_iso, validate_injection_settings,
    RuleAssignment,
    RuleMatchFeedback,
    RuleMatchReceipt,
    RuleDecision,
    RuleScopeStats,
    RuleException,
    RuleHitReceipt,
    RuleHitFeedback,
)
from .rule_scope import (
    effective_assignments,
    normalize_assignment,
    canonical_project_ref,
    validate_automatic_assignment,
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
    injection_policy TEXT NOT NULL DEFAULT 'relevant',
    priority INTEGER NOT NULL DEFAULT 0,
    supersedes TEXT DEFAULT '[]',
    provenance TEXT DEFAULT '[]',
    agent_instance_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    canonical_hash TEXT DEFAULT '',
    dedup_domain TEXT NOT NULL DEFAULT 'relevant'
);
CREATE INDEX IF NOT EXISTS idx_records_canonical_hash ON records(canonical_hash);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
CREATE TABLE IF NOT EXISTS rule_assignments (
    memory_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    effect TEXT NOT NULL DEFAULT 'include',
    priority_override INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, target_type, target_id, project_ref, effect),
    FOREIGN KEY (memory_id) REFERENCES records(memory_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rule_assignments_memory ON rule_assignments(memory_id);
CREATE TABLE IF NOT EXISTS rule_match_receipts (
    receipt_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    share_group_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL,
    task_hash TEXT NOT NULL,
    task TEXT NOT NULL,
    assignment_ids TEXT NOT NULL DEFAULT '[]',
    selection_reason TEXT NOT NULL DEFAULT '',
    matcher_version TEXT NOT NULL DEFAULT 'rule-bootstrap-v1',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    project_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    runtime_role TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    context_hash TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (memory_id) REFERENCES records(memory_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rule_match_receipts_share_group ON rule_match_receipts(share_group_id);
CREATE INDEX IF NOT EXISTS idx_rule_match_receipts_agent ON rule_match_receipts(agent_instance_id);
CREATE INDEX IF NOT EXISTS idx_rule_match_receipts_task ON rule_match_receipts(task_hash);
-- v2: feedback is an append-only event stream.  A receipt may receive many
-- feedback events over time; the "effective" one is resolved by authority
-- (user > agent > hook > unobserved) and latest created_at.  No UNIQUE
-- constraint on receipt_id.
CREATE TABLE IF NOT EXISTS rule_match_feedbacks (
    feedback_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    actor TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent',
    authority INTEGER NOT NULL DEFAULT 3,
    supersedes_feedback_id TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (receipt_id) REFERENCES rule_match_receipts(receipt_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rule_match_feedbacks_receipt_created ON rule_match_feedbacks(receipt_id, created_at);
CREATE TABLE IF NOT EXISTS rule_decisions (
    decision_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    before_state TEXT NOT NULL DEFAULT '{}',
    after_state TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    undo_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    rule_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    target_ids TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_rule_decisions_rule ON rule_decisions(rule_id);
CREATE INDEX IF NOT EXISTS idx_rule_decisions_actor ON rule_decisions(actor);
CREATE TABLE IF NOT EXISTS rule_scope_stats (
    rule_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    corrected INTEGER NOT NULL DEFAULT 0,
    wrong_scope INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, agent_instance_id, project_ref)
);
CREATE INDEX IF NOT EXISTS idx_rule_scope_stats_agent ON rule_scope_stats(agent_instance_id);
CREATE INDEX IF NOT EXISTS idx_rule_scope_stats_project ON rule_scope_stats(project_ref);
CREATE TABLE IF NOT EXISTS rule_exceptions (
    exception_id TEXT PRIMARY KEY,
    parent_rule TEXT NOT NULL,
    child_exception TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    rollback TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(parent_rule, child_exception)
);
CREATE INDEX IF NOT EXISTS idx_rule_exceptions_parent ON rule_exceptions(parent_rule);
CREATE INDEX IF NOT EXISTS idx_rule_exceptions_child ON rule_exceptions(child_exception);
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

_RULE_ASSIGNMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_assignments (
    memory_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    effect TEXT NOT NULL DEFAULT 'include',
    priority_override INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, target_type, target_id, project_ref, effect),
    FOREIGN KEY (memory_id) REFERENCES records(memory_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rule_assignments_memory ON rule_assignments(memory_id);
"""

_RULE_LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_decisions (
    decision_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    before_state TEXT NOT NULL DEFAULT '{}',
    after_state TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    undo_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    rule_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    target_ids TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_rule_decisions_rule ON rule_decisions(rule_id);
CREATE INDEX IF NOT EXISTS idx_rule_decisions_actor ON rule_decisions(actor);
CREATE TABLE IF NOT EXISTS rule_scope_stats (
    rule_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    corrected INTEGER NOT NULL DEFAULT 0,
    wrong_scope INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, agent_instance_id, project_ref)
);
CREATE INDEX IF NOT EXISTS idx_rule_scope_stats_agent ON rule_scope_stats(agent_instance_id);
CREATE INDEX IF NOT EXISTS idx_rule_scope_stats_project ON rule_scope_stats(project_ref);
CREATE TABLE IF NOT EXISTS rule_exceptions (
    exception_id TEXT PRIMARY KEY,
    parent_rule TEXT NOT NULL,
    child_exception TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    rollback TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(parent_rule, child_exception)
);
CREATE INDEX IF NOT EXISTS idx_rule_exceptions_parent ON rule_exceptions(parent_rule);
CREATE INDEX IF NOT EXISTS idx_rule_exceptions_child ON rule_exceptions(child_exception);
"""

# 允许通过 _update_record_field 更新的列（白名单，防 SQL 注入）
_RECORD_COLUMNS = {
    "body", "status", "kind", "confidence", "conflict_group_id",
    "locked", "injection_policy", "priority", "supersedes", "provenance", "agent_instance_id",
    "created_at", "updated_at",
}

# Mandatory rules never compete with task-relevant recall.  These independent
# limits keep their bounded context safe and are enforced at mutation time.
MANDATORY_MAX_ITEMS = 20
MANDATORY_MAX_CHARS = 12000
MANDATORY_BROADCAST_MAX_ITEMS = 20
MANDATORY_BROADCAST_MAX_CHARS = 12000


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
        self.rule_assignments_bak_path = self.root / "rule_assignments.jsonl"
        self.rule_match_receipts_bak_path = self.root / "rule_match_receipts.jsonl"
        self.rule_match_feedbacks_bak_path = self.root / "rule_match_feedbacks.jsonl"
        self.rule_decisions_bak_path = self.root / "rule_decisions.jsonl"
        self.rule_scope_stats_bak_path = self.root / "rule_scope_stats.jsonl"
        self.rule_exceptions_bak_path = self.root / "rule_exceptions.jsonl"
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
            # A read-only consumer (MCP read/bootstrap) can be the first
            # process opened after upgrade.  Upgrade the existing database
            # before creating immutable/ro query connections; otherwise old
            # rows lack new columns and sqlite.Row lookup fails at read time.
            self._migrate_existing_records_schema()

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
            (self.rule_assignments_bak_path, self.list_rule_assignments()),
            (self.rule_match_receipts_bak_path, self.list_rule_match_receipts()),
            (self.rule_match_feedbacks_bak_path, self.list_rule_match_feedbacks()),
            (self.rule_decisions_bak_path, self.list_rule_decisions()),
            (self.rule_scope_stats_bak_path, self.list_rule_scope_stats()),
            (self.rule_exceptions_bak_path, self.list_rule_exceptions()),
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
            # A checkpoint can remove the sidecar between ``exists`` and
            # ``stat``.  That narrow race is common on Windows because the
            # hook/MCP writer lives in another process.  Probe once instead:
            # a disappearing WAL simply means the main DB is now current.
            try:
                has_live_wal = wal_path.stat().st_size > 0
            except FileNotFoundError:
                has_live_wal = False
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
            # Serialize first-open upgrades with read-only consumers too.  The
            # column inspection below must happen after this lock is acquired.
            conn.execute("BEGIN IMMEDIATE")
            # Existing pre-feature databases need their records columns before
            # _SCHEMA creates indexes/FTS objects that reference them.
            had_records = bool(
                conn.execute("PRAGMA table_info(records)").fetchall()
            )
            had_records_fts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='records_fts'"
            ).fetchone() is not None
            self._migrate_records_schema(conn)
            conn.executescript(_SCHEMA)
            self._migrate_rule_assignments(conn)
            self._migrate_rule_lifecycle_schema(conn)
            if had_records and not had_records_fts:
                # External-content FTS needs an explicit rebuild after it is
                # first attached to a pre-existing records table; otherwise
                # its UPDATE trigger can report a malformed index.
                conn.execute("INSERT INTO records_fts(records_fts) VALUES ('rebuild')")
            # S3.2: schema version 标记(在事务内提交)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '3') "
                "ON CONFLICT(key) DO UPDATE SET value='3'"
            )
            self._migrate_records_schema(conn)

    def _migrate_existing_records_schema(self) -> None:
        """Transactionally migrate an existing group before read-only access."""
        uri = f"file:{self.db_path}?mode=rw"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            # Deferred transactions allow two upgraded processes to inspect
            # the same old columns and race ALTER TABLE.  IMMEDIATE serializes
            # inspection + migration; the waiting opener re-reads columns.
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_records_schema(conn)
                conn.executescript(_RULE_ASSIGNMENT_SCHEMA)
                self._migrate_rule_assignments(conn)
                conn.executescript(_RULE_LIFECYCLE_SCHEMA)
                self._migrate_rule_lifecycle_schema(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def _migrate_records_schema(self, conn: sqlite3.Connection) -> None:
        """Idempotent, lossless records-column migration on an open DB."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()}
        if not cols:
            return
        if "canonical_hash" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN canonical_hash TEXT DEFAULT ''")
        if "injection_policy" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN injection_policy TEXT NOT NULL DEFAULT 'relevant'")
        if "priority" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        if "dedup_domain" not in cols:
            conn.execute("ALTER TABLE records ADD COLUMN dedup_domain TEXT NOT NULL DEFAULT 'relevant'")
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

    def _migrate_rule_assignments(self, conn: sqlite3.Connection) -> None:
        """Losslessly scope pre-audience mandatory records without broadcasting.

        Historical rows with a writer are converted to that writer's private
        audience.  Rows without provenance remain deliberately unassigned;
        bootstrap reports them and fails closed instead of granting a global
        capability by accident.
        """
        # Early audience builds lacked FK enforcement. Rebuild once, dropping
        # orphan rows while preserving every valid assignment.
        has_fk = bool(conn.execute("PRAGMA foreign_key_list(rule_assignments)").fetchall())
        if not has_fk:
            conn.execute("ALTER TABLE rule_assignments RENAME TO rule_assignments_legacy")
            conn.executescript(_RULE_ASSIGNMENT_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO rule_assignments "
                "(memory_id,target_type,target_id,project_ref,effect,priority_override,created_at,updated_at) "
                "SELECT a.memory_id,a.target_type,a.target_id,a.project_ref,a.effect,"
                "a.priority_override,a.created_at,a.updated_at "
                "FROM rule_assignments_legacy a JOIN records r ON r.memory_id=a.memory_id"
            )
            conn.execute("DROP TABLE rule_assignments_legacy")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rule_assignments_memory "
                "ON rule_assignments(memory_id)"
            )
        rows = conn.execute(
            "SELECT memory_id, agent_instance_id FROM records "
            "WHERE injection_policy='always'"
        ).fetchall()
        now = _now_iso()
        for row in rows:
            exists = conn.execute(
                "SELECT 1 FROM rule_assignments WHERE memory_id=? LIMIT 1",
                (row["memory_id"],),
            ).fetchone()
            if exists or not (row["agent_instance_id"] or "").strip():
                continue
            conn.execute(
                "INSERT OR IGNORE INTO rule_assignments "
                "(memory_id,target_type,target_id,project_ref,effect,priority_override,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (row["memory_id"], "agent", row["agent_instance_id"], "", "include", None, now, now),
            )
        # Canonical body identity remains stable; this domain prevents a
        # relevant fact and an audience-scoped mandatory rule from collapsing.
        records = conn.execute(
            "SELECT memory_id,injection_policy,agent_instance_id,dedup_domain "
            "FROM records"
        ).fetchall()
        changes: list[tuple[str, str]] = []
        for row in records:
            assignments = self._list_rule_assignments_conn(
                conn, row["memory_id"],
            )
            domain = self._dedup_domain(
                row["injection_policy"], assignments,
                writer_id=row["agent_instance_id"] or "",
                memory_id=row["memory_id"],
            )
            if (row["dedup_domain"] or "relevant") != domain:
                changes.append((domain, row["memory_id"]))
        if changes:
            # External-content FTS update triggers can be malformed on a
            # pre-feature database until their first rebuild. dedup_domain is
            # unindexed metadata, so suspend/recreate triggers around migration.
            trigger_rows = conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='records' AND sql IS NOT NULL"
            ).fetchall()
            for trigger in trigger_rows:
                conn.execute(
                    f'DROP TRIGGER IF EXISTS "{trigger["name"]}"'
                )
            conn.executemany(
                "UPDATE records SET dedup_domain=? WHERE memory_id=?",
                changes,
            )
            for trigger in trigger_rows:
                conn.execute(trigger["sql"])
            has_fts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='records_fts'"
            ).fetchone()
            if has_fts:
                conn.execute(
                    "INSERT INTO records_fts(records_fts) VALUES ('rebuild')"
                )

    @staticmethod
    def _migrate_rule_lifecycle_schema(conn: sqlite3.Connection) -> None:
        """Create lifecycle tables and repair columns from early prototypes."""
        conn.executescript(_RULE_LIFECYCLE_SCHEMA)
        # The table definitions above are intentionally additive.  A few
        # development snapshots created the decision table before action and
        # target metadata existed; add those columns without rewriting rows.
        for table, name, sql in (
            ("rule_decisions", "rule_id", "TEXT NOT NULL DEFAULT ''"),
            ("rule_decisions", "action", "TEXT NOT NULL DEFAULT ''"),
            ("rule_decisions", "target_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("rule_decisions", "metadata", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql}")

    @staticmethod
    def _canonical_hash(body: str) -> str:
        """S2.2: 正文规范哈希,用于并发去重唯一索引。"""
        import hashlib
        normalized = " ".join(body.split()).lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _assignment_key(item: RuleAssignment) -> tuple[Any, ...]:
        return (
            item.target_type, item.target_id, item.project_ref, item.effect,
            item.priority_override,
        )

    def _normalize_assignments(
        self, memory_id: str, assignments: list[dict | RuleAssignment],
        *, automatic: bool = False, actor_agent_id: str = "",
    ) -> list[RuleAssignment]:
        normalized: dict[tuple[Any, ...], RuleAssignment] = {}
        for raw in assignments:
            value = raw.to_dict() if isinstance(raw, RuleAssignment) else dict(raw)
            value["memory_id"] = memory_id
            item = (
                validate_automatic_assignment(value, actor_agent_id=actor_agent_id)
                if automatic else normalize_assignment(value)
            )
            if item.target_type == "group":
                if item.target_id and item.target_id != self.group_id:
                    raise ValueError("group audience must target the current shared-memory group")
                item = RuleAssignment(
                    memory_id=memory_id, target_type="group",
                    target_id=self.group_id, project_ref=item.project_ref,
                    effect=item.effect, priority_override=item.priority_override,
                )
            normalized[self._assignment_key(item)] = item
        return sorted(normalized.values(), key=self._assignment_key)

    def _default_assignments(self, record: SharedMemoryRecord) -> list[RuleAssignment]:
        if record.injection_policy != "always" or not record.agent_instance_id:
            return []
        return [RuleAssignment(
            memory_id=record.memory_id, target_type="agent",
            target_id=record.agent_instance_id,
        )]

    def _dedup_domain(
        self,
        injection_policy: str,
        assignments: list[RuleAssignment],
        *,
        writer_id: str = "",
        memory_id: str = "",
    ) -> str:
        if injection_policy != "always":
            return "relevant"
        if not assignments:
            return f"always:legacy-unscoped:{memory_id or writer_id}"
        material = json.dumps(
            [self._assignment_key(item) for item in assignments],
            ensure_ascii=False, separators=(",", ":"),
        )
        return f"always:{stable_hash(material)}"

    def _list_rule_assignments_conn(
        self, conn: sqlite3.Connection, memory_id: str,
    ) -> list[RuleAssignment]:
        rows = conn.execute(
            "SELECT * FROM rule_assignments WHERE memory_id=? "
            "ORDER BY target_type,target_id,project_ref,effect",
            (memory_id,),
        ).fetchall()
        return [RuleAssignment(
            memory_id=row["memory_id"], target_type=row["target_type"],
            target_id=row["target_id"] or "", project_ref=row["project_ref"] or "",
            effect=row["effect"] or "include",
            priority_override=row["priority_override"],
            created_at=row["created_at"] or "", updated_at=row["updated_at"] or "",
        ) for row in rows]

    def _insert_assignments(
        self, conn: sqlite3.Connection, memory_id: str,
        assignments: list[RuleAssignment],
    ) -> None:
        now = _now_iso()
        for item in assignments:
            conn.execute(
                "INSERT OR IGNORE INTO rule_assignments "
                "(memory_id,target_type,target_id,project_ref,effect,priority_override,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (memory_id, item.target_type, item.target_id, item.project_ref,
                 item.effect, item.priority_override, now, now),
            )

    @staticmethod
    def _safe_json_str_list(value: Any) -> list[str]:
        items = SharedMemoryStore._safe_json_list(value)
        return [str(item) for item in items]

    def _row_to_rule_match_receipt(
        self, row: sqlite3.Row,
    ) -> RuleMatchReceipt:
        def _col(name: str, default: Any = "") -> Any:
            try:
                return row[name]
            except (KeyError, IndexError):
                return default
        return RuleMatchReceipt(
            receipt_id=row["receipt_id"],
            memory_id=row["memory_id"],
            share_group_id=row["share_group_id"],
            agent_instance_id=row["agent_instance_id"],
            task_hash=row["task_hash"],
            task=row["task"],
            assignment_ids=self._safe_json_str_list(row["assignment_ids"]),
            selection_reason=row["selection_reason"] or "",
            matcher_version=row["matcher_version"] or "rule-bootstrap-v1",
            confidence=float(row["confidence"]),
            created_at=row["created_at"] or "",
            project_ref=canonical_project_ref(str(_col("project_ref", "") or "")),
            provider=str(_col("provider", "") or ""),
            runtime_role=str(_col("runtime_role", "") or ""),
            session_id=str(_col("session_id", "") or ""),
            context_hash=str(_col("context_hash", "") or ""),
        )

    def _row_to_rule_match_feedback(
        self, row: sqlite3.Row,
    ) -> RuleMatchFeedback:
        def _col(name: str, default: Any = "") -> Any:
            try:
                return row[name]
            except (KeyError, IndexError):
                return default
        return RuleMatchFeedback(
            feedback_id=row["feedback_id"],
            receipt_id=row["receipt_id"],
            outcome=row["outcome"],
            actor=row["actor"],
            evidence=row["evidence"] or "",
            confidence=float(row["confidence"]),
            created_at=row["created_at"] or "",
            source=str(_col("source", "agent") or "agent"),
            authority=int(_col("authority", 0) or 0),
            supersedes_feedback_id=str(_col("supersedes_feedback_id", "") or ""),
        )

    def _insert_rule_match_receipt(
        self, conn: sqlite3.Connection, receipt: RuleMatchReceipt,
    ) -> None:
        d = receipt.to_dict()
        try:
            row = conn.execute(
                "SELECT * FROM rule_match_receipts WHERE receipt_id=?",
                (d["receipt_id"],),
            ).fetchone()
            has_new_columns = row is not None and "project_ref" in row.keys()
        except sqlite3.Error:
            has_new_columns = False
        if has_new_columns:
            conn.execute(
                "INSERT OR REPLACE INTO rule_match_receipts "
                "(receipt_id,memory_id,share_group_id,agent_instance_id,task_hash,task,"
                "assignment_ids,selection_reason,matcher_version,confidence,created_at,"
                "project_ref,provider,runtime_role,session_id,context_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    d["receipt_id"], d["memory_id"], d["share_group_id"],
                    d["agent_instance_id"], d["task_hash"], d["task"],
                    json.dumps(d["assignment_ids"], ensure_ascii=False),
                    d["selection_reason"], d["matcher_version"],
                    d["confidence"], d["created_at"],
                    d.get("project_ref", ""), d.get("provider", ""),
                    d.get("runtime_role", ""), d.get("session_id", ""),
                    d.get("context_hash", ""),
                ),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO rule_match_receipts "
                "(receipt_id,memory_id,share_group_id,agent_instance_id,task_hash,task,"
                "assignment_ids,selection_reason,matcher_version,confidence,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    d["receipt_id"], d["memory_id"], d["share_group_id"],
                    d["agent_instance_id"], d["task_hash"], d["task"],
                    json.dumps(d["assignment_ids"], ensure_ascii=False),
                    d["selection_reason"], d["matcher_version"],
                    d["confidence"], d["created_at"],
                ),
            )

    def _insert_rule_match_feedback(self, conn: sqlite3.Connection, feedback: RuleMatchFeedback) -> None:
        d = feedback.to_dict()
        try:
            # Probe the schema, not a row: an empty table has no rowid=1, which used to
            # make the widened INSERT (source/authority/supersedes) silently skipped and
            # every feedback event read back with the default "agent" source.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(rule_match_feedbacks)").fetchall()}
            has_new_columns = {"source", "authority", "supersedes_feedback_id"}.issubset(columns)
        except sqlite3.Error:
            has_new_columns = False
        if has_new_columns:
            conn.execute(
                "INSERT INTO rule_match_feedbacks "
                "(feedback_id,receipt_id,outcome,actor,evidence,confidence,created_at,"
                "source,authority,supersedes_feedback_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    d["feedback_id"], d["receipt_id"], d["outcome"], d["actor"],
                    d["evidence"], d["confidence"], d["created_at"],
                    d.get("source", "agent"), d.get("authority", 3),
                    d.get("supersedes_feedback_id", ""),
                ),
            )
        else:
            conn.execute(
                "INSERT INTO rule_match_feedbacks "
                "(feedback_id,receipt_id,outcome,actor,evidence,confidence,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    d["feedback_id"], d["receipt_id"], d["outcome"], d["actor"],
                    d["evidence"], d["confidence"], d["created_at"],
                ),
            )

    @staticmethod
    def _include_assignments(
        assignments: list[RuleAssignment],
    ) -> list[RuleAssignment]:
        return [item for item in assignments if item.effect == "include"]

    def audiences_may_overlap(
        self,
        left: list[RuleAssignment],
        right: list[RuleAssignment],
    ) -> bool:
        """Conservative overlap test used by dedup/conflict/supersede."""
        left_includes = self._include_assignments(left)
        right_includes = self._include_assignments(right)
        if not left_includes or not right_includes:
            return False
        broad = {"group", "system"}
        for a in left_includes:
            for b in right_includes:
                if a.target_type in broad or b.target_type in broad:
                    return True
                if (
                    a.project_ref and b.project_ref
                    and a.project_ref != b.project_ref
                ):
                    continue
                if a.target_type == "agent" and b.target_type == "agent":
                    if a.target_id == b.target_id:
                        return True
                    continue
                if a.target_type == "agent_project" and b.target_type in {
                    "agent", "agent_project",
                }:
                    if a.target_id == b.target_id:
                        return True
                    continue
                if b.target_type == "agent_project" and a.target_type == "agent":
                    if a.target_id == b.target_id:
                        return True
                    continue
                if (
                    a.target_type == b.target_type
                    and a.target_id == b.target_id
                ):
                    return True
                # Different provider/project/role dimensions can coexist.
                if a.target_type != b.target_type:
                    return True
        return False

    def record_domain_overlaps(
        self,
        record: SharedMemoryRecord,
        incoming_policy: str,
        incoming_assignments: list[RuleAssignment],
    ) -> bool:
        if record.injection_policy != incoming_policy:
            return False
        if incoming_policy != "always":
            return True
        # A duplicate merge retains the existing record's audience.  Overlap is
        # insufficient here: merging agent A's rule with a group rule would
        # silently remove one audience.  Only equivalent normalized sets are
        # therefore mergeable; partially-overlapping rules remain distinct.
        existing = self._normalize_assignments(
            record.memory_id, self.list_rule_assignments(record.memory_id),
        )
        incoming = self._normalize_assignments(record.memory_id, incoming_assignments)
        return {
            self._assignment_key(item) for item in existing
        } == {
            self._assignment_key(item) for item in incoming
        }

    # ------------------------------------------------------------------
    # 行 <-> 对象 转换
    # ------------------------------------------------------------------

    def _insert_record(
        self, conn: sqlite3.Connection, record: SharedMemoryRecord,
        *, dedup_domain: str | None = None,
    ) -> None:
        d = record.to_dict()
        # S2.2: 计算 canonical_hash
        c_hash = self._canonical_hash(d.get("body", ""))
        conn.execute(
            """INSERT OR REPLACE INTO records
               (memory_id, body, kind, status, confidence, conflict_group_id, locked,
                injection_policy, priority, supersedes, provenance, agent_instance_id, created_at, updated_at, canonical_hash, dedup_domain)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d["memory_id"], d["body"], d["kind"], d["status"], d["confidence"],
             d.get("conflict_group_id", ""), 1 if d.get("locked") else 0,
             d.get("injection_policy", "relevant"), d.get("priority", 0),
             json.dumps(d.get("supersedes", []), ensure_ascii=False),
             json.dumps(d.get("provenance", []), ensure_ascii=False),
             d.get("agent_instance_id", ""), d.get("created_at", ""), d.get("updated_at", ""),
             c_hash, dedup_domain or (
                 "relevant" if record.injection_policy != "always"
                 else f"always:legacy-unscoped:{record.memory_id}"
             )),
        )

    def _row_to_record(self, row: sqlite3.Row) -> SharedMemoryRecord:
        columns = set(row.keys())
        d = {
            "memory_id": row["memory_id"],
            "body": row["body"],
            "kind": row["kind"],
            "status": row["status"],
            "confidence": row["confidence"],
            "conflict_group_id": row["conflict_group_id"] or "",
            "locked": bool(row["locked"]),
            "injection_policy": (
                (row["injection_policy"] or "relevant")
                if "injection_policy" in columns else "relevant"
            ),
            "priority": (
                (row["priority"] if row["priority"] is not None else 0)
                if "priority" in columns else 0
            ),
            "supersedes": self._safe_json_list(row["supersedes"]),
            "provenance": self._safe_json_list(row["provenance"]),
            "agent_instance_id": row["agent_instance_id"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return SharedMemoryRecord.from_dict(d)

    @staticmethod
    def _safe_json_list(value: Any) -> list[Any]:
        """Read historical JSON without allowing one corrupt row to DoS a group."""
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    def _validate_mandatory_budget(
        self,
        record: SharedMemoryRecord,
        *,
        assignments: list[RuleAssignment] | None = None,
        conn: sqlite3.Connection | None = None,
        replacing_id: str = "",
    ) -> None:
        """Reject a prospective active mandatory record before it is stored."""
        validate_injection_settings(record.injection_policy, record.priority)
        if (
            record.status != SharedMemoryStatus.ACTIVE
            or record.injection_policy != "always"
        ):
            return
        query_conn = conn
        owns_conn = query_conn is None
        if query_conn is None:
            query_conn = self._connect()
        try:
            audience = assignments
            if audience is None:
                lookup_id = replacing_id or record.memory_id
                audience = self._list_rule_assignments_conn(query_conn, lookup_id)
                if not audience:
                    audience = self._default_assignments(record)
            includes = [item for item in audience if item.effect == "include"]
            if not includes:
                return
            rows = query_conn.execute(
                "SELECT memory_id,body FROM records "
                "WHERE status='active' AND injection_policy='always'"
            ).fetchall()
            existing: dict[str, tuple[str, list[RuleAssignment]]] = {}
            for row in rows:
                if row["memory_id"] in {record.memory_id, replacing_id}:
                    continue
                existing[row["memory_id"]] = (
                    row["body"] or "",
                    self._list_rule_assignments_conn(query_conn, row["memory_id"]),
                )

            broad_types = {"group", "system", "project", "provider", "runtime_role"}
            broad_existing = {
                memory_id: body for memory_id, (body, items) in existing.items()
                if any(item.effect == "include" and item.target_type in broad_types for item in items)
            }
            if any(item.target_type in broad_types for item in includes):
                if (
                    len(broad_existing) + 1 > MANDATORY_BROADCAST_MAX_ITEMS
                    or sum(len(body) for body in broad_existing.values()) + len(record.body or "")
                    > MANDATORY_BROADCAST_MAX_CHARS
                ):
                    raise ValueError("mandatory_broadcast_budget_exceeded")

            # Enforce against every potentially matching runtime identity, not
            # merely same-shaped assignments.  E.g. agent(A)+agent_project(A,p)
            # has a shared context and must consume one common 20-item budget.
            all_audiences = [items for _body, items in existing.values()]
            all_audiences.append(audience)
            candidates = self._potential_effective_contexts(all_audiences)
            for context in candidates:
                incoming_includes, incoming_excludes = effective_assignments(
                    audience, context,
                )
                if not incoming_includes or incoming_excludes:
                    continue
                scoped = {
                    memory_id: body for memory_id, (body, items) in existing.items()
                    if (matched := effective_assignments(items, context))[0]
                    and not matched[1]
                }
                if (
                    len(scoped) + 1 > MANDATORY_MAX_ITEMS
                    or sum(len(body) for body in scoped.values()) + len(record.body or "")
                    > MANDATORY_MAX_CHARS
                ):
                    raise ValueError(
                        "mandatory_rule_budget_exceeded: "
                        f"max_items={MANDATORY_MAX_ITEMS}, max_chars={MANDATORY_MAX_CHARS}"
                    )
        finally:
            if owns_conn:
                query_conn.close()

    def _potential_effective_contexts(
        self, audiences: list[list[RuleAssignment]],
    ) -> list[EffectiveAgentContext]:
        """Finite representatives for all audience matches relevant to a write.

        Assignment predicates are equality tests over four independent runtime
        dimensions.  Values occurring in a rule plus one non-matching sentinel
        per dimension fully cover their truth values, so this is exhaustive
        without guessing identities outside the trusted runtime context.
        """
        values: dict[str, set[str]] = {
            "agent": {"__other_agent__"}, "project": {"__other_project__"},
            "provider": {"__other_provider__"}, "role": {"__other_role__"},
        }
        for assignments in audiences:
            for item in assignments:
                if item.target_type in {"agent", "agent_project"} and item.target_id:
                    values["agent"].add(item.target_id)
                if item.target_type == "project":
                    values["project"].add(item.project_ref or item.target_id)
                elif item.project_ref:
                    values["project"].add(item.project_ref)
                if item.target_type == "provider" and item.target_id:
                    values["provider"].add(item.target_id)
                if item.target_type == "runtime_role" and item.target_id:
                    values["role"].add(item.target_id)
        return [
            EffectiveAgentContext(
                agent_instance_id=agent, share_group_id=self.group_id,
                project_ref=project, provider=provider, runtime_role=role,
            )
            for agent in sorted(values["agent"])
            for project in sorted(values["project"])
            for provider in sorted(values["provider"])
            for role in sorted(values["role"])
        ]

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

    def _insert_rule_decision(self, conn: sqlite3.Connection, decision: RuleDecision) -> None:
        d = decision.to_dict()
        conn.execute(
            "INSERT OR REPLACE INTO rule_decisions "
            "(decision_id,actor,before_state,after_state,reason,confidence,undo_id,"
            "created_at,rule_id,action,target_ids,metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d["decision_id"], d["actor"],
                json.dumps(d.get("before", {}), ensure_ascii=False),
                json.dumps(d.get("after", {}), ensure_ascii=False),
                d.get("reason", ""), d.get("confidence", 1.0),
                d.get("undo_id", ""), d.get("created_at", ""),
                d.get("rule_id", ""), d.get("action", ""),
                json.dumps(d.get("target_ids", []), ensure_ascii=False),
                json.dumps({
                    key: d.get(key)
                    for key in (
                        "status", "memory_id", "parent_rule_id", "kind",
                        "assignments", "target_type", "target_id", "project_ref",
                        "scope_confidence", "scope_reason", "blocked_reason",
                        "body", "version_id", "feedback_id", "receipt_id",
                        "child_rule_id", "metadata",
                    )
                    if d.get(key) not in (None, "", [], {})
                }, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _decode_json_value(value: Any, default: Any) -> Any:
        if value is None or value == "":
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            # Preserve historical opaque before/after strings rather than
            # failing the entire group's audit history.
            return value

    def _row_to_rule_decision(self, row: sqlite3.Row) -> RuleDecision:
        columns = set(row.keys())
        return RuleDecision(
            decision_id=row["decision_id"],
            actor=row["actor"],
            before=self._decode_json_value(row["before_state"], {}),
            after=self._decode_json_value(row["after_state"], {}),
            reason=row["reason"] or "",
            confidence=float(row["confidence"]),
            undo_id=row["undo_id"] or "",
            created_at=row["created_at"] or "",
            rule_id=row["rule_id"] if "rule_id" in columns else "",
            action=row["action"] if "action" in columns else "",
            target_ids=self._safe_json_str_list(
                row["target_ids"] if "target_ids" in columns else "[]"
            ),
            **self._decode_json_value(
                row["metadata"] if "metadata" in columns else "{}", {}
            ) if isinstance(
                self._decode_json_value(
                    row["metadata"] if "metadata" in columns else "{}", {}
                ), dict
            ) else {},
        )

    def _insert_rule_scope_stats(self, conn: sqlite3.Connection, stats: RuleScopeStats) -> None:
        d = stats.to_dict()
        project_ref = canonical_project_ref(d.get("project_ref", ""))
        conn.execute(
            "INSERT OR REPLACE INTO rule_scope_stats "
            "(rule_id,agent_instance_id,project_ref,total,accepted,corrected,wrong_scope,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                d["rule_id"], d.get("agent_instance_id", ""), project_ref,
                d["total"], d["accepted"], d["corrected"], d["wrong_scope"],
                d.get("created_at", ""), d.get("updated_at", ""),
            ),
        )

    @staticmethod
    def _row_to_rule_scope_stats(row: sqlite3.Row) -> RuleScopeStats:
        return RuleScopeStats(
            rule_id=row["rule_id"],
            agent_instance_id=row["agent_instance_id"] or "",
            project_ref=row["project_ref"] or "",
            total=int(row["total"] or 0),
            accepted=int(row["accepted"] or 0),
            corrected=int(row["corrected"] or 0),
            wrong_scope=int(row["wrong_scope"] or 0),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def _insert_rule_exception(self, conn: sqlite3.Connection, exception: RuleException) -> None:
        d = exception.to_dict()
        conn.execute(
            "INSERT OR REPLACE INTO rule_exceptions "
            "(exception_id,parent_rule,child_exception,priority,reason,rollback,active,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                d["exception_id"], d["parent_rule"], d["child_exception"],
                d["priority"], d.get("reason", ""),
                json.dumps(d.get("rollback", {}), ensure_ascii=False),
                1 if d.get("active", True) else 0,
                d.get("created_at", ""), d.get("updated_at", ""),
            ),
        )

    def _row_to_rule_exception(self, row: sqlite3.Row) -> RuleException:
        return RuleException(
            exception_id=row["exception_id"],
            parent_rule=row["parent_rule"],
            child_exception=row["child_exception"],
            priority=int(row["priority"] or 0),
            reason=row["reason"] or "",
            rollback=self._decode_json_value(row["rollback"], {}),
            active=bool(row["active"]),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
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

    def write_rule_with_assignments(
        self, record: SharedMemoryRecord, *,
        assignments: list[dict | RuleAssignment] | None = None,
        decision: DecisionEvent | RuleDecision | None = None,
        dedup_domain: str = "",
        automatic: bool = False,
        actor_agent_id: str = "",
    ) -> SharedMemoryRecord:
        """Atomically persist record, audience and optional audit decision."""
        validate_injection_settings(record.injection_policy, record.priority)
        decision_actor = getattr(decision, "actor", "") if decision is not None else ""
        automatic = automatic or isinstance(decision, RuleDecision) or str(decision_actor).casefold().startswith("auto")
        actor_agent_id = actor_agent_id or record.agent_instance_id
        normalized = self._normalize_assignments(
            record.memory_id, assignments or [], automatic=automatic,
            actor_agent_id=actor_agent_id,
        )
        if record.injection_policy == "always" and not normalized:
            normalized = self._default_assignments(record)
        if record.injection_policy != "always" and normalized:
            raise ValueError("rule assignments require injection_policy=always")
        domain = dedup_domain or self._dedup_domain(
            record.injection_policy, normalized,
            writer_id=record.agent_instance_id, memory_id=record.memory_id,
        )
        c_hash = self._canonical_hash(record.body)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT memory_id,provenance FROM records "
                "WHERE canonical_hash=? AND dedup_domain=? "
                "AND status='active' LIMIT 1",
                (c_hash, domain),
            ).fetchone()
            if existing:
                old = json.loads(existing["provenance"] or "[]")
                new = record.to_dict().get("provenance", [])
                merged = old + [item for item in new if item not in old]
                conn.execute(
                    "UPDATE records SET provenance=?,updated_at=? WHERE memory_id=?",
                    (json.dumps(merged, ensure_ascii=False),
                     record.updated_at, existing["memory_id"]),
                )
                record.memory_id = existing["memory_id"]
            else:
                self._validate_mandatory_budget(
                    record, assignments=normalized, conn=conn,
                )
                self._insert_record(conn, record, dedup_domain=domain)
                self._insert_assignments(conn, record.memory_id, normalized)
            if decision is not None:
                if isinstance(decision, RuleDecision):
                    self._insert_rule_decision(conn, decision)
                else:
                    self._insert_decision(conn, decision)
            conn.commit()
            return record
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_record(
        self, record: SharedMemoryRecord, *,
        assignments: list[dict | RuleAssignment] | None = None,
        dedup_domain: str = "",
    ) -> None:
        self.write_rule_with_assignments(
            record, assignments=assignments, dedup_domain=dedup_domain,
        )
        self._append_jsonl(self.records_bak_path, record)

    def append_decision(self, decision: DecisionEvent | RuleDecision) -> None:
        """追加决策事件。"""
        if isinstance(decision, RuleDecision):
            self.append_rule_decision(decision)
            return
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

    def append_rule_decision(self, decision: RuleDecision) -> RuleDecision:
        """Persist one structured lifecycle decision idempotently."""
        caller_decision = decision
        if not isinstance(decision, RuleDecision):
            raw = (
                decision.to_dict() if hasattr(decision, "to_dict")
                else dict(decision)
            )
            # ``rule_creation.RuleDecision`` is a mapping used by the
            # rolling service layer and predates this persistence model.  It
            # has no actor/before/after fields; keep it auditable by treating
            # the service result as an automatic decision and preserving its
            # full payload in the structured after state.
            raw.setdefault("actor", "auto")
            raw.setdefault("before", {})
            raw.setdefault("after", dict(raw))
            decision = RuleDecision.from_dict(raw)
        payload = decision.to_dict()
        if not payload["created_at"]:
            payload["created_at"] = _now_iso()
        persisted = RuleDecision.from_dict(payload)
        with self._tx() as conn:
            self._insert_rule_decision(conn, persisted)
            row = conn.execute(
                "SELECT * FROM rule_decisions WHERE decision_id=?",
                (persisted.decision_id,),
            ).fetchone()
        # The optional rule_creation service exposes its own Mapping-shaped
        # RuleDecision.  Return that object to preserve its mapping contract;
        # native schema callers receive the canonical persisted model.
        result = (
            caller_decision if not isinstance(caller_decision, RuleDecision)
            else (self._row_to_rule_decision(row) if row else persisted)
        )
        self._append_jsonl(self.rule_decisions_bak_path, result)
        return result

    append_auto_decision = append_rule_decision

    def get_rule_decision(self, decision_id: str) -> RuleDecision | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        return self._row_to_rule_decision(row) if row else None

    def list_rule_decisions(
        self, *, decision_id: str | None = None,
        actor: str | None = None, rule_id: str | None = None,
        memory_id: str | None = None, undo_id: str | None = None,
    ) -> list[RuleDecision]:
        sql = "SELECT * FROM rule_decisions"
        clauses: list[str] = []
        params: list[Any] = []
        if decision_id:
            clauses.append("decision_id=?")
            params.append(decision_id)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if rule_id or memory_id:
            clauses.append("rule_id=?")
            params.append(rule_id or memory_id)
        if undo_id:
            clauses.append("undo_id=?")
            params.append(undo_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, rowid"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_rule_decision(row) for row in rows]

    list_auto_decisions = list_rule_decisions

    def undo_rule_decision(
        self, decision_id: str, actor: str = "user",
    ) -> RuleDecision:
        """Append an explicit inverse decision and optionally restore its snapshot.

        Undo is audit-first: an absent/non-version ``undo_id`` does not make
        the decision disappear; the inverse record still explains what was
        requested.  Snapshot restoration is attempted only when the token is
        a known version in this share group.
        """
        original = self.get_rule_decision(decision_id)
        if original is None:
            raise ValueError("rule_decision_not_found")
        if original.undo_id:
            try:
                if any(item.get("version_id") == original.undo_id for item in self.list_version_snapshots()):
                    self.rollback_to_version(original.undo_id)
            except (FileNotFoundError, ValueError, RuntimeError):
                pass
        now = _now_iso()
        inverse = RuleDecision(
            decision_id=stable_hash("rule-decision-undo", decision_id, actor, now),
            actor=actor,
            before=original.after,
            after=original.before,
            reason=f"undo {decision_id}",
            confidence=original.confidence,
            undo_id=decision_id,
            created_at=now,
            rule_id=original.rule_id,
            action="undo",
            target_ids=list(original.target_ids),
            parent_rule_id=original.parent_rule_id,
            memory_id=original.memory_id,
        )
        return self.append_rule_decision(inverse)

    rollback_rule_decision = undo_rule_decision

    def append_rule_scope_stats(self, stats: RuleScopeStats) -> RuleScopeStats:
        """Upsert cumulative counters for one (rule, Agent, project) scope."""
        if not isinstance(stats, RuleScopeStats):
            stats = RuleScopeStats.from_dict(dict(stats))
        now = _now_iso()
        payload = stats.to_dict()
        if not payload["created_at"]:
            payload["created_at"] = now
        if not payload["updated_at"]:
            payload["updated_at"] = now
        persisted = RuleScopeStats.from_dict(payload)
        with self._tx() as conn:
            self._insert_rule_scope_stats(conn, persisted)
            row = conn.execute(
                "SELECT * FROM rule_scope_stats WHERE rule_id=? AND agent_instance_id=? AND project_ref=?",
                (persisted.rule_id, persisted.agent_instance_id,
                 canonical_project_ref(persisted.project_ref)),
            ).fetchone()
        result = self._row_to_rule_scope_stats(row) if row else persisted
        self._append_jsonl(self.rule_scope_stats_bak_path, result)
        return result

    upsert_rule_scope_stats = append_rule_scope_stats
    update_rule_scope_stats = append_rule_scope_stats

    def record_rule_scope(
        self,
        rule_id: str | RuleScopeStats,
        *,
        agent_instance_id: str = "",
        project_ref: str = "",
        agent: str | None = None,
        project: str | None = None,
        outcome: str = "accepted",
        count: int = 1,
    ) -> RuleScopeStats:
        """Increment feedback counters for a precise runtime scope.

        This method never infers sibling agents, group membership, provider or
        runtime-role dimensions; callers must pass the exact observed scope.
        """
        if isinstance(rule_id, RuleScopeStats):
            return self.append_rule_scope_stats(rule_id)
        if not rule_id:
            raise ValueError("rule_id is required")
        if agent is not None:
            agent_instance_id = agent
        if project is not None:
            project_ref = project
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("count must be a positive integer")
        outcome = str(outcome or "").strip().casefold()
        allowed = {"accepted", "corrected", "wrong_scope", "total", "ignored", "not_applicable"}
        if outcome not in allowed:
            raise ValueError("invalid scope outcome")
        project_ref = canonical_project_ref(project_ref)
        now = _now_iso()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM rule_scope_stats WHERE rule_id=? AND agent_instance_id=? AND project_ref=?",
                (rule_id, agent_instance_id, project_ref),
            ).fetchone()
            if row is None:
                values = {
                    "rule_id": rule_id, "agent_instance_id": agent_instance_id,
                    "project_ref": project_ref, "total": count,
                    "accepted": count if outcome == "accepted" else 0,
                    "corrected": count if outcome == "corrected" else 0,
                    "wrong_scope": count if outcome == "wrong_scope" else 0,
                    "created_at": now, "updated_at": now,
                }
                stats = RuleScopeStats(**values)
                self._insert_rule_scope_stats(conn, stats)
            else:
                stats = self._row_to_rule_scope_stats(row)
                stats.total += count
                if outcome == "accepted":
                    stats.accepted += count
                elif outcome == "corrected":
                    stats.corrected += count
                elif outcome == "wrong_scope":
                    stats.wrong_scope += count
                stats.updated_at = now
                self._insert_rule_scope_stats(conn, stats)
            row = conn.execute(
                "SELECT * FROM rule_scope_stats WHERE rule_id=? AND agent_instance_id=? AND project_ref=?",
                (rule_id, agent_instance_id, project_ref),
            ).fetchone()
        result = self._row_to_rule_scope_stats(row)
        self._append_jsonl(self.rule_scope_stats_bak_path, result)
        return result

    record_scope_stat = record_rule_scope

    def list_rule_scope_stats(
        self, *, rule_id: str | None = None, memory_id: str | None = None,
        agent_instance_id: str | None = None, project_ref: str | None = None,
        agent: str | None = None, project: str | None = None,
    ) -> list[RuleScopeStats]:
        sql = "SELECT * FROM rule_scope_stats"
        clauses: list[str] = []
        params: list[Any] = []
        if rule_id or memory_id:
            clauses.append("rule_id=?")
            params.append(rule_id or memory_id)
        if agent_instance_id is None and agent is not None:
            agent_instance_id = agent
        if project_ref is None and project is not None:
            project_ref = project
        if agent_instance_id is not None:
            clauses.append("agent_instance_id=?")
            params.append(agent_instance_id)
        if project_ref is not None:
            clauses.append("project_ref=?")
            params.append(canonical_project_ref(project_ref))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rule_id, agent_instance_id, project_ref"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_rule_scope_stats(row) for row in rows]

    list_scope_stats = list_rule_scope_stats
    query_rule_scope_stats = list_rule_scope_stats

    def get_rule_scope_stats(
        self, rule_id: str, *, agent_instance_id: str = "",
        project_ref: str = "", agent: str | None = None,
        project: str | None = None,
    ) -> RuleScopeStats | None:
        if agent is not None:
            agent_instance_id = agent
        if project is not None:
            project_ref = project
        project_ref = canonical_project_ref(project_ref)
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_scope_stats WHERE rule_id=? AND agent_instance_id=? AND project_ref=?",
                (rule_id, agent_instance_id, project_ref),
            ).fetchone()
        return self._row_to_rule_scope_stats(row) if row else None

    def append_rule_exception(self, exception: RuleException) -> RuleException:
        """Persist a parent→child exception relation, idempotently."""
        if not isinstance(exception, RuleException):
            exception = RuleException.from_dict(dict(exception))
        now = _now_iso()
        payload = exception.to_dict()
        if not payload["created_at"]:
            payload["created_at"] = now
        if not payload["updated_at"]:
            payload["updated_at"] = now
        persisted = RuleException.from_dict(payload)
        with self._tx() as conn:
            self._insert_rule_exception(conn, persisted)
            row = conn.execute(
                "SELECT * FROM rule_exceptions WHERE exception_id=?",
                (persisted.exception_id,),
            ).fetchone()
        result = self._row_to_rule_exception(row) if row else persisted
        self._append_jsonl(self.rule_exceptions_bak_path, result)
        return result

    add_rule_exception = append_rule_exception

    def get_rule_exception(self, exception_id: str) -> RuleException | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_exceptions WHERE exception_id=?",
                (exception_id,),
            ).fetchone()
        return self._row_to_rule_exception(row) if row else None

    def list_rule_exceptions(
        self, *, parent_rule: str | None = None,
        child_exception: str | None = None,
        active: bool | None = None,
    ) -> list[RuleException]:
        sql = "SELECT * FROM rule_exceptions"
        clauses: list[str] = []
        params: list[Any] = []
        if parent_rule is not None:
            clauses.append("parent_rule=?")
            params.append(parent_rule)
        if child_exception is not None:
            clauses.append("child_exception=?")
            params.append(child_exception)
        if active is not None:
            clauses.append("active=?")
            params.append(1 if active else 0)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, created_at, exception_id"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_rule_exception(row) for row in rows]

    def rollback_rule_exception(
        self, exception_id: str, *, rollback: Any = None,
        decision: RuleDecision | None = None,
    ) -> RuleException:
        """Deactivate an exception while retaining reversible rollback data."""
        now = _now_iso()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM rule_exceptions WHERE exception_id=?",
                (exception_id,),
            ).fetchone()
            if row is None:
                raise ValueError("rule_exception_not_found")
            current = self._row_to_rule_exception(row)
            current.active = False
            current.updated_at = now
            if rollback is not None:
                current.rollback = rollback
            self._insert_rule_exception(conn, current)
            if decision is not None:
                self._insert_rule_decision(conn, decision)
            row = conn.execute(
                "SELECT * FROM rule_exceptions WHERE exception_id=?",
                (exception_id,),
            ).fetchone()
        result = self._row_to_rule_exception(row)
        self._append_jsonl(self.rule_exceptions_bak_path, result)
        return result

    deactivate_rule_exception = rollback_rule_exception

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

    def list_rule_assignments(self, memory_id: str | None = None) -> list[RuleAssignment]:
        sql = "SELECT * FROM rule_assignments"
        params: tuple[Any, ...] = ()
        if memory_id:
            sql += " WHERE memory_id=?"
            params = (memory_id,)
        sql += " ORDER BY memory_id, target_type, target_id, project_ref, effect"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [RuleAssignment(
            memory_id=row["memory_id"], target_type=row["target_type"],
            target_id=row["target_id"] or "", project_ref=row["project_ref"] or "",
            effect=row["effect"] or "include", priority_override=row["priority_override"],
            created_at=row["created_at"] or "", updated_at=row["updated_at"] or "",
        ) for row in rows]

    def append_rule_match_receipt(
        self, receipt: RuleMatchReceipt,
    ) -> RuleMatchReceipt:
        if receipt.share_group_id != self.group_id:
            raise ValueError("receipt share group mismatch")
        if not receipt.receipt_id:
            raise ValueError("receipt_id is required")
        now = _now_iso()
        payload = receipt.to_dict()
        if not payload["created_at"]:
            payload["created_at"] = now
        with self._tx() as conn:
            record = conn.execute(
                "SELECT 1 FROM records WHERE memory_id=?", (receipt.memory_id,),
            ).fetchone()
            if record is None:
                raise ValueError("receipt memory_not_found")
            try:
                self._insert_rule_match_receipt(conn, RuleMatchReceipt(**{**payload, "created_at": payload["created_at"]}))
            except sqlite3.IntegrityError:
                # Idempotent by receipt_id; return existing row directly.
                row = conn.execute(
                    "SELECT * FROM rule_match_receipts WHERE receipt_id=?",
                    (receipt.receipt_id,),
                ).fetchone()
                if row is None:
                    raise
                return self._row_to_rule_match_receipt(row)
        return self.get_rule_match_receipt(receipt.receipt_id)

    def get_rule_match_receipt(
        self, receipt_id: str,
    ) -> RuleMatchReceipt | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_match_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_rule_match_receipt(row)

    def list_rule_match_receipts(
        self,
        *,
        memory_id: str | None = None,
        agent_instance_id: str | None = None,
        task_hash: str | None = None,
        share_group_id: str | None = None,
    ) -> list[RuleMatchReceipt]:
        sql = "SELECT * FROM rule_match_receipts"
        clauses: list[str] = []
        params: list[Any] = []
        if memory_id:
            clauses.append("memory_id=?")
            params.append(memory_id)
        if agent_instance_id:
            clauses.append("agent_instance_id=?")
            params.append(agent_instance_id)
        if task_hash:
            clauses.append("task_hash=?")
            params.append(task_hash)
        if share_group_id:
            clauses.append("share_group_id=?")
            params.append(share_group_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_rule_match_receipt(row) for row in rows]

    def append_rule_match_feedback(
        self,
        feedback: RuleMatchFeedback,
    ) -> RuleMatchFeedback:
        if not feedback.receipt_id:
            raise ValueError("receipt_id is required")
        allowed = {
            "followed", "violated", "not_applicable", "corrected",
            "exception", "ignored", "unobserved",
        }
        if feedback.outcome not in allowed:
            raise ValueError("invalid feedback outcome")
        if feedback.actor is None or str(feedback.actor).strip() == "":
            raise ValueError("actor is required")
        now = _now_iso()
        payload = feedback.to_dict()
        if not payload["created_at"]:
            payload["created_at"] = now
        actor = str(payload["actor"] or "").strip()
        if not actor:
            raise ValueError("actor is required")
        payload["actor"] = actor
        feedback_id = payload["feedback_id"] or stable_hash(
            "rule-feedback", payload["receipt_id"], payload["outcome"],
            actor, payload["evidence"], payload["created_at"],
        )
        payload["feedback_id"] = feedback_id
        with self._tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM rule_match_receipts WHERE receipt_id=?",
                (feedback.receipt_id,),
            ).fetchone()
            if row is None:
                raise ValueError("receipt_not_found")
            try:
                self._insert_rule_match_feedback(
                    conn, RuleMatchFeedback(**{**payload, "feedback_id": feedback_id}),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM rule_match_feedbacks WHERE receipt_id=?",
                    (feedback.receipt_id,),
                ).fetchone()
                if existing is None:
                    raise
                return self._row_to_rule_match_feedback(existing)
        return self.get_rule_match_feedback_by_receipt(feedback.receipt_id)

    def list_rule_match_feedbacks(
        self,
        *,
        receipt_id: str | None = None,
        actor: str | None = None,
    ) -> list[RuleMatchFeedback]:
        sql = "SELECT * FROM rule_match_feedbacks"
        clauses: list[str] = []
        params: list[Any] = []
        if receipt_id:
            clauses.append("receipt_id=?")
            params.append(receipt_id)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_rule_match_feedback(row) for row in rows]

    def get_rule_match_feedback_by_receipt(
        self, receipt_id: str,
    ) -> RuleMatchFeedback | None:
        """Return the *effective* feedback for a receipt.

        v2: feedback is an append-only event stream.  The effective event is
        resolved by precedence order (user > agent > hook > unobserved) then
        by the most recent created_at.  Older ``unobserved`` events never
        block a later explicit user/agent feedback from superseding them.
        """
        from .schema_v3 import FEEDBACK_AUTHORITY_ORDER
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_match_feedbacks WHERE receipt_id=? "
                "ORDER BY created_at, rowid",
                (receipt_id,),
            ).fetchall()
        if not rows:
            return None
        ordered = [self._row_to_rule_match_feedback(row) for row in rows]
        ordered.sort(
            key=lambda item: (
                FEEDBACK_AUTHORITY_ORDER.get(item.source, 0),
                item.created_at or "",
            ),
            reverse=True,
        )
        effective = ordered[0]
        if effective.outcome == "unobserved":
            return None
        return effective

    # Future narrowing logic can use hit terminology without creating a
    # second table or losing compatibility with existing match receipts.
    append_rule_hit_receipt = append_rule_match_receipt
    get_rule_hit_receipt = get_rule_match_receipt
    list_rule_hit_receipts = list_rule_match_receipts
    append_rule_hit_feedback = append_rule_match_feedback
    list_rule_hit_feedbacks = list_rule_match_feedbacks
    get_rule_hit_feedback_by_receipt = get_rule_match_feedback_by_receipt

    def set_rule_assignments(
        self, memory_id: str, assignments: list[dict | RuleAssignment], *,
        automatic: bool = False, actor_agent_id: str = "",
    ) -> list[RuleAssignment]:
        """Replace audience relations atomically; record deletion is separate."""
        normalized = self._normalize_assignments(
            memory_id, assignments, automatic=automatic,
            actor_agent_id=actor_agent_id,
        )
        now = _now_iso()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE memory_id=?", (memory_id,),
            ).fetchone()
            if row is None:
                raise ValueError("memory_not_found")
            record = self._row_to_record(row)
            if record.injection_policy != "always":
                raise ValueError("rule assignments require injection_policy=always")
            self._validate_mandatory_budget(
                record, assignments=normalized, conn=conn,
                replacing_id=memory_id,
            )
            conn.execute("DELETE FROM rule_assignments WHERE memory_id=?", (memory_id,))
            self._insert_assignments(conn, memory_id, normalized)
            conn.execute(
                "UPDATE records SET dedup_domain=?,updated_at=? WHERE memory_id=?",
                (self._dedup_domain(
                    "always", normalized, writer_id=record.agent_instance_id,
                    memory_id=memory_id,
                ), now, memory_id),
            )
        return self.list_rule_assignments(memory_id)

    def replace_actor_assignment(
        self, memory_id: str, actor_agent_id: str,
        assignments: list[dict | RuleAssignment], *, automatic: bool = False,
    ) -> list[RuleAssignment]:
        """Atomically replace only one actor's agent-scoped relations."""
        incoming = self._normalize_assignments(
            memory_id, assignments, automatic=automatic,
            actor_agent_id=actor_agent_id,
        )
        if any(
            item.target_type != "agent" or item.target_id != actor_agent_id
            for item in incoming
        ):
            raise ValueError("actor may replace only its own agent assignment")
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE memory_id=?", (memory_id,),
            ).fetchone()
            if row is None:
                raise ValueError("memory_not_found")
            record = self._row_to_record(row)
            if record.injection_policy != "always":
                raise ValueError("rule assignments require injection_policy=always")
            retained = [
                item for item in self._list_rule_assignments_conn(conn, memory_id)
                if not (item.target_type == "agent" and item.target_id == actor_agent_id)
            ]
            final = self._normalize_assignments(
                memory_id, retained + incoming, automatic=automatic,
                actor_agent_id=actor_agent_id,
            )
            self._validate_mandatory_budget(
                record, assignments=final, conn=conn, replacing_id=memory_id,
            )
            conn.execute(
                "DELETE FROM rule_assignments WHERE memory_id=? "
                "AND target_type='agent' AND target_id=?",
                (memory_id, actor_agent_id),
            )
            self._insert_assignments(conn, memory_id, incoming)
            conn.execute(
                "UPDATE records SET dedup_domain=?,updated_at=? WHERE memory_id=?",
                (self._dedup_domain(
                    "always", final, writer_id=record.agent_instance_id,
                    memory_id=memory_id,
                ), _now_iso(), memory_id),
            )
        return self.list_rule_assignments(memory_id)

    def delete_rule_assignments(self, memory_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM rule_assignments WHERE memory_id=?", (memory_id,))

    def transition_injection_policy(
        self, memory_id: str, injection_policy: str, priority: int, *,
        assignments: list[dict | RuleAssignment] | None = None,
        decision: DecisionEvent | RuleDecision | None = None,
        provenance: list[Provenance] | None = None,
        automatic: bool = False,
        actor_agent_id: str = "",
    ) -> tuple[SharedMemoryRecord, list[RuleAssignment]]:
        """Atomically change policy and its audience lifecycle."""
        injection_policy, priority = validate_injection_settings(
            injection_policy, priority,
        )
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE memory_id=?", (memory_id,),
            ).fetchone()
            if row is None:
                raise ValueError("memory_not_found")
            record = self._row_to_record(row)
            if record.status != SharedMemoryStatus.ACTIVE:
                raise ValueError("injection_policy_transition_requires_active_record")
            decision_actor = getattr(decision, "actor", "") if decision is not None else ""
            transition_automatic = (
                automatic or isinstance(decision, RuleDecision)
                or str(decision_actor).casefold().startswith("auto")
            )
            actor_id = actor_agent_id or record.agent_instance_id
            normalized = self._normalize_assignments(
                memory_id, assignments or [], automatic=transition_automatic,
                actor_agent_id=actor_id,
            )
            if injection_policy == "always":
                if not any(item.effect == "include" for item in normalized):
                    raise ValueError("always policy requires at least one include assignment")
                prospective = SharedMemoryRecord.from_dict({
                    **record.to_dict(),
                    "injection_policy": "always",
                    "priority": priority,
                })
                self._validate_mandatory_budget(
                    prospective, assignments=normalized, conn=conn,
                    replacing_id=memory_id,
                )
            elif normalized:
                raise ValueError("relevant policy cannot retain rule assignments")
            conn.execute(
                "DELETE FROM rule_assignments WHERE memory_id=?", (memory_id,),
            )
            self._insert_assignments(conn, memory_id, normalized)
            domain = self._dedup_domain(
                injection_policy, normalized,
                writer_id=record.agent_instance_id, memory_id=memory_id,
            )
            conn.execute(
                "UPDATE records SET injection_policy=?,priority=?,"
                "dedup_domain=?,provenance=?,updated_at=? WHERE memory_id=?",
                (
                    injection_policy, priority, domain,
                    json.dumps(
                        [item.to_dict() for item in provenance],
                        ensure_ascii=False,
                    ) if provenance is not None else json.dumps(
                        [item.to_dict() for item in record.provenance],
                        ensure_ascii=False,
                    ),
                    _now_iso(), memory_id,
                ),
            )
            if decision is not None:
                if isinstance(decision, RuleDecision):
                    self._insert_rule_decision(conn, decision)
                else:
                    self._insert_decision(conn, decision)
        updated = self.get_record(memory_id)
        if updated is None:
            raise RuntimeError("policy transition lost record")
        return updated, self.list_rule_assignments(memory_id)

    def update_record(self, record: SharedMemoryRecord) -> None:
        """按 memory_id 覆盖写回单条记录。"""
        with self._tx() as conn:
            assignments = self._list_rule_assignments_conn(
                conn, record.memory_id,
            )
            self._validate_mandatory_budget(
                record, assignments=assignments, conn=conn,
                replacing_id=record.memory_id,
            )
            domain = self._dedup_domain(
                record.injection_policy, assignments,
                writer_id=record.agent_instance_id,
                memory_id=record.memory_id,
            )
            self._insert_record(conn, record, dedup_domain=domain)
            self._insert_assignments(conn, record.memory_id, assignments)

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
        """创建版本快照（保存全部 7 类数据），返回 version_id。"""
        version_id = stable_hash("v", self.group_id, _now_iso())
        created_at = _now_iso()
        records = self.list_records()
        snapshot = {
            "records": [r.to_dict() for r in records],
            "rule_assignments": [
                item.to_dict() for item in self.list_rule_assignments()
            ],
            "rule_match_receipts": [item.to_dict() for item in self.list_rule_match_receipts()],
            "rule_match_feedbacks": [
                item.to_dict() for item in self.list_rule_match_feedbacks()
            ],
            "rule_decisions": [item.to_dict() for item in self.list_rule_decisions()],
            "rule_scope_stats": [item.to_dict() for item in self.list_rule_scope_stats()],
            "rule_exceptions": [item.to_dict() for item in self.list_rule_exceptions()],
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
        try:
            snapshot = json.loads(row["snapshot"])
        except (ValueError, TypeError):
            raise ValueError("invalid_snapshot_json")
        prepared = self._validate_snapshot(snapshot)
        # Validate before creating the pre-rollback version or touching any
        # table/pointer.  A malformed snapshot must be a true no-op.
        self.create_version_snapshot(f"pre-rollback to {version_id}")
        # 恢复全部 5 类数据
        self._restore_snapshot(prepared)
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

    def _validate_snapshot(self, snapshot: Any) -> dict[str, list[Any]]:
        """Parse the complete snapshot before a restore can mutate state."""
        if not isinstance(snapshot, dict):
            raise ValueError("invalid_snapshot_structure")
        names = (
            "records", "rule_assignments", "rule_match_receipts",
            "rule_match_feedbacks", "rule_decisions", "rule_scope_stats",
            "rule_exceptions", "events", "decisions",
            "conflicts", "quarantine",
        )
        if any(name in snapshot and not isinstance(snapshot[name], list) for name in names):
            raise ValueError("invalid_snapshot_structure")
        try:
            records = [SharedMemoryRecord.from_dict(value) for value in snapshot.get("records", [])]
            record_ids = {record.memory_id for record in records}
            if len(record_ids) != len(records):
                raise ValueError("duplicate_snapshot_memory_id")
            for record in records:
                validate_injection_settings(record.injection_policy, record.priority)
            records_by_id = {record.memory_id: record for record in records}
            assignments: list[RuleAssignment] = []
            for value in snapshot.get("rule_assignments", []):
                if not isinstance(value, dict):
                    raise ValueError("invalid_snapshot_assignment")
                memory_id = str(value.get("memory_id", ""))
                if memory_id not in record_ids:
                    raise ValueError("snapshot_assignment_without_record")
                if records_by_id[memory_id].injection_policy != "always":
                    raise ValueError("snapshot_assignment_requires_always")
                assignments.extend(self._normalize_assignments(memory_id, [value]))
            rule_match_receipts = []
            for value in snapshot.get("rule_match_receipts", []):
                if not isinstance(value, dict):
                    raise ValueError("invalid_snapshot_rule_match_receipt")
                rule_match_receipts.append(RuleMatchReceipt.from_dict(value))
            rule_match_feedbacks = []
            for value in snapshot.get("rule_match_feedbacks", []):
                if not isinstance(value, dict):
                    raise ValueError("invalid_snapshot_rule_match_feedback")
                rule_match_feedbacks.append(RuleMatchFeedback.from_dict(value))
            rule_decisions = []
            for value in snapshot.get("rule_decisions", []):
                if not isinstance(value, dict):
                    raise ValueError("invalid_snapshot_rule_decision")
                rule_decisions.append(RuleDecision.from_dict(value))
            rule_scope_stats = []
            for value in snapshot.get("rule_scope_stats", []):
                if not isinstance(value, dict):
                    raise ValueError("invalid_snapshot_rule_scope_stats")
                rule_scope_stats.append(RuleScopeStats.from_dict(value))
            rule_exceptions = []
            for value in snapshot.get("rule_exceptions", []):
                if not isinstance(value, dict):
                    raise ValueError("invalid_snapshot_rule_exception")
                rule_exceptions.append(RuleException.from_dict(value))
            receipt_ids = {item.receipt_id for item in rule_match_receipts}
            for feedback in rule_match_feedbacks:
                if feedback.receipt_id not in receipt_ids:
                    raise ValueError("snapshot_feedback_without_receipt")
            events = [MemoryEvent.from_dict(value) for value in snapshot.get("events", [])]
            decisions = [DecisionEvent(
                event_id=value["event_id"], actor=value.get("actor", "user"),
                action=value.get("action", ""), target_ids=list(value.get("target_ids", [])),
                before_hash=value.get("before_hash", ""), after_hash=value.get("after_hash", ""),
                reason=value.get("reason", ""), created_at=value.get("created_at", ""),
            ) for value in snapshot.get("decisions", [])]
            conflicts = [ConflictGroup.from_dict(value) for value in snapshot.get("conflicts", [])]
            quarantine = [QuarantineEntry.from_dict(value) for value in snapshot.get("quarantine", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid_snapshot_structure") from exc
        return {
            "records": records, "rule_assignments": assignments,
            "rule_match_receipts": rule_match_receipts,
            "rule_match_feedbacks": rule_match_feedbacks,
            "rule_decisions": rule_decisions,
            "rule_scope_stats": rule_scope_stats,
            "rule_exceptions": rule_exceptions,
            "events": events, "decisions": decisions,
            "conflicts": conflicts, "quarantine": quarantine,
        }

    def _restore_snapshot(self, snapshot: dict[str, list[Any]]) -> None:
        """用快照原子覆盖全部 5 类数据表。"""
        with self._tx() as conn:
            for table in (
                "rule_match_feedbacks", "rule_match_receipts",
                "rule_decisions", "rule_scope_stats", "rule_exceptions",
                "rule_assignments", "records", "events", "decisions",
                "conflicts", "quarantine",
            ):
                conn.execute(f"DELETE FROM {table}")
            for record in snapshot["records"]:
                assignments = [
                    item for item in snapshot["rule_assignments"]
                    if item.memory_id == record.memory_id
                ]
                self._insert_record(
                    conn, record,
                    dedup_domain=self._dedup_domain(
                        record.injection_policy, assignments,
                        writer_id=record.agent_instance_id,
                        memory_id=record.memory_id,
                    ),
                )
            for item in snapshot["rule_assignments"]:
                self._insert_assignments(conn, item.memory_id, [item])
            for row in conn.execute(
                "SELECT * FROM records WHERE status='active' "
                "AND injection_policy='always'"
            ).fetchall():
                record = self._row_to_record(row)
                assignments = self._list_rule_assignments_conn(
                    conn, record.memory_id,
                )
                self._validate_mandatory_budget(
                    record, assignments=assignments, conn=conn,
                    replacing_id=record.memory_id,
                )
            for event in snapshot["events"]:
                self._insert_event(conn, event)
            for decision in snapshot["rule_decisions"]:
                self._insert_rule_decision(conn, decision)
            for stats in snapshot["rule_scope_stats"]:
                self._insert_rule_scope_stats(conn, stats)
            for exception in snapshot["rule_exceptions"]:
                self._insert_rule_exception(conn, exception)
            for decision in snapshot["decisions"]:
                self._insert_decision(conn, decision)
            for conflict in snapshot["conflicts"]:
                self._insert_conflict(conn, conflict)
            for entry in snapshot["quarantine"]:
                self._insert_quarantine(conn, entry)
            for receipt in snapshot["rule_match_receipts"]:
                self._insert_rule_match_receipt(conn, receipt)
            for feedback in snapshot["rule_match_feedbacks"]:
                self._insert_rule_match_feedback(conn, feedback)

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
            rule_assignments = [
                item.to_dict()
                for row in conn.execute(
                    "SELECT DISTINCT memory_id FROM rule_assignments "
                    "ORDER BY memory_id"
                )
                for item in self._list_rule_assignments_conn(
                    conn, row["memory_id"],
                )
            ]
            rule_match_receipts = [
                self._row_to_rule_match_receipt(row).to_dict()
                for row in conn.execute(
                    "SELECT * FROM rule_match_receipts ORDER BY rowid"
                )
            ]
            rule_match_feedbacks = [
                self._row_to_rule_match_feedback(row).to_dict()
                for row in conn.execute(
                    "SELECT * FROM rule_match_feedbacks ORDER BY rowid"
                )
            ]
            events = [
                self._row_to_event(row).to_dict()
                for row in conn.execute("SELECT * FROM events ORDER BY rowid")
            ]
            decisions = [
                self._row_to_decision(row).to_dict()
                for row in conn.execute("SELECT * FROM decisions ORDER BY rowid")
            ]
            rule_decisions = [
                self._row_to_rule_decision(row).to_dict()
                for row in conn.execute("SELECT * FROM rule_decisions ORDER BY rowid")
            ]
            rule_scope_stats = [
                self._row_to_rule_scope_stats(row).to_dict()
                for row in conn.execute("SELECT * FROM rule_scope_stats ORDER BY rowid")
            ]
            rule_exceptions = [
                self._row_to_rule_exception(row).to_dict()
                for row in conn.execute("SELECT * FROM rule_exceptions ORDER BY rowid")
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
            "rule_assignments": rule_assignments,
            "rule_match_receipts": rule_match_receipts,
            "rule_match_feedbacks": rule_match_feedbacks,
            "events": events,
            "decisions": decisions,
            "rule_decisions": rule_decisions,
            "rule_scope_stats": rule_scope_stats,
            "rule_exceptions": rule_exceptions,
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
        rule_decisions = self.list_rule_decisions()
        rule_exceptions = self.list_rule_exceptions()
        conflicts = self.list_conflicts()
        quarantine = self.list_quarantine()
        return {
            "share_group_id": self.group_id,
            "total_records": len(records),
            "active": sum(1 for r in records if r.status == SharedMemoryStatus.ACTIVE),
            "active_mandatory": sum(
                1 for r in records
                if r.status == SharedMemoryStatus.ACTIVE
                and r.injection_policy == "always"
            ),
            "shadowed": sum(1 for r in records if r.status == SharedMemoryStatus.SHADOWED),
            "conflicted": sum(1 for r in records if r.status == SharedMemoryStatus.CONFLICTED),
            "quarantined": sum(1 for r in records if r.status == SharedMemoryStatus.QUARANTINED),
            "deleted": sum(1 for r in records if r.status == SharedMemoryStatus.DELETED),
            "total_events": len(events),
            "total_decisions": len(decisions),
            "total_rule_decisions": len(rule_decisions),
            "total_rule_exceptions": len(rule_exceptions),
            "total_rule_scope_stats": len(self.list_rule_scope_stats()),
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
            self.rule_assignments_bak_path,
            self.rule_match_receipts_bak_path,
            self.rule_match_feedbacks_bak_path,
            self.rule_decisions_bak_path,
            self.rule_scope_stats_bak_path,
            self.rule_exceptions_bak_path,
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
                    "rule_match_feedbacks", "rule_match_receipts",
                    "rule_decisions", "rule_scope_stats", "rule_exceptions",
                    "rule_assignments", "records", "active_version", "versions",
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
            ("rule_assignments.jsonl", self._migrate_rule_assignments_jsonl),
            ("rule_match_receipts.jsonl", self._migrate_rule_match_receipts_jsonl),
            ("rule_match_feedbacks.jsonl", self._migrate_rule_match_feedbacks_jsonl),
            ("rule_decisions.jsonl", self._migrate_rule_decisions_jsonl),
            ("rule_scope_stats.jsonl", self._migrate_rule_scope_stats_jsonl),
            ("rule_exceptions.jsonl", self._migrate_rule_exceptions_jsonl),
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
                    record = SharedMemoryRecord.from_dict(d)
                    # A pre-audience backup has no authoritative scope.  Keep
                    # it legacy-unscoped rather than inventing writer scope.
                    domain = self._dedup_domain(
                        record.injection_policy, [],
                        writer_id=record.agent_instance_id,
                        memory_id=record.memory_id,
                    )
                    self._insert_record(
                        conn, record, dedup_domain=domain,
                    )
                except (ValueError, KeyError):
                    continue

    def _migrate_rule_assignments_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for value in self._read_jsonl(path):
                try:
                    memory_id = str(value.get("memory_id", ""))
                    exists = conn.execute(
                        "SELECT 1 FROM records WHERE memory_id=?", (memory_id,),
                    ).fetchone()
                    if not exists:
                        raise ValueError("assignment record missing")
                    item = self._normalize_assignments(memory_id, [value])[0]
                    self._insert_assignments(conn, memory_id, [item])
                except (AttributeError, IndexError, KeyError, ValueError):
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

    def _migrate_rule_match_receipts_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_rule_match_receipt(
                        conn, RuleMatchReceipt.from_dict(d),
                    )
                except (ValueError, KeyError, sqlite3.IntegrityError):
                    continue

    def _migrate_rule_match_feedbacks_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_rule_match_feedback(
                        conn, RuleMatchFeedback.from_dict(d),
                    )
                except (ValueError, KeyError, sqlite3.IntegrityError):
                    continue

    def _migrate_rule_decisions_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_rule_decision(conn, RuleDecision.from_dict(d))
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError):
                    continue

    def _migrate_rule_scope_stats_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_rule_scope_stats(conn, RuleScopeStats.from_dict(d))
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError):
                    continue

    def _migrate_rule_exceptions_jsonl(self, path: Path) -> None:
        with self._tx() as conn:
            for d in self._read_jsonl(path):
                try:
                    self._insert_rule_exception(conn, RuleException.from_dict(d))
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError):
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
                "rule_assignments": self._read_jsonl(vdir / "rule_assignments.jsonl"),
                "rule_match_receipts": self._read_jsonl(
                    vdir / "rule_match_receipts.jsonl",
                ),
                "rule_match_feedbacks": self._read_jsonl(
                    vdir / "rule_match_feedbacks.jsonl",
                ),
                "rule_decisions": self._read_jsonl(vdir / "rule_decisions.jsonl"),
                "rule_scope_stats": self._read_jsonl(vdir / "rule_scope_stats.jsonl"),
                "rule_exceptions": self._read_jsonl(vdir / "rule_exceptions.jsonl"),
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
