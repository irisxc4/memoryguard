"""Deterministic V2 workspace storage layout.

V2 keeps facts in separate SQLite domains.  A layout object is pure path
calculation by default; callers explicitly opt in to directory creation so a
read-only consumer can never create a workspace as a side effect.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import ClassVar, Iterator, Mapping


class LayoutError(ValueError):
    """A path or domain is outside the V2 layout contract."""


@dataclass(frozen=True)
class WorkspaceV2Layout:
    """Paths for one workspace's V2 data plane.

    The exact database names are part of the storage contract.  In particular,
    the projection domain intentionally contains two databases and the system
    database is named ``manifest.db``.
    """

    workspace: Path

    ROOT_NAME: ClassVar[str] = ".memoryguard"
    DOMAIN_DB_NAMES: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "runtime": ("runtime.db",),
        "memory": ("memory.db",),
        "rules": ("rules.db",),
        "evidence": ("evidence.db",),
        "content": ("content.db",),
        "knowledge": ("knowledge.db",),
        "codegraph": ("codegraph.db",),
        "assets": ("assets.db",),
        "projection": ("scenario.db", "profile.db"),
        "system": ("manifest.db",),
    }
    DOMAINS: ClassVar[tuple[str, ...]] = tuple(DOMAIN_DB_NAMES)

    def __post_init__(self) -> None:
        workspace = Path(self.workspace).expanduser().resolve()
        if workspace.name == self.ROOT_NAME:
            raise LayoutError("workspace must be the project root, not .memoryguard")
        object.__setattr__(self, "workspace", workspace)

    @property
    def root(self) -> Path:
        return self.workspace / self.ROOT_NAME

    @property
    def base(self) -> Path:
        """Alias for :attr:`root` used by storage callers."""

        return self.root

    def domain_dir(self, domain: str) -> Path:
        self._check_domain(domain)
        return self.root / domain

    def db_paths(self, domain: str | None = None) -> tuple[Path, ...] | dict[str, tuple[Path, ...]]:
        """Return database paths, optionally restricted to one domain."""

        if domain is not None:
            self._check_domain(domain)
            return tuple(self.domain_dir(domain) / name for name in self.DOMAIN_DB_NAMES[domain])
        return {
            item: tuple(self.domain_dir(item) / name for name in names)
            for item, names in self.DOMAIN_DB_NAMES.items()
        }

    @property
    def databases(self) -> dict[str, tuple[Path, ...]]:
        return self.db_paths()  # type: ignore[return-value]

    @property
    def paths(self) -> dict[str, tuple[Path, ...]]:
        """Mapping alias for callers that prefer attribute-style access."""

        return self.databases

    @property
    def database_paths(self) -> dict[str, tuple[Path, ...]]:
        return self.databases

    @property
    def runtime(self) -> Path:
        return self.domain_dir("runtime")

    @property
    def memory(self) -> Path:
        return self.domain_dir("memory")

    @property
    def rules(self) -> Path:
        return self.domain_dir("rules")

    @property
    def evidence(self) -> Path:
        return self.domain_dir("evidence")

    @property
    def content(self) -> Path:
        return self.domain_dir("content")

    @property
    def knowledge(self) -> Path:
        return self.domain_dir("knowledge")

    @property
    def codegraph(self) -> Path:
        return self.domain_dir("codegraph")

    @property
    def assets(self) -> Path:
        return self.domain_dir("assets")

    @property
    def projection(self) -> Path:
        return self.domain_dir("projection")

    @property
    def system(self) -> Path:
        return self.domain_dir("system")

    @property
    def all_db_paths(self) -> tuple[Path, ...]:
        return tuple(path for paths in self.databases.values() for path in paths)

    @property
    def runtime_db(self) -> Path:
        return self._single("runtime")

    @property
    def memory_db(self) -> Path:
        return self._single("memory")

    @property
    def rules_db(self) -> Path:
        return self._single("rules")

    @property
    def evidence_db(self) -> Path:
        return self._single("evidence")

    @property
    def content_db(self) -> Path:
        return self._single("content")

    @property
    def knowledge_db(self) -> Path:
        return self._single("knowledge")

    @property
    def codegraph_db(self) -> Path:
        return self._single("codegraph")

    @property
    def assets_db(self) -> Path:
        return self._single("assets")

    @property
    def scenario_db(self) -> Path:
        return self.db_paths("projection")[0]  # type: ignore[index]

    @property
    def profile_db(self) -> Path:
        return self.db_paths("projection")[1]  # type: ignore[index]

    @property
    def manifest_db(self) -> Path:
        return self._single("system")

    def ensure_dirs(self) -> None:
        """Create the V2 directory tree (never creates database files)."""

        # A symlink/junction at either boundary would make a later SQLite
        # open write outside the workspace even when the lexical path looks
        # correct.  Check before mkdir (which otherwise follows it).
        self._assert_safe_component(self.root, allow_missing=True)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LayoutError(f"cannot create V2 root: {self.root}") from exc
        self._assert_safe_component(self.root, allow_missing=False)
        for domain in self.DOMAINS:
            domain_path = self.domain_dir(domain)
            self._assert_safe_component(domain_path, allow_missing=True)
            try:
                domain_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise LayoutError(f"cannot create V2 domain directory: {domain_path}") from exc
            self._assert_safe_component(domain_path, allow_missing=False)

    def assert_contained(self, path: str | Path) -> Path:
        """Return a resolved path after enforcing ``.memoryguard`` containment."""

        candidate = Path(path).expanduser().resolve()
        root = self.root.resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise LayoutError(f"path escapes V2 layout: {candidate}") from exc
        return candidate

    def assert_database_path(self, path: str | Path, domain: str) -> Path:
        """Validate one exact V2 database path for a write operation.

        ``connect_database`` intentionally remains a low-level primitive and
        accepts arbitrary paths.  All V2 schema/bootstrap and manifest writes
        call this method first so a typo, symlink, junction, or reparse point
        cannot redirect SQLite outside the fixed layout.
        """

        self._check_domain(domain)
        expected_paths = self.db_paths(domain)
        assert isinstance(expected_paths, tuple)
        candidate = Path(path).expanduser()
        candidate_key = os.path.normcase(os.path.abspath(os.fspath(candidate)))
        expected = next(
            (
                expected_path
                for expected_path in expected_paths
                if os.path.normcase(os.path.abspath(os.fspath(expected_path))) == candidate_key
            ),
            None,
        )
        if expected is None:
            raise LayoutError(
                f"database path is not the exact V2 {domain!r} layout path: {candidate}"
            )

        self._assert_safe_component(self.root, allow_missing=True)
        domain_path = self.domain_dir(domain)
        self._assert_safe_component(domain_path, allow_missing=True)
        # A pre-existing database itself may be a symlink/reparse point even
        # when both parent directories are safe.
        self._assert_safe_component(expected, allow_missing=True)
        try:
            resolved = expected.resolve(strict=False)
            resolved.relative_to(self.root.resolve(strict=False))
        except (OSError, ValueError) as exc:
            raise LayoutError(f"database path escapes V2 layout: {expected}") from exc
        return expected

    # Names used by callers that prefer an explicit write-oriented verb.
    assert_write_path = assert_database_path

    def contains(self, path: str | Path) -> bool:
        try:
            self.assert_contained(path)
        except LayoutError:
            return False
        return True

    def iter_db_paths(self) -> Iterator[tuple[str, Path]]:
        for domain, paths in self.databases.items():
            for path in paths:
                yield domain, path

    def _single(self, domain: str) -> Path:
        paths = self.db_paths(domain)
        assert isinstance(paths, tuple)
        if len(paths) != 1:
            raise LayoutError(f"domain has multiple databases: {domain}")
        return paths[0]

    @classmethod
    def _check_domain(cls, domain: str) -> None:
        if domain not in cls.DOMAIN_DB_NAMES:
            raise LayoutError(f"unknown V2 storage domain: {domain!r}")

    @staticmethod
    def _is_reparse_or_symlink(path: Path) -> bool:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise LayoutError(f"cannot inspect V2 path component: {path}") from exc
        # ``st_file_attributes`` is populated by Windows for junctions and
        # other reparse points.  POSIX symlinks are covered by S_ISLNK.
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & 0x0400
        )

    @classmethod
    def _assert_safe_component(cls, path: Path, *, allow_missing: bool) -> None:
        if not path.exists() and not path.is_symlink():
            if allow_missing:
                return
            raise LayoutError(f"required V2 path is missing: {path}")
        if cls._is_reparse_or_symlink(path):
            raise LayoutError(f"V2 path cannot be a symlink or reparse point: {path}")
