"""Read-only discovery and optional import of legacy Agent Skill manifests.

The scanner only reads profile JSON and skill manifest front matter.  It never
executes a script, copies a source body, follows a symlink/reparse point, or
mutates a legacy path.  Import is an explicit second step that writes only
declarations, hashes and reference links through :class:`SkillStore`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping, Sequence

from ..storage.layout import WorkspaceV2Layout
from ..storage.database import open_database
from ..storage.transaction import transaction
from ..skills_v2.models import SkillBinding, SkillDefinition, SkillEvidenceRef, SkillMutationContext, _SKILL_CONTEXT_CAPABILITY, validate_digest, validate_relative_ref, canonical_json
from ..skills_v2.store import SkillStore


class SkillMigrationError(RuntimeError):
    """A legacy skill source cannot be scanned safely."""


class SkillMigrationReadError(SkillMigrationError):
    """Strict migration read failed closed."""


@dataclass(frozen=True)
class SkillMigrationItem:
    source_path: str
    source_hash: str
    source_kind: str = "skill_manifest"
    name: str = ""
    namespace: str = "legacy"
    version: int = 1
    description: str = ""
    entrypoint_ref: str = "SKILL.md"
    entrypoint_hash: str = ""
    declaration: Mapping[str, Any] = field(default_factory=dict)
    unknown_fields: tuple[str, ...] = ()
    provider: str = ""
    profile_id: str = ""
    source_ref: str = ""

    def __post_init__(self) -> None:
        validate_digest(self.source_hash, "source_hash")
        validate_digest(self.entrypoint_hash, "entrypoint_hash")
        validate_relative_ref(self.entrypoint_ref, "entrypoint_ref")
        if not self.source_path:
            raise SkillMigrationError("migration source path is required")
        object.__setattr__(self, "source_ref", self.source_ref or self.source_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path, "source_hash": self.source_hash,
            "source_kind": self.source_kind, "name": self.name,
            "namespace": self.namespace, "version": self.version,
            "description": self.description, "entrypoint_ref": self.entrypoint_ref,
            "entrypoint_hash": self.entrypoint_hash, "declaration": dict(self.declaration),
            "unknown_fields": list(self.unknown_fields), "provider": self.provider,
            "profile_id": self.profile_id,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class SkillMigrationErrorEntry:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class SkillMigrationSnapshot:
    items: tuple[SkillMigrationItem, ...] = ()
    errors: tuple[SkillMigrationErrorEntry, ...] = ()
    unknown_fields: tuple[tuple[str, str], ...] = ()
    source_digest: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def status(self) -> str:
        return "READY" if self.ok else "FAILED"

    @property
    def migrated(self) -> int:
        return len(self.items)

    @property
    def replayed(self) -> int:
        return 0

    @property
    def blocked(self) -> int:
        return len(self.errors)

    @property
    def source_count(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "errors": [error.to_dict() for error in self.errors],
            "unknown_fields": [list(item) for item in self.unknown_fields],
            "source_digest": self.source_digest,
            "ok": self.ok,
            "status": self.status,
            "migrated": self.migrated,
            "replayed": self.replayed,
            "blocked": self.blocked,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe(path: Path, roots: Sequence[Path]) -> bool:
    raw = Path(os.path.abspath(os.fspath(path.expanduser())))
    lexical = raw
    while True:
        try:
            if WorkspaceV2Layout._is_reparse_or_symlink(lexical):
                return False
        except Exception:
            return False
        if lexical.parent == lexical:
            break
        lexical = lexical.parent
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=True))
        except (ValueError, OSError, RuntimeError):
            continue
        current = resolved
        while current != current.parent:
            try:
                if WorkspaceV2Layout._is_reparse_or_symlink(current):
                    return False
            except Exception:
                return False
            if current == root.resolve(strict=True):
                break
            current = current.parent
        return True
    return False


def _normalize_source_ref(value: str | Path) -> str:
    """Normalize a ledger/map source reference without accepting escapes."""

    text = str(value).strip().replace("\\", "/")
    if not text or "\x00" in text or text.startswith("/") or text.startswith("//"):
        raise SkillMigrationError("migration source reference must be relative")
    if len(text) >= 2 and text[1] == ":":
        raise SkillMigrationError("migration source reference cannot contain a drive")
    if "://" in text or text.casefold().startswith(("file:", "http:", "https:", "urn:")):
        raise SkillMigrationError("migration source reference cannot be a URI")
    parts = tuple(part for part in text.split("/") if part not in {""})
    if not parts or any(part in {".", ".."} for part in parts):
        raise SkillMigrationError("migration source reference contains traversal")
    return "/".join(parts)


def _validate_entrypoint(path: Path, entry_ref: str, root: Path) -> str:
    """Validate a relative entrypoint and reject missing/reparse targets."""

    ref = validate_relative_ref(entry_ref, "entrypoint_ref")
    candidate = path.parent / Path(ref.replace("/", os.sep))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise SkillMigrationError("entrypoint escapes root or is missing") from exc
    if not candidate.is_file() or not _safe(candidate, (root,)):
        raise SkillMigrationError("entrypoint is not a regular non-reparse file")
    return ref


def _scalar(value: str) -> Any:
    item = value.strip()
    if not item:
        return ""
    if item.casefold() in {"true", "yes"}:
        return True
    if item.casefold() in {"false", "no"}:
        return False
    try:
        if item.startswith("[") or item.startswith("{"):
            return json.loads(item)
        return int(item)
    except (ValueError, json.JSONDecodeError):
        return item.strip("'\"")


def _manifest(path: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Read JSON or YAML-ish front matter, never the markdown body."""

    suffix = path.suffix.casefold()
    raw = path.read_text(encoding="utf-8", errors="strict")
    data: dict[str, Any] = {}
    if suffix == ".json":
        parsed = json.loads(raw)
        if not isinstance(parsed, Mapping):
            raise ValueError("skill manifest must be a JSON object")
        data = dict(parsed)
    else:
        lines = raw.splitlines()
        if lines and lines[0].strip() == "---":
            end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
            if end is None:
                raise ValueError("skill front matter is not terminated")
            for line in lines[1:end]:
                if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                data[key.strip()] = _scalar(value)
        # No front matter is a valid legacy declaration; the folder name is
        # used as the stable display name and the file hash as its evidence.
    known = {"name", "id", "namespace", "description", "version", "entrypoint", "entrypoint_ref", "capabilities", "permissions", "execution_policy", "bindings", "scopes", "assets", "asset_refs", "evidence", "evidence_refs", "metadata"}
    unknown = tuple(sorted(str(key) for key in data if str(key) not in known))
    return data, unknown


class SkillMigrationReader:
    """Read-only reader for existing agent profiles/provider skill surfaces."""

    def __init__(self, workspace: str | Path, *, provider_roots: Iterable[str | Path] = (), skill_roots: Iterable[str | Path] = ()) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.profile_root = self.workspace / ".memoryguard" / "agent-profiles"
        self.provider_roots = tuple(Path(item).expanduser() for item in provider_roots)
        self.explicit_roots = tuple(Path(item).expanduser() for item in skill_roots)

    def _roots(self) -> tuple[Path, ...]:
        defaults = (
            self.workspace / ".agents" / "skills",
            self.workspace / ".claude" / "skills",
            self.workspace / ".cursor" / "skills",
            self.workspace / ".codex" / "skills",
            self.workspace / ".trae" / "skills",
        )
        roots = list(defaults) + list(self.explicit_roots) + list(self.provider_roots)
        result: list[Path] = []
        for root in roots:
            root = root.expanduser()
            if not root.is_absolute():
                root = self.workspace / root
            root = Path(os.path.abspath(os.fspath(root)))
            if root not in result:
                result.append(root)
        # Profiles are data-only declarations.  We inspect them to discover a
        # %WORKSPACE% skill surface, but never read a %HOME% path implicitly.
        if self.profile_root.is_dir() and _safe(self.profile_root, (self.workspace,)):
            for profile in sorted(self.profile_root.glob("*.json")):
                if not _safe(profile, (self.workspace,)):
                    continue
                try:
                    data = json.loads(profile.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                surfaces = data.get("surfaces", []) if isinstance(data, Mapping) else []
                if not isinstance(surfaces, list):
                    continue
                for surface in surfaces:
                    if not isinstance(surface, Mapping):
                        continue
                    role = str(surface.get("surface_role", "")).casefold()
                    template = str(surface.get("path_template", ""))
                    if role != "skill_surface" or "%WORKSPACE%" not in template:
                        continue
                    candidate = Path(template.replace("%WORKSPACE%", str(self.workspace)))
                    if "skills" in candidate.parts and candidate not in result:
                        result.append(candidate)
        return tuple(result)

    def scan(self, *, strict: bool = False) -> SkillMigrationSnapshot:
        items: list[SkillMigrationItem] = []
        errors: list[SkillMigrationErrorEntry] = []
        unknown: list[tuple[str, str]] = []
        for root in self._roots():
            if not root.exists():
                continue
            if not root.is_dir() or not _safe(root, (self.workspace, *self.provider_roots)):
                errors.append(SkillMigrationErrorEntry(str(root), "unsafe_root", "skill root is outside the allowed workspace/provider roots"))
                continue
            try:
                candidates = sorted({*root.rglob("SKILL.md"), *root.rglob("skill.json"), *root.rglob("manifest.json")})
            except OSError as exc:
                errors.append(SkillMigrationErrorEntry(str(root), "scan_error", str(exc)))
                continue
            for path in candidates:
                if not path.is_file() or not _safe(path, (root,)):
                    errors.append(SkillMigrationErrorEntry(str(path), "unsafe_path", "symlink/reparse or escaped skill manifest"))
                    continue
                try:
                    source_hash = _sha256(path)
                    validate_digest(source_hash, "source_hash")
                    data, unknown_keys = _manifest(path)
                    # Validate all front-matter values before returning a
                    # READY snapshot.  This rejects secrets/raw fields in the
                    # scan phase rather than after the first import write.
                    canonical_json(data)
                    name = str(data.get("name", data.get("id", path.parent.name)) or path.parent.name).strip()
                    namespace = str(data.get("namespace", data.get("provider", "legacy")) or "legacy").strip()
                    version = int(data.get("version", 1) or 1)
                    entry_ref = str(data.get("entrypoint_ref", data.get("entrypoint", path.name)) or path.name)
                    entry_ref = _validate_entrypoint(path, entry_ref, root)
                    entry = path.parent / Path(entry_ref.replace("/", os.sep))
                    entry_hash = _sha256(entry)
                    validate_digest(entry_hash, "entrypoint_hash")
                    declaration = {key: data[key] for key in ("capabilities", "permissions", "execution_policy", "metadata") if key in data}
                    source_abs = str(path.resolve())
                    source_ref = path.resolve().relative_to(self.workspace).as_posix()
                    source_ref = _normalize_source_ref(source_ref)
                    item = SkillMigrationItem(source_path=source_abs, source_ref=source_ref, source_hash=source_hash, source_kind="skill_manifest", name=name, namespace=namespace, version=version, description=str(data.get("description", "") or ""), entrypoint_ref=entry_ref, entrypoint_hash=entry_hash, declaration=declaration, unknown_fields=unknown_keys)
                    items.append(item)
                    unknown.extend((source_ref, key) for key in unknown_keys)
                except (OSError, UnicodeError, ValueError, SkillMigrationError, json.JSONDecodeError) as exc:
                    errors.append(SkillMigrationErrorEntry(str(path), "manifest_read_error", str(exc)))
        items.sort(key=lambda item: item.source_path)
        source_digest = hashlib.sha256("\n".join(item.source_path + ":" + item.source_hash for item in items).encode("utf-8")).hexdigest()
        snapshot = SkillMigrationSnapshot(tuple(items), tuple(errors), tuple(sorted(set(unknown))), source_digest)
        if strict and not snapshot.ok:
            raise SkillMigrationReadError("skill migration scan failed closed: " + "; ".join(error.code for error in snapshot.errors))
        return snapshot

    read = scan
    scan_skill_manifests = scan

    def import_into(self, store: SkillStore, context: SkillMutationContext, *, strict: bool = True, conn: Any | None = None, fail_after: int | None = None) -> SkillMigrationSnapshot:
        snapshot = self.scan(strict=strict)
        for index, item in enumerate(snapshot.items, 1):
            if fail_after is not None and index > int(fail_after):
                raise SkillMigrationError("injected skill migration failure")
            binding = SkillBinding("agent", context.agent_instance_id)
            evidence = SkillEvidenceRef(source_ref=item.source_ref, digest=item.source_hash, authority="migration")
            definition = SkillDefinition(name=item.name, namespace=item.namespace, version=item.version, description=item.description, declaration=item.declaration, entrypoint_ref=item.entrypoint_ref, entrypoint_hash=item.entrypoint_hash, bindings=(binding,), evidence_refs=(evidence,))
            result = store.register(definition, context=context, idempotency_key=f"migration:{item.source_ref}:{item.source_hash}", reason="legacy skill manifest migration", conn=conn)
            store.record_migration_map(source_path=item.source_ref, source_hash=item.source_hash, source_kind=item.source_kind, skill_id=result.skill_id, version_id=result.version_id, metadata={"namespace": item.namespace}, conn=conn)
            for field_name in item.unknown_fields:
                store.record_unknown(source_path=item.source_ref, field_name=field_name, value={"field": field_name}, conn=conn)
        return snapshot

    def migrate(self, store: SkillStore | None = None, context: SkillMutationContext | None = None, *, strict: bool = True, fail_after: int | None = None) -> SkillMigrationSnapshot:
        """Import in one transaction, or just scan when no source exists."""

        snapshot = self.scan(strict=strict)
        if not snapshot.items:
            return snapshot
        target = store or SkillStore(self.workspace)
        if context is None:
            context = SkillMutationContext._from_capability(_SKILL_CONTEXT_CAPABILITY,
                workspace_id=str(self.workspace), share_group_id="migration",
                agent_instance_id="migration", project_ref="migration",
                provider="migration", runtime_role="migration", actor="migration",
                authority="migration", admin=True,
            )
        try:
            with open_database(target.db_path) as conn:
                with transaction(conn):
                    self.import_into(target, context, strict=strict, conn=conn, fail_after=fail_after)
            return snapshot
        except Exception:
            # transaction() has rolled back every declaration/map/ledger row;
            # preserve the source snapshot for the caller and re-raise.
            raise

    run = migrate
    execute = migrate


# Naming aliases used by migration callers in earlier phases.
V1SkillsMigrator = SkillMigrationReader
SkillsMigrationReader = SkillMigrationReader
SkillMigrationReport = SkillMigrationSnapshot


__all__ = [
    "SkillMigrationError", "SkillMigrationErrorEntry", "SkillMigrationItem",
    "SkillMigrationReadError", "SkillMigrationReader", "SkillMigrationReport",
    "SkillMigrationSnapshot", "SkillsMigrationReader", "V1SkillsMigrator",
]
