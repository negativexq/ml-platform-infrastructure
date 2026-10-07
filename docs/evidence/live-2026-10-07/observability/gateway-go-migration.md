# Go gateway migration — 2026-10-07

The acceptance lab now runs two Go gateway replicas. API, reconciler and migration
processes remain on the existing Python image. The chart defaults to `gateway.runtime=go`
and a separate gateway image; `gateway.runtime=python` retains the shared-image rollback.

Target: Kubernetes 1.32, single-node ARM64 kind. Final gateway artifact:
`localhost:5201/mlp-gateway-go@sha256:6e2d8490ef0b3e473ea4e6c6c1bb12edc601d29475ffa75cd3d7afb61c2f7925`.
The image was built from `services/gateway-go/Dockerfile`, using the digest-pinned
Go 1.26.6 builder, not the previous host-binary PoC recipe. The final scratch image is
6.75 MB (6.44 MiB), non-root, with a CA bundle and read-only filesystem.

## Contract work

- API-key expiry/revocation/hash checks, public endpoint/project scope, cache expiry,
  body limits, readiness, request IDs, headers, public error shapes and forwarding tests.
- OIDC discovery/JWKS and asymmetric signature verification, issuer/audience/expiry/
  issued-at/subject validation, issuer-scoped caller hash, groups/platform admins and
  project membership authorization. Negative token and authorization tests passed.
- OpenAI model override, stream usage request, conservative token reservation and
  completion/debt/refund settlement. **88 Python-generated vectors** match payload semantics,
  budget and rejection text. Invalid/missing usage retains the reservation; bounded
  JSON/SSE metering, overflow and streaming tests passed. A live GPU model was not used.
- Per-operation upstream/read-gap deadlines replace the PoC total deadline. An active
  stream outlasting its one-second timeout passes; header timeout and incomplete-body
  behavior are covered. Caller credentials are not forwarded; W3C context/baggage are.
- Real PostgreSQL tests cover two independent pools, concurrent admission, debt/refund,
  held-row lock timeout/recovery, runtime-role readiness and membership/outsider reads.
  Readiness now requires exactly schema head `0021` and the full least-privilege gateway
  role contract. Database/auth and limiter errors remain redacted fail-closed 503s.

`go test -race ./...`, explicit file-backed PostgreSQL acceptance and `go vet ./...` passed.
Tests use temporary unique bucket/member fixtures; database credentials are not evidence.

## Source image and security

The [first production-source candidate scan](gateway-go-initial-rejected-trivy.json) exposed HIGH/CRITICAL findings in older PoC pins.
It was rejected before rollout. pgx, OTel and affected transitive libraries were upgraded;
the final [Trivy report](gateway-go-release-trivy.json) has **zero HIGH/CRITICAL findings**.
[Source/binary provenance](gateway-go-release-provenance.json) records the runtime source hashes and build metadata.
The [CycloneDX SBOM](gateway-go-release-sbom.cdx.json) records the final artifact. This is
not a claim of zero findings at all severities.

`make cp-gateway-go-release-check` repeats race/vet, source build, severity gate, image
metadata and SBOM. The complete [manual command output artifacts](gateway-go-release-check/image.json)
were produced successfully, including file-backed real PostgreSQL tests. Rebuilding regenerated
the BuildKit attestation (OCI index `038f3618…`), while the runtime manifest remains identical
to the deployed artifact (`680b5187…`); provenance records both. The deployed digest is the
one used for live tests. The existing Python release check remains relevant for API/reconciler/
migrations and the retained Python gateway. Neither command alone replaces live acceptance.

## Recorded live checks

The first source-built candidate (`cca52147…`) passed a
[five-minute normal-OTel soak](gateway-go-migration-soak.json): **150,000/150,000** requests,
500 offered/completed RPS, no transport errors, 503s, queue drops or accounting loss,
**0.153 mean CPU core**, p50 0.885 ms / p95 1.615 ms / p99 26.563 ms. Go loadgen used
keep-alive with **2-second client idle expiry** (B) below the gateway's 5-second timeout.
This was 429-only caller-debt load with real function/refusal smoke calls outside the timed
window, not 500 successful inference RPS. The runtime had 1 CPU / 1 GiB, 12 limiter workers,
15 DB connections, normal OTel (1% traces, 20% latency, 30-second export).
[Trace/counter verification](gateway-go-migration-telemetry.json) passed separately.

The final artifact adds stricter exact-head readiness and automatic Docker target architecture
selection. Its own [final soak](gateway-go-final-soak.json) also passed **150,000/150,000**
requests with no errors/drops/accounting loss, 499.998 completed RPS, **0.157 mean CPU core**,
p95 **1.852 ms**, and approximately 16 MiB end-window cgroup memory. The
[final trace/counter check](gateway-go-final-telemetry.json) passed. These are caller-debt
rejection results; they do not establish successful inference capacity.

The initial Python-to-Go Helm upgrade reached 2/2 ready. A probe setup used the wrong API
Service port, so **no continuous migration-traffic claim** is made for that initial switch.
The corrected [Go rolling restart](gateway-go-rolling.json) delivered **200/200 real function
responses**, no transport failures and 200 matching request IDs/rate-limit headers. That
restart was on the first source-built candidate. The [final-image rolling restart](gateway-go-final-rolling.json)
also passed **200/200 real function responses**, with no transport failures and all headers
validated. These are availability/contract probes, not load capacity measurements.

On the installed final image, a [real PostgreSQL outage](gateway-go-outage.json) returned
health 200 / readiness 503 and five inference 503s (`data_store_unavailable`); after DB restart
readiness/inference returned 200 without restarting the gateway. The drill covered roughly
11.1 seconds including DB restart; it did not prove high-concurrency limiter-outage behavior.

[Installed state](gateway-go-installed-state.json) records the final 2/2 gateway, 2/2 API,
2/2 reconciler and healthy PostgreSQL. Temporary test keys were revoked and fixtures removed.
Helm upgraded the lab release with explicit digest and retained OTLP endpoint/resource
configuration. A lab-only post-render step preserved existing API/reconciler deployment
specifications; their pod templates were checked unchanged. Their existing OTLP settings
were also persisted in Helm values for later upgrades. Production Go diagnostics on
8082 are disabled. PreStop waits 10 seconds, followed by bounded graceful shutdown;
rolling strategy, PDB and topology spreading remain in the chart.

## Scope still open

Actual identity-provider/in-cluster OIDC acceptance, GPU/vLLM/HF, sustained successful
upstream/LLM streaming load, high-concurrency outage, multi-node node-loss/drain,
external TLS/ingress, full runtime dashboard parity and architecture-specific clean platform
release reruns remain gates. Go maximum throughput is unmeasured; the historical
[1,000/1,500 RPS PoC comparison](gateway-runtime-ab.md) used older dependencies and short
rejection windows. API/reconciler are not being rewritten.

For rollback, retain the existing shared Python image digest and set
`gateway.runtime=python`. A Helm rollback to the pre-migration revision also restores
that runtime; rollback under sustained traffic is a separate acceptance drill.
