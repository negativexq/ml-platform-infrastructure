# Unprofiled gateway OTel A/B — 2026-10-07

Same clean ARM64 candidate image, disposable 1 CPU / 1GiB gateway, 12 workers,
15 DB connections, Go generator, 500 offered RPS for ten seconds, concurrency 200.
Six sequential samples use SDK modes `on off off on on off`, three per mode.
There is no profiler, source optimization or production deployment change.

`off` sets `OTEL_SDK_DISABLED=true`: providers remain no-op; conditional SQLAlchemy,
HTTPX, DB, phase and runtime instrumentation is bypassed. Native FastAPI telemetry
wrappers and application calls to no-op instruments can remain. This measures the
combined effect of the application's SDK switch, not exporter traffic alone or the
removal of every OTel import/call. JSON logs, SQL, auth/cache and budgets remain active.
`on` exports metrics every second and samples traces at 1% as in the earlier probes.

| Order | SDK mode | Completed RPS incl. drain | Client request p95 ms | Gateway CPU seconds / 5,000 requests |
|---|---|---:|---:|---:|
| 1 | on | 405.3 | 637.5 | 12.13 |
| 2 | off | 500.0 | 268.4 | 5.56 |
| 3 | off | 500.0 | 130.9 | 4.15 |
| 4 | on | 397.3 | 715.2 | 12.47 |
| 5 | on | 469.6 | 485.9 | 10.68 |
| 6 | off | 500.0 | 97.5 | 4.35 |

All **30,000 requests returned 429**: no transport errors, 503s, dropped or unissued
requests. On-mode limiter phase counts were exactly 5,000 per sample; off-mode pod
metrics were absent. All integrity checks passed; the temporary key was revoked and
its bucket/resources deleted. Installed API/gateway/reconciler retain their settings.

Mean completed RPS: **424.1 on / 500.0 off**. Pod CPU per request: **2.352ms on /
0.938ms off**, a **60.1% reduction** (includes readiness and diagnostic endpoint cost).
This demonstrates material SDK/instrumentation overhead under this fixture, independent
of the much heavier CPU profiler. It does not identify how much is metrics vs traces,
SQL hooks vs phase hooks, or exporter work. Those require separate controlled toggles.

Off-mode throughput is **capped by the offered 500 RPS**, so this is not its maximum
capacity. Request p95 is from Go HTTP timings; client scheduling/queue delay is separately
recorded in JSON, and neither should be confused with gateway phase histograms.
Single-node laptop resource contention and short samples produce visible run variation.
These are **rejection-only** results, not 500 successful model inferences or sustained
production capacity. Do not disable production observability based on this experiment;
reduce or sample diagnostic work while preserving operational signals, then repeat A/B.

[Raw results and image digests](gateway-otel-ab.json).
[Profiler findings and limitations](gateway-cpu-profile.md).
[Replay procedure](../../../local-verification.md#gateway-cpu-and-otel-comparison-probes--2026-10-07).
