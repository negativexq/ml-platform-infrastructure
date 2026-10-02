# The inference gateway: calling models from outside the platform

A project's models are reached from outside through one door: the gateway. It checks who is
calling, whether they may, whether there is room under the limits. Then it forwards the call to
the serving system and counts the call. It is a separate service from the control plane's API
(`controlplane/gateway_main.py`), so prediction traffic scales on its own and never competes with
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

The paths for other kinds of endpoint are reserved in the contract: `…/chat/completions` for
LLMs (OpenAI-compatible, streamed) and `…/invoke` for functions. The gateway already streams
replies through and counts *units* (requests now, tokens for LLMs). Adding those kinds means
a new upstream and unit counting, not a new gateway.

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

The buckets live in each gateway replica's memory, so N replicas allow up to N times the
configured rate. The `RateLimiter` port takes a shared store (Redis) when that matters.

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
| Issue a key | `POST /projects/{p}/api-keys` | API access, or Settings, **API keys** |
| Revoke a key | `DELETE /projects/{p}/api-keys/{key_id}` | the key's **Revoke** |
| See usage (viewer) | `GET /projects/{p}/endpoints/{name}/usage` | API access, **Usage** |

Each change is previewed before it is made: who loses access, what a new limit would have
refused in the last hour. Each change is also in the audit trail (`endpoint.exposure_changed`,
`api_key.created`, `api_key.revoked`).

## Watching it

* **Metrics:** `mlp_gateway_requests_total`, `mlp_gateway_units_total` and
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
