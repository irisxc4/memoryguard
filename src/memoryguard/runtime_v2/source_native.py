"""V2-native scan budgets, file constants, and source adapters.

The safe V2 services only need bounded inventory and adapter construction.  The
implementation lives here so those services do not import the legacy source
module or its stateful registry constructor.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from ..schema_v3 import (
    CandidateStatus,
    CoverageEntry,
    SourceObject,
    SourceRoot,
    SourceRootType,
    _now_iso,
    normalize_rel_path,
    stable_hash,
)


@dataclass
class ScanBudget:
    """Hard limits applied while inventorying a source root."""

    max_files: int = 50000
    max_total_size: int = 500 * 1024 * 1024
    max_single_file: int = 10 * 1024 * 1024
    max_depth: int = 20
    timeout_seconds: int = 120


TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".json", ".jsonl", ".rst", ".yaml", ".yml", ".toml",
}
META_EXTS = {".sqlite", ".sqlite3", ".db", ".vscdb"}
DEFAULT_PROJECT_EXCLUDE = [
    ".memoryguard/**",
    ".git/**",
    "node_modules/**",
    "__pycache__/**",
    ".cursor/**",
    ".trellis/**",
    ".agents/**",
    "**/*.plan.md",
]
INSTRUCTION_FILES = {
    "AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "GEMINI.md", "CODEBUDDY.md",
    ".cursorrules", ".windsurfrules", "copilot-instructions.md",
}
TEXT_MEDIA_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".txt": "text/plain", ".json": "application/json",
    ".jsonl": "application/x-jsonlines", ".rst": "text/x-rst",
    ".yaml": "application/yaml", ".yml": "application/yaml",
    ".toml": "application/toml",
    ".sqlite": "application/x-sqlite3", ".sqlite3": "application/x-sqlite3",
    ".db": "application/x-sqlite3", ".vscdb": "application/x-sqlite3",
}


@dataclass
class DetectionResult:
    applicable: bool
    confidence: float
    notes: str = ""


@dataclass
class AdapterCapability:
    adapter_name: str
    supported_features: list[str]
    unsupported_features: list[str]
    notes: str = ""


class SourceAdapter:
    """Minimal adapter contract used by bounded native scans."""

    def detect(self, root: Path) -> DetectionResult:
        raise NotImplementedError

    def inventory(self, root: Path, budget: ScanBudget) -> tuple[list[Path], list[CoverageEntry]]:
        raise NotImplementedError

    def read(self, candidate: Path, root: Path) -> tuple[SourceObject | None, CoverageEntry]:
        raise NotImplementedError

    def normalize_hint(self, source: SourceObject) -> dict[str, Any]:
        del source
        return {}

    def explain(self) -> AdapterCapability:
        raise NotImplementedError


class DirectoryAdapter(SourceAdapter):
    """Inventory a project or selected directory with the configured policy."""

    def __init__(self, source_root: SourceRoot):
        self.root = source_root

    def detect(self, root: Path) -> DetectionResult:
        if root.is_dir():
            return DetectionResult(applicable=True, confidence=1.0)
        return DetectionResult(applicable=False, confidence=1.0, notes="not a directory")

    def inventory(self, root: Path, budget: ScanBudget) -> tuple[list[Path], list[CoverageEntry]]:
        candidates: list[Path] = []
        truncation_entries: list[CoverageEntry] = []
        total_size = 0
        start = time.time()
        root_id = self.root.root_id

        def truncated(rel: str, reason: str, status: CandidateStatus) -> None:
            truncation_entries.append(
                CoverageEntry(
                    source_root_id=root_id,
                    relative_path=rel,
                    status=status,
                    reason=reason,
                )
            )

        if not self.root.recursive:
            for path in root.iterdir():
                rel = str(path.relative_to(root)).replace("\\", "/")
                if len(candidates) >= budget.max_files:
                    truncated(rel, "max_files budget exhausted", CandidateStatus.SKIPPED_BY_POLICY)
                    break
                if time.time() - start > budget.timeout_seconds:
                    truncated(rel, "timeout budget exhausted", CandidateStatus.SKIPPED_BY_POLICY)
                    break
                if path.is_file():
                    candidates.append(path)
            return candidates, truncation_entries

        for dirpath, dirnames, filenames in os.walk(root, followlinks=self.root.follow_symlinks):
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth >= budget.max_depth:
                dirnames[:] = []
                truncated(
                    str(Path(dirpath).relative_to(root)).replace("\\", "/") + "/",
                    "max_depth budget exhausted",
                    CandidateStatus.SKIPPED_BY_POLICY,
                )
                continue
            rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
            if self._excluded(rel_dir + "/"):
                dirnames[:] = []
                continue
            dirnames[:] = [
                directory
                for directory in dirnames
                if not self._excluded(rel_dir + "/" + directory + "/")
            ]
            for filename in filenames:
                path = Path(dirpath) / filename
                rel = str(path.relative_to(root)).replace("\\", "/")
                if self._excluded(rel) or not self._included(rel):
                    continue
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    truncated(rel, f"stat failed: {exc}", CandidateStatus.UNREADABLE)
                    continue
                if size > budget.max_single_file:
                    truncated(
                        rel,
                        f"file too large ({size} > {budget.max_single_file})",
                        CandidateStatus.SKIPPED_BY_POLICY,
                    )
                    continue
                if total_size + size > budget.max_total_size:
                    truncated(
                        rel,
                        "max_total_size budget exhausted",
                        CandidateStatus.SKIPPED_BY_POLICY,
                    )
                    return candidates, truncation_entries
                total_size += size
                candidates.append(path)
                if len(candidates) >= budget.max_files:
                    truncated(rel, "max_files budget reached", CandidateStatus.SKIPPED_BY_POLICY)
                    return candidates, truncation_entries
                if time.time() - start > budget.timeout_seconds:
                    truncated(rel, "timeout budget exhausted", CandidateStatus.SKIPPED_BY_POLICY)
                    return candidates, truncation_entries
        return candidates, truncation_entries

    def read(self, candidate: Path, root: Path) -> tuple[SourceObject | None, CoverageEntry]:
        rel = str(candidate.relative_to(root)).replace("\\", "/")
        root_id = self.root.root_id
        source_object_id = stable_hash(root_id, normalize_rel_path(rel))
        try:
            resolved_root = root.resolve()
            resolved_candidate = candidate.resolve()
            resolved_candidate.relative_to(resolved_root)
        except (ValueError, OSError) as exc:
            return None, CoverageEntry(
                source_root_id=root_id,
                relative_path=rel,
                status=CandidateStatus.UNREADABLE,
                reason=f"containment violation: {exc}",
            )
        if candidate.is_symlink():
            try:
                target = Path(os.readlink(candidate)).resolve()
                target.relative_to(resolved_root)
            except (ValueError, OSError) as exc:
                return None, CoverageEntry(
                    source_root_id=root_id,
                    relative_path=rel,
                    status=CandidateStatus.UNREADABLE,
                    reason=f"symlink escapes root: {exc}",
                )
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            return None, CoverageEntry(
                source_root_id=root_id,
                relative_path=rel,
                status=CandidateStatus.UNREADABLE,
                reason=str(exc),
            )
        ext = candidate.suffix.lower()
        if ext in META_EXTS:
            media_type = TEXT_MEDIA_TYPES.get(ext, "application/x-sqlite3")
            try:
                stat_result = candidate.stat()
                content_hash = stable_hash(
                    str(candidate), str(stat_result.st_size), str(int(stat_result.st_mtime))
                )
            except OSError as exc:
                return None, CoverageEntry(
                    source_root_id=root_id,
                    relative_path=rel,
                    status=CandidateStatus.UNREADABLE,
                    reason=str(exc),
                    size=size,
                    media_type=media_type,
                )
            obj = SourceObject(
                source_object_id=source_object_id,
                source_root_id=root_id,
                relative_path=rel,
                content_hash=content_hash,
                media_type=media_type,
                read_status="meta",
                captured_at=_now_iso(),
            )
            return obj, CoverageEntry(
                source_root_id=root_id,
                relative_path=rel,
                status=CandidateStatus.READ,
                size=size,
                media_type=media_type,
                reason="sqlite meta-read only",
            )
        if ext not in TEXT_EXTS and candidate.name not in INSTRUCTION_FILES:
            return None, CoverageEntry(
                source_root_id=root_id,
                relative_path=rel,
                status=CandidateStatus.UNSUPPORTED,
                reason=f"ext {ext}",
                size=size,
                media_type="application/octet-stream",
            )
        media_type = TEXT_MEDIA_TYPES.get(ext, "text/plain")
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return None, CoverageEntry(
                source_root_id=root_id,
                relative_path=rel,
                status=CandidateStatus.UNREADABLE,
                reason=str(exc),
                size=size,
                media_type=media_type,
            )
        obj = SourceObject(
            source_object_id=source_object_id,
            source_root_id=root_id,
            relative_path=rel,
            content_hash=stable_hash(content),
            media_type=media_type,
            read_status="read",
            captured_at=_now_iso(),
        )
        return obj, CoverageEntry(
            source_root_id=root_id,
            relative_path=rel,
            status=CandidateStatus.READ,
            size=size,
            media_type=media_type,
        )

    def _included(self, relative_path: str) -> bool:
        if not self.root.include:
            return True
        for pattern in self.root.include:
            if fnmatch.fnmatch(relative_path, pattern):
                return True
            if pattern.startswith("**/") and fnmatch.fnmatch(relative_path, pattern[3:]):
                return True
        return False

    def _excluded(self, relative_path: str) -> bool:
        return any(fnmatch.fnmatch(relative_path, pattern) for pattern in self.root.exclude)

    def explain(self) -> AdapterCapability:
        return AdapterCapability(
            adapter_name="DirectoryAdapter",
            supported_features=["detect", "inventory", "read"],
            unsupported_features=[],
            notes="通用目录扫描，支持 include/exclude glob",
        )


class ProjectDirectoryAdapter(DirectoryAdapter):
    """Project directory adapter."""


class SelectedDirectoryAdapter(DirectoryAdapter):
    """Explicitly selected directory adapter."""


class SelectedFileAdapter(SourceAdapter):
    """Single-file adapter."""

    def __init__(self, source_root: SourceRoot):
        self.root = source_root

    def detect(self, root: Path) -> DetectionResult:
        return DetectionResult(applicable=root.is_file(), confidence=1.0)

    def inventory(self, root: Path, budget: ScanBudget) -> tuple[list[Path], list[CoverageEntry]]:
        del budget
        return ([root] if root.is_file() else [], [])

    def read(self, candidate: Path, root: Path) -> tuple[SourceObject | None, CoverageEntry]:
        del root
        relative_path = candidate.name
        root_id = self.root.root_id
        source_object_id = stable_hash(root_id, normalize_rel_path(relative_path))
        extension = candidate.suffix.lower()
        media_type = TEXT_MEDIA_TYPES.get(extension, "text/plain")
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            return None, CoverageEntry(
                source_root_id=root_id,
                relative_path=relative_path,
                status=CandidateStatus.UNREADABLE,
                reason=str(exc),
            )
        if extension in META_EXTS:
            try:
                stat_result = candidate.stat()
                content_hash = stable_hash(
                    str(candidate), str(stat_result.st_size), str(int(stat_result.st_mtime))
                )
            except OSError as exc:
                return None, CoverageEntry(
                    source_root_id=root_id,
                    relative_path=relative_path,
                    status=CandidateStatus.UNREADABLE,
                    reason=str(exc),
                    size=size,
                )
            obj = SourceObject(
                source_object_id=source_object_id,
                source_root_id=root_id,
                relative_path=relative_path,
                content_hash=content_hash,
                media_type=media_type,
                read_status="meta",
                captured_at=_now_iso(),
            )
            return obj, CoverageEntry(
                source_root_id=root_id,
                relative_path=relative_path,
                status=CandidateStatus.READ,
                size=size,
                media_type=media_type,
                reason="sqlite meta-read only",
            )
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return None, CoverageEntry(
                source_root_id=root_id,
                relative_path=relative_path,
                status=CandidateStatus.UNREADABLE,
                reason=str(exc),
                size=size,
            )
        obj = SourceObject(
            source_object_id=source_object_id,
            source_root_id=root_id,
            relative_path=relative_path,
            content_hash=stable_hash(content),
            media_type=media_type,
            read_status="read",
            captured_at=_now_iso(),
        )
        return obj, CoverageEntry(
            source_root_id=root_id,
            relative_path=relative_path,
            status=CandidateStatus.READ,
            size=size,
            media_type=media_type,
        )

    def explain(self) -> AdapterCapability:
        return AdapterCapability(
            adapter_name="SelectedFileAdapter",
            supported_features=["detect", "inventory", "read"],
            unsupported_features=[],
        )


class ObsidianVaultAdapter(DirectoryAdapter):
    """Obsidian vault adapter with conservative default filters."""

    def __init__(self, source_root: SourceRoot):
        super().__init__(source_root)
        if not source_root.exclude:
            source_root.exclude = [".obsidian/**", ".trash/**", ".git/**"]
        if not source_root.include:
            source_root.include = ["**/*.md", "**/*.canvas", "**/*.base", "**/*.png", "**/*.jpg"]

    def detect(self, root: Path) -> DetectionResult:
        if not root.is_dir():
            return DetectionResult(applicable=False, confidence=1.0)
        is_vault = (root / ".obsidian").is_dir()
        return DetectionResult(
            applicable=True,
            confidence=0.9 if is_vault else 0.5,
            notes="obsidian_vault detected" if is_vault else "fallback to generic markdown dir",
        )

    def explain(self) -> AdapterCapability:
        return AdapterCapability(
            adapter_name="ObsidianVaultAdapter",
            supported_features=["detect", "inventory", "read"],
            unsupported_features=["plugins", "base_formulas", "remote_urls"],
            notes="仅扫描 Markdown/Canvas/Base/附件；不执行插件或公式",
        )


def _make_adapter(root: SourceRoot) -> SourceAdapter:
    """Construct the adapter selected by a validated source root."""
    if root.type == SourceRootType.PROJECT_DIRECTORY:
        return ProjectDirectoryAdapter(root)
    if root.type == SourceRootType.SELECTED_DIRECTORY:
        return SelectedDirectoryAdapter(root)
    if root.type == SourceRootType.SELECTED_FILE:
        return SelectedFileAdapter(root)
    if root.type == SourceRootType.OBSIDIAN_VAULT:
        return ObsidianVaultAdapter(root)
    raise ValueError(f"unknown SourceRootType: {root.type}")


make_adapter = _make_adapter


__all__ = [
    "AdapterCapability",
    "DEFAULT_PROJECT_EXCLUDE",
    "DirectoryAdapter",
    "DetectionResult",
    "INSTRUCTION_FILES",
    "META_EXTS",
    "ObsidianVaultAdapter",
    "ProjectDirectoryAdapter",
    "ScanBudget",
    "SelectedDirectoryAdapter",
    "SelectedFileAdapter",
    "SourceAdapter",
    "TEXT_EXTS",
    "TEXT_MEDIA_TYPES",
    "_make_adapter",
    "make_adapter",
]
