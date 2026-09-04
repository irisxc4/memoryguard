# Token evidence benchmark

Run from repository root:

```powershell
python scripts/benchmark_usage_telemetry.py --workspace . --window-days 7 --sync --output docs/benchmarks/latest.local.json
```

Output is JSON. `--sync` explicitly imports available local host reports and
may update `.memoryguard/usage_telemetry.sqlite`. Without `--sync`, a missing
database returns `sample_status: "no_sample"` without creating one. Unknown or
unsupported bases return `sample_status: "unsupported"`. Add `--require-sample`
when a proof run must fail if no usable local sample exists.

## Evidence path

`ContextEngine` candidate/delivered body units → Hook final wrapper units →
`usage_events` → `get_usage_summary` → GUI Token page and this report.

The first three steps are existing production paths. This script only syncs or
reads them; it does not calculate a second token model.

Claim rules:

- `measured`: provider-reported input/output token counts. The report also
  shows a derived total; it makes no billing, cost, user-count, or savings
  claim.
- `estimated`: MemoryGuard `mg_deterministic_unit` baseline/delivered values.
  Its ratio is an estimate, never provider billing tokens.
- `unsupported`: unknown or unsupported measurement basis is excluded and is
  not reported as zero usage or zero savings. `--require-sample` fails for
  `unsupported` and `no_sample`.

The report stores neither conversation text nor raw source paths. Do not use
downloads, installs, stars, or impressions as a proxy for users.

`local-sample.none.json` is a real read-only run from this checkout with no
workspace telemetry database. It is evidence of no available local sample,
not a claim of zero usage.
