# P7 SQLite maintenance contract

This document defines the small physical-maintenance surface that sits below
the P7 reference audit.  It is intentionally not a garbage collector: no
operation removes business rows or content blobs.

## Read-only storage report

`StorageReporter.report()` opens the exact database path with SQLite `mode=ro`.
The resulting `StorageReport` records logical pages (`page_count -
freelist_count`), derived/allocated pages, free pages, page size, WAL/SHM
bytes, journal mode, `auto_vacuum`, `integrity_check`, foreign-key errors,
schema fingerprint, per-table row counts/digests, and a small ACL mode sample.
Missing files, symlink/reparse components, partial schemas, and paths outside
`WorkspaceV2Layout` fail closed.  Reporting does not create a file, checkpoint
a WAL, or issue a write PRAGMA.

## Automatic maintenance

`auto_maintenance(..., apply=False)` is the default dry-run.  An explicit
`apply=True` may issue only:

```sql
PRAGMA wal_checkpoint(PASSIVE);
PRAGMA optimize;
```

It never rewrites rows and does not imply cutover authority.

## Incremental vacuum

`incremental_vacuum(..., apply=False)` is also dry-run by default.  Applying it
requires all of the following:

* `PRAGMA auto_vacuum` is `INCREMENTAL`;
* a trusted `MaintenanceContext` and an unexpired lease owned by that actor;
* manifest state is exactly `V2_ACTIVE` and `expected_generation` is a strict
  CAS match;
* explicit `writer_quiesced=True` and `outbox_drained=True` evidence.

`V2_BUILDING` and `V2_READY` are rejected.  The operation invokes only
`PRAGMA incremental_vacuum`, and never deletes a business record.

## Deep compaction

`deep_compact(..., apply=False)` is opt-in and forbidden for `manifest.db`.
An apply run uses this sequence:

1. validate the same ACTIVE/CAS/lease/quiescence/outbox gates and run
   `integrity_check` plus `foreign_key_check`;
2. issue `PRAGMA wal_checkpoint(TRUNCATE)` and require a drained WAL;
3. run `VACUUM INTO` a unique temporary file in the same directory;
4. validate the temporary schema fingerprint, row counts, row digests,
   integrity/FK result, and ACL mode sample;
5. close every SQLite handle, copy a backup, and atomically replace the source;
6. validate the replacement and restore the backup on any fault.

The temporary and backup files are removed on both success and failure.  The
same-directory replacement and close-before-rename rule are required on
Windows, where an open SQLite handle otherwise prevents `rename`/`unlink`.

The implementation follows SQLite's documented [`PRAGMA`](https://www.sqlite.org/pragma.html)
semantics and [`VACUUM INTO`](https://www.sqlite.org/lang_vacuum.html) contract.
