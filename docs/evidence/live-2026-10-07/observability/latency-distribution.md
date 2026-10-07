# Existing OTel latency distribution — 2026-10-07

This inspects **recorded telemetry**, without a new benchmark, profiler or gateway rewrite.
The original single-gateway Go sample completed 5,000 requests: 737 status 200, 4,263
status 429. Historical cumulative Prometheus snapshots use the same instance and epochs
1791332668 → 1791332688 as the saved limiter phase evidence. Gateway duration count is
exactly 5,000. This is summed request wall time, not the pod's elapsed CPU time.

Mean application request duration: **490.33 ms**. Native HTTP duration: **490.65 ms**.
Status-separated native HTTP means: **1431.22 ms for 200**, **328.05 ms for 429**.

| Component | Mean ms per all 5,000 requests | Share of summed gateway wall time |
| --- | --- | --- |
| Waiting for limiter executor worker | 228.64 | 46.63% |
| Upstream response headers, amortized over all calls | 191.58 | 39.07% |
| Limiter return/scheduling/observation residual | 34.74 | 7.08% |
| Actual limiter work | 25.69 | 5.24% |
| Streaming, amortized over all calls | 7.33 | 1.49% |
| Auth + route cache + authorization | 0.020 | 0.004% |
| Other gateway residual | 2.34 | 0.48% |

Upstream headers average **1299.75 ms among the 737 forwarded requests**; multiplying
by 737/5000 gives the amortized value above. Streaming is weighted in the same way.
The limiter's 289.06 ms total contains queue + work; it is not added a second time.
First-chunk and upstream-total timers overlap headers/stream and are excluded from this
partition. Residuals are unnamed timing differences, not verified CPU categories.
Percentages describe summed concurrent request durations, not fractions of pod wall time.

Within the **25.69 ms limiter work**, recorded DB execution contributes approximately
**21.26 ms/request** (about three statements) and acquisition **2.17 ms/request**.
The remaining approximately **2.25 ms** includes unmeasured fetch/commit/application/
observation effects. DB series include a small amount of readiness/background work;
this nested allocation is approximate and must not be added to the outer work duration.
DBAPI execution includes server/network/lock/scheduling waits, not only SQL CPU.

The rejection-only [worker experiment](worker-experiment.md) independently removes all
upstream calls: 12 workers still spend 131–177 ms in worker queue, 16–24 ms doing limiter
work; 24/15 raises work to ~50 ms and acquisition to ~21 ms with 18 errors each time.
24/30 removes acquisition errors, but query execution rises to ~45 ms per request and
throughput does not improve. This supports executor/service-rate and DB contention as
latency bottlenecks; it does not establish a Python function CPU breakdown.

## CPU attribution boundary

Current OTel spans/histograms measure elapsed duration. cgroups show pod CPU usage and
throttling, but do not divide CPU into FastAPI, SQLAlchemy, OTel, serialization or thread
scheduling. Those percentages require a CPU profiler. High CPU plus these latency
histograms does not establish that Python business logic is the primary CPU consumer,
or quantify the gain from a Go rewrite. No Go gateway PoC or profiler was run here.

## Trace store issue discovered during inspection

Tempo's 512MiB-limited container terminated with **OOMKilled / exit 137**, finished at
2026-10-07T00:48:20Z, during historical trace inspection. The exact triggering allocation
was not profiled. It recovered to 1/1 at unchanged limits; Prometheus retained the
historical metrics used above. The subsequent [limited-concurrency replay](tempo-tuning.md) passes the recorded
historical window; sustained/larger-dataset query capacity remains open. Existing saved trace summaries remain evidence of prior export.
No memory-limit increase or collector/pod restart was requested by this inspection.

[Machine-readable snapshots and calculation](latency-distribution.json) retain metric
labels, deltas, weighting and scope. Temporary query port forwards were stopped.

Subsequent evidence: [CPU profile](gateway-cpu-profile.md) identifies telemetry and
DB client CPU candidates, with 3.3-times profiler slowdown and 81% attribution coverage.
This follow-up does not turn the wall-time percentages above into CPU shares.
