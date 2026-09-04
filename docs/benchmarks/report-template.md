# Token evidence report — [UTC date]

Command: `python scripts/benchmark_usage_telemetry.py --workspace [authorized workspace] --window-days 7 --sync --output [report.json]`

Sample status: `[available | unsupported | no_sample | error]`  
Scope: local telemetry only; conversion estimates respect requested scope,
host measurements remain host-wide.

Measured host tokens: provider-reported input `[n]`, output `[n]`; derived
total `[n]`. These are not billing, cost, or savings results.

Estimated MemoryGuard conversion units: baseline `[n]`, delivered `[n]`,
change `[n]`, ratio `[n | unavailable]`. Basis: `mg_deterministic_unit`; not
provider billing tokens.

Unsupported measurement basis: `[reason or provider + reason]`. Do not replace
with zero. `unsupported` and `no_sample` must fail a required-sample run.

Conclusion: `[Only state claims supported by report JSON. If no_sample, say
"暂无本地样本 / no local sample" and make no usage or savings claim.]`
