# Python versus Go gateway PoC

Recorded 2026-10-07 on Kubernetes 1.32 / ARM64 kind. The function/classic-model Go
PoC is implemented in `services/gateway-go`; the installed gateway remains Python.
**At 500 rejection RPS, Go used 70.7% less gateway CPU per completed request.** It also
completed 1,000 and 1,500 offered RPS without errors/drops in these 60-second windows.
Python saturated its one-core limit and dropped offered traffic at those higher rates.

| Offered RPS | Runtime | Completed RPS incl. drain | CPU cores | HTTP p95 | Cgroup memory after | Drops / unissued | Gate |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 500 | python | 499.99 | 0.622 | 250.93 ms | 190.5 MiB | 0 / 0 | PASS |
| 500 | go | 499.99 | 0.182 | 1.48 ms | 9.1 MiB | 0 / 0 | PASS |
| 1,000 | python | 554.42 | 0.998 | 407.99 ms | 252.0 MiB | 21,718 / 1 | **FAIL** |
| 1,000 | go | 1000.00 | 0.332 | 2.25 ms | 8.5 MiB | 0 / 0 | PASS |
| 1,500 | go | 1499.87 | 0.483 | 21.56 ms | 13.3 MiB | 0 / 0 | PASS |
| 1,500 | python | 518.44 | 0.990 | 545.20 ms | 197.7 MiB | 53,670 / 0 | **FAIL** |

All completed timed responses were HTTP 429. There were no transport errors or 503s
in these six windows. The 500 comparison passed all traffic gates. The higher-load
comparison correctly retains `passed: false` and exits 1 because both overloaded Python
arms failed; neither failure is relabeled as acceptance. Actual inside-window Python
throughput was 551.35 RPS at 1,000 offered and 518.83 at 1,500 offered; Go was 999.98
and 1,499.75. Drain durations are included in completed-RPS and CPU denominators.

At 1,000 offered, Python completed 38,281 of 60,000 planned slots, dropped 21,718 and
left one unissued. At 1,500 offered, it completed 36,330 of 90,000 and dropped 53,670.
Every slot is accounted for. Client queue p95 grew to several seconds under Python
overload; HTTP p95 excludes that queue and therefore must not be read as end-to-end
latency. Go completed all 30,000 / 60,000 / 90,000 slots at the three rates.

## Comparable work and telemetry

Every arm used normal OTel, a fresh 1-CPU / 1-GiB gateway pod, 12 limiter workers,
15 DB connections and the same Go loadgen (2 CPUs / 384 MiB, concurrency 200,
queue 5,000, HTTP timeout 15 seconds). Client keep-alive remained enabled with idle
timeout **2 seconds**, below both servers' 5-second timeout: the selected B setting.

Both stacks read real API keys/endpoints from the same PostgreSQL runtime-role schema,
verify key hashes, enforce project/endpoint scopes, use five-second key/route caches,
throttle last-use writes and execute the same three-statement limiter transaction.
The embedded CTE matches Python after parameter notation changes. Go retained ordered
inserts/row locks, DB clock after lock acquisition, all-or-none admission, shared debt
and lock/statement timeouts. Per-checkout pre-ping was retained; there is no added SQL
batch/statement-cache optimization in this PoC. Runtime/driver choices still differ:
this measures Go + pgx + manual OTel versus Python + SQLAlchemy/psycopg + FastAPI OTel,
not an isolated language/compiler speed ratio. Cache misses use a joined route query
in Go rather than Python's repository/ORM lookups.

Normal telemetry remains enabled in both: exact counters, parent-based 1% tracing,
independent random 20% latency metrics, custom phase/DB/limiter metrics and 30-second
exports, with generic SQL spans and duplicate native HTTP metrics off. Two observed
regular counter increments occurred per 60-second run. Exact request-counter deltas
match completed responses in every arm; exact DB error counters stayed zero.

At 500 RPS, sampled mean limiter worker queue was 27.15 ms in Python versus 0.163 ms
in Go; limiter work was 7.33 versus 1.02 ms. At 1,500, Go's queue/work means rose to
2.33 / 1.74 ms while still completing all requests. Go DB query timing includes result
scanning; Python DBAPI timing excludes result fetching. Means, sampled histogram
percentiles and HTTP percentiles have different populations and are not additive.

The [live telemetry check](gateway-runtime-telemetry-check.json) passed for both 500
arms: real Tempo server spans, absent SQL spans, linked upstream client spans for the
function smoke, caller-free operational counters and separate caller usage counters.
Go counters include 30,105 requests (load + 100 warm-ups + 5 contract checks) and 30,104
red/error usage events (the real 200 smoke is excluded). This checks recorded cumulative
counter consistency, not proof that every possible trace/export was delivered.

## Correctness evidence and limits

Before timed load, each of the six arms passed a real function request through KServe
with HTTP 200, request ID preservation and rate-limit headers, plus missing-token /
wrong-secret 401 and wrong-operation / unknown-endpoint 404 checks. These single
positive requests are functional forwarding smoke, **not successful-inference load**.
Timed callers were deliberately placed in debt; no timed request reached an upstream.

Go race/vet tests passed. Real PostgreSQL tests used two Go pools to contend for one
caller budget (exactly one admission across 100 concurrent attempts), then checked
shared debt/refund, duplicate rejection, bounded row-lock failure, recovery, gateway
role/schema readiness and the real function route. Unit tests cover project/endpoint
refusals, expiry/revocation and cache expiry, readiness/body limits, fail-closed errors,
streaming/cancellation and incomplete upstream body transport failure.

The order was Python→Go at 500 and 1,000, Go→Python at 1,500. One sequential sample per
runtime/rate is subject to shared-lab/order variance; these are not confidence intervals.
Cgroup memory includes file cache and differs with the scratch Go image/Python image;
it is not RSS or long-term leak evidence. Go's maximum was **not** found: 1,500 is the
highest tested load, not its capacity ceiling. These are 60-second windows, not a Go
five-minute soak or a final release artifact/target-architecture gate.

OIDC callers and LLM reservation/settlement are outside the PoC and fail closed. Its
upstream deadline differs from HTTPX's per-read timeout; slow-stream timeout, complete
contract/DB outage/HA parity and successful inference load remain migration gates.
The [component contract](../../../../services/gateway-go/README.md) lists these gaps.
The image is a static host-compiled scratch candidate; the source Dockerfile release
build, image scan/SBOM and production switching are not established by this experiment.

The evidence supports continuing the Go gateway migration work on a measured basis.
It does not support switching the installed gateway before the remaining contract and
successful-upstream tests. API/reconciler remain Python.

## Evidence and reproduction

- [500 RPS raw comparison](gateway-runtime-500.json)
- [1,000 / 1,500 RPS raw comparison, including Python failures](gateway-runtime-high-load.json)
- [Summary and derived CPU values](gateway-runtime-summary.json)
- [Go image/source provenance](gateway-go-provenance.json)
- [Prior keep-alive A/B/C](gateway-connection-ab.md)

```sh
.venv/bin/python scripts/controlplane_gateway_worker_check.py \
  --cases 12:15 12:15 --telemetry-variants normal normal \
  --gateway-runtimes python go --connection-modes short-idle short-idle \
  --contract-smoke --rps 500 500 --duration-seconds 60 \
  --probe-image localhost:5201/mlp-controlplane@sha256:d86dc44f230d9c34154ac209649cab0579661990cb8b7122e9261ce817083d5b \
  --go-gateway-image localhost:5201/mlp-gateway-go@sha256:5d4dbb808e939c2980d35d92b318de15c29451e960882e45a33558db06a48ac9 \
  --loadgen-image localhost:5201/mlp-loadgen@sha256:71b96f3f0651ce343ac6e2b41fe009ec270909c52378f39e68bfba258de0cfc5 \
  --out /tmp/gateway-runtime-new.json
```

For higher load, repeat each flag for four cases and use runtimes `python go go python`
and RPS `1000 1000 1500 1500`. Existing output paths are refused. The runner revokes its
unique API key and removes probe pods/jobs/config/secret; installed deployments retain
their images/settings. The PostgreSQL integration forward and temporary test credentials
were removed before benchmarking. No commit or git push was performed.
