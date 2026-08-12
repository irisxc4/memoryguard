"""V2-scoped semantic duplicate lookup.

The legacy semantic-dedup module already owns the deterministic embedding
backend.  This adapter only supplies the V2 read scope and returns
``MemoryAtom`` candidates; it never opens a second store or falls back to a
legacy record API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from ..memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from ..memory.store import stable_digest
from ..semantic_dedup import (
    DEFAULT_SEMANTIC_THRESHOLD,
    EmbeddingBackend,
    HashBackend,
    cosine_similarity,
)


def canonical_text(value: str) -> str:
    """Normalize only layout/case; preserve the memory meaning."""

    return " ".join(str(value or "").split()).casefold()


def canonical_hash(value: str) -> str:
    """Return the deterministic V2 duplicate key for one body."""

    return stable_digest(canonical_text(value))


@dataclass(frozen=True)
class V2DedupMatch:
    """One scoped candidate returned by :class:`V2SemanticDeduplicator`."""

    atom: MemoryAtom
    similarity: float
    exact: bool = False

    @property
    def memory_id(self) -> str:
        return self.atom.memory_id

    def to_dict(self) -> dict[str, object]:
        # This is intentionally body-free: native callers can use the match
        # for a decision without receiving another content read oracle.
        return {
            "memory_id": self.atom.memory_id,
            "atom_id": self.atom.atom_id,
            "share_group_id": self.atom.share_group_id,
            "status": self.atom.status,
            "kind": self.atom.kind,
            "similarity": self.similarity,
            "exact": self.exact,
        }


class V2SemanticDeduplicator:
    """Find semantic candidates inside one explicit V2 group scope.

    ``MemoryReadScope.agent_instance_id`` is deliberately empty by default,
    which means all members of the *same* group are visible to this service.
    The group and workspace remain mandatory, so another group cannot enter
    the candidate set.
    """

    DEFAULT_STATUSES = frozenset({"active", "low_confidence", "conflicted"})

    def __init__(
        self,
        store: MemoryAtomStore,
        scope: MemoryReadScope,
        *,
        backend: EmbeddingBackend | None = None,
        threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
        statuses: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(store, MemoryAtomStore):
            raise TypeError("v2 semantic dedup requires MemoryAtomStore")
        if not isinstance(scope, MemoryReadScope):
            scope = MemoryReadScope.from_value(scope)  # type: ignore[arg-type]
        if not scope.share_group_id:
            raise ValueError("semantic dedup requires share_group_id")
        if not 0.0 <= float(threshold) <= 1.0:
            raise ValueError("semantic dedup threshold must be between 0 and 1")
        self.store = store
        self.scope = scope
        self.backend = backend or HashBackend()
        self.threshold = float(threshold)
        self.statuses = frozenset(
            str(item) for item in (statuses or self.DEFAULT_STATUSES)
        )

    def _atoms(self, statuses: Iterable[str] | None = None) -> list[MemoryAtom]:
        allowed = frozenset(str(item) for item in (statuses or self.statuses))
        atoms = self.store.list_atoms(
            scope=self.scope,
            include_building=True,
        )
        return [
            atom for atom in atoms
            if atom.share_group_id == self.scope.share_group_id
            and atom.status in allowed
        ]

    def find(
        self,
        text: str,
        *,
        threshold: float | None = None,
        statuses: Iterable[str] | None = None,
    ) -> list[V2DedupMatch]:
        """Return exact and semantic matches in deterministic order."""

        body = str(text or "")
        normalized = canonical_text(body)
        if not normalized:
            return []
        limit = self.threshold if threshold is None else float(threshold)
        if not 0.0 <= limit <= 1.0:
            raise ValueError("semantic dedup threshold must be between 0 and 1")
        atoms = self._atoms(statuses)
        if not atoms:
            return []

        exact_key = canonical_hash(body)
        vector = self.backend.embed_text(body)
        matches: list[V2DedupMatch] = []
        for atom in atoms:
            exact = (
                atom.canonical_hash == exact_key
                or canonical_text(atom.body) == normalized
            )
            if exact:
                similarity = 1.0
            else:
                similarity = cosine_similarity(
                    vector,
                    self.backend.embed_text(str(atom.body or "")),
                )
            if exact or similarity >= limit:
                matches.append(V2DedupMatch(atom, float(similarity), exact))

        matches.sort(
            key=lambda item: (
                not item.exact,
                -item.similarity,
                item.atom.memory_id,
                item.atom.atom_id,
            )
        )
        return matches

    def find_semantic_duplicates(
        self,
        text: str,
        *,
        threshold: float | None = None,
    ) -> list[V2DedupMatch]:
        """Named compatibility seam for callers migrating from V1."""

        return self.find(text, threshold=threshold)


__all__ = [
    "V2DedupMatch",
    "V2SemanticDeduplicator",
    "canonical_hash",
    "canonical_text",
]
