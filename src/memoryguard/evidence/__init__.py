"""V2 evidence reference storage.

The evidence domain deliberately stores references, revisions and digests only.
Source text belongs to the content plane and is never accepted by this API.
"""

from .store import Evidence, EvidenceLink, EvidenceReadScope, EvidenceStore, validate_authority

__all__ = ["Evidence", "EvidenceLink", "EvidenceReadScope", "EvidenceStore", "validate_authority"]
