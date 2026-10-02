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
| **M14** | Project lifecycle & Kubernetes isolation | reconciler creates namespace `mlp-<name>`, ServiceAccount, ResourceQuota, LimitRange, baseline NetworkPolicy, `project_id` labels; 10× reconcile = no change; manual namespace delete → `READY → DRIFTED → PROVISIONING → READY`; safe delete; audit of create/reconcile/delete |
| **M15** | Workloads & runs | `JobDefinition` → `Run` → `WorkflowProvider` → Argo → Pod; status sync PENDING→RUNNING→SUCCEEDED/FAILED/CANCELLED; exit code, timings, logs via platform; retry = new Run; duplicate submission = one workload; `image: nonexistent:tag` ends FAILED with the reason kept |
| **M16** | Pipeline DAG + MLflow tracking | versioned `PipelineDefinition`; cycle detection; compile to Argo DAG; parallel branches; failed upstream skips downstream; every `StepRun` in the DB; deterministic platform run ↔ MLflow run mapping; Run → MLflow run → artifact in one platform query |
| **M17** | Model, evaluation & promotion | `ModelVersion` from training output; threshold-driven evaluation; REJECTED can't be promoted; atomic audited promotion; champion history kept; MLflow alias synced with platform state; manual alias move detected as `DRIFT` |
| **M18** | Deployment & endpoint abstraction | `ServingProvider` (KServe first); non-approved model can't go to production; DB object first, reconciler creates the resource; READY only after the model really loads and a prediction succeeds; immutable revisions; deleted KServe resource is recreated |
| **M19** | Canary, safety & rollback | traffic split with per-revision metrics; latency/error-rate gates; injected 500s → traffic back to 100% old champion, candidate FAILED, champion unchanged; rollback audited |
| **M20** | Minimal UI | talks only to the Platform API (never MLflow/Argo/Kubernetes); projects, runs/DAG/logs, models/evaluations/promote, deployments/metrics/rollback |
| **M21** | Platform MVP freeze (`v0.1.0`) | `make platform-e2e` on a fresh kind cluster: project → pipeline → run → track → register → evaluate → candidate → deploy → predict → second model → canary → promote → rollback; audit trail reconstructs the whole lifecycle; destroy/recreate works |

### Not before M21

Feast · Redis/Valkey online store · Kafka · lakeFS · Kueue · Keycloak · OPA ·
enterprise multi-tenant RBAC · automated drift retraining · AWS · notebooks/JupyterHub.
