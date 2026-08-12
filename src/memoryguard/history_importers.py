"""Safe, incremental import of pre-MemoryGuard local conversation logs.

Raw host transcripts remain in :class:`ConversationHistoryStore`; they never
enter SharedMemoryStore, MemoryIR, or bootstrap.  Only stable, documented-ish
JSONL shapes are parsed.  Proprietary databases are inventory-only so a host
upgrade cannot silently turn binary cache contents into user history.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .adapters import ImportedConversation
from .content.conversation_sync import ConversationEvent, ConversationSync
from .content.store import ContentStore, stable_id
from .runtime_v2.group_native import GroupControlService
from .runtime_v2.history_store import V2HistoryScope


SUPPORTED_PROVIDERS = ("codex", "claude", "cursor")
MAX_BATCH_FILES = 25
MAX_BATCH_BYTES = 64 * 1024 * 1024
# Large historical sessions are not rejected by size.  We preserve a bounded
# prefix/index, then mark the source partial for transparent later review.
MAX_SOURCE_READ_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_CHARS = 100_000


@dataclass(frozen=True)
class HistoryImportSource:
    provider: str
    path: Path
    status: str = "ready"  # ready|unsupported|index_only|oversized
    supported: bool = True
    reason: str = ""

    def fingerprint(self) -> str:
        stat = self.path.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    def to_dict(self) -> dict[str, Any]:
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "provider": self.provider,
            "path": str(self.path),
            "file_count": 1,
            "byte_count": size,
            "supported": self.supported,
            "status": "supported" if self.supported else "unsupported",
            "support_reason": self.reason or ("stable_jsonl" if self.supported else "unsupported_format"),
            "matched_agent_id": "",
            # Compatibility aliases for pre-UI callers.
            "reason": self.reason,
            "bytes": size,
        }


@dataclass(frozen=True)
class ParsedHistoryFile:
    conversation: ImportedConversation | None
    read_bytes: int
    truncated: bool = False


def _default_home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()).expanduser().resolve()


def _iter_files(root: Path, pattern: str) -> Iterable[Path]:
    if not root.is_dir():
        return []
    try:
        return (path for path in root.rglob(pattern) if path.is_file())
    except OSError:
        return []


def discover_local_history_sources(
    home: str | Path | None = None,
    *,
    workspace: str | Path | None = None,
    agent_ids_by_provider: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Inventory local historical sources without opening their contents.

    The result is intentionally UI-ready: each source has a stable provider,
    explicit support state and byte count.  Unsupported databases are shown,
    never parsed.
    """
    root = Path(home).expanduser().resolve() if home is not None else _default_home()
    sources: list[HistoryImportSource] = []
    for folder in (root / ".codex" / "sessions", root / ".codex" / "archived_sessions"):
        sources.extend(HistoryImportSource("codex", path) for path in _iter_files(folder, "*.jsonl"))
    claude_root = root / ".claude" / "projects"
    for path in _iter_files(claude_root, "*.jsonl"):
        if path.name.lower() == "history.jsonl":
            sources.append(HistoryImportSource("claude", path, "index_only", False,
                                               "prompt_index_not_full_conversation"))
        else:
            sources.append(HistoryImportSource("claude", path))
    cursor_root = root / ".cursor" / "projects"
    for path in _iter_files(cursor_root, "*.jsonl"):
        if "agent-transcripts" in {part.lower() for part in path.parts}:
            sources.append(HistoryImportSource("cursor", path))
    for name in ("conversation-search.db", "state.vscdb"):
        for path in _iter_files(root / ".cursor", name):
            sources.append(HistoryImportSource("cursor", path, "unsupported", False,
                                               "proprietary_database_manual_export_required"))
    trae_root = root / "AppData" / "Roaming" / "TRAE SOLO CN"
    if trae_root.is_dir():
        for path in _iter_files(trae_root, "*.vscdb"):
            sources.append(HistoryImportSource("trae", path, "unsupported", False,
                                               "proprietary_database_manual_export_required"))
    sources.sort(key=lambda item: (item.provider, str(item.path).lower()))
    result = [source.to_dict() for source in sources]
    state = _load_state(Path(workspace).expanduser().resolve()) if workspace is not None else {}
    agent_ids_by_provider = agent_ids_by_provider or {}
    workspace_path = Path(workspace).expanduser().resolve() if workspace is not None else None
    detected_providers = _detected_provider_by_agent(workspace_path) if workspace_path else {}
    by_provider: dict[str, dict[str, int]] = {}
    for item in result:
        if not item["supported"]:
            continue
        source_key = f"{item['provider']}:{item['path']}"
        scope = _active_scope(workspace_path, item["provider"], agent_ids_by_provider, detected_providers) if workspace_path else None
        if scope is None:
            item["status"] = "pending_binding" if workspace_path else "supported"
        else:
            try:
                state_status = _state_status(
                    state.get(source_key, ""), _path_fingerprint(Path(item["path"])),
                )
            except OSError:
                item["status"] = "error"
                item["support_reason"] = "source_disappeared_during_inventory"
                continue
            item["status"] = state_status
            if state_status == "partial":
                item["support_reason"] = "bounded_prefix_imported"
            item["matched_agent_id"] = scope.agent_instance_id
    for item in result:
        bucket = by_provider.setdefault(item["provider"], {"files": 0, "bytes": 0, "supported_files": 0})
        bucket["files"] += 1
        bucket["bytes"] += int(item["bytes"])
        bucket["supported_files"] += int(bool(item["supported"]))
    return {"home": str(root), "sources": result, "providers": by_provider}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                kind = str(item.get("type") or item.get("kind") or "").lower()
                if kind in {"thinking", "reasoning", "tool_use", "tool_result", "function_call", "function_call_output"}:
                    continue
                candidate = item.get("text") or item.get("content") or item.get("value")
                if isinstance(candidate, str):
                    parts.append(candidate)
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    if isinstance(value, dict):
        return _text(value.get("content") or value.get("text"))
    return ""


def _visible_message(raw: dict[str, Any], provider: str) -> dict[str, str] | None:
    message = raw.get("message") if isinstance(raw.get("message"), dict) else raw
    role = str(message.get("role") or raw.get("role") or "").lower()
    if role not in {"user", "assistant"}:
        return None
    # Codex records wrap user-visible messages in a response_item payload.
    payload_type = str(message.get("type") or raw.get("type") or "").lower()
    if payload_type in {"reasoning", "function_call", "function_call_output", "tool_call", "tool_result"}:
        return None
    content = _text(message.get("content") or message.get("text") or raw.get("content"))
    if not content:
        return None
    return {
        "role": role,
        "content": content[:MAX_MESSAGE_CHARS],
        "event_id": str(message.get("event_id") or message.get("event_key") or message.get("id") or raw.get("event_id") or raw.get("id") or ""),
        "created_at": str(message.get("timestamp") or raw.get("timestamp") or raw.get("created_at") or "")[:80],
        "content_type": "text",
    }


def _metadata_project_ref(*values: Any) -> str:
    """Extract only explicit host metadata; never inspect transcript text."""
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in ("project_ref", "projectRef", "cwd", "workspace", "workspace_path"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            found = _metadata_project_ref(metadata)
            if found:
                return found
    return ""


def _parse_jsonl(source: HistoryImportSource) -> ParsedHistoryFile:
    messages: list[dict[str, str]] = []
    title = ""
    external_id = source.path.stem
    project_ref = ""
    read_bytes = 0
    truncated = False
    try:
        with source.path.open("rb") as handle:
            for raw_line in handle:
                if read_bytes + len(raw_line) > MAX_SOURCE_READ_BYTES:
                    truncated = True
                    break
                read_bytes += len(raw_line)
                line = raw_line.decode("utf-8", errors="replace")
                try:
                    raw = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(raw, dict):
                    continue
                payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
                if source.provider == "codex":
                    meta = payload if str(raw.get("type") or "") == "session_meta" else {}
                    external_id = str(meta.get("id") or meta.get("session_id") or external_id)
                    title = title or str(meta.get("title") or "")
                    project_ref = project_ref or _metadata_project_ref(meta, payload, raw)
                    if str(raw.get("type") or "") != "response_item":
                        continue
                    candidate = _visible_message(payload, source.provider)
                else:
                    external_id = str(raw.get("sessionId") or raw.get("session_id") or external_id)
                    title = title or str(raw.get("title") or payload.get("title") or "")
                    project_ref = project_ref or _metadata_project_ref(raw, payload)
                    candidate = _visible_message(payload, source.provider)
                if candidate is not None:
                    messages.append(candidate)
    except OSError:
        return ParsedHistoryFile(None, read_bytes, truncated)
    # Keep the index even when a giant first JSONL event cannot be safely
    # decoded within the budget.  No synthetic body is created.
    if not external_id:
        return ParsedHistoryFile(None, read_bytes, truncated)
    if not title:
        # V2 stores do not infer a title while opening a read-only history
        # projection.  Preserve the first visible user message as the
        # provider-neutral session label when the host omitted one; transcript
        # payloads remain event content and are never written to receipts.
        first_user = next(
            (str(item.get("content") or "").strip() for item in messages
             if str(item.get("role") or "").casefold() == "user"
             and str(item.get("content") or "").strip()),
            "",
        )
        title = first_user.splitlines()[0].strip() if first_user else ""
    if truncated:
        # Do not turn a rollout filename into the user-facing session title.
        # The history store derives a readable first-user/fallback title.
        title = f"{title} [部分导入]" if title else ""
    return ParsedHistoryFile(
        ImportedConversation(
            conv_id=external_id[:1024], title=title[:500], messages=messages,
            project_ref=project_ref, project_source="metadata" if project_ref else "unknown",
        ),
        read_bytes,
        truncated,
    )


def _load_state(workspace: Path) -> dict[str, str]:
    path = workspace / ".memoryguard" / "history" / "import-state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _path_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _state_status(value: str, fingerprint: str) -> str:
    if value == fingerprint:
        return "complete"
    if value == f"partial:{fingerprint}":
        return "partial"
    return "importable"


def _save_state(workspace: Path, state: dict[str, str]) -> None:
    path = workspace / ".memoryguard" / "history" / "import-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _provider_product(value: str) -> str:
    value = str(value or "").strip().lower()
    return {"claude-code": "claude", "claude_code": "claude"}.get(value, value)


def _detected_provider_by_agent(workspace: Path) -> dict[str, str]:
    """Bindings grant store access; discovery proves the host product.

    A persisted discovery ledger avoids a fresh home scan.  When unavailable,
    use the current locator; inability to prove identity deliberately fails
    closed instead of assigning a provider's history to another Agent.
    """
    latest = workspace / ".memoryguard" / "discovery" / "latest.json"
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        result = {
            str(item.get("instance_id") or ""): _provider_product(str(item.get("product") or ""))
            for item in data.get("instances", [])
            if isinstance(item, dict) and item.get("instance_id")
        }
        if result:
            return result
    except (OSError, ValueError):
        pass
    try:
        from .agent_locator import AgentLocator
        instances, _ = AgentLocator(workspace).detect_instances()
        return {str(item.instance_id): _provider_product(str(item.product)) for item in instances}
    except (OSError, RuntimeError, ValueError):
        return {}


def _active_scope(
    workspace: Path | None,
    provider: str,
    agent_ids_by_provider: dict[str, str],
    detected_providers: dict[str, str] | None = None,
) -> V2HistoryScope | None:
    if workspace is None:
        return None
    agent_id = str(agent_ids_by_provider.get(provider) or "").strip()
    if not agent_id:
        return None
    detected_providers = detected_providers if detected_providers is not None else _detected_provider_by_agent(workspace)
    if detected_providers.get(agent_id) != _provider_product(provider):
        return None
    try:
        binding = GroupControlService(workspace, write=False).active_binding_for_agent(agent_id)
    except Exception:
        return None
    if not isinstance(binding, dict):
        return None
    group = str(binding.get("share_group_id") or "").strip()
    if not group:
        return None
    return V2HistoryScope(
        agent_instance_id=agent_id,
        provider=provider,
        share_group_id=group,
        authorized_agent_ids=(agent_id,),
        shared_read=not group.startswith("personal-"),
    )


def backfill_local_history(
    workspace: str | Path,
    *,
    agent_ids_by_provider: dict[str, str],
    home: str | Path | None = None,
    continuation: dict[str, Any] | None = None,
    max_files: int = MAX_BATCH_FILES,
    max_bytes: int = MAX_BATCH_BYTES,
    shadow: Any | None = None,
    shadow_max_turns: int = 1000,
    shadow_max_chars: int = 1_000_000,
) -> dict[str, Any]:
    """Import one bounded batch of supported local history.

    ``agent_ids_by_provider`` is explicit and must point to an *active*
    binding.  Missing providers report ``pending_binding`` rather than being
    assigned to the active GUI Agent by accident.  Completion is recomputed
    from per-source receipts on every call; a pending provider can therefore
    never be skipped by another provider's cursor.
    """
    root = Path(workspace).expanduser().resolve()
    inventory = discover_local_history_sources(
        home, workspace=root, agent_ids_by_provider=agent_ids_by_provider,
    )
    ready = [HistoryImportSource(
        provider=str(item["provider"]), path=Path(str(item["path"])),
        status=str(item["status"]), supported=bool(item["supported"]), reason=str(item["reason"]),
    ) for item in inventory["sources"] if item["supported"]]
    max_files = max(1, min(int(max_files), MAX_BATCH_FILES))
    max_bytes = max(1, min(int(max_bytes), MAX_BATCH_BYTES))
    state = _load_state(root)
    imported = skipped = errors = processed_bytes = processed_files = partial = 0
    imported_by_provider: dict[str, int] = {}
    shadow_results: list[dict[str, Any]] = []
    detected_providers = _detected_provider_by_agent(root)
    pending_binding = sorted({
        item.provider for item in ready
        if _active_scope(root, item.provider, agent_ids_by_provider, detected_providers) is None
    })
    for source in ready:
        if source.provider in pending_binding:
            continue
        try:
            size = source.path.stat().st_size
            fingerprint = source.fingerprint()
        except OSError:
            errors += 1
            continue
        state_key = f"{source.provider}:{source.path}"
        if _state_status(state.get(state_key, ""), fingerprint) == "complete":
            skipped += 1
            continue
        read_budget = min(size, MAX_SOURCE_READ_BYTES)
        if processed_files >= max_files or (processed_bytes and processed_bytes + read_budget > max_bytes):
            break
        parsed = _parse_jsonl(source)
        processed_files += 1
        processed_bytes += parsed.read_bytes
        if parsed.conversation is None:
            skipped += 1
            state[state_key] = fingerprint
            continue
        scope = _active_scope(root, source.provider, agent_ids_by_provider, detected_providers)
        if scope is None:  # binding may have changed during a long batch
            pending_binding = sorted(set(pending_binding) | {source.provider})
            continue
        conversation = parsed.conversation
        events = [
            ConversationEvent(
                external_object_key=str(conversation.conv_id),
                content=str(message.get("content") or ""),
                role=str(message.get("role") or "user"),
                ordinal=index,
                event_id=str(message.get("event_id") or message.get("id") or ""),
                source_revision=fingerprint,
                title=str(conversation.title or "")[:512],
                provider=source.provider,
                workspace_id=str(root),
                agent_instance_id=scope.agent_instance_id,
                project_ref=str(conversation.project_ref or scope.project_ref or ""),
                share_group_id=scope.share_group_id,
                metadata={"source_path": str(source.path)},
                locator={"message_index": index},
            )
            for index, message in enumerate(conversation.messages)
            if isinstance(message, dict) and str(message.get("content") or "").strip()
            and str(message.get("role") or "").casefold() in {"user", "assistant"}
        ]
        if not events:
            skipped += 1
            state[state_key] = fingerprint
            continue
        content = ContentStore(
            root,
            workspace_id=str(root),
            trust_domain="conversation-import",
            sensitivity="normal",
            retention_authority="workspace",
        )
        result = ConversationSync(content).sync(
            stable_id("history-import", source.provider, str(source.path)),
            events,
            owner_id="history-import:" + scope.agent_instance_id,
            max_turns=max(1, len(events)),
            max_chars=max(1, sum(len(str(event.content)) for event in events)),
        )
        imported += 1
        imported_by_provider[source.provider] = imported_by_provider.get(source.provider, 0) + 1
        if parsed.truncated:
            partial += 1
            state[state_key] = f"partial:{fingerprint}"
        else:
            state[state_key] = fingerprint
    _save_state(root, state)
    final_inventory = discover_local_history_sources(
        home, workspace=root, agent_ids_by_provider=agent_ids_by_provider,
    )
    remaining_by_provider: dict[str, int] = {}
    for item in final_inventory["sources"]:
        if item["status"] in {"importable", "pending_binding"}:
            remaining_by_provider[item["provider"]] = remaining_by_provider.get(item["provider"], 0) + 1
    remaining = sum(remaining_by_provider.values())
    return {
        "ok": True,
        "status": "pending_binding" if pending_binding else (
            "importing" if remaining else ("error" if errors else "complete")
        ),
        "imported": imported,
        "imported_by_provider": imported_by_provider,
        "skipped": skipped,
        "errors": errors,
        "partial": partial,
        "processed_bytes": processed_bytes,
        "pending_binding": pending_binding,
        "continuation": {"providers": remaining_by_provider} if remaining else None,
        "progress": {"processed_files": processed_files, "remaining_files": remaining},
        "shadow": shadow_results if shadow is not None else None,
        "inventory": final_inventory,
    }
