# Roadmap

Where the platform stands, what is missing, and what comes next. Kept up to date with the
code; the original milestone plan is in [history/](history/).

## Technical-review progress (2026-10-05)

Priorities and the original findings are in [technical-review.md](technical-review.md).
Code fixes do not close the real-cluster gates below.

- KServe readiness now verifies the platform revision on the immutable Knative backend,
  including a separately verified previous backend for canary metrics. Custom containers
  and ready services at zero pods do not require `modelStatus == UpToDate`.
- MLflow alias reconciliation skips functions and Hub-based LLMs.
- New function versions require `repository@sha256:<64 hex digits>`. Tags are rejected;
  API docs, UI and demo fixtures match. Existing tagged versions are not rewritten and
  should be replaced with new digest registrations before relying on rollback identity.
- Project, run, pipeline, deployment and rollout batches isolate/log per-entity errors,
  then continue. Failed entities use process-local exponential backoff (5–300 seconds).
- [Control-plane image and chart](installation.md) are prepared: API, gateway, single
  reconciler, RBAC, migration hook, Services and optional Ingresses. Image runtime checks,
  dependency installation/bootstrap and live readiness/policy validation remain open.
- [Connectivity matrix](networking.md) and database/OIDC policy corrections are prepared;
  configurable serving ingress and workload egress policies are implemented; actual CNI
  tests remain open.
- Pending deployment checks: annotation propagation, reconciler permission to read
  Knative Revisions, real canary metric attribution and scale-to-zero/reactivation.

## Done

| Area | What works today |
| --- | --- |
| **Projects and access** | Projects with their own namespace and quota. OIDC sign-in for the browser (server-side PKCE, signed session cookie) and for bearer tokens. Project roles (`invoker` < `viewer` < `operator` < `admin`) for users and groups, plus platform admins. A fail-closed policy table and an audit trail |
| **Training** | Jobs and pipeline DAGs on Argo Workflows. Retry, cancel and logs; step timelines and failure reasons. Lineage from a run to the model versions it produced |
| **Models** | Versions from the MLflow registry, or for LLMs from the Hugging Face Hub. Thresholds and evaluation, promotion to champion, registry aliases kept in sync |
| **Serving** | Immutable revisions. Canary rollouts with gates (error rate, p95, minimum traffic) that roll back automatically. Manual rollback. Metrics and trends per revision |
| **LLMs** | LLM models with GPU and context settings. Versions from the Hugging Face Hub, judged on offline results. KServe's Hugging Face runtime (vLLM) on GPUs. Per-project GPU quota, set by platform admins and checked on deploy and canary. In the UI: model and hub forms with verdict previews, a chat playground, OpenAI snippets, token usage per caller, and GPU quota and use |
| **Gateway** | A separate service that makes endpoints public. Per-caller API keys, shown once and stored hashed. Limits per endpoint and per key: requests, or tokens for LLMs. Streaming, OpenAI-compatible chat completions, usage by caller. Ingress, TLS and NetworkPolicy manifests |
| **Monitoring** | Monitor page (reconcilers, API, gateway, external systems). Grafana dashboard, 10 promtool-tested alerts, traces from request to reconciler |
| **Functions** | The team's own container behind an endpoint. Versions are images, deployable at once, served by KServe on Knative with a replica range (scale to zero), concurrency and environment. Called at `POST …/invoke` with any JSON, through the gateway with keys and limits, with canaries and rollback like models |
| **Web UI** | App shell with a sidebar and project switcher. Pages for runs, pipelines, models, deployments, activity, settings, members and API access. Charts built to a data-viz spec. Previews before risky changes. Light, dark and mobile |

## Missing in the code

### LLM end-to-end check
- **`make gateway-e2e`:** add an LLM case (stream, token count, token limit) against a
  stand-in OpenAI-compatible server.

### Functions
- **Image pull secrets.** Private registries need a pull secret in each project namespace.
  There is no per-project mechanism yet, so it is created by hand.
- **Cold starts are not shown.** Neither the gateway nor the Monitor reports how often calls
  wait for a function to start.

## Missing to install it on a cluster

- **Build and verify the prepared control-plane image and chart.**
  `make cp-docker-build` builds `mlp-controlplane:dev`; `helm/controlplane` packages
  API/reconciler/gateway, RBAC and migrations. Helm lint/schema checks pass, but no image
  has been built or deployed for this work. See [installation.md](installation.md).
- **Verify networking and readiness in the target topology.** The [matrix](networking.md)
  records required paths. `/readyz` checks DB/schema; `/healthz` provides process liveness.
  Policies and probes still need real runtime acceptance.
- **Argo Workflows in `make local-up`.** Its `workflowNamespaces` and RBAC must cover the
  `mlp-*` namespaces.
- **Per-project secrets:**
  - MLflow and S3 credentials for training steps.
  - The Hugging Face token (`mlp-hf-token`) for gated models.

  Today all of these are created by hand.

## Not yet run against the real thing

Code and tests exist; these have only met fakes, schema validation or kubeconform. The steps
for each are in [local-verification.md](local-verification.md).

- KServe:
  - the MLflow server;
  - the Knative canary traffic split;
  - vLLM on a GPU.
- Argo Workflows running real jobs and pipelines.
- The gateway in a cluster, behind an ingress with TLS (cert-manager), and with its
  NetworkPolicy.
- Prometheus scraping `mlp_*` in the cluster, and the alerts firing.
- A Keycloak client-credentials token accepted by the gateway.

Already verified for real here:
- PostgreSQL, through the migrations and the full test suite.
- Prometheus 3.1: every query the platform makes.
- Keycloak 26.4: browser sign-in.
- The gateway end to end, as a real process with HTTP to a model server.
- Chromium: the UI tests.
- The alerts with promtool, the manifests with kubeconform.

## Known limitations

- **Rate limits are kept per gateway replica.** With N replicas, the real limit can be up to
  N times the configured one. A shared store (Redis) behind the `RateLimiter` port fixes it.
- **Token admission uses estimates.** Missing usage and interrupted streams consume their
  reservation. A model-specific tokenizer is still needed for exact hard token ceilings.
- **LLM evaluation uses results supplied at registration.** There is no built-in evaluation
  harness.
- **Canary rollouts need `CP_PROMETHEUS_URL`.** Without it, they do not advance.
- **Missing operations:**
  - no durable log archive or physical run-history purging;
  - no log streaming;
  - no automatic discovery of model versions after a pipeline run.
- **`constraints/controlplane.txt` was edited by hand** (`httpx`). Regenerate it with
  `make lock`.
- **AWS is designed in Terraform but has never been applied.**

## Next, in order

1. **Close the review's remaining P0 work:** serving-path/workload policies, pinned
   dependencies and control-plane bootstrap. Image/chart source and readiness/revision
   fixes, DB/schema readiness and configurable policies are prepared; build/runtime checks
   and real topology verification remain open.
2. **Prove the CPU lifecycle:** project → training → model version → serving → gateway →
   canary/rollback → function scale-to-zero/reactivation, following `local-verification.md`.
3. **Finish LLM verification:** add the stand-in HTTP streaming/token-limit e2e case, then
   run GPU serving, real ingress/TLS streaming and quota gates when resources are available.
4. **Production hardening:**
   - a shared rate-limit store;
   - live acceptance checks for [project secrets](secrets.md), including rotation/restart and private-image pulls;
   - live deletion/retention and control-plane restore acceptance drills;
   - log streaming.
5. **AWS:** EKS, RDS and S3 from the existing Terraform.

## Further remediation (2026-10-05)

DB/schema readiness, topology-configured policies, function resources/probes, per-entity
backoff, persisted deadlines, deployment deletion and optional workflow retention are
implemented. LLM requests reserve budgets before forwarding and retain them on missing
usage/interruption. Control-plane backup/restore tooling is prepared.

See [operations.md](operations.md) and [recovery.md](recovery.md). Migrations `0014`/`0015`
and cluster/runtime/restore behavior still need their real-system gates; local checks
are recorded in [local-verification.md](local-verification.md).

### Serving drift and project credential management

Serving reconciliation now compares the owned predictor configuration, including image,
environment, arguments, resources, probes and secret references. A same-revision drift
replaces the owned predictor with a resource-version precondition and a fresh apply ID;
readiness waits for the matching Knative backend. Unowned metadata/defaults are preserved
where comparison allows them. Controller-default interactions still need a live KServe gate.

Project secret create/list/rotate/delete API and Settings UI are implemented. Values stay
in project-owned Kubernetes Secrets; registration validates names/keys and immutable
workload revisions snapshot references. Rotation uses version checks, referenced deletion
is protected, and secret values are excluded from API responses and audit payloads.
Migration `0016` and live workload/rotation/recovery gates remain pending. See
[secrets.md](secrets.md) for scope and limits. The remaining shared limiter, replica GPU
accounting, Hub pinning, automatic discovery and bootstrap work is still open.
