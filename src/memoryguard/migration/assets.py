"""Read-only V1 asset-file migration into the V2 Asset Registry.

Only metadata, file hashes and stable relative references cross the migration
boundary.  The source files are opened read-only and never rewritten.  Files
whose ownership or authority cannot be established are retained as blocked
metadata/map/ledger entries; the migrator never guesses an owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..assets_v2 import (
    UNKNOWN_ACL,
    AssetMutationContext,
    AssetStore,
    AssetPathError,
    AssetMigrationError,
)
from ..storage.database import open_database
from ..storage.layout import WorkspaceV2Layout
from ..storage.transaction import transaction


_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "secret", "secrets", "token", "tokens", "password", "passwd",
        "credential", "credentials", "api", "api_key", "apikey", "api_keys",
        "key", "authority", "authority_id", "owner", "owner_id", "admin",
        "admin_id", "is_admin", "acl", "acl_digest", "acl_hash", "namespace",
        "namespace_id", "scope", "scope_id", "capability", "capability_id",
        "code", "command", "body", "payload", "content", "text", "raw",
        "binary", "bytes", "blob", "transcript", "document", "agent", "agent_id",
        "agent_instance_id", "project", "project_id", "runtime", "runtime_role",
        "group", "group_id", "authorization", "auth", "private_key", "secret_key",
    }
)
_MAX_METADATA_DEPTH = 8
_MAX_METADATA_NODES = 2048
_MAX_METADATA_BYTES = 64 * 1024
_MAX_METADATA_STRING = 8 * 1024
_MARKER_KEYS = {
    "namespace_id": "namespace_id",
    "workspace_id": "workspace_id",
    "agent_instance_id": "agent_instance_id",
    "agent_id": "agent_instance_id",
    "project_ref": "project_ref",
    "project": "project_ref",
    "provider": "provider",
    "provider_id": "provider",
    "share_group_id": "share_group_id",
    "group_id": "share_group_id",
    "runtime_role": "runtime_role",
    "runtime": "runtime_role",
    "owner": "owner",
    "owner_id": "owner",
    "authority": "authority",
    "authority_id": "authority",
}


def _now() -> str:
    # The report is diagnostic only; source-derived rows intentionally do not
    # include volatile timestamps in their immutable metadata.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _metadata_key_tokens(raw_key: Any) -> tuple[str, ...]:
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(raw_key)).casefold()
    return tuple(token for token in re.split(r"[^a-z0-9]+", key) if token)


def _metadata_key_name(raw_key: Any) -> str:
    return "_".join(_metadata_key_tokens(raw_key))


def _metadata_key_forbidden(raw_key: Any) -> bool:
    key = _metadata_key_name(raw_key)
    if not key:
        return True
    # Structural facts intentionally allowed by the asset contract.
    if key in {"content_hash", "content_digest", "provider", "provider_id", "output_hash"}:
        return False
    return key in _SENSITIVE_METADATA_KEYS


def _safe_metadata(value: Any, *, depth: int = 0, _state: list[int] | None = None) -> Any:
    """Recursively retain bounded, non-sensitive metadata only."""

    state = _state if _state is not None else [0]
    if depth > _MAX_METADATA_DEPTH:
        return {"metadata_truncated": True}
    state[0] += 1
    if state[0] > _MAX_METADATA_NODES:
        return {"metadata_truncated": True}
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _metadata_key_forbidden(key):
                continue
            safe = _safe_metadata(item, depth=depth + 1, _state=state)
            output[key] = safe
        encoded = _stable_json(output).encode("utf-8")
        if len(encoded) > _MAX_METADATA_BYTES:
            return {"metadata_truncated": True}
        return output
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item, depth=depth + 1, _state=state) for item in value]
    if isinstance(value, str):
        if len(value) > _MAX_METADATA_STRING or any(ord(char) < 32 for char in value):
            return ""
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def _assert_source_safe(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    current = absolute
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AssetMigrationError(f"cannot inspect source path: {current}") from exc
        else:
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
                raise AssetMigrationError(f"source path contains symlink/reparse component: {current}")
        if current.parent == current:
            break
        current = current.parent
    # ``absolute`` is already normalized lexically.  Do not call
    # ``resolve()`` here: callers need the original path spelling so a
    # symlink/reparse bypass cannot be hidden by canonicalization.
    return absolute


@dataclass(frozen=True)
class _SourceFile:
    path: Path
    source_ref: str
    kind: str
    owner: str
    authority: str
    provider_hint: str = ""


@dataclass
class AssetMigrationReport:
    status: str = "READY"
    migrated: int = 0
    replayed: int = 0
    blocked: int = 0
    source_count: int = 0
    errors: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "READY" and not self.errors

    @property
    def target_count(self) -> int:
        return self.migrated

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "migrated": self.migrated,
            "replayed": self.replayed,
            "blocked": self.blocked,
            "source_count": self.source_count,
            "errors": list(self.errors),
            "files": [dict(item) for item in self.files],
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class V1AssetMigrator:
    """Import known V1 asset manifests without touching their source bytes."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        layout: WorkspaceV2Layout | None = None,
        source_paths: Mapping[str, str | Path] | Iterable[str | Path] | None = None,
        sources: Mapping[str, str | Path] | Iterable[str | Path] | None = None,
        migration_id: str = "",
        fault_hook: Callable[[str], Any] | None = None,
        fail_at: str | None = None,
    ) -> None:
        # Keep the caller's lexical root separately.  Passing only a resolved
        # ``WorkspaceV2Layout`` to AssetStore would otherwise hide a symlink or
        # reparse component that was present in the original V1 workspace.
        self.source_workspace = _assert_source_safe(Path(workspace).expanduser())
        self.layout = layout or WorkspaceV2Layout(self.source_workspace)
        self.workspace = self.layout.workspace
        self.source_paths = source_paths if source_paths is not None else sources
        self.migration_id = str(migration_id or "asset-phase5")
        self.fault_hook = fault_hook
        self.fail_at = fail_at

    def _fault(self, step: str) -> None:
        if self.fail_at and self.fail_at == step:
            raise AssetMigrationError(f"injected asset migration failure at {step}")
        if self.fault_hook is not None:
            self.fault_hook(step)

    def _discover(self) -> list[_SourceFile]:
        values: list[tuple[str, Path]] = []
        selected = self.source_paths
        if isinstance(selected, Mapping):
            values = [(str(label), Path(path)) for label, path in selected.items()]
        elif selected is not None:
            values = [("", Path(path)) for path in selected]
        else:
            root = self.workspace / ".memoryguard"
            candidates: list[tuple[str, Path]] = []
            for name in ("source-manifest.json", "source_manifest.json", "manifest.json"):
                candidates.append(("source_manifest", root / name))
            native = root / "native_releases"
            if native.is_dir() and not native.is_symlink():
                for path in sorted(native.glob("*/manifest.json")):
                    candidates.append(("native_release", path))
            profiles = root / "agent-profiles"
            if profiles.is_dir() and not profiles.is_symlink():
                candidates.extend(("agent_profile", path) for path in sorted(profiles.glob("*.json")))
            providers = root / "providers"
            if providers.is_dir() and not providers.is_symlink():
                candidates.extend(("provider", path) for path in sorted(providers.glob("*.json")))
            values = candidates
        result: list[_SourceFile] = []
        seen: set[str] = set()
        for label, raw_path in values:
            path = _assert_source_safe(raw_path.expanduser())
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(self.workspace).as_posix()
            except ValueError as exc:
                raise AssetMigrationError(f"source file is outside workspace: {path}") from exc
            if rel in seen:
                continue
            seen.add(rel)
            kind, owner, authority, provider = self._classify(path, label)
            result.append(_SourceFile(path, rel, kind, owner, authority, provider))
        return result

    @staticmethod
    def _classify(path: Path, label: str) -> tuple[str, str, str, str]:
        hint = f"{label}:{path.as_posix()}".casefold()
        text_name = path.name.casefold()
        if "native_release" in hint or "native-releases" in hint or "native_releases" in hint:
            return "native_release", "native_release", "release", ""
        if "agent_profile" in hint or "agent-profiles" in hint or "agent_profiles" in hint:
            return "agent_profile", "agent_profile", "profile", ""
        if "provider" in hint:
            provider = path.stem if path.stem and path.stem not in {"manifest", "config"} else ""
            return "provider", provider or UNKNOWN_ACL, "provider", provider
        if "manifest" in hint or text_name in {"manifest.json", "source-manifest.json", "source_manifest.json"}:
            return "source_manifest", "system", "manifest", ""
        return "unknown", UNKNOWN_ACL, UNKNOWN_ACL, ""

    @staticmethod
    def _parse(path: Path) -> tuple[dict[str, Any], str, dict[str, str]]:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise AssetMigrationError(f"cannot read source file: {path}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
            source_mapping = parsed if isinstance(parsed, Mapping) else {}
            metadata = _safe_metadata(parsed if isinstance(parsed, Mapping) else {"json_type": type(parsed).__name__})
            markers: dict[str, str] = {}
            for raw_key, value in source_mapping.items():
                canonical = _MARKER_KEYS.get("_".join(_metadata_key_tokens(raw_key)))
                if canonical and isinstance(value, (str, int, float, bool)) and not isinstance(value, bool):
                    text = str(value)
                    if text and len(text) <= _MAX_METADATA_STRING and not any(ord(char) < 32 for char in text):
                        markers[canonical] = text
        except (UnicodeError, json.JSONDecodeError):
            metadata = {"format": "opaque", "parse_error": True}
            markers = {}
        return metadata if isinstance(metadata, dict) else {}, hashlib.sha256(raw).hexdigest(), markers

    def _context(self, source: _SourceFile, markers: Mapping[str, str]) -> AssetMutationContext:
        def text(*keys: str, fallback: str = UNKNOWN_ACL) -> str:
            for key in keys:
                value = markers.get(key)
                if value is not None and str(value):
                    return str(value)
            return fallback

        provider = text("provider", "provider_id", fallback=source.provider_hint or UNKNOWN_ACL)
        group = text("share_group_id", "group_id")
        return AssetMutationContext(
            namespace_id=text("namespace_id", fallback="v1-assets"),
            workspace_id=str(self.workspace),
            agent_instance_id=text("agent_instance_id", "agent_id"),
            project_ref=text("project_ref", "project"),
            provider=provider,
            share_group_id=group,
            runtime_role=text("runtime_role", "runtime"),
            actor="v1-assets-migrator",
            authority="migration",
            admin=True,
        )

    def migrate(self, *, fail_after: int | None = None, strict: bool = True, **_: Any) -> AssetMigrationReport:
        report = AssetMigrationReport()
        try:
            sources = self._discover()
            report.source_count = len(sources)
            descriptors: list[tuple[_SourceFile, dict[str, Any], str, int, dict[str, str]]] = []
            for source in sources:
                self._fault(f"read:{source.source_ref}")
                metadata, digest, markers = self._parse(source.path)
                _hash, size = _sha256(source.path)
                # Never persist a mutable source path as an absolute location;
                # it is represented only by ``source_ref`` below.
                descriptors.append((source, metadata, digest, size, markers))
            if not descriptors:
                report.status = "NO_SOURCE"
                return report
            store = AssetStore(self.layout, source_workspace=self.source_workspace)
            counter = 0
            with open_database(store.db_path) as conn:
                with transaction(conn):
                    for source, metadata, digest, size, markers in descriptors:
                        counter += 1
                        if fail_after is not None and counter > int(fail_after):
                            raise AssetMigrationError("injected asset migration failure")
                        self._fault(f"write:{source.source_ref}")
                        context = self._context(source, markers)
                        owner = source.owner
                        authority = source.authority
                        parsed_owner = str(markers.get("owner") or "")
                        parsed_authority = str(markers.get("authority") or "")
                        if parsed_owner and parsed_owner not in {owner, source.provider_hint}:
                            owner = UNKNOWN_ACL
                        if parsed_authority and parsed_authority != authority:
                            authority = UNKNOWN_ACL
                        blocked = source.kind == "unknown" or owner == UNKNOWN_ACL or authority == UNKNOWN_ACL
                        acl_metadata = {
                            "source_ref": source.source_ref,
                            "source_kind": source.kind,
                            "source_hash": digest,
                            "size_bytes": size,
                            "metadata_digest": hashlib.sha256(_stable_json(metadata).encode("utf-8")).hexdigest(),
                        }
                        # Keep only safe scalar/structured metadata; in
                        # particular never include a JSON body or file bytes.
                        acl_metadata.update(_safe_metadata(metadata))
                        asset = store.register_asset(
                            source.source_ref,
                            asset_kind=f"v1_{source.kind}",
                            metadata=acl_metadata,
                            state="blocked" if blocked else "active",
                            context=context,
                            conn=conn,
                        )
                        version = store.register_version(
                            asset.asset_id,
                            "source",
                            content_hash=digest,
                            size_bytes=size,
                            metadata={"source_hash": digest, "source_ref": source.source_ref},
                            context=context,
                            conn=conn,
                        )
                        store.register_location(
                            asset.asset_id,
                            source.source_ref,
                            version_id=version.version_id,
                            root=self.workspace,
                            content_hash=digest,
                            size_bytes=size,
                            context=context,
                            conn=conn,
                        )
                        store.record_migration_map(
                            source.kind,
                            source.source_ref,
                            source.source_ref,
                            "asset",
                            asset.asset_id,
                            target_hash=digest,
                            status="blocked" if blocked else "mapped",
                            metadata={"source_hash": digest},
                            context=context,
                            conn=conn,
                        )
                        if blocked:
                            report.blocked += 1
                            store._record_unknown(conn, source_domain=source.kind, source_ref=source.source_ref, field="owner", value=owner, reason="unknown_owner")
                            store._record_unknown(conn, source_domain=source.kind, source_ref=source.source_ref, field="authority", value=authority, reason="unknown_authority")
                        report.migrated += 1
                        report.files.append({"source_ref": source.source_ref, "asset_id": asset.asset_id, "version_id": version.version_id, "sha256": digest, "size_bytes": size, "status": "blocked" if blocked else "mapped"})
            return report
        except Exception as exc:
            report.status = "FAILED"
            report.errors.append(f"{type(exc).__name__}: {exc}")
            if strict:
                raise
            return report

    run = migrate
    execute = migrate


AssetMigrator = V1AssetMigrator
AssetsMigrator = V1AssetMigrator


__all__ = ["AssetMigrationReport", "V1AssetMigrator", "AssetMigrator", "AssetsMigrator"]
