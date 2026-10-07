# Go inference gateway

The control-plane chart defaults to this gateway. API, reconciler and migrations remain
Python. The previous Python gateway is retained as `gateway.runtime=python` for rollback.
See [migration evidence](../../docs/evidence/live-2026-10-07/observability/gateway-go-migration.md).

The public URLs, API-key hash/revocation checks, project/endpoint authorization, five-second
key/route caches, 60-second last-used touches, error envelope, request IDs, rate-limit/model
headers and endpoint body/readiness limits retain the Python contract. OIDC validates
issuer, audience, required expiry/issued-at/subject and asymmetric signatures against
cached discovery/JWKS. The caller identity hashes `issuer + sub`; project membership and
platform-admin subjects use the existing schema.

Functions, MLflow/V2 and OpenAI chat forwarding stream responses without forwarding caller
credentials. Connect timeout is five seconds; other upstream operations/read gaps use the
endpoint timeout, allowing active streams to outlast it. Cancellation and incomplete bodies
abort the stream. LLM reservations use the conservative Python byte estimator; complete,
valid usage refunds/charges the shared bucket. Missing, malformed or incomplete usage
retains at least the reservation. Actual GPU/vLLM acceptance remains a separate gate.

`pgxpool` defaults to 15 connections, with 12 limiter workers, a three-second acquire/connect
bound and checkout pre-ping. The limiter retains ordered bucket locks, DB clock, all-or-none
admission, debt/refund, two-second lock and three-second statement timeouts. Readiness checks
exact schema head `0021` and the full least-privilege gateway DB-role contract. Migrations
run from the Python image; update and verify this head when releasing a new schema.

Only the **normal** OTel profile is supported: independent metrics/traces export, 1% parent-based
root traces, 20% latency samples, exact request/usage/unit/token counters, DB/limiter/phase and
pool metrics, and 30-second metric export. Server/upstream spans propagate trace context and
baggage. SQL auto-spans are absent. `OTEL_*_EXPORTER=none` disables that signal;
`OTEL_SDK_DISABLED=true` disables both. No configured OTLP endpoint means no exporter.
Pod identity comes from the downward API, with explicit resource attributes taking priority.
Python diagnostic mode remains available through the rollback runtime.

`/healthz` and `/readyz` listen on 8081. Diagnostic 8082 is disabled unless
`CP_GATEWAY_PROBE_ENABLED=true`; it is for disposable acceptance fixtures and is not a Service
port. Scratch/non-root image preStop uses `/mlp-gateway-go --drain-wait=10s`, followed by
20 seconds of graceful shutdown within the chart's 30-second termination period.

```sh
make gateway-go-check
CP_GATEWAY_GO_IMAGE=your-registry/mlp-gateway-go:release \
CP_GATEWAY_GO_PLATFORM=linux/arm64 make cp-gateway-go-release-check
```

The release command runs race tests/vet, builds from a digest-pinned Go builder, rejects
HIGH/CRITICAL Trivy findings and emits image metadata plus a CycloneDX SBOM. Set the chart's
`gateway.image.repository` and `gateway.image.digest` to the verified artifact. ARM64 is the
recorded live target; other architectures require their own runtime acceptance.

`TestPostgresContract` requires explicit `CP_GO_TEST_CONFIG`: a mode-0600 JSON file with
`Admin` and `Gateway` DSNs. It uses unique temporary buckets/membership subjects, validates
two-pool concurrency, debt/refund, real lock timeout/recovery and scoped membership reads,
and cleans its fixtures. Never put these DSNs in reports or command arguments.
