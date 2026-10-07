# Normal OTel profile: two five-minute gateway soaks

Recorded 2026-10-07 on Kubernetes 1.32 / ARM64 kind. Both runs maintained approximately
500 completed RPS across regular metric exports, with stable sampled resources. **Both
strict zero-error traffic gates failed:** 300,000 attempts produced 299,997 HTTP 429
responses and three transport failures. The failures are preserved in the evidence.

Follow-up: the [connection A/B/C](gateway-connection-ab.md) reproduced a reused socket
reset at the 5-second server idle boundary and passed the B-only short-idle fixture.
The original failures below remain preserved.

## Results

| Measurement | First run | Repeat |
| --- | ---: | ---: |
| Offer window | 300 s | 300 s |
| Planned / offered / started | 150,000 each | 150,000 each |
| Completed HTTP 429 | 149,999 | 149,998 |
| Completed RPS, including drain | 500.00 | 499.94 |
| Transport errors | 1, unclassified | 2, connection reset |
| HTTP 503 / dropped / unscheduled / cancelled | 0 each | 0 each |
| Mean pod CPU | 0.556 core | 0.532 core |
| CPU per completed request | 1.113 ms | 1.064 ms |
| HTTP p50 / p95 / p99 | 1.31 / 157.88 / 353.83 ms | 1.28 / 175.02 / 384.87 ms |
| Client queue p95 / p99 | 0.009 / 31.28 ms | 0.010 / 275.50 ms |
| Sampled cgroup memory range | 242.5–252.7 MiB | 232.2–239.7 MiB |
| Lifetime cgroup memory peak | 253.7 MiB | 240.8 MiB |
| Steady Python threads after warm-up | 18 | 18 |
| Observed periodic counter increments | 10 | 10 |
| Exact DB error counter delta | 0 | 0 |
| At least 99% offered RPS completed inside window | PASS | PASS |
| Strict zero-error traffic gate | **FAIL** | **FAIL** |

Mean CPU is approximately half the one-core limit at 500 RPS in this rejection fixture;
this does not establish maximum capacity. The tail remains substantial despite a low
median. Threads start at seven and grow as the executor initializes, then remain at 18.
Five-second samples show no progressive thread or memory growth over these five-minute
windows. Cgroup memory includes file cache; the lifetime peak also includes startup.
This is not a long-term leak guarantee.

## Bottleneck signals and errors

| Sampled mean | First run | Repeat |
| --- | ---: | ---: |
| Limiter worker queue | 16.14 ms | 17.88 ms |
| Limiter work | 5.02 ms | 4.35 ms |
| DB acquire | 0.519 ms | 0.371 ms |
| DB statement execution | 1.298 ms | 1.116 ms |
| Event-loop lag | 1.112 ms | 1.073 ms |

Latency metrics use independent random 20% observations. These are means over different
populations and cannot be added to reconstruct HTTP p95. SQL execution includes network
and lock time. A cluster-wide ungranted-lock snapshot every 200 ms saw maxima of 11 and
10 waiters; it does not measure endpoint-specific lock duration.

The first generator did not classify its single failure. The repeat added bounded,
normalized error diagnostics without changing retries, HTTP timeouts, connection pools
or load scheduling. Its two failures were `connection_reset` while awaiting headers,
at 165.36 and 175.37 seconds, after only 0.291 and 0.863 ms. Neither returned an HTTP
status. These observations do not point to a DB timeout or CPU saturation, but they do
not establish the reset's origin. The next investigation is the client/server socket,
keep-alive and connection-reuse path; the strict availability gate remains open.

## Telemetry and scope

Each timed run included ten observed periodic counter increments at the normal
30-second export interval. Before/after forced flushes were outside the timed CPU
window. Exact exported request-counter deltas match the observed response counts in
both runs. The repeat's read-only [telemetry check](gateway-normal-soak-telemetry-check.json)
passed Prometheus usage queries and real Tempo HTTP-span/absent-SQL-span checks while
explicitly retaining `traffic_gate_passed: false`. This is response-counter consistency,
not proof that every possible trace/export was delivered.

The disposable gateway had one CPU, 1 GiB, 12 executor workers and DB capacity 15.
The Go generator had two CPUs, 384 MiB, concurrency 200, queue capacity 5,000,
15-second HTTP timeout and a 30-second drain. A deliberately indebted caller ensured
429-only traffic throughout; 100 warm-up responses were outside the window. Normal
telemetry retained metrics, 1% root tracing, custom DB/limiter/phase histograms sampled
at 20%, SQL auto-tracing off and duplicate native HTTP metrics/operation spans off.

No request in this fixture forwarded successful inference to KServe. Successful
upstream/streaming load, longer soaks and a final clean release-artifact rerun remain
open. Installed API/gateway/reconciler deployments retained their earlier images and
settings. Test pods/jobs/config/credentials were removed and the test key revoked.

The probe used control-plane candidate
`localhost:5201/mlp-controlplane@sha256:d86dc44f230d9c34154ac209649cab0579661990cb8b7122e9261ce817083d5b`.
The repeat used the diagnostic loadgen image described in
[build provenance](gateway-soak-loadgen-provenance.json); Go race tests, vet and Linux
ARM64 compilation passed. This candidate is not the final clean release artifact.

## Evidence and reproduction

- [First raw run](gateway-normal-soak.json)
- [Repeat raw run, including reset timing](gateway-normal-soak-repeat.json)
- [Aggregate measurements](gateway-normal-soak-summary.json)
- [Previous short profile comparison](gateway-hotpath-ab.md)

Use a fresh output path; existing evidence is intentionally not overwritten:

```sh
.venv/bin/python scripts/controlplane_gateway_worker_check.py \
  --cases 12:15 --telemetry-variants normal --rps 500 --duration-seconds 300 \
  --probe-image localhost:5201/mlp-controlplane@sha256:d86dc44f230d9c34154ac209649cab0579661990cb8b7122e9261ce817083d5b \
  --loadgen-image localhost:5201/mlp-loadgen@sha256:1460cea65aba8763039a3d31dac2ba13db61c0a2496909cdc3fcfcd284080f22 \
  --out /tmp/gateway-normal-soak-new.json
```

The runner exits nonzero when the traffic gate fails, preserves the completed report
and cleans its resources. `--allow-availability-failure` on the separate telemetry
checker permits read-only inspection of a failed run; it never overrides that gate.
