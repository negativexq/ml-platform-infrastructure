# The inference gateway: calling models from outside the platform

A project's models are reached from outside through one door: the gateway. It checks who is
calling, whether they may, whether there is room under the limits. Then it forwards the call to
the serving system and counts the call. It is a separate service from the control plane's API
(`services/gateway-go`; Python rollback: `controlplane/gateway_main.py`), so prediction traffic scales on its own and never competes with
management calls.

```
caller ──HTTPS──► ingress ──► gateway ──► serving (KServe, via Knative's local gateway)
                               │  reads endpoints and keys (cached 5 s)
                               └► platform database
```

## The public contract

```http
POST https://api.example.com/v1/{project}/{endpoint}/predict
Authorization: Bearer mlp_live_7f3a9c2e_Qx8...
Content-Type: application/json

{ "instances": [[0.42, 1200, 3, 0.18]] }
```

```http
HTTP/1.1 200 OK
X-Request-Id: req_4be2c1d09a7f3e51
X-MLP-Model: scorer v3            (only when one version serves all traffic)
RateLimit-Limit: 120
RateLimit-Remaining: 117
RateLimit-Reset: 2

{ "predictions": [0.83] }
```

* **The URL never changes when the model does.** New versions, canaries and rollbacks happen
  behind the endpoint's name.
* **`X-Request-Id`** is passed through to the model, or generated, and returned. Quote it
  when something goes wrong.
* **One error shape**: `{"error": {"code", "message", "request_id"}}`.

| Status | Code | When |
| --- | --- | --- |
| 401 | `unauthenticated` | no key, an unknown key, or a revoked or expired one |
| 403 | `forbidden` | the key may not call this endpoint |
| 404 | `not_found` | no such endpoint, **or it is internal** (the two are not told apart) |
| 409 | `not_ready` | the endpoint is not READY |
| 413 | `too_large` | the body is over the endpoint's limit |
| 429 | `rate_limited` | over a limit; `Retry-After` says when to retry |
| 502 | `upstream_unavailable` | the model did not answer |
| 504 | `timeout` | no answer within the endpoint's timeout |

The path names what an endpoint speaks. A model answers `…/predict`, an LLM answers
`…/chat/completions` (below). `…/invoke` is reserved for functions. Calling the wrong one is a
404 that names the right path.

## LLM endpoints: chat completions

An endpoint serving an LLM speaks the OpenAI chat completions API, so any OpenAI client works
by changing its base URL:

```http
POST https://api.example.com/v1/{project}/{endpoint}/chat/completions
Authorization: Bearer mlp_live_...

{ "messages": [{"role": "user", "content": "Why was my card declined?"}],
  "max_tokens": 256, "stream": true }
```

```python
client = OpenAI(base_url="https://api.example.com/v1/customer-support/assistant-prod",
                api_key=os.environ["MLP_API_KEY"])
for chunk in client.chat.completions.create(model="assistant-prod", stream=True,
                                            messages=[{"role": "user", "content": "Hello"}]):
    print(chunk.choices[0].delta.content or "", end="") if chunk.choices else None
```

* **`model` is set by the gateway.** It addresses the served model by the endpoint's name;
  callers may send anything there.
* **Streaming.** With `"stream": true` the reply is server-sent events, passed through as
  they arrive (never buffered). The gateway adds `stream_options.include_usage` so the last
  event carries the token counts.
* **Validation.** The body must be a JSON object with a non-empty `messages` list; otherwise
  `400 invalid_request`.
* **Inside the cluster** the call goes to KServe's Hugging Face server (vLLM) at
  `/openai/v1/chat/completions`.

### Tokens: metering and limits

* **What counts.** For an LLM a unit is a **token**: prompt plus completion, read from the
  reply's `usage` (the last streamed event, or the JSON body).
* **Limits reserve capacity before forwarding.** The gateway atomically reserves a
  byte-based prompt estimate plus a bounded output cap in both endpoint and caller buckets.
  The default output cap is at most 256; callers can supply positive `max_tokens` or
  `max_completion_tokens`. Completed valid usage refunds unused units; an overrun is debt.
  The estimate is not an exact tokenizer; production reservations are shared across replicas.
* **Defaults.** A new LLM endpoint starts at 20,000 tokens per minute, a 512 KB body and a
  120-second timeout. An admin changes them like any limits; a key's own limit is in tokens
  too.
* **Usage.** Usage by caller reports total, prompt and completion tokens
  (`GET …/usage`, and the UI's API access panel).
* **Missing counts or interrupted streams.** The full reservation remains charged. Invalid
  usage never silently becomes zero. See [operations.md](operations.md) for admission errors,
  conservative failure charging and concurrency behavior.

## Function endpoints: invoke

A function is the team's own container behind an endpoint. Callers send any JSON and get
the function's JSON back:

```http
POST https://api.example.com/v1/{project}/{endpoint}/invoke
Authorization: Bearer mlp_live_...

{ "ticket": "My card was declined at checkout" }
```

* **The container.** It listens on its port (8080 by default) and answers `POST /`. The
  gateway forwards the body unchanged and passes the reply through.
* **Scaling.** It runs as a KServe custom predictor on Knative, with its own replica range
  (`min_scale` to `max_scale`) and requests per replica. A minimum of 0 scales it to zero
  when idle, so the first call after a quiet spell waits for a cold start. The default
  60-second timeout leaves room for that.
* **Limits and usage** count requests, like a model. A new function endpoint starts at 600
  requests per minute, a 1 MB body and a 60-second timeout.
* **Versions.** A new version is an immutable container image reference with
  `@sha256:<64 lowercase hex digits>`. Tags are rejected because they can move. Existing
  tagged versions should be replaced with new digest registrations. There is
  nothing to evaluate, so a version is deployable as soon as it is registered. A canary of it
  is still judged on error rate and latency in real traffic.

## Who may call

* **API keys** (`mlp_live_<id>_<secret>`) are issued per caller, for named endpoints, with an
  optional limit of their own and an expiry.
  * The secret is shown once; only its SHA-256 is stored.
  * The `mlp_live_` prefix lets secret scanners recognise a leaked key.
  * Revoking is immediate in the database and takes effect at the gateway within its 5-second
    cache.
* **Signed-in callers** (an OAuth client-credentials token from the same identity provider)
  need the **invoker** role in the project. The invoker role allows calling and nothing else
  (`docs/identity.md`).
* **Only public endpoints** are reachable. An admin opens one (`exposure: public`) and sets its
  limits. Closing it again keeps the keys but refuses their calls.

## Limits

Two token buckets per call, both of which must have room:

* **The endpoint's limit**, for all callers together, protects the model.
* **The caller's limit**, a key's own or else the endpoint's, gives each caller a fair share.

Production defaults to `CP_GATEWAY_LIMIT_STORE=postgres`: the existing control-plane
database stores buckets, debt and reservations across gateway replicas/restarts. Both
buckets are locked in stable order and updated atomically using database time. Backend
errors return a redacted 503 before forwarding; lock/statement waits are bounded.
Settlement failures retain the reservation and log a generic warning. Bucket rows currently
have no automatic expiry; include their growth in database maintenance. Demo mode and an
explicit `memory` setting use per-process buckets, so additional replicas multiply capacity.

`make cp-http-test` uses native HTTP servers to check function forwarding, JSON/streaming
chat, concurrent reservation, missing usage and peer disconnection. It starts no PostgreSQL,
Docker or cluster; real ingress/TLS and GPU acceptance remain pending.

## Running it

| | |
| --- | --- |
| Demo | `make cp-demo` serves the gateway at `http://localhost:8080/gateway` next to the API, with `credit-risk-prod` public and demo keys (printed at start-up) |
| Locally against PostgreSQL | `make cp-gateway` (port 8081) |
| End-to-end check | `make gateway-e2e`: real PostgreSQL, the real gateway process, a v2-protocol model server |
| Cluster | `k8s/gateway/gateway.yaml`: Deployment (2 replicas, PDB), Service, Ingress with TLS, NetworkPolicy |

The control plane API needs `CP_GATEWAY_URL` (e.g. `https://api.example.com`) to show public
URLs in the UI.

## Managing it

| | API (admin unless noted) | UI |
| --- | --- | --- |
| Open or close, set limits | `PATCH /projects/{p}/endpoints/{name}` | Deployment page, **API access** |
| Try a function (invoker) | `POST /projects/{p}/endpoints/{name}/predict` with any JSON, through the platform | Deployment page, **Try it** |
| Try an LLM (invoker) | `POST /projects/{p}/endpoints/{name}/chat`, through the platform, not streamed | Deployment page, **Playground** |
| Issue a key | `POST /projects/{p}/api-keys` | API access, or Settings, **API keys** |
| Revoke a key | `DELETE /projects/{p}/api-keys/{key_id}` | the key's **Revoke** |
| See usage (viewer) | `GET /projects/{p}/endpoints/{name}/usage` | API access, **Usage** |

Each change is previewed before it is made: who loses access, what a new limit would have
refused in the last hour. Each change is also in the audit trail (`endpoint.exposure_changed`,
`api_key.created`, `api_key.revoked`).

## Watching it

* **Metrics:** operational `mlp_gateway_requests_total` omits caller; per-caller red/error
  usage uses `mlp_gateway_usage_requests_total`. Exact `mlp_gateway_units_total` and
  `mlp_gateway_duration_seconds` (`docs/observability.md`).
* **Monitor page:** has the gateway's traffic, error rate and p95.
* **Alerts:** `GatewayHighErrorRate` and `GatewayHighLatency` fire per endpoint.
* **Label safety:** a label never carries a string an outsider chose. Unknown endpoints count
  as `(unknown)` and callers without a valid key as `(anonymous)`.

## Verified, and what is not

* **Verified here:**
  * Unit and API tests (memory and PostgreSQL).
  * Browser tests: the UI issues a key, shows it once, calls through the gateway, revokes it
    and closes the endpoint.
  * The usage and health queries against a real Prometheus.
  * The alert rules with promtool.
  * The manifests with kubeconform.
  * `make gateway-e2e`.
* **Not verified:** KServe and Knative's gateway in a real cluster, the ingress controller, TLS
  and cert-manager (`docs/local-verification.md` §12).


## Runtime and rollback

The chart defaults to `gateway.runtime=go` with its own `gateway.image.repository` and
`gateway.image.digest`. API/reconciler/migrations still use the shared Python image.
Go supports the normal OTel profile, `CP_GATEWAY_WORKERS` (12) and
`CP_GATEWAY_DB_POOL_CAPACITY` (15). Diagnostic 8082 is disabled in installed workloads.
Use `gateway.extraEnv` for OTLP endpoint/resource overrides.

For a Python rollback, select `gateway.runtime=python`; it uses the existing shared
`image` values and the previous uvicorn process. Keep that image's digest available.
The Go source image and live acceptance are documented in the
[migration report](evidence/live-2026-10-07/observability/gateway-go-migration.md).
