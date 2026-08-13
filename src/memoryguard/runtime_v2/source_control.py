"""V2-native source connector control and bounded filesystem inspection.

The authoritative source registry is ``ContentStore.source_connectors``.  This
module never constructs the legacy ``SourceRegistry`` or reads its config
files.  Source bodies remain on disk and are read only for an explicit bounded
preview/extraction request; the connector table stores metadata only.

Admin GUI sessions may enumerate all workspace connectors.  Non-admin callers
see only source IDs present in the V2 selection manifest for their trusted
Agent.  Browser-supplied IDs never widen this set.
"""
from __future__ import annotations

from collections import deque
import hashlib
import os
from pathlib import Path
import time
from typing import Any, Mapping

from ..content.store import ContentStore, stable_id
from ..storage.database import open_database_snapshot
from ..storage.layout import WorkspaceV2Layout
from .group_native import GroupControlService
from .safe_services import _canonical, _contained, _reparse_point


MAX_FILES = 1_000
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DEPTH = 20
MAX_SECONDS = 30
MAX_PREVIEW_BYTES = 512 * 1024

_SOURCE_TYPES = frozenset({"selected_directory", "selected_file", "obsidian_vault"})
_TYPE_ALIASES = {
    "directory": "selected_directory",
    "selected_directory": "selected_directory",
    "file": "selected_file",
    "selected_file": "selected_file",
    "obsidian": "obsidian_vault",
    "obsidian_vault": "obsidian_vault",
}
_TEXT_EXTENSIONS = frozenset(
    {
        ".md", ".markdown", ".txt", ".json", ".jsonl", ".toml", ".yaml", ".yml",
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css",
        ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".c", ".h", ".cpp", ".hpp",
        ".rb", ".php", ".sql", ".sh", ".ps1", ".ini", ".cfg", ".conf", ".xml",
    }
)
_MEDIA_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
    ".json": "application/json", ".jsonl": "application/x-ndjson", ".toml": "application/toml",
    ".yaml": "application/yaml", ".yml": "application/yaml", ".py": "text/x-python",
    ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
    ".ts": "text/typescript", ".tsx": "text/typescript", ".jsx": "text/javascript",
    ".html": "text/html", ".css": "text/css", ".go": "text/x-go", ".rs": "text/x-rust",
}


class SourceControlError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "source_control_failed")
        super().__init__(self.code)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_admin(context: Mapping[str, Any]) -> bool:
    return bool(context.get("admin") is True or context.get("is_admin") is True)


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.casefold(), "text/plain" if path.suffix.casefold() in _TEXT_EXTENSIONS else "application/octet-stream")


def _display_name(path: Path) -> str:
    return (path.name or path.anchor or "Source")[:256]


class SourceControlService:
    service_name = "source_control"

    def __init__(self, workspace: str | Path) -> None:
        self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace = self.layout.workspace
        self.db_path = self.layout.content_db

    def _preflight(self) -> str:
        if not self.db_path.is_file():
            return "missing"
        try:
            store = ContentStore(self.workspace, initialize=False)
            state = store._preflight_aux_schema()
        except Exception as exc:
            raise SourceControlError("v2_content_schema_invalid") from exc
        if state != "current":
            raise SourceControlError("v2_content_schema_invalid")
        return "current"

    def _authorized_ids(self, context: Mapping[str, Any]) -> set[str] | None:
        if _is_admin(context):
            return None
        agent = _text(context.get("agent_instance_id") or context.get("trusted_agent_id"))
        if not agent:
            return set()
        try:
            return set(GroupControlService(self.workspace, write=False).selected_source_ids(agent))
        except Exception:
            return set()

    def _connectors(self, context: Mapping[str, Any], *, enabled: bool | None = True) -> list[dict[str, Any]]:
        if self._preflight() != "current":
            return []
        allowed = self._authorized_ids(context)
        predicates = [
            "workspace_id=?",
            "source_type IN ('selected_directory','selected_file','obsidian_vault','directory','file','obsidian')",
        ]
        params: list[Any] = [str(self.workspace)]
        if enabled is not None:
            predicates.append("enabled=?")
            params.append(1 if enabled else 0)
        with open_database_snapshot(self.db_path) as conn:
            rows = conn.execute(
                "SELECT source_id,provider,source_type,external_root_key,enabled,created_at,updated_at "
                "FROM source_connectors WHERE " + " AND ".join(predicates) + " ORDER BY source_id",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            source_id = str(row[0])
            if allowed is not None and source_id not in allowed:
                continue
            result.append(
                {
                    "source_id": source_id,
                    "provider": str(row[1] or ""),
                    "source_type": str(row[2] or ""),
                    "external_root_key": str(row[3] or ""),
                    "enabled": bool(row[4]),
                    "created_at": str(row[5] or ""),
                    "updated_at": str(row[6] or ""),
                }
            )
        return result

    @staticmethod
    def _root(connector: Mapping[str, Any]) -> tuple[Path, str]:
        source_type = _TYPE_ALIASES.get(_text(connector.get("source_type")).casefold())
        if source_type not in _SOURCE_TYPES:
            raise SourceControlError("source_type_unsupported")
        raw = _text(connector.get("external_root_key"))
        if not raw:
            raise SourceControlError("source_path_missing")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise SourceControlError("relative_source_path")
        if _reparse_point(path):
            raise SourceControlError("reparse_point_blocked")
        root = _canonical(path)
        if not root.exists():
            raise SourceControlError("source_root_missing")
        if source_type == "selected_file" and not root.is_file():
            raise SourceControlError("source_type_mismatch")
        if source_type != "selected_file" and not root.is_dir():
            raise SourceControlError("source_type_mismatch")
        return root, source_type

    def add(
        self,
        path: str,
        source_type: str,
        context: Mapping[str, Any],
        *,
        display_name: str = "",
    ) -> dict[str, Any]:
        if not _is_admin(context):
            raise SourceControlError("admin_capability_required")
        raw = _text(path)
        if not raw:
            raise SourceControlError("source_path_required")
        value = Path(raw).expanduser()
        if not value.is_absolute():
            raise SourceControlError("relative_source_path")
        if _reparse_point(value):
            raise SourceControlError("reparse_point_blocked")
        resolved = _canonical(value)
        if not resolved.exists():
            raise SourceControlError("source_path_not_found")
        kind = _TYPE_ALIASES.get(_text(source_type).casefold())
        if kind is None:
            raise SourceControlError("invalid_source_type")
        if kind == "selected_directory" and (resolved / ".obsidian").is_dir():
            kind = "obsidian_vault"
        if kind == "selected_file" and not resolved.is_file():
            raise SourceControlError("source_type_mismatch")
        if kind != "selected_file" and not resolved.is_dir():
            raise SourceControlError("source_type_mismatch")
        content = ContentStore(self.workspace, initialize=True)
        existing = None
        for row in content.list_source_connectors(workspace_id=str(self.workspace)):
            row_kind = _TYPE_ALIASES.get(_text(row.get("source_type")).casefold())
            row_path = _text(row.get("external_root_key"))
            if row_kind != kind or not row_path:
                continue
            try:
                same_path = _canonical(Path(row_path).expanduser()) == resolved
            except (OSError, RuntimeError):
                same_path = False
            if same_path:
                existing = row
                break
        if existing is None:
            source_id = stable_id("gui-source", str(self.workspace), kind, str(resolved))
            content.upsert_source_connector(
                source_id=source_id,
                provider="memoryguard-gui",
                source_type=kind,
                external_root_key=str(resolved),
                workspace_id=str(self.workspace),
                enabled=True,
            )
            changed = True
        else:
            source_id = str(existing.get("source_id") or "")
            if not source_id:
                raise SourceControlError("source_id_missing")
            changed = False
        agent = _text(context.get("agent_instance_id"))
        if agent:
            try:
                control = GroupControlService(self.workspace, write=True)
                selected = control.selected_source_ids(agent)
                if source_id not in selected:
                    selected.append(source_id)
                selection_digest = hashlib.sha256("\n".join(sorted(selected)).encode("utf-8")).hexdigest()
                control.record_selection(agent, selected, selection_digest)
            except Exception as exc:
                # Connector write already committed; fail closed so the UI does
                # not claim per-agent authorization was applied when it was not.
                raise SourceControlError("source_selection_receipt_failed") from exc
        return {
            "ok": True,
            "status": "succeeded",
            "root_id": source_id,
            "source_id": source_id,
            "type": kind,
            "display_name": (_text(display_name) or _display_name(resolved))[:256],
            "changed": changed,
        }

    def remove(self, source_id: str, context: Mapping[str, Any]) -> dict[str, Any]:
        if not _is_admin(context):
            raise SourceControlError("admin_capability_required")
        target = _text(source_id)
        if not target:
            raise SourceControlError("source_id_required")
        content = ContentStore(self.workspace, initialize=False)
        if content._preflight_aux_schema() != "current":
            raise SourceControlError("v2_content_schema_invalid")
        changed = content.set_source_connector_enabled(target, False, workspace_id=str(self.workspace))
        if not changed:
            raise SourceControlError("source_not_removable")
        return {"ok": True, "status": "succeeded", "root_id": target, "source_id": target, "changed": True}

    def list_sources(self, context: Mapping[str, Any]) -> dict[str, Any]:
        if not self.db_path.is_file():
            return {"ok": True, "status": "NO_SOURCE", "service": self.service_name, "sources": [], "total": 0}
        rows = self._connectors(context, enabled=True)
        sources: list[dict[str, Any]] = []
        for item in rows:
            source_id = item["source_id"]
            try:
                root, kind = self._root(item)
                state = "READY"
                exists = True
            except SourceControlError as exc:
                root = None
                kind = item["source_type"]
                state = "MISSING" if exc.code == "source_root_missing" else "BLOCKED"
                exists = False
            sources.append(
                {
                    "root_id": source_id,
                    "source_id": source_id,
                    "type": kind,
                    "display_name": _display_name(root) if root is not None else source_id,
                    "scope": "workspace",
                    "enabled": True,
                    "state": state,
                    "path_exists": exists,
                }
            )
        return {
            "ok": True,
            "status": "READY" if sources else "NO_SOURCE",
            "service": self.service_name,
            "sources": sources,
            "total": len(sources),
        }

    def _inventory_one(self, connector: Mapping[str, Any]) -> tuple[list[tuple[Path, int]], int, str]:
        root, kind = self._root(connector)
        deadline = time.monotonic() + MAX_SECONDS
        accepted: list[tuple[Path, int]] = []
        skipped = 0
        total = 0
        if kind == "selected_file":
            candidates = deque([(root, 0)])
        else:
            candidates = deque([(root, 0)])
        while candidates:
            current, depth = candidates.popleft()
            if time.monotonic() >= deadline:
                skipped += len(candidates) + 1
                break
            if depth > MAX_DEPTH:
                skipped += 1
                continue
            if current.is_dir():
                try:
                    entries = sorted(current.iterdir(), key=lambda item: item.name.casefold())
                except OSError:
                    skipped += 1
                    continue
                for child in entries:
                    if _reparse_point(child):
                        skipped += 1
                        continue
                    if not _contained(child, root):
                        skipped += 1
                        continue
                    candidates.append((child, depth + 1))
                continue
            if not current.is_file():
                skipped += 1
                continue
            if len(accepted) >= MAX_FILES:
                skipped += len(candidates) + 1
                break
            try:
                size = int(current.stat().st_size)
            except OSError:
                skipped += 1
                continue
            if size > MAX_FILE_BYTES:
                skipped += 1
                continue
            if total + size > MAX_TOTAL_BYTES:
                skipped += len(candidates) + 1
                break
            accepted.append((current, size))
            total += size
        return accepted, skipped, kind

    def scan_summary(self, context: Mapping[str, Any]) -> dict[str, Any]:
        rows = self._connectors(context, enabled=True) if self.db_path.is_file() else []
        roots: list[dict[str, Any]] = []
        coverage = {"candidate_count": 0, "readable": 0, "unsupported": 0, "unreadable": 0, "skipped_by_policy": 0, "unaccounted_count": 0}
        for item in rows:
            try:
                accepted, skipped, _kind = self._inventory_one(item)
            except SourceControlError as exc:
                coverage["unreadable"] += 1
                roots.append({"root_id": item["source_id"], "state": "BLOCKED", "code": exc.code})
                continue
            readable = sum(1 for path, _ in accepted if path.suffix.casefold() in _TEXT_EXTENSIONS)
            unsupported = len(accepted) - readable
            coverage["candidate_count"] += len(accepted)
            coverage["readable"] += readable
            coverage["unsupported"] += unsupported
            coverage["skipped_by_policy"] += skipped
            roots.append({"root_id": item["source_id"], "state": "READY" if not skipped and not unsupported else "PARTIAL", "candidate_count": len(accepted), "readable": readable, "unsupported": unsupported, "skipped_by_policy": skipped})
        if not roots:
            status = "NO_SOURCE"
        elif any(row["state"] == "BLOCKED" for row in roots):
            status = "BLOCKED"
        elif any(row["state"] == "PARTIAL" for row in roots):
            status = "PARTIAL"
        else:
            status = "READY"
        return {"ok": status != "BLOCKED", "status": status, "service": self.service_name, "coverage": coverage, "roots": roots}

    def raw_summary(self, context: Mapping[str, Any]) -> dict[str, Any]:
        rows = self._connectors(context, enabled=True) if self.db_path.is_file() else []
        groups: list[dict[str, Any]] = []
        coverage = {"candidate_count": 0, "read": 0, "unsupported": 0, "unreadable": 0, "skipped_by_policy": 0, "unaccounted_count": 0}
        for item in rows:
            try:
                root, _kind = self._root(item)
                accepted, skipped, _kind = self._inventory_one(item)
            except SourceControlError as exc:
                coverage["unreadable"] += 1
                groups.append({"root_id": item["source_id"], "files": [], "state": "BLOCKED", "code": exc.code})
                continue
            files: list[dict[str, Any]] = []
            for candidate, size in accepted:
                try:
                    relative = candidate.name if root.is_file() else candidate.relative_to(root).as_posix()
                except ValueError:
                    coverage["unaccounted_count"] += 1
                    continue
                supported = candidate.suffix.casefold() in _TEXT_EXTENSIONS
                files.append({"root_id": item["source_id"], "relative_path": relative, "size": size, "media_type": _media_type(candidate), "read_status": "read" if supported else "unsupported", "authorized": True})
                coverage["candidate_count"] += 1
                coverage["read" if supported else "unsupported"] += 1
            coverage["skipped_by_policy"] += skipped
            groups.append({"root_id": item["source_id"], "files": files, "file_count": len(files), "state": "READY" if not skipped else "PARTIAL"})
        coverage["coverage_status"] = "complete" if not any(coverage[key] for key in ("unsupported", "unreadable", "skipped_by_policy", "unaccounted_count")) else "partial"
        return {"ok": True, "status": "succeeded", "groups": groups, "coverage": coverage}

    def _connector(self, source_id: str, context: Mapping[str, Any]) -> dict[str, Any]:
        target = _text(source_id)
        rows = self._connectors(context, enabled=True)
        match = next((item for item in rows if item["source_id"] == target), None)
        if match is None:
            raise SourceControlError("source_root_not_found")
        return match

    def resolve_root(self, source_id: str, context: Mapping[str, Any]) -> tuple[Path, str]:
        """Resolve one already-authorized connector to its canonical local root.

        This is an internal capability seam for consumers such as CodeGraph.
        Callers receive a root only after the same connector visibility and
        containment checks used by file preview; a browser path is never an
        alternative authorization source.
        """

        connector = self._connector(source_id, context)
        root, kind = self._root(connector)
        if _reparse_point(root):
            raise SourceControlError("reparse_point_blocked")
        return root, kind

    def resolve_file(self, source_id: str, relative_path: str, context: Mapping[str, Any]) -> tuple[Path, Path]:
        connector = self._connector(source_id, context)
        root, _kind = self._root(connector)
        if root.is_file():
            target = root
        else:
            relative = Path(_text(relative_path))
            if not str(relative) or relative.is_absolute() or ".." in relative.parts:
                raise SourceControlError("relative_source_path_required")
            target = _canonical(root / relative)
            if not _contained(target, root):
                raise SourceControlError("path_out_of_scope")
        if _reparse_point(target):
            raise SourceControlError("reparse_point_blocked")
        if not target.is_file():
            raise SourceControlError("file_not_found")
        return root, target

    def content_preview(self, source_id: str, relative_path: str, context: Mapping[str, Any]) -> dict[str, Any]:
        root, target = self.resolve_file(source_id, relative_path, context)
        if target.suffix.casefold() not in _TEXT_EXTENSIONS:
            raise SourceControlError("source_file_unsupported")
        try:
            size = int(target.stat().st_size)
            with target.open("rb") as stream:
                raw = stream.read(MAX_PREVIEW_BYTES + 1)
        except OSError as exc:
            raise SourceControlError("source_file_unreadable") from exc
        truncated = len(raw) > MAX_PREVIEW_BYTES
        raw = raw[:MAX_PREVIEW_BYTES]
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceControlError("source_file_encoding_unsupported") from exc
        return {
            "ok": True,
            "status": "succeeded",
            "root_id": source_id,
            "display_name": _display_name(root),
            "relative_path": target.name if root.is_file() else target.relative_to(root).as_posix(),
            "content": content,
            "size": size,
            "returned_bytes": len(raw),
            "truncated": truncated,
            "media_type": _media_type(target),
        }

    def resolve_path(self, path: str, context: Mapping[str, Any]) -> tuple[str, Path, Path]:
        raw = _text(path)
        if not raw:
            raise SourceControlError("path_required")
        target = Path(raw).expanduser()
        if not target.is_absolute():
            raise SourceControlError("relative_source_path")
        if _reparse_point(target):
            raise SourceControlError("reparse_point_blocked")
        resolved = _canonical(target)
        connectors = self._connectors(context, enabled=True)
        if not connectors:
            raise SourceControlError("no_source")
        for connector in connectors:
            try:
                root, _kind = self._root(connector)
            except SourceControlError:
                continue
            inside = resolved == root if root.is_file() else _contained(resolved, root)
            if inside:
                if _reparse_point(resolved):
                    raise SourceControlError("reparse_point_blocked")
                return connector["source_id"], root, resolved
        raise SourceControlError("path_out_of_scope")

    def preview_path(self, path: str, context: Mapping[str, Any]) -> dict[str, Any]:
        source_id, _root, resolved = self.resolve_path(path, context)
        return {"ok": True, "status": "READY", "service": self.service_name, "root_id": source_id, "reference": "source:" + hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24]}

    def dispatch(self, operation: str, payload: Mapping[str, Any] | None = None, *, context: Mapping[str, Any], **_: Any) -> dict[str, Any]:
        data = dict(payload or {})
        name = str(operation or "")
        try:
            if name in {"list_sources", "source_list"}:
                return self.list_sources(context)
            if name in {"scan_summary", "source_scan"}:
                return self.scan_summary(context)
            if name in {"raw_summary", "source_memory_summary"}:
                return self.raw_summary(context)
            if name in {"content_preview", "source_content_preview"}:
                return self.content_preview(str(data.get("source_id") or data.get("root_id") or ""), str(data.get("relative_path") or ""), context)
            if name in {"preview_path", "source_preview"}:
                return self.preview_path(str(data.get("path") or ""), context)
            if name == "source_add":
                return self.add(str(data.get("path") or ""), str(data.get("source_type") or ""), context, display_name=str(data.get("display_name") or ""))
            if name == "source_remove":
                return self.remove(str(data.get("source_id") or data.get("root_id") or ""), context)
            raise SourceControlError("source_operation_unknown")
        except SourceControlError as exc:
            return {"ok": False, "status": "BLOCKED", "service": self.service_name, "code": exc.code, "error": exc.code}


__all__ = ["SourceControlError", "SourceControlService"]
