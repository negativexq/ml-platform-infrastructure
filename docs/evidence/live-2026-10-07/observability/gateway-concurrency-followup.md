# Gateway concurrency and idle-timeout follow-up

Source follow-up to `873c6f6`; the earlier image/load evidence remains historical.

OIDC JWT signature/claim validation now runs outside the provider mutex. Discovery and
JWKS use immutable snapshots published under short locks and separate singleflight
operations. A valid fresh cached signing key bypasses an in-progress unknown-kid refresh.
Cold discovery/JWKS calls coalesce, individual waiters respect cancellation, and shared
network operations have independent ten-second deadlines. Failed refreshes do not mark
old keys fresh; one-hour key expiry and unknown-kid refresh throttling remain enforced.

API-key and route caches have separate RW locks and per-entry flights. Database calls run
without cache/touch locks. Shared fills are bounded to six seconds and survive one waiter's
cancellation; negative entries and five-second TTL remain. TTL starts before the read, so a
slow read cannot extend the revocation snapshot's cache lifetime.

Go public-server idle timeout now defaults to **60 seconds**, configurable with
`CP_GATEWAY_IDLE_TIMEOUT` / Helm `gateway.idleTimeout` (1s–10m). Header/read and upstream
streaming deadlines remain independent. Private runtime metadata reports the actual idle
setting. Longer idle timeouts move the connection-closing boundary; they do not eliminate
all races with external client/proxy connection reuse, and inference POST retries were not
added.

Regression checks cover blocked cache misses versus cache hits/route loads/touches,
coalesced same-key reads with canceled waiters, negative-cache TTL after slow reads,
32 concurrent cached JWT validations during a blocked JWKS refresh, key rotation,
coalesced discovery/JWKS with canceled waiters, invalid idle settings, and a real Go HTTP
client reusing its connection after 5.2 seconds idle. Race tests and repeated concurrent
cases, vet, Ruff and Helm rendering/lint are the verification for these source changes.

The benchmark helper explicitly defaults Go probes to **5 seconds** for historical Python
comparability. Set `--go-server-idle-timeout-seconds 60` to exercise the production setting.
It also explicitly overrides cloned Go worker/pool settings so per-case values take effect.

## Live artifact rollout

Source commit `8e4dd9b` is now installed as
`localhost:5201/mlp-gateway-go@sha256:fcadc04330e475f3c612c14e4af50ee13b8b32daca4b7c70c74c872b20ab7a5c`
on the single-node ARM64 Kubernetes 1.32 acceptance lab. The manual release command
passed race tests, vet, source image build and the HIGH/CRITICAL Trivy gate;
[scan](gateway-concurrency-release/trivy.json),
[SBOM](gateway-concurrency-release/sbom.cdx.json) and
[provenance](gateway-concurrency-release/provenance.json) record the artifact.

Helm revision 18 reached two ready gateway replicas with `CP_GATEWAY_IDLE_TIMEOUT=60s`.
The lab post-renderer preserved existing API/reconciler Deployment specs and their pod
templates were checked unchanged. Migration hooks were skipped for this gateway-only
change; the database schema remains `0021`.

A temporary in-cluster HTTPX client sent **200/200 successful real function calls** through
the gateway Service during the Helm rollout, with zero transport errors and all request-ID
and rate-limit headers validated. After rollout, a client with a 90-second idle budget
reused the same connection after **5.2 seconds** idle; `/healthz` and `/readyz` returned 200.
[Rolling probe](gateway-concurrency-release/rolling.json) and
[installed state](gateway-concurrency-release/installed.json) record these results.
The temporary key was revoked, probe Job/Secret deleted and API port-forward closed.

These are availability/contract checks, not a new capacity benchmark. Real identity-provider/
multi-key load and heterogeneous clients near the new 60-second closing boundary remain
acceptance steps; earlier 500-RPS warm API-key results do not measure those workloads.
