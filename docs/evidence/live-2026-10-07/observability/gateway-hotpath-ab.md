# Gateway hot-path observability profile — 2026-10-07

The gateway now has normal, diagnostic and acceptance profiles. The implementation
separates tracing from metrics; disabling generic SQL tracing retains custom DB metrics.
See [configuration, counter migration and histogram semantics](../../../observability.md#gateway-telemetry-profiles).
API/reconciler SQL tracing defaults remain unchanged.

Normal defaults: metrics on when configured; root traces 1%; SQL spans off; latency
histograms independently sampled at 20%; exact request/red/error/unit/token counters;
30-second export; duplicate native HTTP metrics and operation spans off. Caller is
removed from the operational request counter, while caller usage/red/error counters
remain available separately. Diagnostic enables SQL tracing, 100% root trace sampling
and latency observations, operation spans/native metrics and a one-second export.
Explicit overrides/standard environment precedence are documented in the configuration.

## Controlled live experiment

Same actual-Dockerfile candidate image, **1 CPU / 1GiB gateway, 12 workers, 15 DB
connections**, Go generator with 2 CPU / 384MiB, 500 offered RPS, ten-second window,
concurrency 200 and 5,000 bounded queue. No CPU profiler. Each sample has 100 warm-up
429 requests outside the measurement. Temporary indebted caller; no model forwarding.

| Variant | Generic SQL spans | Root trace ratio | Caller on operational requests | Export ms | Latency observations | Native HTTP metrics/operation spans |
|---|---|---|---|---:|---|---|
| full | on | 1% | on | 1,000 | 100% | on/on |
| sql-off | off | 1% | on | 1,000 | 100% | on/on |
| trace5 | off | 5% | on | 1,000 | 100% | on/on |
| lean-labels | off | 5% | off | 1,000 | 100% | on/on |
| normal-interval | off | 5% | off | 30,000 | 100% | on/on |
| normal | off | 1% | off | 30,000 | 20% | off/off |
| off | SDK disabled | none | not exported | not exported | none exported | off/off |

"Full" is the full instrumentation reference, **not 100% traces**: the earlier
benchmark already used 1%. The normal step combines several changes; it is not an
isolated test of latency sampling or duplicate HTTP metrics. Additional full/normal
brackets retain visibility into laptop variance. All variants use the same new image;
the new caller-specific rejection counter is present in all enabled variants.

| Order | Variant | Completed RPS incl. drain | Gateway timed CPU seconds | HTTP request p95 ms | Mean worker queue ms | Mean loop lag ms |
|---:|---|---:|---:|---:|---:|---:|
| 1 | full | 477.2 | 10.19 | 467.4 | 283.84 | 1.86 |
| 2 | sql-off | 500.0 | 6.46 | 63.1 | 4.64 | 0.70 |
| 3 | trace5 | 488.4 | 6.85 | 327.7 | 27.12 | 1.39 |
| 4 | lean-labels | 500.0 | 7.82 | 233.0 | 40.92 | 1.08 |
| 5 | normal-interval | 487.6 | 7.24 | 394.7 | 65.80 | 2.31 |
| 6 | normal | 492.2 | 6.93 | 467.6 | 119.89 | 3.18 |
| 7 | off | 499.9 | 5.09 | 234.1 | 0.00 | 0.00 |
| 8 | normal | 500.0 | 5.31 | 90.2 | 5.70 | 0.88 |
| 9 | full | 489.2 | 10.23 | 370.9 | 147.82 | 2.47 |
| 10 | normal | 500.0 | 4.96 | 36.8 | 1.86 | 0.54 |

Full references: **477–489 RPS**, mean timed CPU **10.21s**.
Normal: **492–500 RPS**, mean timed CPU **5.73s**,
an observed **43.8% reduction** versus full.
Off: approximately 500 RPS, 5.09s. Throughput near 500 is capped by offered load,
not a maximum-capacity measurement. Means of separate p95s are not pooled quantiles;
normal request p95 ranged from 36.8 to 467.6ms, so the evidence does not establish a
stable latency SLO or improvement in every individual run.

The first SQL-only toggle achieved 500 RPS and 6.46 CPU seconds while custom DB,
limiter and phase histograms remained **100% recorded**, at the same one-second export
and 1% trace sampling. This supports generic SQL tracing as a valuable first removal.
The 5% trace, label and interval steps are single samples with substantial variability;
they do not prove an independent speedup for each change. One caller per fixture does
not establish high-cardinality production scalability.

## Telemetry integrity and limits

All **50,000 timed requests returned 429**, without 503s, transport errors, dropped,
unissued or canceled requests. Every enabled sample exported an exact request delta
of 5,000. Normal phase counts are consistent with independent 20% sampling; they are
not treated as exact request counts. Off-mode metrics were absent.

Nine explicit sampled-parent warm-up traces were read back from real Tempo. Each had
a server span. Full references had 16 SQL spans in their cold-cache warm-up; SQL-off
and normal had zero SQL spans. Custom DB/limiter/phase metrics still arrived. This
validates parent propagation and the SQL switch; it is not a census of all sampled
traces or a promise of zero collector/exporter loss.

Prometheus validated the caller-usage migration queries. The last normal pod exported
5,100 operational requests **without caller** and 5,100 caller-specific rejections
**with caller** (including 100 warm-ups). Exact units/token preservation is also covered
by tests with latency sampling disabled. Diagnostic still supports full SQL tracing.

Before/after forced metric flushes and scrape settling happen **outside the timed CPU
window**. Ten seconds is shorter than the normal 30-second export period, so these
results do not amortize full steady-state exporter cost. Longer enabled-telemetry runs
must include multiple normal export cycles. Cgroup CPU includes readiness/diagnostics
and any periodic export within the window; it is not Python business-logic CPU alone.
Gauge peaks depend on export cadence and should not be read as exact occupancy peaks.

This is a short single-node ARM64 rejection-only test, **not successful inference
capacity, a closed sustained 500 RPS gate or a final clean release artifact**. Installed
API/gateway/reconciler remain unchanged at 2/2 ready; probe resources and caller buckets
were deleted and the test API key revoked. Production telemetry was not disabled.

## Verification and artifacts

- Full Python suite: **773 passed, 100 skipped**; targeted telemetry/gateway tests: 61 passed, 5 skipped.
- Ruff/scoped mypy (15 sources), Helm lint and diagnostic-profile render passed.
- Actual control-plane Dockerfile build and `pip check` passed. Source is uncommitted;
  this candidate is not a final release/runtime/scan/SBOM gate.
- [Raw samples](gateway-hotpath-ab.json), [aggregate summary](gateway-hotpath-summary.json),
  [build/source provenance](gateway-hotpath-image.json).
- [Trace checks](gateway-hotpath-telemetry-check.json), [caller metric schema checks](gateway-hotpath-schema-check.json).

Repeat with a current profile-capable image digest and a new evidence path:

```bash
.venv/bin/python scripts/controlplane_gateway_worker_check.py \
  --cases 12:15 12:15 12:15 12:15 12:15 12:15 12:15 12:15 12:15 12:15 \
  --telemetry-variants full sql-off trace5 lean-labels normal-interval normal off normal full normal \
  --probe-image localhost:5201/mlp-controlplane@sha256:d86dc44f230d9c34154ac209649cab0579661990cb8b7122e9261ce817083d5b \
  --out /tmp/gateway-hotpath-ab-repeat.json
```

Use `scripts/controlplane_gateway_telemetry_check.py --report <report> --out <new-path>`
to read warm-up traces and counter schemas on the same lab; Prometheus must retain the
last normal pod's recent metric window. Keep a 500 RPS Go client and compare enabled
telemetry before any Go gateway rewrite. Next: sustained normal-profile load over
multiple export cycles, successful upstream/streaming acceptance and the clean release rerun.
