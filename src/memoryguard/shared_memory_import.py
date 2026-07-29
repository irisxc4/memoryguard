"""从各 Agent 已授权原生记忆根导入 SharedMemoryStore。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .content_parsers import parse_file
from .governance_engine import GovernanceEngine
from .governance_scope import GovernanceScope, resolve_scoped_roots
from .schema_v3 import MemoryEvent, stable_hash, _now_iso
from .source_registry import SourceRegistry


NATIVE_CATEGORIES = frozenset({"native_memory", "project_memory"})
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".jsonl"}


def _iter_root_files(root_path: Path) -> list[Path]:
    if not root_path.exists():
        return []
    if root_path.is_file():
        return [root_path]
    out: list[Path] = []
    for p in sorted(root_path.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() in TEXT_SUFFIXES:
            out.append(p)
    return out


def import_native_memories_to_group(
    workspace: str | Path,
    share_group_id: str,
    agent_instance_ids: list[str],
) -> dict[str, Any]:
    """将各 Agent 已启用原生/项目记忆文件导入共享组（经治理引擎）。"""
    workspace = Path(workspace).resolve()
    reg = SourceRegistry(workspace)
    all_roots = reg.list_all_sources()
    engine = GovernanceEngine(workspace, share_group_id)

    imported_files: list[str] = []
    written_ids: list[str] = []
    skipped = 0

    for agent_id in agent_instance_ids:
        gscope = GovernanceScope(mode="agent", agent_instance_id=agent_id)
        roots, err = resolve_scoped_roots(all_roots, gscope, enabled_only=True)
        if err:
            continue
        for root in roots:
            if root.source_category not in NATIVE_CATEGORIES:
                continue
            root_path = Path(root.path)
            for file_path in _iter_root_files(root_path):
                rel = (
                    file_path.name
                    if root_path.is_file()
                    else str(file_path.relative_to(root_path)).replace("\\", "/")
                )
                segments = parse_file(
                    file_path,
                    surface_hint=root.surface_id or "",
                )
                for seg in segments:
                    if seg.signal_level == "meta" or not (seg.body or "").strip():
                        skipped += 1
                        continue
                    body = seg.body.strip()
                    event = MemoryEvent(
                        event_id=stable_hash(
                            "native_import", share_group_id, agent_id,
                            root.root_id, rel, seg.locator, body[:120], _now_iso(),
                        ),
                        agent_instance_id=agent_id,
                        share_group_id=share_group_id,
                        raw_content=body,
                        metadata={
                            "source_root_id": root.root_id,
                            "relative_path": rel,
                            "extraction_origin": "native_memory_import",
                            "source_category": root.source_category,
                            "locator": seg.locator,
                            "title": seg.title,
                        },
                        auto_actions=[],
                        created_at=_now_iso(),
                    )
                    result = engine.auto_write(
                        event,
                        idempotency_key=stable_hash(
                            "native_import",
                            share_group_id,
                            agent_id,
                            root.root_id,
                            rel,
                            seg.locator,
                            body,
                        ),
                    )
                    written_ids.append(result["memory_id"])
                imported_files.append(str(file_path))

    decision = engine.record_governance_decision(
        actor="user",
        action="import_native_memories",
        target_ids=written_ids[:200],
        reason=f"imported {len(imported_files)} files into {share_group_id}",
    )
    version_id = decision["version_id"]

    return {
        "ok": True,
        "share_group_id": share_group_id,
        "agent_count": len(agent_instance_ids),
        "files_imported": len(imported_files),
        "records_written": len(written_ids),
        "segments_skipped": skipped,
        "version_id": version_id,
        "imported_files": imported_files[:80],
    }
