"""Reference-only adapter over the V2 Content Plane.

The adapter is intentionally *not* a compatibility wrapper around the V1
``KnowledgeStore``.  It reads only ``content.db`` through an exact
``ContentReadScope`` and emits the four fields that a recall layer may safely
consume: ``summary``, ``ref``, ``hash`` and ``trust=reference_only``.  A
denied/missing row returns the same empty result, so callers cannot use the
adapter as an existence oracle.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from ..content.store import ContentReadScope, ContentStore, UNKNOWN_ACL


_FORBIDDEN = frozenset(
    {
        "body",
        "text",
        "raw",
        "content",
        "document",
        "document_body",
        "conversation",
        "conversation_body",
        "transcript",
        "full_transcript",
        "authority",
        "authorities",
        "ownership",
        "owner",
        "acl",
        "scope",
        "namespace_id",
        "workspace_id",
        "agent_instance_id",
        "project_ref",
        "provider",
        "share_group_id",
        "sensitivity",
        "policy_class",
        # Locators and candidate metadata are not public references.  These
        # names are deliberately denied even when a legacy row happens to
        # contain them alongside a safe label.
        "path",
        "secret",
        "secrets",
    }
)

_SAFE_LABEL_KEYS = ("title", "summary", "label", "section", "chapter")

# A reference envelope must never become a side channel for the metadata that
# the V2 read plane intentionally excludes.  Match complete tokens (rather
# than replacing broad substrings) so ordinary labels such as ``contention``
# and ``pathway`` remain intact; unsafe values are omitted as a whole.
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:body|text|content|path|secret|secrets|authority|authorities|"
    r"ownership|owner|acl)(?![A-Za-z0-9])"
)

# Absolute paths, UNC paths, URI/file references, and slash-delimited path
# shapes are all implementation details rather than safe human labels.
_PATH_VALUE_RE = re.compile(
    r"(?ix)"
    r"(?:^[a-z]:[\\/]"
    r"|^\\\\"
    r"|^/(?:[^/\s]+(?:[/\\]|$))"
    r"|(?:^|[\s\"'])(?:file|https?|s3)://"
    # Reject every RFC-style URI scheme when it is the complete public value,
    # not only the small set of schemes listed above.  ``data:`` URIs are
    # also sensitive even though they do not require ``//``.
    r"|^[A-Za-z][A-Za-z0-9+.-]*://"
    r"|^data:"
    # A relative path is only unambiguous when it is the complete value or
    # has a filename extension.  This keeps ordinary labels such as
    # ``A/B testing`` intact while still rejecting ``src/module.py``.
    r"|^(?:\.\.?[/\\]|(?:[^/\s\"']+[/\\])+[^/\s\"']+)$"
    r"|(?:^|[\s\"'])(?:[^/\s\"']+[/\\])+[^/\s\"']+\.[A-Za-z0-9]{1,16}(?=$|[\s\"'])"
    r")"
)


def _safe_public_text(value: Any, *, limit: int = 2048) -> str:
    """Return one safe, bounded public label or an empty value.

    This is intentionally reject-on-match rather than a string replacement:
    redaction must not manufacture a misleading title, and ordinary labels
    that merely contain a longer unrelated word should survive unchanged.
    """

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or _SENSITIVE_VALUE_RE.search(text) or _PATH_VALUE_RE.search(text):
        return ""
    return text[: max(1, int(limit))]


@dataclass(frozen=True)
class KnowledgeReference:
    """Safe recall envelope; deliberately no body/authority fields."""

    summary: str
    ref: str
    hash: str
    trust: str = "reference_only"

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", _safe_public_text(self.summary))
        object.__setattr__(self, "ref", _safe_public_text(self.ref))
        object.__setattr__(self, "hash", _safe_public_text(self.hash))
        object.__setattr__(self, "trust", "reference_only")

    @property
    def source_digest(self) -> str:
        return self.hash

    def to_dict(self) -> dict[str, str]:
        return {
            "summary": self.summary,
            "ref": self.ref,
            "hash": self.hash,
            "trust": "reference_only",
        }

    as_dict = to_dict


def _safe_summary(row: Mapping[str, Any]) -> str:
    """Read bounded labels only; never fall back to blob text."""

    for key in _SAFE_LABEL_KEYS:
        value = _safe_public_text(row.get(key))
        if value:
            return value
    raw_locator = row.get("locator_json")
    locator: Mapping[str, Any] | None = None
    if isinstance(raw_locator, Mapping):
        locator = raw_locator
    elif isinstance(raw_locator, str) and raw_locator:
        try:
            parsed = json.loads(raw_locator)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping):
            locator = parsed
    if locator is not None:
        for key in _SAFE_LABEL_KEYS:
            value = _safe_public_text(locator.get(key))
            if value:
                return value
    for key in ("content_role", "object_type"):
        value = _safe_public_text(row.get(key))
        if value:
            return value
    return ""


def _safe_limit(value: Any, default: int = 100) -> int:
    try:
        return max(1, min(1000, int(value)))
    except (TypeError, ValueError):
        return default


class KnowledgeV2Adapter:
    """Strict reference adapter backed only by :class:`ContentStore`."""

    layer = "knowledge"
    trusted = False

    def __init__(
        self,
        store: ContentStore,
        *,
        namespace_id: str | None = None,
        content_role: str = "knowledge",
    ) -> None:
        if not isinstance(store, ContentStore):
            raise TypeError("KnowledgeV2Adapter requires a V2 ContentStore")
        self.store = store
        self.namespace_id = str(namespace_id or "")
        self.content_role = str(content_role or "knowledge")

    @staticmethod
    def _scope(scope: ContentReadScope | None) -> ContentReadScope | None:
        if not isinstance(scope, ContentReadScope):
            return None
        # Every ACL dimension is required.  Empty/UNKNOWN values are denied
        # rather than treated as wildcards, and surrounding whitespace is a
        # conflicting identity rather than a normalizable alias.
        dimensions = (
            scope.namespace_id,
            scope.workspace_id,
            scope.agent_instance_id,
            scope.project_ref,
            scope.provider,
            scope.share_group_id,
            scope.sensitivity,
            scope.policy_class,
        )
        if any(not isinstance(value, str) or value != value.strip() or not value or value == UNKNOWN_ACL for value in dimensions):
            return None
        return scope

    @staticmethod
    def _reference(row: Mapping[str, Any]) -> KnowledgeReference:
        # Occurrence IDs are opaque references.  They are sufficient for a
        # later governed resolver but reveal no path, namespace or body.
        ref = str(row.get("occurrence_id") or "")
        digest = str(row.get("canonical_hash") or "")
        return KnowledgeReference(summary=_safe_summary(row), ref=ref, hash=digest)

    def read(
        self,
        scope: ContentReadScope | None,
        *,
        query: str = "",
        limit: int = 100,
        occurrence_id: str | None = None,
    ) -> tuple[dict[str, str], ...]:
        """Return reference-only rows for one exact ContentReadScope.

        Every ACL predicate is explicit and the namespace is part of the
        predicate.  Missing/denied rows deliberately return ``()`` without a
        status or count marker (existence-neutral failure).
        """

        checked = self._scope(scope)
        if checked is None:
            return ()
        namespace_id = self.namespace_id or checked.namespace_id
        if namespace_id != checked.namespace_id:
            return ()
        predicates = [
            "o.active=1",
            "b.namespace_id=?",
            "o.workspace_id=?",
            "o.agent_instance_id=?",
            "o.project_ref=?",
            "o.provider=?",
            "o.share_group_id=?",
            "o.sensitivity=?",
            "o.policy_class=?",
        ]
        params: list[Any] = [
            namespace_id,
            checked.workspace_id,
            checked.agent_instance_id,
            checked.project_ref,
            checked.provider,
            checked.share_group_id,
            checked.sensitivity,
            checked.policy_class,
        ]
        if self.content_role:
            predicates.append("o.content_role=?")
            params.append(self.content_role)
        if occurrence_id:
            predicates.append("o.occurrence_id=?")
            params.append(str(occurrence_id))
        query_text = str(query or "").strip()
        if query_text:
            # Search labels/locator only.  Never use ``b.text`` as a search
            # source; doing so would turn this metadata adapter into a body
            # disclosure path.
            predicates.append("(so.title LIKE ? OR o.locator_json LIKE ?)")
            like = f"%{query_text}%"
            params.extend((like, like))
        sql = (
            "SELECT o.occurrence_id,o.source_object_id,o.occurrence_key,o.locator_json,"
            "o.content_role,o.sensitivity,o.policy_class,o.provider,"
            "b.canonical_hash,so.title,so.object_type "
            "FROM content_occurrences o JOIN content_blobs b ON b.blob_id=o.blob_id "
            "JOIN source_objects so ON so.source_object_id=o.source_object_id "
            "WHERE " + " AND ".join(predicates) + " ORDER BY o.occurrence_id LIMIT ?"
        )
        params.append(_safe_limit(limit))
        try:
            with self.store.connection() as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception:
            # A partially configured content plane is equivalent to no
            # configured knowledge for a read adapter; no error detail leaks.
            return ()
        return tuple(self._reference(dict(row)).to_dict() for row in rows)

    retrieve = read
    list = read
    read_candidates = read

    def get(self, occurrence_id: str, scope: ContentReadScope | None) -> dict[str, str] | None:
        rows = self.read(scope, occurrence_id=str(occurrence_id), limit=1)
        return rows[0] if rows else None

    get_reference = get

    def __call__(self, scope: ContentReadScope | None, **kwargs: Any) -> tuple[dict[str, str], ...]:
        return self.read(scope, **kwargs)


KnowledgeAdapter = KnowledgeV2Adapter
ReferenceOnlyKnowledgeAdapter = KnowledgeV2Adapter


__all__ = [
    "KnowledgeAdapter",
    "KnowledgeReference",
    "KnowledgeV2Adapter",
    "ReferenceOnlyKnowledgeAdapter",
]
