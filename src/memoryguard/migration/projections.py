"""Read-only migration of legacy JSON projections into Phase 3-A stores."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence

from ..projection_v2 import (
    ProfileProjector,
    ProjectionReadScope,
    ProjectionStore,
    ScenarioProjector,
)


class ProjectionMigrationError(RuntimeError):
    """Legacy projection source is unreadable or unsafe."""


@dataclass
class ProjectionMigrationReport:
    status: str = "OK"
    source_status: str = "NO_SOURCE"
    files: int = 0
    projections: int = 0
    ledger: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"OK", "PARTIAL"} and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_status": self.source_status,
            "files": self.files,
            "projections": self.projections,
            "ledger": self.ledger,
            "errors": list(self.errors),
        }

    as_dict = to_dict


_FORBIDDEN = frozenset(
    {
        "body", "raw", "raw_content", "content", "text", "document",
        "document_body", "conversation", "conversation_body",
        "full_transcript", "transcript", "raw_text", "source_text",
        "payload", "full_content", "content_body",
        "authority", "authorities", "admin", "administrator",
        "permission", "permissions", "capability", "capabilities",
        "role", "roles", "effect", "effects", "grant", "grants",
        "deny", "denies", "allow", "allows", "access", "acl",
        "policy", "policy_class", "visibility", "principal", "subject",
        "is_admin", "admin_flag",
        "scope", "scope_key", "workspace", "workspace_id", "agent_instance_id",
        "project_ref", "provider", "share_group_id", "sensitivity",
    }
)
_KNOWN_ROOT_FIELDS = frozenset(
    {
        "snapshot_id", "content_hash", "projection_mode", "derivation_engine",
        "llm_used", "profile_id", "profile_version", "scope", "scope_key",
        "mode", "metadata", "meta", "nodes", "edges", "snapshot",
    }
)
_CONTROL_KEY_TOKENS = frozenset(
    {
        "authority", "admin", "administrator", "permission", "capability",
        "role", "effect", "grant", "deny", "allow", "access", "acl",
        "policy", "scope", "visibility", "principal", "subject",
    }
)


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _derived_evidence_hash(file_digest: str, evidence_id: str) -> str:
    return hashlib.sha256(f"legacy-evidence\x1f{file_digest}\x1f{evidence_id}".encode("utf-8")).hexdigest()


def _lexical_path(value: str | Path) -> Path:
    """Normalize ``..`` without resolving symlinks or reparse points."""

    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _assert_safe_path(value: str | Path, *, label: str, allow_missing: bool = True) -> Path:
    """Reject symlink/reparse components before any source traversal.

    ``Path.resolve()`` is deliberately not used here: accepting its result
    would turn a symlink outside an authorized root into an apparently safe
    source.  Every existing ancestor and the final path are inspected with
    ``lstat`` instead.
    """

    path = _lexical_path(value)
    current = path
    while True:
        try:
            exists = current.exists() or current.is_symlink()
        except OSError as exc:
            raise ProjectionMigrationError(f"cannot inspect {label}: {current}") from exc
        if exists:
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise ProjectionMigrationError(f"cannot inspect {label}: {current}") from exc
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
                raise ProjectionMigrationError(f"{label} contains symlink/reparse component: {current}")
        elif current == path and not allow_missing:
            raise ProjectionMigrationError(f"{label} is missing: {path}")
        parent = current.parent
        if parent == current:
            break
        current = parent
    if path.exists() and not path.is_dir() and not path.is_file():
        raise ProjectionMigrationError(f"{label} is not a regular path: {path}")
    return path


def _contained(path: Path, roots: Sequence[Path]) -> bool:
    candidate = _lexical_path(path)
    for root in roots:
        try:
            candidate.relative_to(_lexical_path(root))
            return True
        except ValueError:
            continue
    return False


def _norm_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _is_forbidden_key(value: Any) -> bool:
    key = _norm_key(value)
    if key in _FORBIDDEN:
        return True
    return any(key.startswith(f"{token}_") or key.endswith(f"_{token}") for token in _CONTROL_KEY_TOKENS)


def _metadata_scalars(value: Any, *, path: str = "") -> tuple[dict[str, Any], tuple[str, ...]]:
    """Keep only safe, bounded metadata and return unknown-field paths."""

    issues: list[str] = []

    def walk(item: Any, depth: int, current: str) -> Any:
        if depth > 8:
            issues.append(f"{current}:depth")
            return None
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                name = str(key)
                child_path = f"{current}.{name}" if current else name
                if _is_forbidden_key(name):
                    issues.append(f"{child_path}:forbidden")
                    continue
                result[name] = walk(child, depth + 1, child_path)
            return result
        if isinstance(item, (list, tuple)):
            return [walk(child, depth + 1, f"{current}[{index}]") for index, child in enumerate(item)]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        issues.append(f"{current}:unsupported_type")
        return None

    result = walk(value if isinstance(value, Mapping) else {}, 0, path)
    assert isinstance(result, dict)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ProjectionMigrationError("legacy projection metadata exceeds 64 KiB")
    return result, tuple(issues)


class V1ProjectionMigrator:
    """Migrate legacy projection JSON as derived, reference-only inputs."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        projection_store: ProjectionStore | None = None,
        source_root: str | Path | None = None,
        profile_root: str | Path | None = None,
        scope: ProjectionReadScope | None = None,
    ) -> None:
        self.workspace = _assert_safe_path(workspace, label="workspace")
        source_candidate = (
            _lexical_path(source_root)
            if source_root is not None
            else self.workspace / ".memoryguard" / "projections"
        )
        profile_candidate = (
            _lexical_path(profile_root)
            if profile_root is not None
            else self.workspace / ".memoryguard" / "agent-profiles"
        )
        self.source_root = _assert_safe_path(source_candidate, label="source_root")
        self.profile_root = _assert_safe_path(profile_candidate, label="profile_root")
        for label, root in (("source_root", self.source_root), ("profile_root", self.profile_root)):
            if root.exists() and not root.is_dir():
                raise ProjectionMigrationError(f"{label} is not a directory: {root}")
        # Explicit roots are authorized source roots even when a caller keeps
        # legacy files outside the project.  They remain lexical paths and
        # cannot authorize a symlink escape.
        self._authorized_roots = (self.workspace, self.source_root, self.profile_root)
        self.store = projection_store or ProjectionStore(self.workspace)
        self.scope = scope or ProjectionReadScope(workspace_id=str(self.workspace), provider="legacy", sensitivity="normal", policy_class="private")
        self.last_report: ProjectionMigrationReport | None = None

    def _files(self) -> list[tuple[str, Path]]:
        paths: list[tuple[str, Path]] = []
        for kind, root in (("scenario", self.source_root), ("profile", self.profile_root)):
            if not root.is_dir():
                continue
            # Inspect every traversal result, not only JSON candidates.  A
            # symlinked directory can otherwise hide an external JSON file.
            try:
                items = sorted(root.rglob("*"))
            except OSError as exc:
                raise ProjectionMigrationError(f"cannot traverse {kind} source: {root}") from exc
            for item in items:
                path = _assert_safe_path(item, label=f"{kind} source", allow_missing=False)
                if not _contained(path, (root,)) or not _contained(path, self._authorized_roots):
                    raise ProjectionMigrationError(f"{kind} source escapes authorized roots: {path}")
                if not path.is_file() or path.suffix.lower() != ".json":
                    continue
                if path.name.endswith(".deleted.json") or path.name.startswith("."):
                    continue
                paths.append((kind, path))
        return paths

    def _read(self, path: Path) -> tuple[dict[str, Any], bytes, str]:
        path = _assert_safe_path(path, label="legacy projection file", allow_missing=False)
        if not _contained(path, self._authorized_roots):
            raise ProjectionMigrationError(f"legacy projection escapes authorized roots: {path}")
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectionMigrationError(f"legacy projection unreadable: {path}") from exc
        if not isinstance(value, Mapping):
            raise ProjectionMigrationError(f"legacy projection must be object: {path}")
        return dict(value), raw, _sha_bytes(raw)

    def _scope_for(self, root: Mapping[str, Any]) -> ProjectionReadScope:
        raw = root.get("scope")
        if isinstance(raw, Mapping):
            values = {
                "workspace_id": str(raw.get("workspace_id") or self.scope.workspace_id),
                "agent_instance_id": str(raw.get("agent_instance_id") or self.scope.agent_instance_id),
                "project_ref": str(raw.get("project_ref") or self.scope.project_ref),
                "provider": str(raw.get("provider") or self.scope.provider),
                "share_group_id": str(raw.get("share_group_id") or self.scope.share_group_id),
                "sensitivity": str(raw.get("sensitivity") or self.scope.sensitivity),
                "policy_class": str(raw.get("policy_class") or self.scope.policy_class),
            }
            return ProjectionReadScope(**values)
        return self.scope

    def _refs(self, root: Mapping[str, Any], file_digest: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        atoms: list[dict[str, str]] = []
        evidence: list[dict[str, str]] = []

        def evidence_ref(raw: Any) -> dict[str, str] | None:
            if isinstance(raw, Mapping):
                evidence_id = str(raw.get("evidence_id") or raw.get("id") or "")
                evidence_hash = str(raw.get("evidence_hash") or raw.get("digest") or raw.get("hash") or "")
            else:
                evidence_id, evidence_hash = str(raw), ""
            if not evidence_id:
                return None
            return {
                "evidence_id": evidence_id,
                "evidence_hash": evidence_hash or _derived_evidence_hash(file_digest, evidence_id),
                "relation": "supports",
            }

        raw_nodes = root.get("nodes")
        if isinstance(raw_nodes, list):
            for index, node in enumerate(raw_nodes):
                if not isinstance(node, Mapping):
                    continue
                atom_id = str(node.get("memory_id") or node.get("atom_id") or "")
                if not atom_id:
                    continue
                atom_hash = str(node.get("canonical_hash") or node.get("content_hash") or "")
                atoms.append({"atom_id": atom_id, "atom_hash": atom_hash})
                for raw in node.get("evidence_links") or node.get("evidence") or ():
                    item = evidence_ref(raw)
                    if item is not None:
                        evidence.append(item)
        raw_evidence = root.get("evidence_links") or root.get("evidence") or ()
        if isinstance(raw_evidence, list):
            for raw in raw_evidence:
                item = evidence_ref(raw)
                if item is not None:
                    evidence.append(item)
        if not evidence:
            evidence.append({"evidence_id": f"legacy-evidence-{file_digest[:32]}", "evidence_hash": file_digest, "relation": "legacy_source"})
        dedup_evidence = {(item["evidence_id"], item["relation"]): item for item in evidence}
        return atoms, list(dedup_evidence.values())

    def _metadata(self, root: Mapping[str, Any], *, source_ref: str) -> dict[str, Any]:
        raw = {key: value for key, value in root.items() if key in _KNOWN_ROOT_FIELDS and key not in {"nodes", "edges", "snapshot"}}
        safe, issues = _metadata_scalars(raw)
        for key in root:
            if str(key) not in _KNOWN_ROOT_FIELDS:
                self.store.record_ledger(source_ref, "unknown_field", str(key))
        for issue in issues:
            self.store.record_ledger(source_ref, "metadata_filtered", issue)
        return safe

    def migrate(self) -> ProjectionMigrationReport:
        files = self._files()
        report = ProjectionMigrationReport(source_status="READY" if files else "NO_SOURCE")
        if not files:
            self.last_report = report
            return report
        for kind, path in files:
            root, raw, digest = self._read(path)
            before = path.read_bytes()
            key = path.relative_to(self.source_root if kind == "scenario" else self.profile_root).with_suffix("").as_posix()
            source_ref = str(path)
            atoms, evidence = self._refs(root, digest)
            metadata = self._metadata(root, source_ref=source_ref)
            projector = ScenarioProjector(self.store) if kind == "scenario" else ProfileProjector(self.store)
            projector.project(key, atoms=atoms, evidence=evidence, scope=self._scope_for(root), metadata=metadata, source_digest=digest)
            if path.read_bytes() != before:
                raise ProjectionMigrationError(f"legacy projection changed while reading: {path}")
            report.files += 1
            report.projections += 1
        report.ledger = self.store.counts("scenario")["ledger"] + self.store.counts("profile")["ledger"]
        self.last_report = report
        return report

    run = migrate
    execute = migrate


ProjectionMigrator = V1ProjectionMigrator


__all__ = [
    "ProjectionMigrationError",
    "ProjectionMigrationReport",
    "ProjectionMigrator",
    "V1ProjectionMigrator",
]
