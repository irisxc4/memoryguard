# GUI V2 + CodeGraph complete migration candidate

Status: **release blocked**  
Snapshot date: 2026-08-11

This note describes the post-v0.6.2 GUI V2 / CodeGraph migration candidate. It is not a published release and must not be treated as a `production_complete` declaration.

## What is complete

- GUI canonical registry is `162 / 162` implemented with `0` retired, `0` neutral, and `0` blocker operations.
- MCP is `57 / 57` implemented and Hook is `1 / 1` implemented. The only remaining retired native surfaces are six legacy CLI compatibility commands: `plan`, `apply`, `verify`, `undo`, `import`, and `gc`.
- Governance conflict/quarantine/supersede/neuron/rollback flows use V2-native stores and governed receipts.
- History discovery/backfill, import, maintenance GC, hook mode/uninstall, bounded source/raw previews, and request-queue compatibility are on native V2 routes.
- GUI and Knowledge background work use durable `TaskRun` state with persisted progress, cancellation, recovery, and bounded shutdown.
- Release rollback stores the previous target in the Content Plane as a held blob/occurrence; public/task receipts expose stable references and digests instead of source bodies or backup paths.
- CodeGraph schema and native operations preserve trusted scope, revision, tombstone, outbox, source role, provenance, source maps, semantic edge context, and bounded query/path/explain/affected behavior without storing source bodies.
- The Graphify integration extracts embedded Python-hosted HTML/JavaScript and reconstructs the production GUI semantic chain through `control_handler`, `handler_api`, `api_surface`, and `surface_native` edges.

## Verification

- Full MemoryGuard test suite: **1761 passed / 0 failed** across all 186 `tests/test_*.py` files.
- Reference Audit / SQLite / outbox / task lifecycle focused gate: **106 passed**.
- Transport / registry / readiness focused gate: **104 passed**.
- Graphify embedded/provenance focused gate: **15 passed**.
- GUI launcher / acceptance smoke: **12 passed**.
- Real MemoryGuard Graphify export for `interactive.py`, `knowledge_gui.py`, `gui.py`, `surfaces.py`, and `native_ports.py`: **1570 nodes / 2078 edges / 0 diagnostics**.
- Verified production-only chain: `加入书架 → addBook → knowledge_add → GuiOperationSpec:knowledge_add → gui_knowledge_command`.
- Clean MemoryGuard sdist → wheel rebuild and isolated install: PASS; rebuilt wheel contains no `__pycache__`, `.pyc`, or `.pyo` entries.
- Runtime/cutover AST import audit: no `SharedMemoryStore` or `ManagedStore` imports.

Native registry digest:

`45d1b85b4353532a843baf5da2a5e0752d2e7d60b9455ede6e69c8e39ddc3ee1`

## Graphify packaging hard stop resolved

Graphify Core is now maintained in-tree under `src/memoryguard/graphify_core/`, derived from Graphify 0.9.19 under the preserved MIT license. MemoryGuard no longer requires an external `graphifyy` distribution, a PATH-visible `graphify` CLI, a private wheel, or changes to `site-packages/graphify`.

The built-in engine owns structural source discovery/extraction, embedded Python-hosted HTML/JavaScript projection, source-role provenance, and the body-free MemoryGuard metadata export. CodeGraph query/path/explain/affected remain MemoryGuard-native operations rather than delegated Graphify product commands.

Packaging verification for the built-in core:

- MemoryGuard wheel contains `memoryguard/graphify_core` source plus `LICENSE.graphify.txt` and `NOTICE.md`.
- Wheel metadata contains no `graphifyy` dependency.
- A fresh venv installing only the MemoryGuard wheel and its declared parser dependencies can import `memoryguard.graphify_core` and build a CodeGraph while no `graphifyy` distribution is installed.
- The GUI production `build_codegraph` TaskRun succeeds through the built-in core and persists a READY CodeGraph.

This removes the former external-Graphify release blocker; any remaining release blockers must be assessed independently.
