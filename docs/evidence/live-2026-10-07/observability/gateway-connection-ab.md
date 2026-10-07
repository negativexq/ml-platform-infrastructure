# Gateway keep-alive A/B/C

Recorded 2026-10-07 on single-node ARM64 kind / Kubernetes 1.32.
**B passed the five-minute zero-error rejection fixture at 500 RPS.** A reproduced a
reset on a reused connection idle for 4,998.77 ms, near the server's 5-second keep-alive
boundary. C incurred substantial connection churn, errors and CPU saturation.

| Measurement | A: default idle 90 s | B: idle 2 s | C: keep-alive off |
| --- | ---: | ---: | ---: |
| HTTP 429 completed | 149,999 | 150,000 | 146,609 |
| Transport errors | 1 | 0 | 856 |
| Queue drops | 0 | 0 | 2535 |
| Completed RPS including drain | 499.99 | 500.00 | 471.86 |
| Inside-window RPS | 500.00 | 500.00 | 471.64 |
| Gateway mean CPU cores | 0.643 | 0.595 | 0.980 |
| Generator mean CPU cores | 0.093 | 0.090 | 0.349 |
| HTTP p50 / p95 / p99 (ms) | 1.54 / 341.79 / 442.77 | 1.42 / 197.96 / 361.89 | 409.71 / 522.28 / 628.41 |
| Client queue p95 (ms) | 0.601 | 0.009 | 10682.026 |
| New / reused connection acquisitions | 2,247 / 147,753 | 3,340 / 146,660 | 146,609 / 0 |
| Exact DB error counter | 0 | 0 | 0 |
| Strict traffic gate | **FAIL** | PASS | **FAIL** |

Each arm planned/offered 150,000 requests over 300 seconds. All observed HTTP statuses
were 429; no 503s, unscheduled requests or canceled-before-start requests occurred.
Accounting passed for all arms, including C: 146,609 completed + 856 transport failures
+ 2,535 queue drops = 150,000. The comparison runner correctly exits 1 and records
`passed: false` because A and C failed; B's individual integrity/availability/capacity
gates are all true. Exact request-counter deltas match completed responses in all arms.
Ten regular metric-counter increments were observed per arm; DB error counters were zero.

## Connection evidence

A's reset occurred at 205.13 seconds, during headers, after 1.542 ms. `GotConn` reported
`reused: true`, `was_idle: true`, idle duration 4,998.768585 ms. Go's default idle timeout
was 90 seconds while the probe's Uvicorn server used its default 5 seconds. This is
strong evidence for a close/reuse race at the server keep-alive boundary, supported by
the clean B result with client idle timeout 2 seconds. No packet capture was collected;
this does not prove the origin of every previous unclassified failure.

B retained reuse for 146,660/150,000 acquisitions (97.77%) and opened 3,340 connections.
The recommended setting for this acceptance client is `--idle-conn-timeout 2s`, preserving
keep-alive while expiring idle entries before this server's 5-second boundary. For other
targets, select an idle timeout below the actual server/proxy timeout. Go's generic CLI
default was preserved so A remains reproducible; no production deployment was changed.

C reported 856 `network` errors. All 16 bounded samples were headers-phase failures
before acquiring a connection, starting near 89.84 seconds. This differs from A's reused
socket reset. The normalized report does not retain the underlying errno, so port,
conntrack or other network resource exhaustion is not proven. With 146,609 fresh
connections and no reuse, gateway CPU averaged 0.98 core; client queue p95 grew to
10.68 seconds and the runner required a drain. Keep-alive off is unsuitable here.

## Scope and reproducibility

The same diagnostic Go image, connection callbacks, 1-CPU/1-GiB gateway, normal OTel
profile, 12 workers, DB capacity 15, Go concurrency 200, queue 5,000 and 15-second HTTP
timeout were used in all arms. Arms ran sequentially on fresh disposable gateway pods.
No application retry was added; Go transport retains its ordinary internal behavior.
Connection callback counts are acquisitions, not unique socket IDs. CPU and latency
comparisons have shared-lab/order variance; one run per arm is not a general performance
confidence interval. In particular, A/B p95 differences cannot all be attributed to the
rare reset. CPU measurement includes each arm's actual load/drain window.

Threads remained at 18 after warm-up. Installed API/gateway/reconciler images/settings
were preserved. Probe pods/jobs/config/secret were removed and the API key revoked.
The fixture intentionally rejects every request: it does not prove 500 successful
inference RPS, successful streaming or final clean release-artifact capacity.

- [Raw A/B/C report](gateway-connection-ab.json)
- [Summary](gateway-connection-ab-summary.json)
- [Diagnostic image and source provenance](gateway-connection-ab-provenance.json)
- [Earlier two normal-profile soaks](gateway-normal-soak.md)

```sh
.venv/bin/python scripts/controlplane_gateway_worker_check.py \
  --cases 12:15 12:15 12:15 --telemetry-variants normal normal normal \
  --connection-modes baseline short-idle no-keepalive \
  --rps 500 500 500 --duration-seconds 300 \
  --probe-image localhost:5201/mlp-controlplane@sha256:d86dc44f230d9c34154ac209649cab0579661990cb8b7122e9261ce817083d5b \
  --loadgen-image localhost:5201/mlp-loadgen@sha256:71b96f3f0651ce343ac6e2b41fe009ec270909c52378f39e68bfba258de0cfc5 \
  --out /tmp/gateway-connection-ab-new.json
```

For a B-only repeat, use one case/variant/RPS and `--connection-modes short-idle`.
Go race tests, vet, Linux ARM64 compilation and transport-mode fixtures passed before
this run. The source image is an uncommitted diagnostic candidate, not a release image.
