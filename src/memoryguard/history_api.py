"""Stable, MCP-ready handlers for progressive raw-history retrieval.

The MCP server may import ``TOOL_DEFINITIONS`` and call ``handle_history_tool``
after deriving the trusted agent identity from its binding.  These handlers do
not trust an arbitrary requested ``agent_instance_id``.
"""
from __future__ import annotations

from typing import Any

from .conversation_history import ConversationHistoryStore, HistoryAccessResolver


TOOL_DEFINITIONS = [
    {"name": "memoryguard_history_search", "description": "Search local history; returns IDs and summaries, never raw turns.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "scope": {"type": "object"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}, "required": ["query"]}},
    {"name": "memoryguard_history_timeline", "description": "Read a bounded preview around one authorized turn.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "anchor_turn_id": {"type": "string"}, "scope": {"type": "object"}, "radius": {"type": "integer"}}, "required": ["session_id", "anchor_turn_id"]}},
    {"name": "memoryguard_history_read", "description": "Read one authorized session or turn; raw content is final-stage only.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "turn_id": {"type": "string"}, "scope": {"type": "object"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}}},
    {"name": "memoryguard_history_extract_preview", "description": "Preview long-term-memory candidates with history evidence; never writes memory.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "turn_ids": {"type": "array", "items": {"type": "string"}}, "scope": {"type": "object"}}, "required": ["session_id"]}},
]


def handle_history_tool(name: str, args: dict[str, Any], *, workspace: str, trusted_agent_id: str) -> dict[str, Any]:
    scope = HistoryAccessResolver(workspace).resolve(trusted_agent_id, args.get("scope"))
    store = ConversationHistoryStore(workspace)
    if name == "memoryguard_history_search":
        return store.search(scope, str(args.get("query") or ""), limit=args.get("limit", 20), offset=args.get("offset", 0))
    if name == "memoryguard_history_timeline":
        return store.timeline(scope, str(args.get("session_id") or ""), str(args.get("anchor_turn_id") or ""), radius=args.get("radius", 4))
    if name == "memoryguard_history_read":
        return store.read(scope, session_id=str(args.get("session_id") or ""), turn_id=str(args.get("turn_id") or ""), limit=args.get("limit", 100), offset=args.get("offset", 0))
    if name == "memoryguard_history_extract_preview":
        return store.extract_preview(scope, str(args.get("session_id") or ""), turn_ids=args.get("turn_ids"), limit=args.get("limit", 20))
    raise ValueError("unknown_history_tool")
