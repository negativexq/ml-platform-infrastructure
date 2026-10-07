# OTel-disabled single-gateway capacity probe — 2026-10-07

Following the [500 RPS SDK A/B](gateway-otel-ab.md), the same disposable clean-candidate
ARM64 gateway receives increasing Go-generated load. Fixed settings: **1 CPU / 1GiB,
12 workers, 15 DB connections, SDK disabled**, concurrency 200, ten-second offer window,
15-second HTTP timeout and bounded 15,000-request client queue. No profiler or production
setting change. The endpoint is not forwarded: a temporary indebted caller returns 429.

| Offered RPS | Completed RPS incl. drain | Completed inside 10s RPS | HTTP request p95 ms | Client queue p95 ms | Gateway CPU cores |
|---:|---:|---:|---:|---:|---:|
| 750 | 654.1 | 643.0 | 359.4 | 1232.5 | 1.00 |
| 1000 | 677.1 | 673.4 | 334.6 | 4344.5 | 0.99 |
| 1500 | 690.8 | 668.1 | 325.1 | 10931.0 | 1.00 |
| 1500 | 606.9 | 582.2 | 447.8 | 13908.7 | 0.99 |

All **47,500 requests returned 429**, with zero 503s, transport errors, dropped,
unissued or canceled requests. All request-integrity checks pass. CPU reaches roughly
one core; most quota periods are throttled. Increasing offered rate from 750 to 1,500
mostly increases queueing rather than throughput. The two 1,500 RPS repeats achieve
**691 and 607 completed RPS**, averaging approximately 649. This is a short observed
**600–700 RPS plateau**, not a proven stable maximum or sustained production capacity.

HTTP request p95 excludes the load generator's queue. At 1,500 offered RPS that queue
has seconds of delay; the apparently modest HTTP p95 does not mean the whole offered
load is handled promptly. Client p95s cannot be added to derive an end-to-end p95.
The full drain is counted in completed RPS, and inside-window completions are reported
separately. Queue retention is bounded; a longer overload would require shedding work.

This test removes SDK work but keeps no-op wrappers/calls, Python HTTP handling,
auth/cache, PostgreSQL limiter and its client stack. It does not isolate residual CPU
into DB client, HTTP, scheduler or kernel, and it does not prove 600–700 successful
model inferences per second. DB metrics disappear when SDK is disabled. Cluster-wide
lock waiter samples remain in JSON, but are not exact endpoint lock times.

All temporary caller buckets, revoked-key fixtures and probe resources were cleaned up;
the temporary key is revoked. Installed API/gateway/reconciler remain at the same image
and settings with telemetry active. No Go gateway or optimization was installed.
[Raw request, queue, cgroup and image evidence](gateway-otel-off-capacity.json).

Replay on the explicit acceptance lab (requires its generator image and DB secrets):

```bash
.venv/bin/python scripts/controlplane_gateway_worker_check.py \
  --cases 12:15 12:15 12:15 12:15 \
  --otel-modes off off off off \
  --rps 750 1000 1500 1500 --queue-size 15000 \
  --out /tmp/gateway-otel-off-capacity-repeat.json
```

The next useful optimization experiment is to retain core request/usage/limiter counters
while reducing diagnostic phase/DB histogram recording, or separately toggle traces
and metrics. Repeat with telemetry enabled and sustained traffic before deciding on a
Go gateway. Turning off production telemetry is not the outcome of this probe.
