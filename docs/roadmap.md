# Roadmap

Current consolidated status (2026-10-06): [status.md](status.md). Dated findings and test records below retain their original scope.

Current security remediation, local evidence and outstanding deployment/test work: [security-hardening.md](security-hardening.md).

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
- [Control-plane image and chart](installation.md) are prepared: API, gateway, Lease-elected
  reconcilers, RBAC, migration hook, Services and optional Ingresses. Image runtime checks,
  pinned dependency/bootstrap bundles and live runtime/readiness/admission checks have
  passed in the [2026-10-06 evidence batch](evidence/live-2026-10-06/README.md). Full CPU
  lifecycle and multi-node failures remain open.
- [Connectivity matrix](networking.md) and database/OIDC policy corrections are prepared;
  configurable serving ingress and workload egress policies are implemented; actual CNI
  ingress allow/deny tests passed on an enforcing engine; strict-egress checks remain open.
- Pending deployment checks: annotation propagation, reconciler permission to read
  Knative Revisions, real canary metric attribution and scale-to-zero/reactivation.

## Done

| Area | What works today |
| --- | --- |
| **Projects and access** | Projects with their own namespace and quota. OIDC sign-in for the browser (server-side PKCE, signed session cookie) and for bearer tokens. Project roles (`invoker` < `viewer` < `operator` < `admin`) for users and groups, plus platform admins. A fail-closed policy table and an audit trail |
| **Training** | Jobs and pipeline DAGs on Argo Workflows. Retry, cancel and logs; step timelines and failure reasons. Lineage from a run to the model versions it produced |
| **Models** | Versions from the MLflow registry (including discovery after successful pipelines), or full-commit-pinned LLM versions from the Hugging Face Hub. Thresholds and evaluation, promotion to champion, registry aliases kept in sync |
| **Serving** | Immutable revisions. Canary rollouts with gates (error rate, p95, minimum traffic) that roll back automatically. Manual rollback. Metrics and trends per revision |
| **LLMs** | LLM models with GPU and context settings. Versions from the Hugging Face Hub, judged on offline results. KServe's Hugging Face runtime (vLLM) on GPUs. Per-project GPU quota, set by platform admins and checked on deploy, rollback and canary using maximum replica capacity. In the UI: model and hub forms with verdict previews, a chat playground, OpenAI snippets, token usage per caller, and GPU quota and use |
| **Gateway** | A separate service that makes endpoints public. Per-caller API keys, shown once and stored hashed. Limits per endpoint and per key: requests, or tokens for LLMs. Streaming, OpenAI-compatible chat completions, usage by caller. Ingress, TLS and NetworkPolicy manifests |
| **Monitoring** | Monitor page (reconcilers, API, gateway, external systems). Grafana dashboard, 10 promtool-tested alerts, traces from request to reconciler |
| **Functions** | The team's own container behind an endpoint. Versions are images, deployable at once, served by KServe on Knative with a replica range (scale to zero), concurrency and environment. Called at `POST …/invoke` with any JSON, through the gateway with keys and limits, with canaries and rollback like models |
| **Web UI** | App shell with a sidebar and project switcher. Pages for runs, pipelines, models, deployments, activity, settings, members and API access. Charts built to a data-viz spec. Previews before risky changes. Light, dark and mobile |

## Missing in the code

### Gateway acceptance
- Native HTTP function/LLM tests cover streaming, usage refunds, concurrent reservations
  and disconnects (`make cp-http-test`). Real ingress/TLS and GPU serving remain live gates.

### Functions
- Private registry references are managed through project Secrets and registration selectors.
  Actual image pulls and credential rotation still need a live workload check.
- **Cold starts are not shown.** Neither the gateway nor the Monitor reports how often calls
  wait for a function to start.

## Missing to install it on a cluster

- **Build and verify the prepared control-plane image and chart.**
  `make cp-docker-build` builds `mlp-controlplane:dev`; `helm/controlplane` packages
  API/reconciler/gateway, RBAC and migrations. The ARM64 lab has a passing clean
  control-plane image gate and a live deployment; repeat the gate for the final release
  artifact and resolve the serving/initializer scan findings. See [installation.md](installation.md).
- **Verify networking and readiness in the target topology.** The [matrix](networking.md)
  records required paths. `/readyz` checks DB/schema; `/healthz` provides process liveness.
  Policies and probes still need real runtime acceptance.
- **Argo Workflows in `make local-up`.** Its `workflowNamespaces` and RBAC must cover the
  `mlp-*` namespaces.
- **Per-project secrets:**
  - MLflow and S3 credentials for training steps.
  - The Hugging Face token (`mlp-hf-token`) for gated models.

  Manage these with the project Secrets API/UI. Classic S3/MinIO storage-initializer authentication is implemented through revision
  accounts and storage_secret references; validate it with real private artifacts. Other
  private/gated provider paths still need configuration/contracts; see [secrets.md](secrets.md).

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

- **Production limits use shared PostgreSQL buckets.** Native concurrency tests pass;
  cross-process PostgreSQL locking and outage behavior remain live acceptance gates.
  Explicit `CP_GATEWAY_LIMIT_STORE=memory` and demo mode keep per-process budgets.
- **Token admission uses estimates.** Missing usage and interrupted streams consume their
  reservation. A model-specific tokenizer is still needed for exact hard token ceilings.
- **LLM evaluation uses results supplied at registration.** There is no built-in evaluation
  harness.
- **Canary rollouts need `CP_PROMETHEUS_URL`.** Without it, they do not advance.
- **Missing operations:**
  - no durable log archive or physical run-history purging;
  - no log streaming.
- Dependencies are regenerated for Linux/amd64 with pinned uv; deliberate upgrades and
  image/runtime validation remain release work. See [installation.md](installation.md).
- **AWS is designed in Terraform but has never been applied.**

## Next, in order

1. **Close deployment and evidence gates:** prepare migration `0021`, verified stable
   identity grants and separated runtime database users; build/scan the pinned images,
   deploy chart 0.3.1 and prove admission/PSA/CNI enforcement. Source implementations
   are present; real controller, image and topology behavior still needs evidence.
2. **Prove the CPU lifecycle:** project → training → model version → serving → gateway →
   canary/rollback → function scale-to-zero/reactivation, following `local-verification.md`.
3. **Finish LLM verification:** run GPU serving, real ingress/TLS streaming and
   maximum-replica/canary quota gates when resources are available.
4. **Production hardening:**
   - shared PostgreSQL limiter concurrency/outage acceptance;
   - live acceptance checks for [project secrets](secrets.md), including rotation/restart and private-image pulls;
   - live deletion/retention and control-plane restore acceptance drills;
   - stable audit actor_subject persistence and safe rate-bucket GC;
   - durable logs/streaming and cold-start observability.
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
[secrets.md](secrets.md) for scope and limits.

### Shared budgets and release preparation

Production gateway buckets now live in PostgreSQL (migration `0017`), with atomic
endpoint/caller reservations. LLM GPU admission reserves per-replica GPUs multiplied by
`max_scale`, including active/desired and canary overlap (migration `0018`). New Hub
registrations require a full lowercase 40-character commit SHA.

Successful pipeline runs trigger delayed, lineage-scoped MLflow discovery with durable
checkpoints (migration `0019`); discovery does not evaluate or promote versions. Job/model
forms select secret names and keys; Settings shows referencing definitions/revisions.

Base-image/dependency pins, checksum-verified bootstrap bundles and manual-sync GitOps
manifests are prepared. See [installation.md](installation.md). These code changes are
locally verified; image builds, live migrations through current head `0021`, PostgreSQL concurrency,
cluster installation, real GPU/TLS workloads and recovery drills remain pending.

### Security, HA and manual acceptance tooling

API Secret CRUD and workload/log reads are restricted by project RoleBindings; the
API ClusterRole retains only namespace get for ownership checks. Existing READY projects repair missing bindings. Migration processes
use PostgreSQL advisory locking and follow the documented expand/contract contract.
API/gateway default to two replicas with PDB/spread/rolling settings; two reconcilers
coordinate via Lease with a main-loop progress watchdog and their own PDB, with explicit
single-process local mode. Kubernetes SDK transport calls have bounded timeouts. Production values enforce
network/site/identity/image configuration. Private S3/MinIO initializer credentials now
have a separate revision-scoped serving account/reference path, with owned-account
cleanup after deployment deletion.

Manual image, limiter load/outage, database recovery and full CPU acceptance commands are
prepared; these remain live gates until executed. See [acceptance.md](acceptance.md).
Logs/archive/streaming, operational cold-start metrics, exact tokenization, adversarial
canary attribution, CNI isolation and GPU/Hub/private-registry failure drills remain open.

### Reconciler admission boundary

The current chart 0.3.1 retains always-on fail-closed native policies for namespace ownership,
namespace-scoped provider writes, exact project RoleBindings/subjects and the workflow
executor Role. Kubernetes 1.30+ is required. Rendered CEL tests and a server dry-run live
gate are available; live cluster enforcement remains unverified until that gate runs.
See [operations.md](operations.md#reconciler-provisioning-trust-boundary).

## 2026-10-06 ARM64 runtime follow-up

Native classic serving and S3 initializer images load real artifacts and reach KServe
READY. Gateway route normalization is deployed and a real prediction returns HTTP 200
with the expected result. The full canary/rollback/cold-start lifecycle remains pending.
Resolve the five fixable HIGH serving/initializer dependency findings before using
these artifacts for a production release. The original clean control-plane scan pass
does not cover them. See [ARM64 evidence](evidence/live-2026-10-06/serving-arm64/README.md).
