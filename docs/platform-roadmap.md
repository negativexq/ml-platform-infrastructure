# Platform roadmap — M13 → M21

Status: **M13 done**, M14–M21 planned. This supersedes the earlier "M13+ = AWS"
plan: AWS is now M22+ and starts only after the platform MVP (M21) is frozen.

## Architectural principle

The platform's public model:

```
Project
  ├── JobDefinition
  ├── PipelineDefinition
  │       └── PipelineRun
  │              └── StepRun
  ├── Model
  │      └── ModelVersion
  │              ├── Evaluation
  │              └── Promotion
  ├── Deployment
  │      └── DeploymentRevision
  └── Endpoint
```

Subsystems are **adapters only**:

```
                    OUR PLATFORM API
                           │
                    Control Plane DB
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
           MLflow        Argo        Serving
          adapter       adapter       adapter
              │            │            │
              ▼            ▼            ▼
           MLflow       Argo WF       KServe
```

PostgreSQL owns lifecycle state. MLflow, Argo and KServe are never the
platform's source of truth; external ids are references, never identity.

Control-plane mutations follow one pattern, from M14 on:

```
desired state → DB → reconciler → Kubernetes → observed state → DB status
```

The API never performs a side effect and then declares success.

### Naming note

The planned package name `platform/` would shadow the standard library's
`platform` module (it breaks pytest, pydantic and others), so the code lives in
`controlplane/`. The layout is otherwise as planned:

```
controlplane/
├── api/             HTTP surface, no business rules
├── domain/          entities + explicit state machines, stdlib only
├── application/     use cases + ports (providers, unit of work)
├── adapters/        mlflow/ workflow/ serving/ + in-memory fakes
├── persistence/     SQLAlchemy, Alembic migrations, in-memory unit of work
├── reconciliation/  desired → observed loops (M14+)
└── tests/
```

## Milestones

| | Goal | Key gates |
| --- | --- | --- |
| **M13** ✅ | Domain foundation | migrations on a clean DB, UUID identity, explicit tested transitions, layer isolation, Project create/get/list, idempotency, audit, OpenAPI, control-plane tests pass with MLflow/Argo/Kubernetes absent |
| **M14** 🚧 | Project lifecycle & Kubernetes isolation | reconciler creates namespace `mlp-<name>`, ServiceAccount, ResourceQuota, LimitRange, baseline NetworkPolicy, `project_id` labels; 10× reconcile = no change; manual namespace delete → `READY → DRIFTED → PROVISIONING → READY`; safe delete; audit of create/reconcile/delete |
| **M15** 🚧 | Workloads & runs | `JobDefinition` → `Run` → `WorkflowProvider` → Argo → Pod; status sync PENDING→RUNNING→SUCCEEDED/FAILED/CANCELLED; exit code, timings, logs via platform; retry = new Run; duplicate submission = one workload; `image: nonexistent:tag` ends FAILED with the reason kept |
| **M16** 🚧 | Pipeline DAG + MLflow tracking | versioned `PipelineDefinition`; cycle detection; compile to Argo DAG; parallel branches; failed upstream skips downstream; every `StepRun` in the DB; deterministic platform run ↔ MLflow run mapping; Run → MLflow run → artifact in one platform query |
| **M17** 🚧 | Model, evaluation & promotion | `ModelVersion` from training output; threshold-driven evaluation; REJECTED can't be promoted; atomic audited promotion; champion history kept; MLflow alias synced with platform state; manual alias move detected as `DRIFT` |
| **M18** 🚧 | Deployment & endpoint abstraction | `ServingProvider` (KServe first); non-approved model can't go to production; DB object first, reconciler creates the resource; READY only after the model really loads and a prediction succeeds; immutable revisions; deleted KServe resource is recreated |
| **M19** 🚧 | Canary, safety & rollback | traffic split with per-revision metrics; latency/error-rate gates; injected 500s → traffic back to 100% old champion, candidate FAILED, champion unchanged; rollback audited |
| **M20** ✅ | Minimal UI | talks only to the Platform API (never MLflow/Argo/Kubernetes); projects, runs/DAG/logs, models/evaluations/promote, deployments/metrics/rollback |
| **M21** | Platform MVP freeze (`v0.1.0`) | `make platform-e2e` on a fresh kind cluster: project → pipeline → run → track → register → evaluate → candidate → deploy → predict → second model → canary → promote → rollback; audit trail reconstructs the whole lifecycle; destroy/recreate works |

### M14 status

Code is in place (`reconciliation/projects.py`, `adapters/kubernetes/`,
`DELETE /projects/{id}`, migration 0002); the gate run and evidence are still
to do. Real-cluster checks use `scripts/envtest.sh` (etcd + kube-apiserver +
kube-controller-manager, no kubelet) because nested `runc` does not work in the
cloud sandbox, so `kind` cannot start pods there. That is enough for M14 (API
objects only) but not for M15+, which need pods.

### M15 status

Code is in place (`Run`, `JobDefinition`, `RunService`, `RunReconciler`, Argo
adapter, job/run API, migration 0003). Nothing has run against a real Argo yet.
Everything left to verify, with commands, is in
[local-verification.md](local-verification.md).

### M16 status

Code is in place (DAG validation, versioned `PipelineDefinition`, `PipelineRun` +
`StepRun` execution, `PipelineRunReconciler`, `MlflowExperimentProvider`, tracking
join by platform tags, migration 0004). The MLflow adapter is tested against a real
MLflow store; Argo DAG behaviour and the end-to-end gates are in
[local-verification.md](local-verification.md).

### M17 status

Code is in place (`Model`/`ModelVersion` with registry discovery and lineage,
threshold evaluation, atomic promotion with a database-enforced single champion,
`ModelAliasReconciler`, migration 0005). The registry adapter is tested against a
real MLflow store. The end-to-end gates are in
[local-verification.md](local-verification.md).

### M18 status

Code is in place (`Deployment`/`DeploymentRevision`/`Endpoint`, approved-model rule,
immutable revisions, `DeploymentReconciler` with observed readiness and drift
recovery, KServe adapter, predict pass-through, migration 0006). The reconciler is
tested against a fake serving system; the KServe adapter has not run against a real
KServe. See [local-verification.md](local-verification.md).

### M19 status

Code is in place (`Rollout` with a pure `evaluate_gate`, `RolloutReconciler`,
deployment rollback, Prometheus metrics adapter, KServe canary support, migration
0007). Gates, the failure drill and the champion-after-evidence rule are tested
against fakes; the KServe canary and Prometheus adapters have not run for real.
See [local-verification.md](local-verification.md).

### M20 status

Done and verified in a real browser: a no-build UI (`controlplane/ui/static`, served at
`/ui`) that can only talk to the Platform API (CSP + tests), with projects, pipeline-run DAG
and logs, models / evaluations / promote, deployments / canary / metrics / rollback.
`make cp-demo` runs it on in-memory fakes. What remains is looking at it with real data;
see [local-verification.md](local-verification.md).

### UI/UX pass (after M20)

React + TypeScript + Vite (typed from the OpenAPI, bundle committed, CSP unchanged): theme,
command palette, shortcuts, filters, create/run forms, step timeline, log tools, activity with
trace ids. Tests in `controlplane/tests/test_ui.py`; see [local-verification.md](local-verification.md) §7.

### Identity (after M20)

OpenID Connect sign-in (bearer tokens for scripts, server-side browser sign-in with an
HttpOnly session) and three project roles (viewer, operator, admin) for users and groups,
enforced by a fail-closed policy table; the audit trail names people. Verified against a real
Keycloak. This is the minimum, not the "enterprise multi-tenant RBAC" deferred below: no OPA,
no custom roles, no fine-grained resource permissions. See [identity.md](identity.md).

### Observability (cross-cutting, after M20)

OpenTelemetry end to end: FastAPI's native tracing/metrics, a lifecycle span per audit event,
the request's trace carried across the API → reconciler boundary, `trace_id` on logs and audit
rows, Collector + Tempo manifests, a control-plane dashboard and promtool-tested alerts.
Verified here with the real Collector, Tempo and Prometheus; the in-cluster part is
[local-verification.md](local-verification.md) §10. Design and metric names:
[observability.md](observability.md).

### Not before M21

Feast · Redis/Valkey online store · Kafka · lakeFS · Kueue · production identity provider (HA Keycloak) · OPA ·
enterprise multi-tenant RBAC · automated drift retraining · AWS · notebooks/JupyterHub.
