# Single-gateway CPU profile — 2026-10-07

A disposable ARM64 gateway, 1 CPU / 1GiB, 12 executor workers and 15 DB connections
received Go-generated 500 offered RPS for ten seconds. Each of three samples completed
5,000 requests, all 429, with no 503 or transport failures. The caller was deliberately
indebted; endpoint limits and installed deployments were preserved.

The middle sample used Yappi 1.7.6, CPU clock, all Python threads, builtins disabled.
The two surrounding samples used the same diagnostic image with profiling inactive.
The image adds only Yappi to the clean candidate image; no production dependency changed.
See [image recipe, aggregation and provenance](gateway-cpu-summary.json) and
[all function statistics and samples](gateway-cpu-profile.json).

| Sample | Completed RPS including drain | Gateway cgroup CPU seconds |
|---|---:|---:|
| Before, profiler inactive | 473.3 | 10.54 |
| Profiler active | 143.8 | 35.12 |
| After, profiler inactive | 475.5 | 9.67 |

Profiling slowed throughput by approximately 3.3 times. Consequently these percentages
identify candidates for investigation; they are not exact normal-runtime CPU shares.

| Module group | Exclusive attributed CPU seconds | Share of attributed CPU |
|---|---:|---:|
| OTel SDK/instrumentation and FastAPI telemetry | 9.91 | 35.2% |
| psycopg | 7.85 | 27.9% |
| SQLAlchemy | 3.58 | 12.7% |
| FastAPI/Starlette excluding telemetry | 2.08 | 7.4% |
| Gateway application/domain/adapters | 1.07 | 3.8% |
| Async/thread runtime | 0.78 | 2.8% |
| HTTP transport/server | 0.59 | 2.1% |
| Other/stdlib/probe | 2.27 | 8.1% |

Exclusive `tsub` values are grouped by module; inclusive parent times are not summed.
28.13 seconds are attributed out of 34.72 seconds of profiled thread CPU (81% coverage).
The remaining 6.59 seconds are unassigned, not proven to be entirely profiler overhead.
Unprofiled native callees can be included in their Python caller's exclusive cost.
CPU clock excludes sleeping time: a separate 200ms sleeping-thread sanity check consumed
less than 50ms CPU and collected three threads. See [Yappi API documentation](https://github.com/sumerc/yappi/blob/master/doc/api.md).

Largest exclusive functions include psycopg prepared-query handling, connection wait
machinery and command execution, plus OTel attribute hashing/cleaning, span creation and
metric aggregation. `Connection.wait` here measures active CPU driving the client;
it does not measure elapsed PostgreSQL lock waiting. Driver function invocation counts
are not exact SQL wire-round-trip counts.

The profiled window recorded approximately 25 metric measurements and 14 histogram
records per request. Trace sampling is 1%; metrics still record every request. This
makes an **unprofiled OTel on/off comparison** the next test before deciding whether to
reduce diagnostic metric frequency or build a Go gateway. This profile alone proves
neither a Go speedup nor that all DB time is client CPU.

All disposable pods/jobs/config/credential resources were deleted, the test key revoked
and its caller bucket removed. API, gateway and reconciler remained 2/2 ready at their
previous image and settings. No optimization or Go gateway PoC was applied.

The subsequent [unprofiled OTel A/B](gateway-otel-ab.md) confirms combined SDK overhead
without profiler slowdown. It does not isolate the metric-versus-trace contribution.
