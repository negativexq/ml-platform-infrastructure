# Local verification — what is written but not yet proven

M14 and M15 were written in a cloud sandbox where `kind` cannot start pods
(nested `runc` fails: `can't get final child's PID from pipe`) and Argo is not
installed. Everything below has to be run on a real machine before the
milestones can be called done and their `docs/evidence/m14|m15/gate.md` written.

**What *was* verified in the sandbox:** ruff + mypy strict, 120+ control-plane
unit/API tests (every use-case test runs against both the in-memory store and a
real PostgreSQL 16), migration/model drift check, and one manual run of
`KubernetesClusterProvider` against a real `kube-apiserver` + `etcd` +
`kube-controller-manager` (`scripts/envtest.sh`): create all resources → second
apply changes nothing → delete one resource → drift detected and repaired →
namespace deleted by the real namespace controller.

**What was not:** anything involving a running pod, Argo, or a CNI.

---

> Sections: 0 setup · 1 M14 gate · 2 M15 gate · 3 M16 gate · 4 M17 gate ·
> 5 M18 gate · 6 untested code · 7 missing pieces.

## 0. Setup on your machine

```bash
make local-up                      # existing kind cluster + platform (M0–M12)
pip install -e ".[dev,controlplane,controlplane-dev]"

# Argo Workflows (not part of local-up yet — see "Missing pieces")
kubectl create ns argo
kubectl apply -n argo -f https://github.com/argoproj/argo-workflows/releases/download/v3.6.2/install.yaml

# PostgreSQL for the control plane: reuse the platform one or any local instance
export CP_DATABASE_URL=postgresql+psycopg://USER:PASS@localhost:5432/controlplane
export CP_KUBECONFIG=$HOME/.kube/config      # context must be the kind cluster
make cp-migrate                    # alembic upgrade head  (0001 → 0006)

make cp-run                        # API on :8080      (terminal 1)
make cp-reconcile                  # reconcilers       (terminal 2)
```

The reconciler needs cluster-wide permissions when run in-cluster; locally your
admin kubeconfig is enough.

Quick sanity (no cluster needed): `make cp-check`.

---

## 1. M14 gate — Project lifecycle & Kubernetes isolation

Drive everything through the API (`curl`/`/docs`), inspect with `kubectl`.

| # | Gate | How to check |
| --- | --- | --- |
| 1 | `POST /projects` writes a DB row | `psql -c "select name,status from projects"` |
| 2 | Reconciler creates the namespace | `kubectl get ns mlp-credit-risk` |
| 3 | Deterministic name `mlp-<name>` | same |
| 4 | `project_id` label on every resource | `kubectl get ns,sa,quota,limitrange,netpol,role,rolebinding -n mlp-credit-risk --show-labels` → `mlp.io/project-id` |
| 5 | ServiceAccount `mlp-workload` | `kubectl -n mlp-credit-risk get sa` |
| 6 | ResourceQuota `mlp-quota` applied | `kubectl -n mlp-credit-risk describe quota` |
| 7 | Baseline NetworkPolicy `mlp-baseline` | `kubectl -n mlp-credit-risk get netpol` **and prove enforcement**: a pod in another namespace must not reach a pod in `mlp-credit-risk` (kindnet enforces; see the M9 drill for the shape) |
| 8 | 10× reconcile changes nothing | record `resourceVersion` of every resource, wait ≥10 reconcile passes, compare; also `select count(*) from audit_events` must not grow |
| 9 | Manual namespace delete is repaired | `kubectl delete ns mlp-credit-risk`; status goes `READY → DRIFTED → PROVISIONING → READY` (`select action,payload from audit_events order by occurred_at`) |
| 10 | Failed provisioning never yields READY | e.g. pre-create `kubectl create ns mlp-other` *without* labels, then `POST /projects {"name":"other"}` → status `FAILED`, `status_reason` says the namespace is not owned |
| 11 | Delete cleans up safely | `DELETE /projects/{id}` → 202; namespace gone; status `DELETED`; project no longer in `GET /projects`; **and** a namespace the project does not own is left alone (repeat #10 then delete → stays `DELETING` with a reason) |
| 12 | Audit trail | `project.created`, `project.provisioning`, `project.provisioned`, `project.drift_detected`, `project.delete_requested`, `project.deleted` all present |

Things to eyeball while doing this:

- A false drift loop: if `status` flaps `READY ↔ DRIFTED` with nothing touched,
  the "desired ⊆ actual" comparison in `adapters/kubernetes/provisioner.py` is
  tripping on a field a newer Kubernetes defaults. Run `kubectl get -o yaml` and
  compare with `_desired()`.
- Namespace deletion hangs in `Terminating` → check the namespace controller /
  finalizers; the project stays `DELETING` (by design) until it is really gone.

---

## 2. M15 gate — Workloads & runs

Create a project (READY), then:

```bash
curl -XPOST localhost:8080/projects/credit-risk/jobs -H 'content-type: application/json' -d '{
  "name":"hello","image":"busybox:1.36","command":["sh","-c","echo hello; sleep 5"],
  "resources":{"cpu":"100m","memory":"64Mi"}}'
curl -XPOST localhost:8080/projects/credit-risk/jobs/hello/runs -H 'Idempotency-Key: r1'
curl localhost:8080/runs/<id>            # poll
curl localhost:8080/runs/<id>/logs
```

| # | Gate | How to check |
| --- | --- | --- |
| 1 | JobDefinition created | `POST …/jobs` → 201; repeat → 200; changed content → 409 |
| 2 | Run created | `POST …/jobs/{job}/runs` → 202, status `PENDING` |
| 3 | Run compiles to an Argo Workflow | `kubectl -n mlp-credit-risk get workflows` (name `run-<16 hex>`, labels `mlp.io/run-id`, `mlp.io/job`) |
| 4 | Real container runs | `kubectl -n mlp-credit-risk get pods` |
| 5 | `PENDING → SUBMITTED → RUNNING → SUCCEEDED` | poll `GET /runs/{id}`; audit events `run.submitted/running/succeeded` |
| 6 | Failing workload → `FAILED` | job `["sh","-c","exit 3"]` → `exit_code: 3` |
| 7 | Cancelled workload → `CANCELLED` | start `sleep 300`, `POST /runs/{id}/cancel` → `CANCELLED`, workflow has `spec.shutdown: Terminate` |
| 8 | Exit code stored | #6 |
| 9 | start/end/duration | `started_at`, `finished_at`, `duration_seconds` on a finished run |
| 10 | Logs via the platform | `GET /runs/{id}/logs` returns `hello` |
| 11 | Retry = new Run, history untouched | `POST /runs/{id}/retry` on a finished run → new id, `retry_of` set; the old run's row is unchanged |
| 12 | Same submission ≠ second workload | repeat the `Idempotency-Key` request → 200, same id, exactly one workflow; also kill the reconciler between submit and DB write (or set a breakpoint) and restart: still one workflow |

**Fault test (the one that matters):** job with `"image":"nonexistent:tag"`.
Expected: `SUBMITTED` → (pod `ImagePullBackOff`) → `FAILED` with the reason kept
in `status_reason`, workflow terminated, not left running until the 1 h
deadline. This relies on a heuristic in `ArgoWorkflowProvider.get_status`
(see below) and is the most likely thing to need adjusting.

Also confirm: `GET /runs/{id}` never shows a workflow uid, pod name or
namespace.

---

## 3. M16 gate — Pipeline DAG + MLflow tracking

Extra setup: point the control plane at MLflow and make the steps able to reach it.

```bash
export CP_MLFLOW_TRACKING_URI=http://localhost:30500                       # control plane -> MLflow
export CP_STEP_MLFLOW_TRACKING_URI=http://platform-mlflow.ml-platform.svc:5000   # what steps get (in-cluster; check your service name)
make cp-migrate                      # 0004
```

Use the training image for real tracking: `docker/training/Dockerfile` →
`kind load docker-image`, job `{"image": "ml-platform-training:dev", "command": ["python","scripts/train.py"]}`.
`scripts/train.py` now reads the `MLP_*` env vars and writes them as MLflow tags.
Check that the image's MinIO/S3 credentials reach the step pods (the project
namespace is new; the M7 training Job got them from its own Secret).

```bash
curl -XPOST localhost:8080/projects/credit-risk/pipelines -H 'content-type: application/json' -d '{
  "name":"flow","steps":[
    {"name":"validate","job":"hello"},
    {"name":"prepare","job":"hello","depends_on":["validate"]},
    {"name":"train","job":"train-job","depends_on":["prepare"]},
    {"name":"evaluate","job":"hello","depends_on":["train"]}]}'
curl -XPOST localhost:8080/projects/credit-risk/pipelines/flow/runs -H 'Idempotency-Key: p1'
curl localhost:8080/pipeline-runs/<id>            # per-step status
curl localhost:8080/pipeline-runs/<id>/tracking   # params, metrics, artifact_uri — no MLflow id needed
```

| # | Gate | How to check |
| --- | --- | --- |
| 1 | PipelineDefinition versioned | repost the same body → 200 same version; change a step → `version: 2`; old version still readable with `?version=1` |
| 2 | Cycle detection | `a→b→a` body → 422 `invalid_argument` ("dependency cycle") |
| 3 | Invalid dependency rejected | unknown step / self-dependency / unknown job → 422 |
| 4 | Compiles to an Argo DAG | `kubectl -n mlp-credit-risk get workflow pr-<16 hex> -o yaml` → `dag.tasks[*].dependencies` match the pipeline |
| 5 | validate → prepare → train → evaluate really in order | compare `started_at`/`finished_at` of the four steps from `GET /pipeline-runs/{id}` (each starts after its predecessor finished) |
| 6 | Parallel branch | diamond `a→(b,c)→d`; `b` and `c` overlap in time (use `sleep 20` in both) |
| 7 | Failed upstream skips downstream | make `prepare` `exit 1`: `train` and `evaluate` → `SKIPPED`, run `FAILED`, `status_reason: failed steps: prepare`. **Check Argo reports omitted tasks** (see §4 item 1) |
| 8 | Every StepRun in the DB | `select step_name,status,exit_code from step_runs where pipeline_run_id=…` — rows exist from creation, none left PENDING under a finished run |
| 9 | Training creates an MLflow run | MLflow UI: experiment `mlp-credit-risk`, run tagged `platform_pipeline_run_id` |
| 10 | Params / metrics / artifact | `GET …/tracking` → `params`, `metrics`, `artifact_uri`; the artifact exists in MinIO |
| 11 | Mapping is deterministic | run `…/tracking` twice → identical; two pipeline runs never see each other's tracked runs |
| 12 | Result without knowing an MLflow id | the `/tracking` response is the whole answer |

**The critical gate:** `Run #N → MLflow run → artifact` from **one** platform
query: `GET /pipeline-runs/{id}/tracking`.

Fault tests: cancel mid-run (`POST /pipeline-runs/{id}/cancel`) → running step
`CANCELLED`, pending steps `CANCELLED`, run `CANCELLED`; kill the reconciler
mid-run and restart → it resumes, no second workflow.

---

## 4. M17 gate — Model, evaluation & promotion

Needs `CP_MLFLOW_TRACKING_URI` (section 3) and a pipeline that registers a model.
Register the version under the name the API tells you, then let the platform find it:

```bash
curl -XPOST localhost:8080/projects/credit-risk/models -H 'content-type: application/json' \
  -d '{"name":"scorer","thresholds":{"r2":{"min":0.9},"rmse":{"max":0.5}}}'
#   -> "registry_name": "credit-risk-scorer"  (train with: --register credit-risk-scorer)
curl -XPOST localhost:8080/projects/credit-risk/models/scorer/discover
curl -XPOST localhost:8080/model-versions/<id>/evaluate
curl -XPOST localhost:8080/model-versions/<id>/promote
```

`scripts/train.py --register credit-risk-scorer` inside a pipeline step produces the
registry version; `discover` is the only way a version enters the platform. The
alias reconciler runs inside `make cp-reconcile` when `CP_MLFLOW_TRACKING_URI` is set.

| # | Gate | How to check |
| --- | --- | --- |
| 1 | Training output → ModelVersion | `discover` returns the new version with `source_pipeline_run_id` equal to the pipeline run |
| 2 | Registry version ↔ platform version mapped | `select version, external_ref from model_versions` |
| 3 | Duplicate discovery creates no duplicate | run `discover` twice → second says `already_known: N`, `created: []` |
| 4 | Evaluation runs | `POST …/evaluate` → `evaluations[0]` with `metrics` and per-threshold `checks` |
| 5 | Thresholds come from config | change them with `PUT /projects/{p}/models/{name}/thresholds`; the next evaluation uses the new ones, recorded evaluations keep theirs |
| 6 | Failed evaluation → REJECTED | threshold above the real metric |
| 7 | REJECTED cannot be promoted | `POST …/promote` → 409 |
| 8 | Passed evaluation → CANDIDATE | threshold below the real metric |
| 9 | Candidate can be promoted | `POST …/promote` → `CHAMPION`, `promotions[0].status: APPLIED` |
| 10 | Promotion is atomic and audited | `audit_events` has exactly one `model_version.promoted`; kill the API between steps / inject a DB error: either everything or nothing changed |
| 11 | Old champion history kept | promote a second candidate → first becomes `ARCHIVED`, its `promotions` still listed; `uq_model_versions_one_champion` prevents two champions |
| 12 | MLflow alias in sync | MLflow UI: alias `champion` → the champion's registry version, `candidate` → newest candidate |
| 13 | **Manual alias move is detected** | in the MLflow UI move `champion` to another version; within one reconcile pass `audit_events` shows `model.alias_drift_detected` then `model.alias_synced`, and the alias is back |

Judgement calls to confirm you agree with:

- Evaluation currently **reads metrics from the MLflow run behind the registry
  version**; it does not launch a separate evaluation job. A version with no
  tracking run is REJECTED (metrics missing = thresholds fail).
- A metrics outage leaves the version `EVALUATING` (retry by calling `evaluate`
  again), never `REJECTED`.
- Alias drift is **repaired immediately** (platform wins); the `alias_drift` flag is
  only visible if the repair fails, otherwise the audit trail is the evidence.
  A *missing* alias is treated as "not synced yet", not drift.
- The "candidate" alias points at the newest candidate.

---

## 5. M18 gate — Deployment & endpoint

Extra setup: KServe must be installed, with the MLflow runtime available (the
`InferenceService` uses `modelFormat: mlflow`, protocol v2), and the serving pods
need credentials for the artifact store.

```bash
# KServe (raw deployment mode avoids needing Knative/Istio locally)
kubectl apply -f https://github.com/kserve/kserve/releases/download/v0.14.1/kserve.yaml
kubectl apply -f https://github.com/kserve/kserve/releases/download/v0.14.1/kserve-cluster-resources.yaml
kubectl patch cm -n kserve inferenceservice-config --type merge \
  -p '{"data":{"deploy":"{\"defaultDeploymentMode\":\"RawDeployment\"}"}}'

# MinIO credentials for the storage initializer, attached to the SA the project namespace
# already has (mlp-workload). Adjust endpoint/keys to your platform-local values.
kubectl -n mlp-credit-risk create secret generic mlp-s3 \
  --from-literal=AWS_ACCESS_KEY_ID=... --from-literal=AWS_SECRET_ACCESS_KEY=...
kubectl -n mlp-credit-risk annotate secret mlp-s3 \
  serving.kserve.io/s3-endpoint=platform-minio.ml-platform.svc:9000 \
  serving.kserve.io/s3-usehttps=0
kubectl -n mlp-credit-risk patch sa mlp-workload -p '{"secrets":[{"name":"mlp-s3"}]}'
```

```bash
curl -XPOST localhost:8080/projects/credit-risk/deployments -H 'content-type: application/json' \
  -d '{"name":"credit-risk-prod"}'
curl -XPOST localhost:8080/projects/credit-risk/deployments/credit-risk-prod/revisions \
  -H 'content-type: application/json' -d '{"model":"scorer","version":1}'
curl localhost:8080/projects/credit-risk/deployments/credit-risk-prod   # poll until READY
curl -XPOST localhost:8080/projects/credit-risk/endpoints/credit-risk-prod/predict \
  -H 'content-type: application/json' \
  -d '{"inputs":[{"name":"x","shape":[1,3],"datatype":"FP64","data":[1,2,3]}]}'
```

| # | Gate | How to check |
| --- | --- | --- |
| 1 | CANDIDATE model deploys | `POST …/revisions` with a CANDIDATE version → 202, `desired_revision: 1` |
| 2 | Non-approved model refused | REGISTERED / EVALUATING / REJECTED / ARCHIVED version → 409 |
| 3 | DB object first | `select * from deployments, deployment_revisions` shows the rows **before** `kubectl get inferenceservice` does (stop the reconciler to see the gap) |
| 4 | Reconciler creates the KServe resource | `kubectl -n mlp-credit-risk get inferenceservice credit-risk-prod -o yaml` (annotation `mlp.io/revision`, `storageUri`) |
| 5 | Model really loads | `kubectl -n mlp-credit-risk logs <predictor pod> -c kserve-container` shows the model loaded; no `CrashLoopBackOff` |
| 6 | Readiness reaches platform state | `GET …/deployments/credit-risk-prod` → `READY`, `active_revision: 1` |
| 7 | Endpoint READY before deployment READY | never `READY` deployment with `PENDING` endpoint; check `audit_events` order `endpoint.ready` then `deployment.ready` |
| 8 | Real prediction succeeds | `POST …/endpoints/credit-risk-prod/predict` returns a prediction; before READY it returns 409 |
| 9 | Revision immutable | `GET` shows revision 1 unchanged after deploying version 2 |
| 10 | New model = new revision | deploy version 2 → `revisions: [1, 2]`, `desired_revision: 2`, then `active_revision: 2`; the endpoint stays READY throughout (old revision serving) — confirm no downtime with a loop of predictions |
| 11 | Old revision history kept | `revisions` still lists 1 with its model version |
| 12 | Delete → recreate | `kubectl -n mlp-credit-risk delete inferenceservice credit-risk-prod`; within a pass: `READY → DEGRADED → DEPLOYING → READY`, endpoint `UNAVAILABLE → READY`, resource back with revision annotation (`select action from audit_events` shows `deployment.drift_detected`, `deployment.redeploying`, `deployment.ready`) |

**The fault drill:** delete the InferenceService (gate 12) while a prediction loop is
running and record how long predictions fail; that is the real recovery time.

Judgement calls to confirm:

- "Approved" = CANDIDATE or CHAMPION. There is no staging/production split yet; the
  deployment *name* is the only environment.
- A FAILED deployment is **not** retried automatically; a new revision (or the same
  version after a fix, via a new revision) retries it. Say if you want auto-retry
  with backoff instead.
- A rollout of revision N+1 keeps the endpoint READY while N serves; if N+1 fails the
  deployment is FAILED and the endpoint UNAVAILABLE even though N might still be up,
  because the platform does not yet track per-revision serving (M19).

---

## 6. Code that has never run against the real thing

Written to the Argo API from knowledge of its schema; unit-tested only as
manifests/dicts. Check each against a real Argo:

00. **KServe adapter (M18), all of it** — `adapters/serving/kserve.py` has only been
    unit-tested as a manifest. Specifically check: (a) the status fields it reads —
    condition `Ready`, `status.modelStatus.transitionStatus == "UpToDate"`,
    `status.modelStatus.lastFailureInfo`, `status.address.url`; if a healthy service
    never turns READY in the platform, print `kubectl get isvc -o yaml` and compare;
    (b) the v2 predict path `/v2/models/<name>/infer` and that the control plane can
    reach `status.address.url` (it is a cluster-internal URL: from your laptop use
    `kubectl port-forward` or run the API in-cluster); (c) `storageUri` for MLflow 3:
    `model_artifact_uri` returns the logged model's `artifact_location`; the model
    server needs the directory that contains `MLmodel`, which may be one level deeper;
    (d) the merge-patch used when the InferenceService already exists.
0. **Omitted DAG tasks (M16)** — `_step_key`/`_NODE_PHASES` assume a task whose
   dependency failed shows up as a node with `type: Skipped`, `phase: Omitted`
   and `displayName` = task name. If steps stay `PENDING` after a failed
   upstream, inspect `kubectl get workflow -o json | jq '.status.nodes'`; the
   reconciler still closes them as `SKIPPED` once the run is terminal, so the
   end state is right even if the intermediate reporting is not.
1. **`adapters/workflow/argo.py` field names** — `status.phase` values,
   `status.nodes[*].{type,phase,message,templateName,id,outputs.exitCode}`,
   and that the pod name equals the node `id` (used by `get_logs`). Argo changed
   pod naming across versions; if logs come back empty, this is why.
2. **Image-pull detection** — relies on the Pending pod node's `message`
   containing `ImagePullBackOff` / `ErrImagePull` / `InvalidImageName`. If Argo
   does not surface that on the node, read the pod's container status instead.
3. **Cancel mapping** — `spec.shutdown: Terminate` → Argo reports phase `Failed`;
   the adapter maps that to `CANCELLED` only because `spec.shutdown` is set.
4. **`activeDeadlineSeconds: 3600`** is hard-coded; no per-job timeout yet.
5. **Idempotent submit** — relies on a 409 from creating a Workflow whose name
   already exists.
6. **Executor permissions** — the project namespace gets Role/RoleBinding
   `mlp-workflow-executor` (`workflowtaskresults` create/patch for SA
   `mlp-workload`). If steps run but the workflow never completes, check the
   Argo version's required RBAC and the pod's `wait` container log.
7. **Provisioner on kind vs. envtest** — only exercised on envtest. Re-run the
   M14 table on kind (the NetworkPolicy and namespace-deletion rows especially).

---

## 7. Missing pieces (not written yet)

- Argo Workflows install in `make local-up` / Helm / GitOps; the Argo
  controller's `workflowNamespaces`/RBAC must cover `mlp-*` namespaces.
- A Helm chart / manifests to deploy the **control plane** itself (API +
  reconciler Deployments, their ClusterRole: namespaces, serviceaccounts,
  resourcequotas, limitranges, networkpolicies, roles, rolebindings,
  `workflows.argoproj.io`, `pods/log`).
- CI job running `scripts/envtest.sh` and an integration test for
  `KubernetesClusterProvider` (currently only a manual smoke).
- `docs/evidence/m14/gate.md`, `docs/evidence/m15/gate.md` and README rows once
  the tables above pass.
- Step pods need MLflow/S3 credentials and a reachable tracking URI; there is
  no per-project secret/config mechanism yet (the M7 Secret lives in `ml-platform`).
- No retry for pipeline runs, no pipeline-run history cleanup, no per-step
  resource defaults.
- No `DELETE` for deployments, no per-revision serving state, no traffic split
  (`KServeServingProvider.set_traffic` raises `NotImplementedError` until M19).
- Predict is a thin pass-through with a 10 s timeout; no auth, rate limiting or
  request logging, and no stable external URL (a gateway).
- Per-project serving credentials are manual (see section 5); the control plane
  does not create them.
- No automatic discovery after a pipeline run succeeds (discovery is an explicit
  call); no comparison against the champion beyond recording its id as `baseline`;
  no rollback-to-previous-champion endpoint.
- Run ids are plain UUIDs; the plan's `run_01J…` display form is not done.
- Workflow pods are never garbage-collected; logs depend on pods surviving.
- No log streaming (single read), no per-run timeout, no resource defaults for
  jobs that omit `resources`.
- Sandbox blocker for future milestones (M15+ in the cloud): nested `runc`.
  Options: run the gates on your machine, or find a sandbox with a working
  container runtime (e.g. rootless `kind` with a userns-capable kernel).
