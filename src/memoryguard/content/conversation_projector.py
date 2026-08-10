"""Explicit V1-history -> V2 ConversationSync shadow bridge.

The bridge is deliberately opt-in.  It never constructs a ``ContentStore``
or changes the activation manifest.  V1 history writes are performed by the
caller first; shadow failures are recorded in a small atomic JSON outbox and
returned as diagnostics without changing the V1 result.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import uuid
from typing import Any, Iterable, Mapping

from ..system.manifest import ManifestError, ManifestManager, ManifestState
from .conversation_sync import ConversationEvent, ConversationSync, SyncResult, SyncRun
from .store import ContentStore, stable_id


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ShadowResult:
    status: str
    code: str = ""
    run_id: str = ""
    source_id: str = ""
    applied: int = 0
    changed: int = 0
    continuation: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "run_id": self.run_id,
            "source_id": self.source_id,
            "applied": self.applied,
            "changed": self.changed,
            "continuation": dict(self.continuation or {}) or None,
        }


class _JsonOutbox:
    """Tiny crash-safe, replayable outbox owned by an explicit bridge."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, rows: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def get(self, key: str) -> dict[str, Any] | None:
        return self._read().get(key)

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        rows = self._read()
        rows[key] = dict(value)
        self._write(rows)


class ConversationShadowBridge:
    """Project normalized V1 history events to an explicitly injected V2 store."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        content_store: ContentStore | None = None,
        manifest: ManifestManager | None = None,
        enabled: bool = False,
        owner_id: str = "history-shadow",
        outbox_path: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.content_store = content_store
        self.manifest = manifest or ManifestManager(self.workspace)
        self.enabled = bool(enabled)
        self.owner_id = str(owner_id or "history-shadow")
        self.outbox = _JsonOutbox(Path(outbox_path) if outbox_path else self.workspace / ".memoryguard" / "history" / "shadow-outbox.json")

    @property
    def available(self) -> bool:
        return self.enabled and isinstance(self.content_store, ContentStore)

    def _gate(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "shadow_disabled"
        if not isinstance(self.content_store, ContentStore):
            return False, "content_store_not_injected"
        try:
            state = self.manifest.current().state
        except (ManifestError, OSError, ValueError):
            return False, "manifest_unreadable"
        if state is ManifestState.V1_ACTIVE:
            return False, "manifest_v1_active"
        if state is ManifestState.V2_BUILDING:
            return True, ""
        if state is ManifestState.V2_ACTIVE:
            return True, ""
        return False, "manifest_shadow_not_allowed"

    def _source_id(self, provider: str, external_id: str, scope: Any) -> str:
        return stable_id(
            "history-shadow-source",
            provider,
            external_id,
            getattr(scope, "agent_instance_id", ""),
            getattr(scope, "project_ref", ""),
            getattr(scope, "share_group_id", ""),
        )

    def _events(self, conversation: Any, *, provider: str, scope: Any, source_revision: str = "") -> list[ConversationEvent]:
        external_id = str(getattr(conversation, "conv_id", "") or "")
        messages = list(getattr(conversation, "messages", []) or [])
        events: list[ConversationEvent] = []
        for ordinal, message in enumerate(messages):
            if not isinstance(message, Mapping):
                continue
            text = str(message.get("content") or "")
            if not text.strip():
                continue
            event_id = str(message.get("event_id") or message.get("event_key") or message.get("id") or "")
            events.append(ConversationEvent(
                external_object_key=external_id,
                content=text,
                role=str(message.get("role") or "user"),
                ordinal=ordinal,
                event_id=event_id,
                source_revision=source_revision,
                title=str(getattr(conversation, "title", "") or ""),
                provider=provider,
                workspace_id=str(getattr(scope, "workspace_id", "") or (self.content_store.workspace_id if self.content_store else "")),
                agent_instance_id=str(getattr(scope, "agent_instance_id", "") or ""),
                project_ref=str(getattr(scope, "project_ref", "") or getattr(conversation, "project_ref", "") or ""),
                share_group_id=str(getattr(scope, "share_group_id", "") or ""),
                policy_class="private",
                metadata={"created_at": str(message.get("created_at") or "")},
            ))
        return events

    def sync_conversation(
        self,
        conversation: Any,
        *,
        provider: str,
        scope: Any,
        source_revision: str = "",
        max_turns: int = 1000,
        max_chars: int = 1_000_000,
        continuation: Mapping[str, Any] | None = None,
        finalize: bool = True,
        source_id_override: str = "",
    ) -> dict[str, Any]:
        allowed, code = self._gate()
        if not allowed:
            return ShadowResult("disabled", code=code).to_dict()
        assert self.content_store is not None
        external_id = str(getattr(conversation, "conv_id", "") or "")
        source_id = str(source_id_override or self._source_id(provider, external_id, scope))
        events = self._events(conversation, provider=provider, scope=scope, source_revision=source_revision)
        key = stable_id("history-shadow-outbox", source_id, source_revision, _json([(e.event_id, e.ordinal, str(e.content)) for e in events]))
        prior = self.outbox.get(key)
        if prior and prior.get("status") == "complete" and not continuation:
            return dict(prior.get("result") or ShadowResult("complete", source_id=source_id).to_dict())
        self.outbox.put(key, {"status": "pending", "source_id": source_id, "provider": provider})
        sync = ConversationSync(self.content_store)
        run: SyncRun | None = None
        try:
            start_position = int(continuation.get("position") or 0) if continuation else 0
            pending_events = events[start_position:]
            if continuation:
                run = SyncRun(
                    source_id=source_id,
                    run_id=str(continuation.get("run_id") or ""),
                    revision=int(continuation.get("revision") or 0),
                    owner_id=self.owner_id,
                    state="scanning",
                    expected_revision=int(continuation.get("expected_revision") or 0),
                )
                cursor = str(continuation.get("cursor") or "")
            elif prior and prior.get("run_id") and self._recoverable_run(str(prior.get("run_id"))):
                # Crash recovery: replay the durable run claim.  The core
                # cursor digest is server-side; omitting a lost raw cursor is
                # safe because stable event IDs make the batch idempotent.
                run = SyncRun(
                    source_id=source_id,
                    run_id=str(prior.get("run_id")),
                    revision=int(prior.get("revision") or 0),
                    owner_id=self.owner_id,
                    state="scanning",
                    expected_revision=int(prior.get("expected_revision") or 0),
                )
                cursor = ""
            else:
                run = sync.begin_sync(source_id, owner_id=self.owner_id, provider=provider, external_root_key=source_id)
                cursor = ""
                self.outbox.put(key, {
                    "status": "pending", "source_id": source_id, "provider": provider,
                    "run_id": run.run_id, "revision": run.revision,
                    "expected_revision": run.expected_revision,
                })
            batches = []
            while pending_events:
                current = pending_events[:max(1, int(max_turns))]
                batch = sync.stage_batch(run, current, max_turns=max_turns, max_chars=max_chars, continuation_cursor=cursor)
                batches.append(batch)
                start_position += batch.applied
                pending_events = pending_events[batch.applied:]
                cursor = batch.continuation_cursor
                if not finalize:
                    break
            if not batches:
                batch = sync.stage_batch(run, [], max_turns=max_turns, max_chars=max_chars, continuation_cursor=cursor)
                batches.append(batch)
            batch = batches[-1]
            continuation_payload = {
                "run_id": run.run_id,
                "revision": run.revision,
                "expected_revision": run.expected_revision,
                "cursor": batch.continuation_cursor,
                "position": start_position,
            } if pending_events else None
            if continuation_payload and not finalize:
                result = ShadowResult("partial", run_id=run.run_id, source_id=source_id, applied=batch.applied, changed=batch.changed, continuation=continuation_payload).to_dict()
            else:
                finished: SyncResult = sync.finish_sync(run, status="complete")
                result = ShadowResult(finished.state, run_id=run.run_id, source_id=source_id, applied=batch.applied, changed=batch.changed).to_dict()
            self.outbox.put(key, {"status": result["status"], "source_id": source_id, "result": result})
            return result
        except Exception as exc:
            if run is not None:
                try:
                    sync.finish_sync(run, status="failed", error_code=type(exc).__name__)
                except Exception:
                    pass
            result = ShadowResult("failed", code=type(exc).__name__, run_id=run.run_id if run else "", source_id=source_id).to_dict()
            self.outbox.put(key, {"status": "failed", "source_id": source_id, "run_id": run.run_id if run else "", "revision": run.revision if run else 0, "expected_revision": run.expected_revision if run else 0, "result": result})
            return result

    def _recoverable_run(self, run_id: str) -> bool:
        if not self.content_store or not run_id:
            return False
        try:
            with self.content_store.connection() as conn:
                row = conn.execute("SELECT state FROM source_sync_state WHERE active_run_id=?", (run_id,)).fetchone()
            return row is not None and str(row[0]) in {"scanning", "applying", "partial"}
        except Exception:
            return False

    def sync_turn(
        self,
        *,
        external_session_id: str,
        provider: str,
        role: str,
        content: str,
        event_id: str,
        title: str,
        created_at: str,
        scope: Any,
        max_turns: int = 1000,
        max_chars: int = 1_000_000,
    ) -> dict[str, Any]:
        conversation = SimpleNamespace(
            # Append projection owns one source object per turn because the
            # legacy content schema keys source objects by external key.
            conv_id=f"{external_session_id}:turn:{event_id or uuid.uuid4().hex}",
            title=title,
            messages=[{"role": role, "content": content, "event_id": event_id, "created_at": created_at}],
        )
        return self.sync_conversation(
            conversation,
            provider=provider,
            scope=scope,
            max_turns=max_turns,
            max_chars=max_chars,
            source_id_override=stable_id("history-shadow-turn", external_session_id, event_id or uuid.uuid4().hex),
        )


__all__ = ["ConversationShadowBridge", "ShadowResult"]
