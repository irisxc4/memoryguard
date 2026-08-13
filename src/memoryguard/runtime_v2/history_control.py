"""V2 local-history discovery and backfill onto the Content Plane.

Discovery never opens transcript bodies. Backfill parses only documented JSONL
shapes, user/assistant visible text, and synchronizes them through
ConversationSync. No ConversationHistoryStore, AgentBindingStore, MemoryIR,
ManagedStore or SharedMemoryStore is imported here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping

from ..content.conversation_sync import ConversationEvent, ConversationSync
from ..content.store import ContentStore, stable_id
from ..storage.database import open_database
from ..storage.layout import WorkspaceV2Layout
from .group_native import GroupControlService
from .task_coordinator import TaskExecution

MAX_BATCH_FILES = 25
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_SOURCE_READ_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_CHARS = 100_000


class HistoryControlError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "history_control_failed")
        super().__init__(self.code)


def _digest(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(item) for item in parts).encode("utf-8")).hexdigest()


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()).expanduser().resolve()


def _unsafe(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400)


def _iter_files(root: Path, pattern: str) -> Iterable[Path]:
    if not root.is_dir() or _unsafe(root):
        return ()
    result: list[Path] = []
    try:
        for path in root.rglob(pattern):
            if _unsafe(path) or not path.is_file():
                continue
            result.append(path.resolve())
    except OSError:
        return ()
    return result


@dataclass(frozen=True)
class HistorySource:
    provider: str
    path: Path
    supported: bool = True
    reason: str = "stable_jsonl"

    @property
    def source_id(self) -> str:
        return "history-source-" + _digest(self.provider, str(self.path).casefold())[:32]

    @property
    def byte_count(self) -> int:
        try:
            return int(self.path.stat().st_size)
        except OSError:
            return 0

    @property
    def revision(self) -> str:
        try:
            info = self.path.stat()
        except OSError as exc:
            raise HistoryControlError("history_source_unavailable") from exc
        return f"{info.st_size}:{info.st_mtime_ns}"


def discover_sources(home: str | Path | None = None) -> tuple[HistorySource, ...]:
    root = Path(home).expanduser().resolve() if home is not None else _home()
    sources: list[HistorySource] = []
    for folder in (root / ".codex" / "sessions", root / ".codex" / "archived_sessions"):
        sources.extend(HistorySource("codex", p) for p in _iter_files(folder, "*.jsonl"))
    claude = root / ".claude" / "projects"
    for path in _iter_files(claude, "*.jsonl"):
        if path.name.casefold() == "history.jsonl":
            sources.append(HistorySource("claude", path, False, "prompt_index_not_full_conversation"))
        else:
            sources.append(HistorySource("claude", path))
    cursor = root / ".cursor" / "projects"
    for path in _iter_files(cursor, "*.jsonl"):
        if "agent-transcripts" in {part.casefold() for part in path.parts}:
            sources.append(HistorySource("cursor", path))
    for name in ("conversation-search.db", "state.vscdb"):
        for path in _iter_files(root / ".cursor", name):
            sources.append(HistorySource("cursor", path, False, "proprietary_database_manual_export_required"))
    trae = root / "AppData" / "Roaming" / "TRAE SOLO CN"
    for path in _iter_files(trae, "*.vscdb"):
        sources.append(HistorySource("trae", path, False, "proprietary_database_manual_export_required"))
    return tuple(sorted(sources, key=lambda item: (item.provider, str(item.path).casefold())))


def _visible_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                kind = str(item.get("type") or item.get("kind") or "").casefold()
                if kind in {"thinking", "reasoning", "tool_use", "tool_result", "function_call", "function_call_output"}:
                    continue
                candidate = item.get("text") or item.get("content") or item.get("value")
                if isinstance(candidate, str):
                    parts.append(candidate)
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    if isinstance(value, Mapping):
        return _visible_text(value.get("content") or value.get("text"))
    return ""


def _project_ref(*values: Any) -> str:
    for value in values:
        if not isinstance(value, Mapping):
            continue
        for key in ("project_ref", "projectRef", "cwd", "workspace", "workspace_path"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[:1024]
        metadata = value.get("metadata")
        if isinstance(metadata, Mapping):
            found = _project_ref(metadata)
            if found:
                return found
    return ""


def _visible_message(raw: Mapping[str, Any]) -> dict[str, str] | None:
    message = raw.get("message") if isinstance(raw.get("message"), Mapping) else raw
    role = str(message.get("role") or raw.get("role") or "").casefold()
    if role not in {"user", "assistant"}:
        return None
    payload_type = str(message.get("type") or raw.get("type") or "").casefold()
    if payload_type in {"reasoning", "function_call", "function_call_output", "tool_call", "tool_result"}:
        return None
    text = _visible_text(message.get("content") or message.get("text") or raw.get("content"))
    if not text:
        return None
    return {
        "role": role,
        "content": text[:MAX_MESSAGE_CHARS],
        "event_id": str(message.get("event_id") or message.get("event_key") or message.get("id") or raw.get("event_id") or raw.get("id") or "")[:512],
        "created_at": str(message.get("timestamp") or raw.get("timestamp") or raw.get("created_at") or "")[:80],
    }


@dataclass(frozen=True)
class ParsedHistory:
    external_id: str
    title: str
    project_ref: str
    messages: tuple[Mapping[str, str], ...]
    read_bytes: int
    truncated: bool


def parse_source(source: HistorySource) -> ParsedHistory:
    external_id = source.path.stem
    title = ""
    project = ""
    messages: list[dict[str, str]] = []
    read_bytes = 0
    truncated = False
    try:
        with source.path.open("rb") as handle:
            for raw_line in handle:
                if read_bytes + len(raw_line) > MAX_SOURCE_READ_BYTES:
                    truncated = True
                    break
                read_bytes += len(raw_line)
                try:
                    raw = json.loads(raw_line.decode("utf-8", errors="replace"))
                except (ValueError, TypeError):
                    continue
                if not isinstance(raw, Mapping):
                    continue
                payload = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else raw
                candidate: dict[str, str] | None
                if source.provider == "codex":
                    meta = payload if str(raw.get("type") or "") == "session_meta" else {}
                    external_id = str(meta.get("id") or meta.get("session_id") or external_id)[:1024]
                    title = title or str(meta.get("title") or "")[:500]
                    project = project or _project_ref(meta, payload, raw)
                    if str(raw.get("type") or "") != "response_item":
                        continue
                    candidate = _visible_message(payload)
                else:
                    external_id = str(raw.get("sessionId") or raw.get("session_id") or external_id)[:1024]
                    title = title or str(raw.get("title") or payload.get("title") or "")[:500]
                    project = project or _project_ref(raw, payload)
                    candidate = _visible_message(payload)
                if candidate is not None:
                    messages.append(candidate)
    except OSError as exc:
        raise HistoryControlError("history_source_read_failed") from exc
    if not external_id:
        raise HistoryControlError("history_source_identity_missing")
    return ParsedHistory(external_id, title, project, tuple(messages), read_bytes, truncated)


class HistoryControlService:
    def __init__(self, workspace: str | Path, *, home: str | Path | None = None) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.home = Path(home).expanduser().resolve() if home is not None else None

    def _agent_map(self) -> dict[str, tuple[str, str]]:
        try:
            from ..agent_locator import AgentLocator
            instances, _ = AgentLocator(self.workspace).detect_instances()
        except Exception:
            instances = []
        groups = GroupControlService(self.workspace, write=False)
        result: dict[str, tuple[str, str]] = {}
        aliases = {"claude-code": "claude", "claude_code": "claude"}
        for instance in instances:
            provider = aliases.get(str(instance.product or "").strip().casefold(), str(instance.product or "").strip().casefold())
            agent = str(instance.instance_id or "").strip()
            if not provider or not agent:
                continue
            binding = groups.active_binding_for_agent(agent)
            if binding is None:
                continue
            result.setdefault(provider, (agent, str(binding.get("share_group_id") or "")))
        return result

    def _source_states(self) -> dict[str, dict[str, Any]]:
        """Read durable per-source import progress without opening transcript bodies."""

        path = WorkspaceV2Layout(self.workspace).content_db
        if not path.is_file():
            return {}
        result: dict[str, dict[str, Any]] = {}
        try:
            with open_database(path, readonly=True) as conn:
                rows = conn.execute(
                    "SELECT s.source_id,s.state,s.last_error_code,m.source_revision,m.active "
                    "FROM source_sync_state s LEFT JOIN source_manifest_items m "
                    "ON m.source_id=s.source_id ORDER BY s.source_id"
                ).fetchall()
        except Exception:
            return {}
        for row in rows:
            source_id = str(row[0] or "")
            if not source_id:
                continue
            item = result.setdefault(source_id, {
                "state": str(row[1] or ""),
                "last_error_code": str(row[2] or ""),
                "revisions": set(),
            })
            if bool(row[4]) and str(row[3] or ""):
                item["revisions"].add(str(row[3]))
        return result

    @staticmethod
    def _durably_current(source: HistorySource, state: Mapping[str, Any] | None) -> bool:
        if not state or str(state.get("state") or "") not in {"complete", "partial"}:
            return False
        revisions = set(state.get("revisions") or ())
        # A completed zero-visible-message source has no manifest items. Local
        # session files are immutable enough to treat that terminal state as
        # current; non-empty imports additionally prove the exact file revision.
        return not revisions or source.revision in revisions

    def _progress_token(self, states: Mapping[str, Mapping[str, Any]] | None = None) -> str:
        current = states if states is not None else self._source_states()
        parts = []
        for source in discover_sources(self.home):
            state = current.get(source.source_id, {}) if isinstance(current, Mapping) else {}
            parts.append(
                f"{source.source_id}:{source.revision}:{state.get('state','')}:"
                f"{','.join(sorted(state.get('revisions') or ()))}"
            )
        return _digest("history-progress", *parts)

    def discover(self) -> dict[str, Any]:
        mapping = self._agent_map()
        states = self._source_states()
        sources = []
        providers: dict[str, dict[str, int]] = {}
        for source in discover_sources(self.home):
            bound = mapping.get(source.provider)
            durable = states.get(source.source_id)
            if not source.supported:
                status = "unsupported"
            elif not bound:
                status = "pending_binding"
            elif self._durably_current(source, durable):
                status = str(durable.get("state") or "complete")
            elif durable and str(durable.get("state") or "") == "failed":
                status = "error"
            else:
                status = "importable"
            item = {
                "source_id": source.source_id,
                "provider": source.provider,
                "file_count": 1,
                "byte_count": source.byte_count,
                "supported": source.supported,
                "status": status,
                "support_reason": source.reason,
                "matched_agent_id": bound[0] if bound else "",
            }
            sources.append(item)
            bucket = providers.setdefault(source.provider, {"files": 0, "bytes": 0, "supported_files": 0})
            bucket["files"] += 1
            bucket["bytes"] += source.byte_count
            bucket["supported_files"] += int(source.supported)
        return {
            "ok": True,
            "status": "succeeded",
            "sources": sources,
            "providers": providers,
            "progress_token": self._progress_token(states),
        }

    def backfill(self, *, execution: TaskExecution | None = None, continuation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        # Durable source_sync_state is authoritative.  The continuation is a
        # replay/idempotency hint only; restarting the GUI must still resume at
        # the first not-yet-terminal source.
        del continuation
        mapping = self._agent_map()
        all_sources = [item for item in discover_sources(self.home) if item.supported]
        states = self._source_states()
        bound_sources = [item for item in all_sources if item.provider in mapping]
        current_sources = [item for item in bound_sources if self._durably_current(item, states.get(item.source_id))]
        fresh_sources = [
            item for item in bound_sources
            if not self._durably_current(item, states.get(item.source_id))
            and str((states.get(item.source_id) or {}).get("state") or "") != "failed"
        ]
        retry_sources = [
            item for item in bound_sources
            if str((states.get(item.source_id) or {}).get("state") or "") == "failed"
        ]
        # Never let historical failures starve unseen Codex/Cursor/Claude
        # sources. Fill spare batch capacity with retries only after fresh work.
        ready = [*fresh_sources, *retry_sources]
        pending_binding = sorted({item.provider for item in all_sources if item.provider not in mapping})
        processed_files = imported_sessions = imported_turns = changed_turns = partial = errors = 0
        skipped = len(current_sources)
        processed_bytes = 0
        content: ContentStore | None = None

        def begin_source_sync(source: HistorySource, agent_id: str) -> tuple[ConversationSync, str, Any]:
            nonlocal content
            if content is None:
                content = ContentStore(
                    self.workspace,
                    workspace_id=str(self.workspace),
                    trust_domain="conversation-history",
                    sensitivity="normal",
                    retention_authority="workspace",
                )
            sync = ConversationSync(content)
            owner = f"history-backfill:{agent_id}"
            run = sync.begin_sync(
                source.source_id,
                owner_id=owner,
                provider=source.provider,
                source_type="local_history",
                external_root_key="history:" + _digest(source.provider, str(source.path).casefold()),
                workspace_id=str(self.workspace),
            )
            return sync, owner, run

        for index, source in enumerate(ready[:MAX_BATCH_FILES]):
            if execution is not None:
                execution.check_cancelled()
                execution.progress(min(80, int(5 + 70 * index / max(1, min(len(ready), MAX_BATCH_FILES)))), "history_backfill", item_count=processed_files)
            if processed_bytes and processed_bytes + min(source.byte_count, MAX_SOURCE_READ_BYTES) > MAX_BATCH_BYTES:
                break
            agent_id, group_id = mapping[source.provider]
            sync, owner, run = begin_source_sync(source, agent_id)
            try:
                parsed = parse_source(source)
            except HistoryControlError as exc:
                errors += 1
                processed_files += 1
                try:
                    sync.finish_sync(
                        run,
                        status="failed",
                        continuation_cursor="",
                        owner_id=owner,
                        error_code=str(exc.code or "history_parse_failed"),
                    )
                except Exception:
                    pass
                continue
            processed_files += 1
            processed_bytes += parsed.read_bytes
            partial += int(parsed.truncated)
            if not parsed.messages:
                skipped += 1
                try:
                    sync.finish_sync(
                        run,
                        status="complete",
                        continuation_cursor="",
                        owner_id=owner,
                        error_code="",
                    )
                except Exception:
                    errors += 1
                continue
            source_id = source.source_id
            events = [
                ConversationEvent(
                    external_object_key=parsed.external_id,
                    content=str(message["content"]),
                    role=str(message["role"]),
                    ordinal=message_index,
                    event_id=(str(message.get("event_id") or "") or stable_id("history-event", source_id, parsed.external_id, message_index)),
                    source_revision=source.revision,
                    title=parsed.title,
                    provider=source.provider,
                    workspace_id=str(self.workspace),
                    agent_instance_id=agent_id,
                    project_ref=parsed.project_ref,
                    share_group_id=group_id,
                    sensitivity="normal",
                    policy_class="private",
                    metadata={"source_provider": source.provider, "bounded_prefix": bool(parsed.truncated)},
                    locator={"message_index": message_index},
                )
                for message_index, message in enumerate(parsed.messages)
            ]
            cursor = ""
            try:
                for start in range(0, len(events), 250):
                    batch = events[start:start + 250]
                    result = sync.stage_batch(
                        run, batch,
                        max_turns=250,
                        max_chars=1_000_000,
                        continuation_cursor=cursor,
                        coverage_status=("partial" if parsed.truncated else "covered"),
                        owner_id=owner,
                    )
                    cursor = result.continuation_cursor
                    imported_turns += int(result.applied)
                    changed_turns += int(result.changed)
                finish = sync.finish_sync(
                    run,
                    status=("partial" if parsed.truncated else "complete"),
                    continuation_cursor=cursor,
                    owner_id=owner,
                    error_code=("bounded_prefix_imported" if parsed.truncated else ""),
                )
                if finish.state in {"complete", "partial"}:
                    imported_sessions += 1
            except Exception:
                errors += 1
                try:
                    sync.finish_sync(run, status="failed", error_code="history_backfill_failed", owner_id=owner)
                except Exception:
                    pass
        fresh_processed = min(processed_files, len(fresh_sources))
        remaining_fresh = max(0, len(fresh_sources) - fresh_processed)
        refreshed_states = self._source_states()
        remaining_failed = sum(
            1 for source in bound_sources
            if str((refreshed_states.get(source.source_id) or {}).get("state") or "") == "failed"
        )
        resolved_retries = sum(
            1 for source in retry_sources
            if str((refreshed_states.get(source.source_id) or {}).get("state") or "") != "failed"
        )
        # Continue through old failures only while the previous batch actually
        # repaired at least one. This drains historical migration failures after
        # a schema fix without turning a permanently bad source into a busy loop.
        should_continue = remaining_fresh > 0 or (remaining_failed > 0 and resolved_retries > 0)
        remaining = remaining_fresh + remaining_failed + len(pending_binding)
        continuation_out = (
            {"progress_token": self._progress_token(refreshed_states)}
            if should_continue else None
        )
        status = (
            "pending_binding" if pending_binding
            else "importing" if should_continue
            else "failed" if errors or remaining_failed
            else "succeeded"
        )
        return {
            "status": status,
            "imported": imported_sessions,
            "turn_count": imported_turns,
            "changed_turn_count": changed_turns,
            "processed_files": processed_files,
            "processed_bytes": processed_bytes,
            "partial": partial,
            "skipped": skipped,
            "errors": errors,
            "pending_binding": pending_binding,
            "remaining_files": remaining,
            "remaining_work_files": remaining_fresh + remaining_failed,
            "remaining_fresh_files": remaining_fresh,
            "retryable_failed_files": remaining_failed,
            "resolved_retry_files": resolved_retries,
            "continuation": continuation_out,
            "storage": "v2_content_plane",
            "memory_record_count": 0,
        }


__all__ = [
    "HistoryControlError", "HistoryControlService", "HistorySource", "ParsedHistory",
    "discover_sources", "parse_source",
]
