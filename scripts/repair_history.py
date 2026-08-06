# -*- coding: utf-8 -*-
"""One-shot repair of the AppData history database (dual-write merge + mojibake).

Repairs two classes of corrupted rows left by earlier architectural bugs:

* **Phase A - dual-write sessions**: a single physical session was written as
  two ``conversation_sessions`` rows (the hook stamped ``provider`` from argv,
  and ``_session_id()`` made provider part of the session identity, so the same
  external_id produced one claude and one cursor row).  Groups are merged when
  they share the same ``external_id`` **and** ``project_ref`` **and** differ in
  provider or agent -- legitimately distinct sessions (different project_ref)
  are never merged.  The canonical row keeps the group; the discarded rows'
  turns are folded in (deduplicated by role+content_hash, ordinals renumbered),
  FTS rows are rewritten, dependent rows (summaries/observations/evidence)
  repoint at the canonical session, and the discarded rows are soft-deleted
  (``deleted_at`` set, keeping the UNIQUE slot so a future write can revive).

* **Phase B - mojibake content**: turns whose content is UTF-8 bytes decoded
  as GBK (``涓嶈兘``/PUA) are repaired via ``repair_utf8_as_gbk``; PUA / U+FFFD
  residue is stripped (documented lossy part) and the turn's ``content`` and
  ``content_hash`` and FTS rows are rewritten.  Structurally double-corrupted
  content (embedded PUA breaks byte alignment) cannot fully recover: only the
  strictly-improved partial result (verified recoveries + PUA stripped) is
  persisted -- recorded in the manifest as ``kind="partial"`` with the stripped
  count -- and turns where nothing safe can be changed are left untouched and
  reported.  The history DB is backed up before any apply.

Idempotent: Phase A only looks at ``deleted_at=''`` rows (a merged group
re-appears as a single row on re-run); Phase B writes nothing on re-run for
already-fixed turns.  Fully-repaired turns are clean afterwards; partially
repaired turns (``kind="partial"``) keep their documented mojibake residue but
have no PUA left, so a re-run reports them under "left untouched" without
writing.  Dry-run by default; pass ``--apply`` to write.  A JSONL manifest is
written on apply.

Usage:
    python scripts/repair_history.py                    # dry-run
    python scripts/repair_history.py --apply            # repair
    python scripts/repair_history.py --apply --workspace <data_home>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from memoryguard.data_home import resolve_data_home  # noqa: E402
from memoryguard.encoding_guard import (  # noqa: E402
    looks_like_mojibake,
    repair_utf8_as_gbk,
    should_repair,
    strip_pua_residue,
)

# FTS is a standalone fts5 table storing its own copy of the indexed text, so a
# rebuild from the canonical tables is the safe way to fix moved/edited rows.
_FTS_DROP = "DROP TABLE IF EXISTS history_fts"
_FTS_CREATE = (
    "CREATE VIRTUAL TABLE history_fts USING fts5(session_id UNINDEXED, "
    "turn_id UNINDEXED, result_type UNINDEXED, title, content, tokenize='unicode61')"
)
_FTS_TURNS = """INSERT INTO history_fts(session_id,turn_id,result_type,title,content)
    SELECT t.session_id,t.turn_id,'turn',s.title,t.content
    FROM conversation_turns t JOIN conversation_sessions s ON s.session_id=t.session_id"""
_FTS_SUMMARIES = """INSERT INTO history_fts(session_id,turn_id,result_type,title,content)
    SELECT ss.session_id,'','summary',s.title,ss.summary
    FROM session_summaries ss JOIN conversation_sessions s ON s.session_id=ss.session_id"""
_FTS_OBSERVATIONS = """INSERT INTO history_fts(session_id,turn_id,result_type,title,content)
    SELECT o.session_id,COALESCE(o.turn_id,''),'observation',s.title,o.summary
    FROM observations o JOIN conversation_sessions s ON s.session_id=o.session_id"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pua_count(text: str) -> int:
    """PUA / U+FFFD codepoints: the documented lossy, unrecoverable part."""
    return sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF or ch == "�")


def _find_merge_groups(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(external_id, project_ref) groups with >1 non-deleted session.

    Merge eligibility: same external_id AND same project_ref AND (distinct
    provider OR distinct agent).  Different-project sessions (e.g. the current
    session under two project dirs) fall into separate groups and are left.
    """
    rows = conn.execute(
        """SELECT external_id, project_ref
             FROM conversation_sessions
            WHERE deleted_at = ''
         GROUP BY external_id, project_ref
           HAVING COUNT(*) > 1
              AND (COUNT(DISTINCT provider) > 1
                   OR COUNT(DISTINCT agent_instance_id) > 1)"""
    ).fetchall()
    return [(r["external_id"], r["project_ref"]) for r in rows]


def _canonical_session(conn: sqlite3.Connection, external_id: str, project_ref: str) -> str:
    """Pick the canonical session id for a merge group.

    Priority: non-claude provider (the bug labelled cursor sessions as claude,
    so the genuinely non-claude row wins) -> earliest imported_at -> non-empty
    project_ref -> smallest session_id (deterministic tiebreak).
    """
    rows = conn.execute(
        """SELECT session_id, provider, agent_instance_id, project_ref, imported_at
             FROM conversation_sessions
            WHERE external_id = ? AND project_ref = ? AND deleted_at = ''""",
        (external_id, project_ref),
    ).fetchall()

    def key(row):
        return (
            row["provider"] == "claude",      # False (non-claude) sorts first
            row["imported_at"] or "",
            row["project_ref"] == "",          # False (non-empty) sorts first
            row["session_id"],
        )

    return sorted(rows, key=key)[0]["session_id"]


def _phase_a(conn: sqlite3.Connection, dry_run: bool) -> dict:
    changes = {"groups": [], "merged_turns": 0, "deduped_turns": 0,
               "soft_deleted": [], "fts_rebuild": False}
    groups = _find_merge_groups(conn)
    for external_id, project_ref in groups:
        sessions = conn.execute(
            """SELECT session_id, provider, agent_instance_id
                 FROM conversation_sessions
                WHERE external_id = ? AND project_ref = ? AND deleted_at = ''""",
            (external_id, project_ref),
        ).fetchall()
        canonical = _canonical_session(conn, external_id, project_ref)
        discarded = [s["session_id"] for s in sessions if s["session_id"] != canonical]

        # Gather every turn of the group, dedup by role+content_hash.
        turns = conn.execute(
            """SELECT turn_id, ordinal, role, content, created_at, content_hash
                 FROM conversation_turns
                WHERE session_id IN (%s)
             ORDER BY created_at, ordinal""" % ",".join("?" * len(sessions)),
            [s["session_id"] for s in sessions],
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        group_merged = group_dedup = 0
        deduped_ids: list[str] = []
        surviving_ids: list[str] = []
        for t in turns:
            key = (t["role"], t["content_hash"])
            if key in seen:
                # Exact duplicate of an already-kept turn: must not survive.
                group_dedup += 1
                deduped_ids.append(t["turn_id"])
                continue
            seen.add(key)
            surviving_ids.append(t["turn_id"])
            group_merged += 1

        if not dry_run:
            # 1) Drop the exact duplicates first (some may live in the
            #    canonical session and would otherwise collide on renumber).
            if deduped_ids:
                conn.executemany(
                    "DELETE FROM conversation_turns WHERE turn_id = ?",
                    [(tid,) for tid in deduped_ids],
                )
            # 2) Temp-bump every surviving turn's ordinal so the renumber
            #    below can never collide on UNIQUE(session_id, ordinal).
            for sid in [s["session_id"] for s in sessions]:
                conn.execute(
                    "UPDATE conversation_turns SET ordinal = ordinal + 1000000 "
                    "WHERE session_id = ? AND turn_id IN (%s)"
                    % ",".join("?" * len(surviving_ids)),
                    (sid, *surviving_ids),
                )
            # 3) Re-point survivors to the canonical session, renumber 1..N.
            new_ordinal = 1
            for tid in surviving_ids:
                conn.execute(
                    "UPDATE conversation_turns SET session_id = ?, ordinal = ? WHERE turn_id = ?",
                    (canonical, new_ordinal, tid),
                )
                new_ordinal += 1
            # Repoint dependent rows at the canonical session.
            for table in ("observations", "evidence_links"):
                conn.execute(
                    f"UPDATE {table} SET session_id = ? WHERE session_id IN ({','.join('?' * len(discarded))})",
                    (canonical, *discarded),
                )
            # Summaries: adopt the newest (canonical primary key must stay unique).
            for sid in discarded:
                canon_sum = conn.execute(
                    "SELECT updated_at FROM session_summaries WHERE session_id = ?", (canonical,)
                ).fetchone()
                disc_sum = conn.execute(
                    "SELECT updated_at FROM session_summaries WHERE session_id = ?", (sid,)
                ).fetchone()
                if disc_sum and (not canon_sum or disc_sum["updated_at"] > canon_sum["updated_at"]):
                    if canon_sum:
                        conn.execute("DELETE FROM session_summaries WHERE session_id = ?", (canonical,))
                    conn.execute(
                        "UPDATE session_summaries SET session_id = ? WHERE session_id = ?", (canonical, sid)
                    )
                elif disc_sum and canon_sum:
                    conn.execute("DELETE FROM session_summaries WHERE session_id = ?", (sid,))
            # Soft-delete discarded sessions (keeps UNIQUE slot for revival).
            conn.execute(
                f"UPDATE conversation_sessions SET deleted_at = ? WHERE session_id IN ({','.join('?' * len(discarded))})",
                (_now(), *discarded),
            )
        changes["groups"].append({
            "external_id": external_id,
            "project_ref": project_ref,
            "canonical": canonical,
            "discarded": discarded,
            "turns_merged": group_merged,
            "turns_deduped": group_dedup,
        })
        changes["merged_turns"] += group_merged
        changes["deduped_turns"] += group_dedup
        changes["soft_deleted"].extend(discarded)
        changes["fts_rebuild"] = True
    return changes


def _phase_b(conn: sqlite3.Connection, dry_run: bool) -> dict:
    changes = {"repaired": 0, "partial": 0, "pua_stripped": 0, "skipped": [], "manifest": []}
    rows = conn.execute(
        "SELECT turn_id, session_id, content FROM conversation_turns WHERE content <> ''"
    ).fetchall()
    for t in rows:
        orig = t["content"]
        # Gate on the same protection domain as guard_persist_content
        # (density-gated should_repair) plus the hard PUA/FFFD signal.  The
        # looser looks_like_mojibake would flag clean turns that merely *quote*
        # mojibake example strings; such turns must be left untouched, not
        # reported as unrecoverable corruption.
        if not (should_repair(orig) or _pua_count(orig) > 0):
            continue
        pua_in = _pua_count(orig)
        repaired = repair_utf8_as_gbk(orig)
        cleaned, _outer = strip_pua_residue(repaired)
        if cleaned == orig:
            # Nothing safe to change: no verified recovery, no PUA to strip.
            # Left byte-identical (bytes preserved for a future better
            # recovery) and reported so the operator can audit it.
            changes["skipped"].append({
                "turn_id": t["turn_id"], "session_id": t["session_id"],
            })
            continue
        pua_stripped = pua_in - _pua_count(cleaned)
        if looks_like_mojibake(cleaned):
            # Structurally double-corrupted (embedded PUA breaks byte
            # alignment): full clean recovery is impossible.  Every change
            # repair_utf8_as_gbk makes is a *verified* recovery or a PUA
            # removal (strict improvement), so persist the partial result --
            # documented in the manifest as kind="partial" with the stripped
            # count -- rather than discard the recovered fragments.
            changes["manifest"].append({
                "turn_id": t["turn_id"], "session_id": t["session_id"],
                "pua_stripped": pua_stripped, "kind": "partial", "still_mojibake": True,
            })
            if not dry_run:
                conn.execute(
                    "UPDATE conversation_turns SET content = ?, content_hash = ? WHERE turn_id = ?",
                    (cleaned, _content_hash(cleaned), t["turn_id"]),
                )
                changes["fts_rebuild"] = True
            changes["partial"] += 1
            changes["pua_stripped"] += pua_stripped
            continue
        entry = {"turn_id": t["turn_id"], "session_id": t["session_id"],
                 "pua_stripped": pua_stripped}
        changes["manifest"].append(entry)
        if not dry_run:
            conn.execute(
                "UPDATE conversation_turns SET content = ?, content_hash = ? WHERE turn_id = ?",
                (cleaned, _content_hash(cleaned), t["turn_id"]),
            )
            changes["fts_rebuild"] = True
        changes["repaired"] += 1
        changes["pua_stripped"] += pua_stripped
    return changes


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute(_FTS_DROP)
    conn.execute(_FTS_CREATE)
    conn.execute(_FTS_TURNS)
    conn.execute(_FTS_SUMMARIES)
    conn.execute(_FTS_OBSERVATIONS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair AppData history: dual-write merge + mojibake.")
    parser.add_argument("--workspace", default=None,
                        help="data home holding .memoryguard/history/history.sqlite (default resolve_data_home())")
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    parser.add_argument("--manifest", default=None, help="JSONL manifest path (apply only)")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve() if args.workspace else resolve_data_home()
    db_path = workspace / ".memoryguard" / "history" / "history.sqlite"
    if not db_path.exists():
        print(f"ABORT: history database not found: {db_path}")
        return 2
    print(f"history db: {db_path}")

    if args.apply:
        import shutil
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        backup = db_path.with_name(f"history.sqlite.bak-{stamp}")
        shutil.copy2(db_path, backup)
        print(f"backup: {backup}")

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row

    def _apply_phase(fn) -> dict:
        if not args.apply:
            return fn(conn, dry_run=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn, dry_run=False)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    # ---- Phase A: dual-write merge -----------------------------------------
    a = _apply_phase(_phase_a)
    print(f"\nPhase A (merge): groups={len(a['groups'])} turns_merged={a['merged_turns']} "
          f"turns_deduped={a['deduped_turns']} soft_deleted={len(a['soft_deleted'])}")
    for g in a["groups"]:
        print(f"  {g['external_id'][:8]} proj={g['project_ref'] or '(empty)'!r} -> {g['canonical'][:14]} "
              f"merged={g['turns_merged']} deduped={g['turns_deduped']} discarded={[s[:10] for s in g['discarded']]}")

    # ---- Phase B: mojibake repair ------------------------------------------
    b = _apply_phase(_phase_b)
    print(f"\nPhase B (mojibake): repaired={b['repaired']} partial={b['partial']} "
          f"pua_stripped={b['pua_stripped']} skipped={len(b['skipped'])}")
    if b["skipped"]:
        print("  already-fixed or unrecoverable (left untouched):", [s["turn_id"] for s in b["skipped"]])

    # ---- FTS rebuild if anything changed -----------------------------------
    if args.apply and (a["fts_rebuild"] or b.get("fts_rebuild")):
        try:
            conn.execute("BEGIN IMMEDIATE")
            _rebuild_fts(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print("\nhistory_fts rebuilt from canonical tables")

    if not args.apply:
        print("\ndry-run complete. Re-run with --apply to write.")
        return 0

    if args.manifest or b["manifest"]:
        manifest_path = Path(args.manifest) if args.manifest else (
            workspace / ".memoryguard" / "history" / "repair-history.jsonl")
        with manifest_path.open("w", encoding="utf-8") as fh:
            for entry in b["manifest"]:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"manifest: {manifest_path} ({len(b['manifest'])} turns)")

    # ---- post-conditions -----------------------------------------------------
    dup = _find_merge_groups(conn)
    pua = 0
    for (content,) in conn.execute(
        "SELECT content FROM conversation_turns WHERE content <> ''"
    ).fetchall():
        _cleaned, lost = strip_pua_residue(content)
        pua += lost
    conn.close()
    print(f"\npost-check: merge_groups={len(dup)} pua_codepoints_remaining={pua}")
    if b["partial"]:
        print(f"note: {b['partial']} turns partially repaired (still_mojibake, PUA stripped); "
              "see manifest for the documented lossy part")
    if dup or pua:
        print("WARNING: residual dual-write groups or PUA content remain")
        return 1
    print("repair verified OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
