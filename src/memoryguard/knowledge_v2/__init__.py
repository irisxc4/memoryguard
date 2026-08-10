"""V2 knowledge reference adapters."""

from .adapter import (
    KnowledgeAdapter,
    KnowledgeReference,
    KnowledgeV2Adapter,
    ReferenceOnlyKnowledgeAdapter,
)
from .service import (
    KNOWLEDGE_CANDIDATE_META,
    KNOWLEDGE_CANDIDATE_SCHEMA,
    KNOWLEDGE_CANDIDATE_SCHEMA_VERSION,
    KNOWLEDGE_CANDIDATE_TABLE,
    KnowledgeV2NativeService,
    KnowledgeV2ReadonlyService,
    KnowledgeV2SchemaError,
    KnowledgeV2ServiceError,
    KnowledgeV2Unavailable,
)

__all__ = [
    "KnowledgeAdapter",
    "KnowledgeReference",
    "KnowledgeV2Adapter",
    "KnowledgeV2NativeService",
    "KnowledgeV2ReadonlyService",
    "KnowledgeV2SchemaError",
    "KnowledgeV2ServiceError",
    "KnowledgeV2Unavailable",
    "KNOWLEDGE_CANDIDATE_META",
    "KNOWLEDGE_CANDIDATE_SCHEMA",
    "KNOWLEDGE_CANDIDATE_SCHEMA_VERSION",
    "KNOWLEDGE_CANDIDATE_TABLE",
    "ReferenceOnlyKnowledgeAdapter",
]
