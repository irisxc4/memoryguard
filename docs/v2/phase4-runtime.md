# Phase4-C runtime context contract

`runtime_v2.context_engine.ContextEngine` is the shadow V2 bootstrap entry for
context construction. It is storage-agnostic and receives retrieval/planner
ports; it does not import or construct `SharedMemoryStore`, history, knowledge,
MCP, GUI, or host-hook implementations.

## Request and packet

`ContextRequest` preserves `task`, `project_hint`, `max_items`, `max_chars`,
`max_tokens`, `read_path`, and the trusted agent/project/group/provider/runtime
identity. Conflicting aliases fail closed. `ContextPacket.to_dict()` keeps the
compatibility envelope:

```text
mandatory, relevant, knowledge, reference_only,
budget, effective_agent, receipts, ready, state, status, error?
```

Only rule candidates may occupy `mandatory`. Scope is checked against trusted
request identity; mismatches are omitted and recorded. History/tool output is
never emitted. Knowledge and reference-only text remains non-authoritative;
reference items carry `trust=reference_only`.

## Budget and safety

`ContextBudget` has independent mandatory and optional item/character/token
caps plus per-item caps. A deterministic `TokenCounter` is injectable; the
default counts Unicode code points so repeated builds are byte-for-byte stable.
Mandatory sensitive, raw, or over-limit candidates fail closed. Optional
candidates are packed by layer and deterministic priority/score/id order,
deduplicated by content digest, and omitted with a receipt when a boundary is
reached. Receipts contain hit/reason/scope/evidence references and token/char
cost; evidence bodies are not copied.

## Readiness

The engine defaults to `ready=false`, `state=V2_BUILDING`. It can build a
shadow packet for acceptance comparison, but does not activate a runtime read
path. Unknown state or missing trusted identity returns a blocked packet. The
acceptance fixture (`scripts/accept_v2_phase4.py`) emits one JSON object and
checks mandatory equivalence, zero scope leaks, recall, lower token cost,
determinism, and the non-ready state.
