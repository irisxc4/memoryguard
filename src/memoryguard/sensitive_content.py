"""共享敏感内容检测。

所有远程 Provider、知识库、长期记忆隔离和 GUI 脱敏必须使用同一组模式。
"""

from __future__ import annotations

import re
from typing import Pattern


NAMED_SENSITIVE_PATTERNS: tuple[tuple[str, Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "aws_secret_key",
        re.compile(
            r"(?i)aws_secret_access_key[\"']?\s*[:=]\s*[\"']?"
            r"[A-Za-z0-9/+=]{40}"
        ),
    ),
    (
        "generic_secret",
        re.compile(
            r"(?i)(api[_-]?key|apikey|token|secret|password|passwd|pwd)"
            r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9+/=_\-]{8,}"
        ),
    ),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]+")),
    (
        "bearer_token",
        re.compile(r"(?i)(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    ),
    (
        "basic_auth",
        re.compile(r"(?i)authorization\s*:\s*basic\s+[A-Za-z0-9+/]+=*"),
    ),
    (
        "cookie_session",
        re.compile(
            r"(?i)(?:cookie\s*:|set-cookie\s*:|session(?:id|_id)?\s*[=:])"
            r"[^\r\n]{8,}"
        ),
    ),
    (
        "private_key",
        re.compile(
            r"-----BEGIN\s+(?:(?:RSA|EC|DSA|OPENSSH)\s+)?PRIVATE\s+KEY-----",
            re.IGNORECASE,
        ),
    ),
    (
        "connection_string",
        re.compile(
            r"(?i)(?:mongodb(?:\+srv)?|postgres(?:ql)?|redis|rediss|amqp|mysql)"
            r"://[^\s\"']+"
        ),
    ),
    (
        "jwt",
        re.compile(
            r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
        ),
    ),
)

SENSITIVE_PATTERNS: tuple[Pattern[str], ...] = tuple(
    pattern for _, pattern in NAMED_SENSITIVE_PATTERNS
)


def find_sensitive_pattern(text: str) -> str:
    """返回首个命中的模式名，未命中返回空串。"""
    for name, pattern in NAMED_SENSITIVE_PATTERNS:
        if pattern.search(text or ""):
            return name
    return ""


def contains_sensitive_content(text: str) -> bool:
    return bool(find_sensitive_pattern(text))


def redact_sensitive_content(text: str) -> str:
    """将所有敏感值替换成带类型的固定标记。"""
    value = text or ""
    for name, pattern in NAMED_SENSITIVE_PATTERNS:
        value = pattern.sub(f"[REDACTED:{name}]", value)
    return value
