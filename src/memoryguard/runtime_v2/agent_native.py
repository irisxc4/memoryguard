"""V2-native Agent discovery, selection and lifecycle operations.

AgentLocator remains the only discovery engine.  This service adds a V2
system.db lifecycle/selection ledger and bounded host file operations without
importing AgentCleanup, SourceRegistry, AgentBindingStore or SharedMemoryStore.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from ..agent_locator import AgentLocator
from ..content.store import ContentStore, stable_id
from ..storage.database import open_database
from .group_native import GroupControlError, GroupControlService, SystemControlStore, _digest, _json, _now


class AgentNativeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "agent_operation_failed")
        super().__init__(self.code)


def _candidate_id(product: str, anchor: str = "") -> str:
    product_key = str(product or "unknown").strip().casefold() or "unknown"
    stable_anchor = str(anchor or "") if product_key == "unknown" else ""
    return "agent-candidate-" + _digest("agent-candidate", product_key, stable_anchor)[:32]


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _assert_no_reparse(path: Path) -> None:
    current = Path(os.path.abspath(os.fspath(path)))
    while True:
        if current.exists() or current.is_symlink():
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
                raise AgentNativeError("agent_path_symlink_or_reparse")
        parent = current.parent
        if parent == current:
            return
        current = parent


class AgentNativeService:
    def __init__(
        self,
        workspace: str | Path,
        *,
        opener: Callable[[Path], Any] | None = None,
        locator_factory: Callable[[Path], Any] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.control = GroupControlService(self.workspace, write=False)
        self.system = SystemControlStore(self.workspace, write=False)
        self.content = ContentStore(self.workspace, initialize=False)
        self.opener = opener or self._system_open
        self._locator_factory = locator_factory or AgentLocator

    @staticmethod
    def _system_open(path: Path) -> None:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", str(path)], close_fds=True)

    def _locator(self) -> Any:
        return self._locator_factory(self.workspace)

    def _marks(self) -> dict[str, dict[str, Any]]:
        if not self.system.db_path.is_file() or self.system._preflight() != "current":
            return {}
        with open_database(self.system.db_path, readonly=True) as conn:
            rows = conn.execute("SELECT candidate_id,product,dir_path,status,reason,updated_at FROM agent_lifecycle_marks ORDER BY candidate_id").fetchall()
        return {
            str(row[0]): {
                "candidate_id": str(row[0]), "product": str(row[1]), "dir_path": str(row[2]),
                "status": str(row[3]), "reason": str(row[4]), "updated_at": str(row[5]),
            }
            for row in rows
        }

    @staticmethod
    def _instance_paths(instance: Any) -> list[str]:
        result: list[str] = []
        for surface in getattr(instance, "surfaces", ()) or ():
            if str(surface.get("status") or "") != "found":
                continue
            path = str(surface.get("resolved_path") or "").strip()
            if path and path not in result:
                result.append(path)
        return result

    @staticmethod
    def _surface_partition(instance: Any) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        """Partition discovered surfaces without treating shared controls as data."""
        private: list[Mapping[str, Any]] = []
        shared: list[Mapping[str, Any]] = []
        install: list[Mapping[str, Any]] = []
        for surface in getattr(instance, "surfaces", ()) or ():
            if not isinstance(surface, Mapping) or str(surface.get("status") or "") != "found":
                continue
            role = str(surface.get("evidence_role") or "").strip().casefold()
            if role == "private_data_evidence":
                private.append(surface)
            elif role == "shared_surface":
                shared.append(surface)
            else:
                install.append(surface)
        return private, shared, install

    def _instances(self) -> tuple[list[Any], dict[str, Any]]:
        return self._locator().detect_instances()

    def _instance_context(self, instance_id: str) -> tuple[Any, str, list[str]]:
        instances, _ = self._instances()
        instance = next((item for item in instances if str(item.instance_id) == str(instance_id)), None)
        if instance is None:
            raise AgentNativeError("agent_instance_not_found")
        candidate = _candidate_id(str(instance.product))
        return instance, candidate, self._instance_paths(instance)

    def _candidate_context(self, candidate_id: str) -> tuple[str, str, list[str], str]:
        cid = str(candidate_id or "").strip()
        if not cid:
            raise AgentNativeError("candidate_id_required")
        instances, _ = self._instances()
        for instance in instances:
            known_id = _candidate_id(str(instance.product))
            if known_id == cid:
                paths = self._instance_paths(instance)
                return str(instance.product), str(instance.instance_id), paths, cid
        for item in self._locator().discover_candidates(include_uninstalled=True, include_stale=True, include_unknown=True):
            candidate = _candidate_id(str(item.product), str(item.dir_path))
            if candidate == cid:
                return str(item.product), "", [str(item.dir_path)], cid
        raise AgentNativeError("candidate_not_found")

    def discover_agents(self) -> dict[str, Any]:
        locator = self._locator()
        instances, ledgers = locator.detect_instances()
        # Discovery is intentionally Profile-bounded.  Return the registry
        # coverage beside the detections so the GUI never implies that a
        # finite set of safe probes equals every product on the market.
        registry = getattr(locator, "registry", None)
        profiles = registry.list_profiles() if registry is not None else []
        known_products = sorted({str(profile.product) for profile in profiles if str(profile.product).strip()})
        marks = self._marks()
        output: list[dict[str, Any]] = []
        for instance in instances:
            item = dict(instance.to_dict())
            candidate = _candidate_id(str(instance.product))
            mark = marks.get(candidate, {})
            item["candidate_id"] = candidate
            item["lifecycle_state"] = "ignored" if mark.get("status") == "uninstalled" else "installed"
            # Keep the support grade (A/B/C/D) separate from the takeover
            # capability (export_only/mcp/native_takeover).  The old mapping
            # overwrote the grade with ``export_only``, making the UI say
            # "支持 export_only" and hiding the actual support classification.
            item["support_level"] = _enum(getattr(instance, "support_level", "")) or "C"
            item["marked_uninstalled"] = mark.get("status") == "uninstalled"
            output.append(item)
        counts = {"found": 0, "missing": 0, "unsupported": 0, "permission_denied": 0, "excluded_by_user": 0, "not_applicable": 0, "unaccounted_count": 0, "surface_count": 0}
        for ledger in ledgers.values():
            values = ledger.counts()
            for key in counts:
                counts[key] += int(values.get(key, 0) or 0)
        return {
            "ok": True, "status": "succeeded", "instances": output,
            "discovery_ledger": counts, "platform": locator.context.platform,
            "host_id": locator.context.host_id,
            "known_profile_count": len(profiles),
            "known_products": known_products,
        }

    def list_candidates(
        self,
        *,
        include_uninstalled: bool = False,
        include_stale: bool = True,
        include_unknown: bool = True,
    ) -> dict[str, Any]:
        marks = self._marks()
        rows: list[dict[str, Any]] = []
        for candidate in self._locator().discover_candidates(include_uninstalled=True, include_stale=include_stale, include_unknown=include_unknown):
            item = dict(candidate.to_dict())
            cid = _candidate_id(str(candidate.product), str(candidate.dir_path))
            marked = marks.get(cid, {}).get("status") == "uninstalled"
            if marked and not include_uninstalled:
                continue
            item["candidate_id"] = cid
            item["marked_uninstalled"] = marked
            rows.append(item)
        return {"ok": True, "status": "succeeded", "candidates": rows, "total": len(rows)}

    def get_selection_tree(self, instance_id: str) -> dict[str, Any]:
        tree = self._locator().get_selection_tree(str(instance_id))
        if "error" in tree:
            raise AgentNativeError("agent_selection_tree_unavailable")
        selected_ids = set(self.control.selected_source_ids(str(instance_id)))

        def annotate(file_row: dict[str, Any]) -> None:
            path = str(file_row.get("path") or "").strip()
            if not path:
                file_row["saved_selected"] = None
                return
            source_id = stable_id("agent-source", str(instance_id), str(Path(path).expanduser().resolve()))
            file_row["source_root_id"] = source_id
            file_row["saved_selected"] = source_id in selected_ids

        for scope_row in tree.get("scopes", []):
            for project in scope_row.get("projects", []):
                for category in project.get("categories", []):
                    for file_row in category.get("files", []):
                        annotate(file_row)
            for category in scope_row.get("categories", []):
                for file_row in category.get("files", []):
                    annotate(file_row)
        tree.update({"ok": True, "status": "succeeded"})
        return tree

    @staticmethod
    def _tree_paths(tree: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for scope_row in tree.get("scopes", []) if isinstance(tree.get("scopes"), list) else []:
            for project in scope_row.get("projects", []) if isinstance(scope_row, Mapping) else []:
                for category in project.get("categories", []) if isinstance(project, Mapping) else []:
                    for file_row in category.get("files", []) if isinstance(category, Mapping) else []:
                        if isinstance(file_row, Mapping) and file_row.get("path"):
                            result[str(Path(str(file_row["path"])).expanduser().resolve())] = dict(file_row)
            for category in scope_row.get("categories", []) if isinstance(scope_row, Mapping) else []:
                for file_row in category.get("files", []) if isinstance(category, Mapping) else []:
                    if isinstance(file_row, Mapping) and file_row.get("path"):
                        result[str(Path(str(file_row["path"])).expanduser().resolve())] = dict(file_row)
        return result

    def resolve_source_path(self, instance_id: str, source_root_id: str) -> str:
        """Resolve a server-issued source token without exposing its path."""
        token = str(source_root_id or '').strip()
        if not token:
            raise AgentNativeError('source_root_id_required')
        allowed = self._tree_paths(self.get_selection_tree(str(instance_id)))
        matches = [
            path for path, row in allowed.items()
            if str(row.get('source_root_id') or '') == token
        ]
        if len(matches) != 1:
            raise AgentNativeError('agent_source_not_found')
        return matches[0]

    def resolve_residual_path(self, candidate_id: str, path_ref: str) -> str:
        """Resolve a server-issued residual token for a guarded file action."""
        token = str(path_ref or '').strip()
        if not token:
            raise AgentNativeError('dir_path_required')
        _product, _instance_id, paths, resolved_candidate = self._candidate_context(candidate_id)
        matches = [
            str(Path(item).expanduser().resolve())
            for item in paths
            if stable_id(
                'agent-residual', resolved_candidate,
                str(Path(item).expanduser().resolve()),
            ) == token
        ]
        if len(matches) != 1:
            raise AgentNativeError('agent_path_not_discovered')
        return matches[0]

    def commit_selection(self, instance_id: str, selected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        instance, _candidate, _paths = self._instance_context(instance_id)
        tree = self.get_selection_tree(instance_id)
        allowed = self._tree_paths(tree)
        normalized: list[tuple[str, Mapping[str, Any]]] = []
        for item in selected:
            if not isinstance(item, Mapping):
                raise AgentNativeError("selection_entry_invalid")
            source_root_id = str(item.get("source_root_id") or "").strip()
            if source_root_id:
                matches = [
                    candidate_path for candidate_path, row in allowed.items()
                    if str(row.get("source_root_id") or "") == source_root_id
                ]
                if len(matches) != 1:
                    raise AgentNativeError("selection_path_not_discovered")
                path = matches[0]
            else:
                path = str(item.get("path") or "").strip()
            if not path:
                raise AgentNativeError("selection_path_required")
            resolved = str(Path(path).expanduser().resolve())
            if resolved not in allowed:
                raise AgentNativeError("selection_path_not_discovered")
            normalized.append((resolved, item))
        source_ids = [stable_id("agent-source", str(instance_id), path) for path, _ in normalized]
        digest = hashlib.sha256(_json({"instance_id": str(instance_id), "source_ids": sorted(source_ids)}).encode("utf-8")).hexdigest()

        content = ContentStore(self.workspace)
        previous = set(self.control.selected_source_ids(str(instance_id)))
        all_touched = previous | set(source_ids)
        previous_enabled: dict[str, bool] = {}
        existing = {str(row.get("source_id") or ""): row for row in content.list_source_connectors(workspace_id=str(self.workspace))}
        for source_id in all_touched:
            if source_id in existing:
                previous_enabled[source_id] = bool(existing[source_id].get("enabled"))
        try:
            for source_id in previous - set(source_ids):
                content.set_source_connector_enabled(source_id, False, workspace_id=str(self.workspace))
            for (path, item), source_id in zip(normalized, source_ids):
                path_obj = Path(path)
                content.upsert_source_connector(
                    source_id=source_id,
                    provider=str(instance.product or "agent"),
                    source_type="directory" if path_obj.is_dir() else "file",
                    external_root_key=path,
                    workspace_id=str(self.workspace),
                    enabled=True,
                )
            result = GroupControlService(self.workspace, write=True).record_selection(
                str(instance_id), source_ids, digest,
            )
        except Exception:
            for source_id in all_touched:
                if source_id in previous_enabled:
                    content.set_source_connector_enabled(source_id, previous_enabled[source_id], workspace_id=str(self.workspace))
                elif source_id in set(source_ids):
                    content.set_source_connector_enabled(source_id, False, workspace_id=str(self.workspace))
            raise
        result.update({"added_source_count": len(set(source_ids) - previous), "disabled_source_count": len(previous - set(source_ids))})
        return result

    def mark_uninstalled(self, candidate_id: str, *, product: str = "", dir_path: str = "", reason: str = "") -> dict[str, Any]:
        cid = str(candidate_id or "").strip()
        if not cid:
            raise AgentNativeError("candidate_id_required")
        request = {"candidate_id": cid, "product": str(product), "dir_path": str(dir_path), "reason": str(reason)}
        store = SystemControlStore(self.workspace, write=True)

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            now = _now()
            conn.execute(
                "INSERT INTO agent_lifecycle_marks(candidate_id,product,dir_path,status,reason,updated_at) VALUES(?,?,?,'uninstalled',?,?) "
                "ON CONFLICT(candidate_id) DO UPDATE SET product=excluded.product,dir_path=excluded.dir_path,status='uninstalled',reason=excluded.reason,updated_at=excluded.updated_at",
                (cid, str(product), str(dir_path), str(reason), now),
            )
            return ({"ok": True, "status": "succeeded", "candidate_id": cid, "marked_uninstalled": True, "changed": True}, cid)

        return store.mutate("mark_agent_uninstalled", _digest("mark", _json(request)), request, apply)

    def unmark_uninstalled(self, candidate_id: str, *, product: str = "") -> dict[str, Any]:
        cid = str(candidate_id or "").strip()
        if not cid:
            raise AgentNativeError("candidate_id_required")
        request = {"candidate_id": cid, "product": str(product)}
        store = SystemControlStore(self.workspace, write=True)

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            now = _now()
            row = conn.execute("SELECT status FROM agent_lifecycle_marks WHERE candidate_id=?", (cid,)).fetchone()
            changed = row is not None and str(row[0]) != "active"
            conn.execute(
                "INSERT INTO agent_lifecycle_marks(candidate_id,product,dir_path,status,reason,updated_at) VALUES(?,?,?,'active','',?) "
                "ON CONFLICT(candidate_id) DO UPDATE SET product=excluded.product,status='active',reason='',updated_at=excluded.updated_at",
                (cid, str(product), "", now),
            )
            return ({"ok": True, "status": "succeeded", "candidate_id": cid, "marked_uninstalled": False, "changed": changed}, cid)

        return store.mutate("unmark_agent_uninstalled", _digest("unmark", _json(request)), request, apply)

    def _validated_target(self, candidate_id: str, requested: str = "") -> tuple[str, str, Path]:
        product, instance_id, allowed, cid = self._candidate_context(candidate_id)
        if not allowed:
            raise AgentNativeError("no_private_data_evidence")
        target = Path(requested).expanduser().resolve() if str(requested or "").strip() else Path(allowed[0]).expanduser().resolve()
        allowed_resolved = {Path(item).expanduser().resolve() for item in allowed}
        if target not in allowed_resolved:
            raise AgentNativeError("agent_path_not_discovered")
        if not target.exists():
            raise AgentNativeError("agent_path_not_found")
        try:
            target.relative_to(self.workspace / ".memoryguard")
            raise AgentNativeError("agent_path_inside_memoryguard")
        except ValueError:
            pass
        _assert_no_reparse(target)
        return product, instance_id, target

    def archive(self, candidate_id: str, *, dir_path: str = "", reason: str = "", dry_run: bool = False) -> dict[str, Any]:
        product, instance_id, target = self._validated_target(candidate_id, dir_path)
        if dry_run:
            return {"ok": True, "status": "succeeded", "dry_run": True, "candidate_id": candidate_id, "path": str(target)}
        stat_info = target.stat()
        archive_id = "archive-" + _digest(
            candidate_id, target, stat_info.st_mtime_ns, stat_info.st_size, time.time_ns()
        )[:32]
        archive_root = self.workspace / ".memoryguard" / "agent-archives" / archive_id
        archive_root.mkdir(parents=True, exist_ok=False)
        destination = archive_root / target.name
        try:
            shutil.move(str(target), str(destination))
        except Exception:
            shutil.rmtree(archive_root, ignore_errors=True)
            raise
        request = {"candidate_id": candidate_id, "source": str(target), "archive_id": archive_id}
        store = SystemControlStore(self.workspace, write=True)

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            now = _now()
            conn.execute(
                "INSERT INTO agent_archives(archive_id,candidate_id,product,original_path,archive_path,status,created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?)",
                (archive_id, str(candidate_id), product, str(target), str(destination), now, now),
            )
            event_id = "cleanup-" + _digest("archive", archive_id)
            conn.execute(
                "INSERT INTO agent_cleanup_history(event_id,operation,candidate_id,archive_id,status,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (event_id, "archive_agent_dir", str(candidate_id), archive_id, "succeeded", _json({"reason": str(reason)}), now),
            )
            return ({"ok": True, "status": "succeeded", "archive_id": archive_id, "candidate_id": str(candidate_id), "product": product, "instance_id": instance_id, "original_path": str(target), "archive_path": str(destination), "reason": str(reason), "archived_at": now}, archive_id)

        try:
            return store.mutate("archive_agent_dir", _digest("archive-op", archive_id), request, apply)
        except Exception:
            if destination.exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(target))
            shutil.rmtree(archive_root, ignore_errors=True)
            raise

    def _archive_row(self, archive_id: str) -> dict[str, Any] | None:
        if not self.system.db_path.is_file() or self.system._preflight() != "current":
            return None
        with open_database(self.system.db_path, readonly=True) as conn:
            row = conn.execute("SELECT archive_id,candidate_id,product,original_path,archive_path,status,created_at,updated_at FROM agent_archives WHERE archive_id=?", (str(archive_id),)).fetchone()
        if row is None:
            return None
        return {"archive_id": str(row[0]), "candidate_id": str(row[1]), "product": str(row[2]), "original_path": str(row[3]), "archive_path": str(row[4]), "status": str(row[5]), "archived_at": str(row[6]), "updated_at": str(row[7])}

    def list_archives(self, *, candidate_id: str = "") -> dict[str, Any]:
        if not self.system.db_path.is_file() or self.system._preflight() != "current":
            return {"ok": True, "status": "succeeded", "archives": [], "total": 0}
        query = "SELECT archive_id,candidate_id,product,original_path,archive_path,status,created_at,updated_at FROM agent_archives WHERE status='active'"
        params: tuple[Any, ...] = ()
        if candidate_id:
            query += " AND candidate_id=?"
            params = (str(candidate_id),)
        query += " ORDER BY created_at DESC,archive_id"
        with open_database(self.system.db_path, readonly=True) as conn:
            rows = conn.execute(query, params).fetchall()
        values = [{"archive_id": str(row[0]), "candidate_id": str(row[1]), "product": str(row[2]), "original_path": str(row[3]), "archive_path": str(row[4]), "status": str(row[5]), "archived_at": str(row[6]), "updated_at": str(row[7])} for row in rows]
        return {"ok": True, "status": "succeeded", "archives": values, "total": len(values)}

    def restore(self, archive_id: str) -> dict[str, Any]:
        row = self._archive_row(archive_id)
        if row is None or row["status"] != "active":
            raise AgentNativeError("archive_not_found")
        source = Path(row["archive_path"])
        target = Path(row["original_path"])
        if not source.exists():
            raise AgentNativeError("archive_payload_missing")
        if target.exists():
            raise AgentNativeError("restore_target_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        store = SystemControlStore(self.workspace, write=True)
        request = {"archive_id": str(archive_id)}

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            now = _now()
            conn.execute("UPDATE agent_archives SET status='restored',updated_at=? WHERE archive_id=? AND status='active'", (now, str(archive_id)))
            conn.execute("INSERT INTO agent_cleanup_history(event_id,operation,candidate_id,archive_id,status,detail_json,created_at) VALUES(?,?,?,?,?,'{}',?)", ("cleanup-" + _digest("restore", archive_id), "restore_archived_agent", row["candidate_id"], str(archive_id), "succeeded", now))
            return ({"ok": True, "status": "succeeded", "archive_id": str(archive_id), "restored_to": str(target), "candidate_id": row["candidate_id"]}, str(archive_id))

        try:
            result = store.mutate("restore_archived_agent", _digest("restore", archive_id), request, apply)
        except Exception:
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
            raise
        try:
            source.parent.rmdir()
        except OSError:
            pass
        return result

    def delete_archive(self, archive_id: str) -> dict[str, Any]:
        row = self._archive_row(archive_id)
        if row is None or row["status"] != "active":
            raise AgentNativeError("archive_not_found")
        source = Path(row["archive_path"])
        deleting = self.workspace / ".memoryguard" / "agent-archives-deleting" / str(archive_id)
        deleting.parent.mkdir(parents=True, exist_ok=True)
        if deleting.exists():
            raise AgentNativeError("archive_delete_staging_exists")
        moved = False
        if source.exists():
            shutil.move(str(source), str(deleting))
            moved = True
        store = SystemControlStore(self.workspace, write=True)
        request = {"archive_id": str(archive_id)}

        def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
            now = _now()
            conn.execute("UPDATE agent_archives SET status='deleted',archive_path=?,updated_at=? WHERE archive_id=? AND status='active'", (str(deleting), now, str(archive_id)))
            conn.execute("INSERT INTO agent_cleanup_history(event_id,operation,candidate_id,archive_id,status,detail_json,created_at) VALUES(?,?,?,?,?,'{}',?)", ("cleanup-" + _digest("delete", archive_id), "delete_archived_agent", row["candidate_id"], str(archive_id), "succeeded", now))
            return ({"ok": True, "status": "succeeded", "archive_id": str(archive_id), "deleted": True}, str(archive_id))

        try:
            result = store.mutate("delete_archived_agent", _digest("delete", archive_id), request, apply)
        except Exception:
            if moved and deleting.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(deleting), str(source))
            raise
        cleanup_pending = False
        if deleting.exists():
            try:
                if deleting.is_dir():
                    shutil.rmtree(deleting)
                else:
                    deleting.unlink()
            except OSError:
                cleanup_pending = True
        result["cleanup_pending"] = cleanup_pending
        return result

    def open_folder(self, *, dir_path: str = "", candidate_id: str = "") -> dict[str, Any]:
        if candidate_id:
            _product, _instance, target = self._validated_target(candidate_id, dir_path)
        else:
            if not dir_path:
                raise AgentNativeError("path_required")
            target = Path(dir_path).expanduser().resolve()
            if not target.exists():
                raise AgentNativeError("path_not_found")
            _assert_no_reparse(target)
        folder = target if target.is_dir() else target.parent
        self.opener(folder)
        return {"ok": True, "status": "succeeded", "opened": True, "path": str(folder)}

    def cleanup_history(self) -> dict[str, Any]:
        if not self.system.db_path.is_file() or self.system._preflight() != "current":
            return {"ok": True, "status": "succeeded", "history": []}
        with open_database(self.system.db_path, readonly=True) as conn:
            rows = conn.execute("SELECT event_id,operation,candidate_id,archive_id,status,detail_json,created_at FROM agent_cleanup_history ORDER BY created_at DESC,event_id LIMIT 500").fetchall()
        values = []
        for row in rows:
            try:
                detail = json.loads(str(row[5] or "{}"))
            except Exception:
                detail = {}
            values.append({"event_id": str(row[0]), "operation": str(row[1]), "candidate_id": str(row[2]), "archive_id": str(row[3]), "status": str(row[4]), "detail": detail if isinstance(detail, Mapping) else {}, "created_at": str(row[6])})
        return {"ok": True, "status": "succeeded", "history": values}

    def residual_cleanup(self, *, instance_id: str = "", candidate_id: str = "") -> dict[str, Any]:
        if instance_id:
            instance, cid, paths = self._instance_context(instance_id)
            product = str(instance.product)
        else:
            product, instance_id, paths, cid = self._candidate_context(candidate_id)
            instance = None
        mark = self._marks().get(cid, {})
        items = []
        install_evidence = []
        data_evidence = []
        private_surfaces, shared_surfaces, install_surfaces = self._surface_partition(instance) if instance is not None else ([], [], [])
        private_paths = {
            str(Path(str(surface.get("resolved_path") or "")).expanduser().resolve())
            for surface in private_surfaces
            if str(surface.get("resolved_path") or "").strip()
        }
        if instance is not None:
            for surface in getattr(instance, "surfaces", ()) or ():
                path = str(surface.get("resolved_path") or "")
                found = str(surface.get("status") or "") == "found"
                if found and surface in install_surfaces:
                    install_evidence.append({"probe_type": str(surface.get("surface_id") or "surface"), "found": found, "detail": path})
        for path_text in paths:
            path = Path(path_text).expanduser()
            exists = path.exists()
            data_evidence.append({"dir_path": str(path), "exists": exists, "file_count": 1 if exists and path.is_file() else 0})
            if exists:
                resolved_path = str(path.resolve())
                items.append({
                    "path": resolved_path,
                    "path_ref": stable_id("agent-residual", cid, resolved_path),
                    "residual_type": "private_data_evidence" if resolved_path in private_paths else "agent_data",
                    "description": "AgentLocator discovered data surface",
                    "archive_preview": {"ok": True},
                })
        archives = self.list_archives(candidate_id=cid)["archives"]
        data_only = bool(private_surfaces) and not bool(install_surfaces)
        return {
            "ok": True, "status": "succeeded", "instance_id": str(instance_id), "candidate_id": cid,
            "product": product,
            "lifecycle_state": "ignored" if mark.get("status") == "uninstalled" else ("data_only" if data_only else "installed"),
            "private_data_surface_count": len(private_surfaces),
            "shared_surface_count": len(shared_surfaces),
            "install_evidence": install_evidence, "data_evidence": data_evidence, "items": items,
            "archive_previews": [item["archive_preview"] for item in items if item["residual_type"] == "private_data_evidence"],
            "archives": archives,
        }

    def get_agent_data(self, instance_id: str) -> dict[str, Any]:
        instance, cid, _paths = self._instance_context(instance_id)
        value = dict(instance.to_dict())
        value.update(self.residual_cleanup(instance_id=str(instance_id)))
        value["source_count"] = len(self.control.selected_source_ids(str(instance_id)))
        value["candidate_id"] = cid
        return value

    def list_agents(self) -> dict[str, Any]:
        discovered = self.discover_agents()
        agents = []
        residuals = []
        active_bindings = {
            str(item.get("agent_instance_id") or ""): dict(item)
            for item in self.control.list_bindings(include_inactive=False).get("bindings", [])
            if isinstance(item, Mapping)
        }
        for item in discovered["instances"]:
            instance, _candidate, _paths = self._instance_context(str(item["instance_id"]))
            private_surfaces, shared_surfaces, install_surfaces = self._surface_partition(instance)
            binding = active_bindings.get(str(item["instance_id"]))
            if binding:
                item["binding"] = binding
                item["binding_status"] = "active"
                item["private_data_surface_count"] = len(private_surfaces)
                item["shared_surface_count"] = len(shared_surfaces)
                agents.append(item)
                continue
            if private_surfaces and not install_surfaces:
                residual = self.residual_cleanup(instance_id=str(item["instance_id"]))
                residual["binding_status"] = "missing"
                residual["control_repair_required"] = True
                residuals.append(residual)
                continue
            if shared_surfaces and not install_surfaces and not private_surfaces:
                continue
            item["private_data_surface_count"] = len(private_surfaces)
            item["shared_surface_count"] = len(shared_surfaces)
            item["binding_status"] = "unbound"
            agents.append(item)
            try:
                # Installed instances remain in the primary agent list; their
                # cleanup detail is not a data-only residual candidate.
                self.residual_cleanup(instance_id=str(item["instance_id"]))
            except AgentNativeError:
                pass
        return {
            "ok": True,
            "status": "succeeded",
            "agents": agents,
            "instances": agents,
            "residuals": residuals,
            "total": len(agents),
            "residual_total": len(residuals),
            "known_profile_count": discovered.get("known_profile_count", 0),
            "known_products": discovered.get("known_products", []),
        }


__all__ = ["AgentNativeError", "AgentNativeService"]
