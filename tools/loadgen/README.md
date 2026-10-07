# mlp-loadgen

Open-loop POST load with `net/http` and `golang.org/x/time/rate`. Requires Go 1.26.

```bash
make loadgen-test
make loadgen-build
# Full endpoint URLs; token comes from the environment, not process arguments.
MLP_LOADGEN_TOKEN="$MLP_API_KEY" /tmp/mlp-loadgen \
  --rps 500 --duration 10s --concurrency 200 --queue-size 400 \
  --targets "$GATEWAY_A/v1/project/endpoint/invoke,$GATEWAY_B/v1/project/endpoint/invoke" \
  --payload @request.json
```

Flags: `--rps`, `--duration`, `--concurrency`, `--queue-size`, `--targets`, `--payload`,
`--stream`, `--timeout`, `--drain-timeout`, `--idle-conn-timeout`,
`--disable-keep-alives`, `--connection-diagnostics`. Payload is JSON or `@file`. `--stream` observes
first body data and drains the stream; include the protocol's streaming option in the
payload separately. Redirects are reported rather than followed. TLS verification stays
on. Targets cannot contain credentials in URL userinfo. Reports identify targets by
index, retaining input order, and do not print tokens, payloads or URLs.

JSON distinguishes:

- Planned slots, actually offered requests, unscheduled slots, queue drops, canceled
  queued requests, started requests, completed bodies, transport errors and HTTP statuses.
- Offering-window RPS, completions within that window, and completed RPS over the actual
  run including drain. A run does not shorten its denominator when the last slot finishes early.
- Scheduling lag (planned slot → enqueue), queue delay (enqueue → worker start), and
  request latency (worker start → full body/error). Percentiles include sample counts.
  Request latency includes HTTP transport pool wait. Dropped requests have no latency sample.
- First body byte for `--stream` (not model TTFT); partial stream read errors remain
  transport errors even when response headers were HTTP 200.
- Per-target completion rate and process CPU seconds/cores, peak RSS and GOMAXPROCS.
  CPU/RSS come from process rusage on Linux/macOS; other platforms currently report zero.
  They do not replace container cgroup CPU/throttling measurements.

A bounded queue keeps client memory bounded. Overflow increments `queue_dropped`; the
scheduler keeps offering independently of worker completion. Maximum planned requests
are 1,000,000. Exit 1 means transport errors, dropped/unscheduled/canceled requests;
HTTP status counts need a separate acceptance assertion (429 can be expected). Exit 2
means invalid CLI/configuration. Successful output does not establish admitted inference
throughput. Latency samples are retained in memory up to the planned-request bound.

Tests exercise round-robin 200/429 distribution, visible overload/queueing, full streaming
and partial-stream timeout; `go test -race` and `go vet` pass. Local 500 RPS fixture
[smoke evidence](../../docs/evidence/live-2026-10-07/observability/go-loadgen-smoke.json)
is not a Kubernetes gateway result or an A/B comparison with the Python generator.

A multi-stage scratch Dockerfile copies the static non-root binary and TLS CA bundle.
Image build/scan/SBOM and same-fixture in-cluster Python/Go comparison remain open:

```bash
docker build -t mlp-loadgen:dev tools/loadgen
```


Transport failures include normalized `transport_error_kinds` and up to 16
`transport_error_samples` with headers/body phase, elapsed request time and position
in the load window. Error messages, target URLs, payloads and credentials are omitted.
Classification does not enable retries or change HTTP timeouts/connection behavior.
A closed-local-port fixture verifies connection-refused classification; body timeouts
remain counted as transport errors even if response headers already carried a status.

Connection experiments preserve Go's default transport unless explicitly overridden.
`--idle-conn-timeout 2s` expires idle connections after two seconds; zero preserves the
Go default (currently 90 seconds). `--disable-keep-alives` opens a fresh connection per
request. Neither setting adds application retries. The transport may perform its normal
internal retries; this tool does not add an idempotency key to its POST requests.

`--connection-diagnostics` adds the same `httptrace.GotConn` observer to every experiment
arm. Reports count acquisitions of new/reused connections, not packets or unique socket
identities. Bounded failure samples include whether a connection was acquired, reused,
idle and its observed idle duration. Callback counts may exceed started requests when
an internal transport retry acquires another connection. The observer adds measurement
cost, so compare equally instrumented arms rather than attributing differences from
older noninstrumented reports entirely to transport changes.
