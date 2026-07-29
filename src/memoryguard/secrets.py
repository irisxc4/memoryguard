"""Secret detection and redaction for publish paths."""

from __future__ import annotations

import re
from typing import Any

from .auto_organizer import SECRET_PATTERNS

# Full PEM private-key block for redaction (BEGIN-only pattern stays in SECRET_PATTERNS
# so AutoOrganizer quarantine still triggers on the BEGIN line).
_PEM_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    r"(?:\r?\n(?!-----END )[^\r\n]+)+"
    r"\r?\n-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
    re.MULTILINE,
)

# BEGIN without matching END: redact from BEGIN through EOF or next delimiter.
_INCOMPLETE_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    r"(?:(?!-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----).)+",
    re.DOTALL,
)

_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (SECRET_PATTERNS[0], "credential"),
    (SECRET_PATTERNS[1], "aws_key"),
    (SECRET_PATTERNS[2], "github_pat"),
    (SECRET_PATTERNS[3], "github_oauth"),
    (SECRET_PATTERNS[4], "openai_key"),
    (SECRET_PATTERNS[5], "slack_token"),
    (_PEM_PRIVATE_KEY_BLOCK, "private_key"),
    (_INCOMPLETE_PEM_PRIVATE_KEY, "private_key"),
]


_REDACTED_LABEL = re.compile(r"\[REDACTED:(\w+)\]")


def labels_in_redacted_text(text: str) -> list[str]:
    """Return secret labels already present as ``[REDACTED:<label>]`` placeholders."""
    if not text:
        return []
    return sorted(set(_REDACTED_LABEL.findall(text)))
    """Detect secret-like substrings in *text* using shared SECRET_PATTERNS."""
    if not text:
        return []
    labels = (
        "credential",
        "aws_key",
        "github_pat",
        "github_oauth",
        "openai_key",
        "slack_token",
        "private_key",
    )
    matches: list[dict[str, Any]] = []
    for index, pattern in enumerate(SECRET_PATTERNS):
        label = labels[index] if index < len(labels) else f"secret_{index}"
        for match in pattern.finditer(text):
            matches.append({
                "pattern": pattern.pattern[:80],
                "label": label,
                "match": match.group()[:80],
                "start": match.start(),
                "end": match.end(),
            })
    return matches


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Replace secret matches with ``[REDACTED:<label>]`` placeholders."""
    if not text:
        return text, []
    labels: list[str] = []
    result = text
    for pattern, label in _REDACT_PATTERNS:
        def _replacer(match: re.Match[str], *, _label: str = label) -> str:
            labels.append(_label)
            return f"[REDACTED:{_label}]"

        result = pattern.sub(_replacer, result)
    return result, sorted(set(labels))
