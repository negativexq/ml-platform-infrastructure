# Architecture

How the platform is built: its parts, how a request becomes running infrastructure, how a
prediction reaches a model, and the rules the code follows. The original infrastructure
layer (the first inference service, GitOps, drills) is described in
[history/infrastructure-architecture.md](history/infrastructure-architecture.md).

## Components

```
 people (browser)               services and partners (API keys, OAuth)
        │                                    │
        ▼                                    ▼
 ┌──────────────┐   OIDC   ┌──────────────────────────┐
 │  Web UI      │◀───────▶│  Identity provider       │
 └──────┬───────┘          └──────────────────────────┘
        ▼
 ┌──────────────────────────────┐      ┌─────────────────────────────────────┐
 │  Control plane API           │      │  Inference gateway                  │
 └──────┬───────────────────────┘      └──────┬──────────────────────────────┘
        ▼                                     ▼
 ┌───────────────────────────────────────────────────┐
 │  PostgreSQL: the single source of lifecycle truth │
 └──────┬────────────────────────────────────────────┘
        ▼
 ┌───────────────────────────────────────────────────┐
 │  Reconcilers                                      │
 └──────┬────────────────────────────────────────────┘
        ▼
 Kubernetes · Argo Workflows · MLflow · KServe (MLflow server, vLLM, or a function's container)
```

| Part | Process | What it does |
| --- | --- | --- |
| **Web UI** | static files served by the API at `/ui` | React and TypeScript, typed from the API's OpenAPI document. Talks only to the platform API, same origin, under a strict CSP |
| **Control plane API** | `controlplane.main:app_factory` | Projects, members, jobs, pipelines, models, deployments, canaries, API keys, GPU quota, audit, Monitor. Writes desired state and returns; it never waits on Kubernetes |
| **Reconcilers** | `controlplane.reconciler_main` | Read desired state, drive external systems toward it, report back |
| **Inference gateway** | `controlplane.gateway_main:app_factory` | The only public way in to models. Checks keys or tokens, applies limits, forwards to the serving system, meters usage. Runs and scales apart from the API |
| **PostgreSQL** | | Every lifecycle state. Schema by Alembic migrations (`controlplane/persistence/migrations`) |
| **Identity provider** | e.g. Keycloak | OpenID Connect: users, groups, tokens |
| **External systems** | | Kubernetes (namespaces, quotas), Argo Workflows (jobs, pipelines), MLflow (runs, registry), KServe on Knative (serving, canary split), Prometheus (metrics the canary gates read) |

The reconcilers:

| Reconciler | Drives |
| --- | --- |
| `projects` | a namespace per project, with labels, ResourceQuota (GPUs included), LimitRange and NetworkPolicy |
| `runs`, `pipeline_runs` | Argo workflows; status, steps and exit codes back into the run |
| `deployments` | the KServe InferenceService of the desired revision; readiness back into the endpoint |
| `rollouts` | canary steps: shift traffic, read the canary's metrics, judge them against the gate, advance or roll back |
| `model_aliases` | the registry's `champion` and `candidate` aliases, kept equal to platform state |

## A change: from request to running infrastructure

```
POST /projects/p/deployments/d/revisions   (operator)
   │  1. authorize: the policy table plus the caller's role in the project
   │  2. checks: version is CANDIDATE or CHAMPION, kind matches, GPUs fit the quota
   │  3. one transaction: a new immutable revision, the desired revision,
   │     status DEPLOYING, an audit event with the person's name and the trace id
   ▼
 202 Accepted  ◀── the API is done
   ⋮
 deployments reconciler (next pass)
   │  4. compare the desired revision with what KServe reports
   │  5. apply the InferenceService (idempotent: the same spec twice changes nothing)
   │  6. KServe ready → deployment READY, endpoint READY, audit event
   ▼
 the UI shows READY (it polls while something is in flight)
```

* **Idempotent and race-safe.** Repeating a request returns the existing result.
  Concurrent writers are caught by compare-and-swap on status.
* **Crash-safe.** Every step can crash and be retried. External resources are named
  deterministically, so a retry finds what was half-made instead of creating a second
  one.
* **One trace.** The request's trace context is stored with the entity, so the
  reconciler's spans continue the same trace.

## A prediction: from caller to model

```
caller ──HTTPS──▶ ingress ──▶ gateway ──▶ KServe (Knative local gateway) ──▶ model server
```

1. **Authenticate:** an API key (`mlp_live_<id>_<secret>`, compared by SHA-256), or an
   OIDC token whose subject has the `invoker` role.
2. **Resolve the endpoint.** Missing and internal look the same (404). Endpoints and keys
   are cached for 5 seconds.
3. **Check:** the key may call this endpoint, the endpoint is READY, the path matches its
   protocol, the body is under its limit.
4. **Rate limit,** with token buckets per endpoint and per caller:
   * **models:** one request is one unit;
   * **LLMs:** admitted while there is room, then charged the tokens they actually used.
5. **Forward and stream the reply back.**
   * A model gets KServe's v2 protocol: `/v2/models/<name>/infer`.
   * An LLM gets OpenAI-compatible chat: `/openai/v1/chat/completions`. A streamed request
     is asked to include usage, so tokens are counted while the events pass through.
   * A function gets the body unchanged at its container's `/`.
6. **Record:** `mlp_gateway_*` metrics by project, endpoint, caller and status. Label values
   are never strings an outsider chose.

## Code structure

Hexagonal, enforced by `controlplane/tests/test_architecture.py`:

```
controlplane/
  domain/           entities, state machines, roles, API keys: pure Python, no frameworks
  application/      use cases and ports (interfaces): no frameworks, no external SDKs
  adapters/         Kubernetes, Argo, MLflow, KServe, Prometheus, OIDC, gateway upstreams,
                    plus an in-memory fake of each (fakes.py)
  persistence/      memory and SQL (SQLAlchemy) implementations of the repositories
  reconciliation/   the reconcilers
  api/              FastAPI routes, schemas, authentication and the policy table
  gateway/          the gateway's HTTP surface
  observability/    OpenTelemetry setup, metrics, traced providers and units of work
  ui/web/           the React source; ui/static/ is the committed build
  main.py, reconciler_main.py, gateway_main.py   composition roots: the only place wiring happens
  demo.py           the whole platform on fakes, seeded with believable history
```

Rules:
* Domain and application import no frameworks or SDKs.
* Only adapters touch external systems.
* Only the composition roots import observability.
* The same tests run against the in-memory store and a real PostgreSQL.

## State

Entities and their lifecycles (`controlplane/domain/states.py`; every transition is
checked and audited):

| Entity | States |
| --- | --- |
| Project | PENDING → PROVISIONING → READY ⇄ DRIFTED, FAILED, DELETING → DELETED |
| Run, pipeline run | PENDING → SUBMITTED → RUNNING → SUCCEEDED, FAILED or CANCELLED |
| Model version | REGISTERED → EVALUATING → CANDIDATE or REJECTED; CANDIDATE → CHAMPION → ARCHIVED |
| Deployment | PENDING → DEPLOYING → READY, DEGRADED or FAILED |
| Endpoint | PENDING → READY ⇄ UNAVAILABLE |
| Rollout | PENDING → PROGRESSING → SUCCEEDED or ROLLED_BACK |

Other records:
* **Immutable:** deployment revisions (model artifact, runtime, GPUs, context).
* **Access:** memberships (subject and role per project) and API keys (hash, endpoints,
  limit, expiry, revocation).
* **Audit:** audit events (actor, action, entity, payload, trace id).

## Access

| Rule | Who |
| --- | --- |
| Read in a project (`GET`) | viewer and up |
| Change in a project | operator and up |
| Thresholds, members, exposure, API keys, deleting the project | admin |
| Call a model (predict, chat) | invoker and up |
| GPU quota | platform admins only |
| `/me`, list and create projects, platform health | any signed-in user |

The policy table (`controlplane/api/auth.py`) fails closed: a route that names no project
and is not listed is refused, and a test fails the build until every route has an owner.
Detail: [identity.md](identity.md).

## Observability

* **Traces:** OpenTelemetry from API requests, reconciler passes, external calls and the
  gateway, sent through the Collector to Tempo.
* **Metrics:** `mlp_*` and HTTP metrics, through the Collector to Prometheus, then to the
  Monitor page, the Grafana dashboard and alerts.
* **Logs:** structured JSON that carries trace ids.

Detail: [observability.md](observability.md).

## Local and AWS

| Local | AWS (designed in `infra/terraform`, not yet applied) |
| --- | --- |
| kind | EKS |
| PostgreSQL in the cluster | RDS PostgreSQL |
| MinIO | S3 |
| local images | ECR |
| Keycloak | the organisation's OIDC provider |
| ingress-nginx and cert-manager | ALB or ingress and ACM |

What is missing to install the platform on a cluster, and what has not yet been verified
there: [roadmap.md](roadmap.md).
