# Verifying on a real cluster

What the platform does that has only been tested against fakes, a real PostgreSQL or schema
validation, and how to check each part on a real Kubernetes cluster. Run these on a
machine with a working container runtime. Earlier cloud-sandbox verification could not
start pods; those results do not establish Argo, KServe, CNI or GPU behavior.

**Already verified without a cluster:**
* Lint, type checks, and the unit, API and browser tests. Every use-case test runs against
  both the in-memory store and a real PostgreSQL.
* Migrations checked against the models.
* Real Prometheus, Keycloak and the gateway end to end (see the sections below).
* One run of the namespace provider against a real `kube-apiserver` + `etcd` +
  `kube-controller-manager` (`scripts/envtest.sh`): resources created, a second apply
  changed nothing, drift was repaired, and the namespace was deleted.

**Not yet verified:** anything that runs a pod, Argo Workflows, KServe or a CNI.

The image/chart and pinned offline bootstrap source are prepared; actual image builds,
cluster installation and runtime checks remain open. See [installation.md](installation.md), [networking.md](networking.md)
and [roadmap.md](roadmap.md).

---

> Sections:
> 0. Setup
> 1. Projects and isolation
> 2. Jobs and runs
> 3. Pipelines and tracking
> 4. Models, evaluation and promotion
> 5. Deployments and endpoints
> 6. Canary rollouts and rollback
> 7. Web UI
> 8. Code that has never run against the real thing
> 9. Missing pieces
> 10. Observability
> 11. Identity
> 12. Gateway
> 13. LLMs on GPUs
> 14. Functions

## 0. Setup on your machine

```bash
make local-up                      # kind cluster with MLflow, PostgreSQL, MinIO, Prometheus, Argo CD
pip install -e ".[dev,controlplane,controlplane-dev]"

# Argo Workflows (not part of local-up yet — see "Missing pieces")
kubectl create ns argo
kubectl apply -n argo -f https://github.com/argoproj/argo-workflows/releases/download/v3.6.2/install.yaml

# PostgreSQL for the control plane: reuse the platform one or any local instance
export CP_DATABASE_URL=postgresql+psycopg://USER:PASS@localhost:5432/controlplane
export CP_KUBECONFIG=$HOME/.kube/config      # context must be the kind cluster
make cp-migrate                    # alembic upgrade head (currently 0013)

make cp-run                        # API on :8080      (terminal 1)
make cp-reconcile                  # reconcilers       (terminal 2)
```

The reconciler needs cluster-wide permissions when run in-cluster; locally your
admin kubeconfig is enough.

Quick sanity (no cluster needed): `make cp-check`.

---

## 1. Projects and isolation

Drive everything through the API (`curl`/`/docs`), inspect with `kubectl`.

| # | Gate | How to check |
| --- | --- | --- |
| 1 | `POST /projects` writes a DB row | `psql -c "select name,status from projects"` |
| 2 | Reconciler creates the namespace | `kubectl get ns mlp-credit-risk` |
| 3 | Deterministic name `mlp-<name>` | same |
| 4 | `project_id` label on every resource | `kubectl get ns,sa,quota,limitrange,netpol,role,rolebinding -n mlp-credit-risk --show-labels` → `mlp.io/project-id` |
| 5 | ServiceAccount `mlp-workload` | `kubectl -n mlp-credit-risk get sa` |
| 6 | ResourceQuota `mlp-quota` applied | `kubectl -n mlp-credit-risk describe quota` |
| 7 | Baseline NetworkPolicy `mlp-baseline` | `kubectl -n mlp-credit-risk get netpol` **and prove enforcement**: a pod in another namespace must not reach a pod in `mlp-credit-risk` (kindnet enforces; see the NetworkPolicy drill in `docs/history/failure-engineering.md` for the shape) |
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

## 2. Jobs and runs

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

## 3. Pipelines and tracking

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
namespace is new; the original training Job got them from its own Secret).

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

## 4. Models, evaluation and promotion

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

## 5. Deployments and endpoints

Extra setup: KServe must be installed, with the MLflow runtime available (the
`InferenceService` uses `modelFormat: mlflow`, protocol v2), and the serving pods
need credentials for the artifact store.

```bash
# KServe. NOTE: plain deployments work in RawDeployment mode (shown below), but canary
# splitting needs KServe *Serverless* mode (Knative Serving + a gateway); see section 6.
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
  because the platform does not yet track per-revision serving (see canary rollouts).

---

## 6. Canary rollouts and rollback

**Setup is heavier than for plain deployments.** KServe's native canary split only exists in Serverless
mode (Knative Serving + Kourier/Istio), and the gates read per-revision Knative
metrics from Prometheus:

```bash
# Knative Serving + Kourier, then KServe in Serverless mode (default), instead of RawDeployment
# (follow the Knative and KServe install docs for the versions you pin)
export CP_PROMETHEUS_URL=http://localhost:9090     # your kube-prometheus-stack; port-forward it
make cp-migrate                                    # 0007
make cp-reconcile                                  # now also drives rollouts
```

Check Prometheus actually has the series the gate queries, for a served revision:
`revision_request_count{namespace_name="mlp-credit-risk",revision_name="<rev>"}` and
`revision_request_latencies_bucket{...}` (queue-proxy metrics; names differ between
Knative versions — adjust `adapters/metrics/prometheus.py` if not).

```bash
D=localhost:8080/projects/credit-risk/deployments/credit-risk-prod
# stable revision 1 (champion) is READY and serving. Candidate = version 2:
curl -XPOST $D/rollouts -H 'content-type: application/json' -d '{
  "model":"scorer","version":2,"steps":[10,25,50,100],
  "gate":{"max_error_rate":0.01,"max_p95_latency_ms":300,"min_requests":50,"step_seconds":60}}'
curl localhost:8080/rollouts/<id>            # watch status, canary_percent, traffic
# drive load through the endpoint the whole time (otherwise the gate fails closed):
k6 run scripts/loadtest/predict.js           # point it at the endpoint / platform predict URL
```

| # | Gate | How to check |
| --- | --- | --- |
| 1 | Traffic split applied | `kubectl -n mlp-credit-risk get isvc credit-risk-prod -o yaml` → `canaryTrafficPercent` follows 10→25→50; `status.components.predictor.traffic` shows two revisions with matching percents |
| 2 | Per-revision metrics separate | in Prometheus, `revision_request_count` for the canary revision and the stable one differ and match the split |
| 3 | Canary health gate exists | `GET /rollouts/{id}` shows `gate`; `audit_events` has `rollout.observing` / `rollout.step_applied` with the `gate` reason |
| 4 | Latency breach stops the rollout | candidate that sleeps (e.g. 2 s per predict) → `ROLLED_BACK`, reason `p95 latency … exceeds …` |
| 5 | Error-rate breach rolls back | **the drill below** |
| 6 | Successful canary → 100% | healthy candidate under load → `SUCCEEDED`, `traffic: {"2":100,"1":0}`, ISVC has no `canaryTrafficPercent` |
| 7 | Champion changes only after success | `GET /projects/credit-risk/models/scorer` → champion still v1 at every step until `SUCCEEDED`, then v2; `model_versions` shows v1 `ARCHIVED` |
| 8 | Rollback returns to previous revision | `POST $D/rollback` → `desired_revision` back to 1, ISVC annotation `mlp.io/revision: 1`, v1 `CHAMPION` again, v2 `ARCHIVED`; revisions 1 and 2 both still listed |
| 9 | Rollback is in the immutable audit trail | `select action,payload from audit_events where action in ('rollout.rolled_back','deployment.rolled_back')`; there is no UPDATE/DELETE path for `audit_events` |
| 10 | Failed model ends at 0% | after the drill: `traffic: {"1":100,"2":0}` and the canary revision receives no requests (Prometheus rate → 0) |

**The failure drill (the one that matters):** make a candidate that errors on
requests — e.g. train/register a model whose `predict` raises for most inputs (a tiny
sklearn `Pipeline` with a custom transformer that raises on a flag feature), evaluate it
to CANDIDATE, then run the rollout with load that exercises it. Expected: `10%` →
(gate observes) → `ROLLED_BACK` with `error rate … exceeds 1.00%`, ISVC back to revision
1 at 100%, v2 `REJECTED`, v1 still `CHAMPION`, `deployment` back to `READY`. Record how
long the canary served errors (it is bounded by `min_requests` + the reconcile interval).

Judgement calls to confirm:

- **A latency breach rolls back**, like an error-rate breach, instead of merely pausing:
  a paused canary would keep taking its share of traffic at bad latency.
- **The gate fails closed**: if a step finishes observing without `min_requests`
  requests, the rollout rolls back ("only N requests after 60s"). A canary nobody
  exercised has proven nothing, but this means **a rollout needs traffic**.
- **A canary that fails in production becomes `REJECTED`** (new `CANDIDATE → REJECTED`
  edge) so it cannot be promoted by accident; abort-before-traffic leaves it `CANDIDATE`.
- **Losing the serving resource mid-rollout rolls back** to stable rather than re-creating
  the canary, because a re-created canary alone would receive 100% of the traffic.
- **Deployment rollback also restores champion state** (new `ARCHIVED → CHAMPION` edge,
  used only here): the platform never calls a model champion that is no longer serving.
  It is refused unless the target revision's model is a former/current champion.
- The final step is `100`: the canary takes all traffic and must still pass the gate
  before the model is promoted, so a rollback after that point means re-deploying stable.

---

## 7. Web UI

Unlike the sections above, this one **was verified here, in a real browser** (Chromium via
Playwright, against the demo control plane on in-memory fakes). What is left for you is
looking at it with *real* data, and the browsers I did not have.

```bash
make cp-demo            # http://localhost:8080 — seeded story, no infrastructure at all
make cp-test            # includes static UI rules and real-browser flows
CP_UI_SCREENSHOTS=/tmp/shots pytest controlplane/tests/test_ui.py   # writes screenshots
```

Against the real stack the UI is the same: `make cp-run` serves it at `/ui` (and `/`
redirects there). It needs no extra configuration.

| # | Gate | Status |
| --- | --- | --- |
| 1 | UI talks only to the Platform API | **verified**: CSP `connect-src 'self'` (a script's `fetch` to another host is blocked — tested via the `securitypolicyviolation` event), only `api.js` calls `fetch`, no absolute URLs, no mention of MLflow/Argo/Kubernetes/KServe/Prometheus anywhere in the UI sources, and every browser request in every test went to the same origin |
| 2 | Not MLflow / Argo / Kubernetes | same as above (static + browser checks) |
| 3 | Project list / detail | **verified** with demo data (counts, statuses, PENDING project) |
| 4 | Pipeline run DAG / status | **verified**: succeeded, failed (downstream skipped), running |
| 5 | Run logs | **verified** (step logs, auto-selects the failed step) |
| 6 | Model versions / evaluations | **verified** (per-threshold checks, promotion history) |
| 7 | Promote action | **verified** (confirm dialog, champion/archived swap shown) |
| 8 | Deployment status | **verified** |
| 9 | Endpoint metrics | **verified** against the fake metrics provider; real Prometheus numbers still need your eyes |
| 10 | Rollback action | **verified**, including that the model's champion state follows |

New read APIs the UI needed (all under `/projects/{p}/`): `summary`, `endpoints`,
`endpoints/{name}/metrics` (per revision, never errors on provider failure), `audit`;
run listings now carry the pipeline / job name.

**To check on your machine with real data:**

- A real Argo DAG with many steps / wide fan-out: the layout is "columns by longest
  dependency chain"; very wide graphs scroll horizontally inside the card.
- Long logs: the log panel is a plain `<pre>` capped at 280 px with scrolling; there is no
  streaming (it re-reads on every 3 s poll, which is fine for KBs, not for MBs).
- Real endpoint metrics from Prometheus (section 6): `5xx rate`, `p95`, `requests/s`
  per revision while a canary runs; and the "Metrics unavailable" banner when
  `CP_PROMETHEUS_URL` is unset or wrong.
- Many projects / runs: verify filters and Show more against real data; the overview
  displays recent runs rather than the entire run history.
- Firefox and Safari. Only Chromium was exercised. The UI uses `<dialog>`,
  ES modules and CSS variables, all supported by current versions of both.
- Behind a reverse proxy / ingress: the CSP and `/ui` prefix assume the UI and API share an
  origin. If you put them on different hosts you must relax `connect-src` deliberately.

Judgement calls to confirm:

- **Actions in the UI:** project/model/job creation, pipeline/job starts, deploy and canary
  forms, promote, evaluate, discover versions, cancel run, abort rollout and rollback.
  The workflow table below describes the current React UI.
- **Authentication:** sign-in and project roles now protect the UI and the API (section 11).
  With `CP_AUTH_MODE=none` (the demo, local runs) there is none, so never expose such a
  deployment beyond a trusted network.
- **Live updates are polling** (3 s, only while something is in flight; the deployment
  page always polls because metrics are live). No WebSocket.
- **Styling is deliberately plain:** system fonts, one CSS file, light/dark by OS setting.

---

### UI/UX additions and the move to React

The UI is now **React + TypeScript** (Vite, TanStack Query for loading and live refresh,
TanStack Router on the URL hash, native `<dialog>` for modals), in `controlplane/ui/web`.
The API client is typed from the control plane's OpenAPI (`make ui-api`), so a changed API that
the UI no longer matches fails `tsc`. The built bundle is committed under `controlplane/ui/static`
(fixed file names, reproducible build): Python tests and pip installs need no Node, and CI
rebuilds and fails on any difference. `make ui-dev` gives hot reload against `make cp-demo`.
The CSP is unchanged (`script-src 'self'; style-src 'self'; connect-src 'self'`). The rules
(only the API client calls the network, no subsystem names, no inline styles, no
`dangerouslySetInnerHTML`) are checked on the TypeScript source.

Verified in a real browser against the demo, like the rest of the UI (`make cp-test`):

- Chrome: light / dark / system theme (remembered; the only thing the UI stores, guarded),
  Ctrl/⌘+K command palette (fuzzy jump to project, model, deployment), `/` focuses the page
  filter, `g p`, `?` help, skip link, skeleton loading, per-page document titles.
- Projects: filter box and status filter (survive live refresh), **New project** form with
  inline validation and server errors.
- Project: **Run pipeline** / **Start job** forms, run filters, *Show more*, recent activity
  with the trace id of each change (copyable; paste it into Tempo).
- Run pages: step timeline on a shared time axis, log tools (wrap, follow while live, copy,
  download), **Run again**, copyable ids.

Day-to-day workflows (each covered by a real-browser test):

| Need | Where |
| --- | --- |
| What is broken right now? | **Needs attention** on the home page and each project overview: failures of the last 24 h, unhealthy deployments, canaries in progress |
| Find a run | **Runs** tab: pipeline or job runs, filter by pipeline/job and status (server-side, `?status=` on the API), paging; filters live in the URL |
| Is this pipeline healthy? | **Pipelines**: success rate and typical duration over the last 20 runs, definition DAG per version, *Run* / *Run vN*, the definition as a `curl` for CI |
| Define and run containers | **Jobs**: *New job* (image, command, resources, env), per-job history and health, *Start*; failed job runs have **Retry** |
| Why did it fail? | Step reasons (exit code, or which upstream step stopped it), log search, error-line highlighting and an *Errors only* filter |
| Models | **Register model** with thresholds (`auc >= 0.9`), *Edit thresholds*, metric deltas against the champion, *Promote*, *Deploy* |
| Ship it | **Deploy a version**: to an existing or new deployment, as a gated canary (steps, error-rate and p95 gates) or a direct replace; API errors show inside the dialog |
| Is it serving? | **Deployments**: serving version, traffic split, live p95 / 5xx; **Try it** sends one request through the platform (with the `curl` equivalent) |
| Who changed what? | **Activity**: key events by default (routine step/canary ticks on request), search, kind filter, problems only, links to the thing changed, trace ids |
| Remove a project | **Settings** → *Delete project*, confirmed by typing its name |

What is left for you: try it against real data and real browsers other than Chromium; the trace
ids in *Activity* only appear when the control plane runs with OTEL export on. OIDC
authentication and project roles are implemented (section 11); actions are anonymous only
with `CP_AUTH_MODE=none`. Verify actor attribution using your configured identity provider.

## 8. Code that has never run against the real thing

Adapters have local manifest and synthetic-response tests. Check workflow behavior
against real Argo, and serving behavior against real KServe/Knative:

000. **Canary adapter and metrics** — `KServeServingProvider` canary support
     (`canaryTrafficPercent` set via merge patch, `null` to remove; the
     `mlp.io/previous-revision` annotation; `components.predictor.latestCreatedRevision` /
     `previousRolledoutRevision` used to label metrics) and all of
     `adapters/metrics/prometheus.py` (metric names, label names `namespace_name` /
     `revision_name`, window `2m`, NaN handling). If gates never pass although traffic
     flows, run the four queries by hand in Prometheus first.
00. **KServe adapter, all of it** — `adapters/serving/kserve.py` has only been
    tested with local manifests and synthetic controller/backend responses. Specifically
    check: (a) the status fields it reads —
    condition `Ready`, `components.predictor.latestCreatedRevision` and
    `latestReadyRevision`, `status.modelStatus.lastFailureInfo`, `status.address.url`;
    the referenced Knative Revision must carry `mlp.io/revision`, belong to the ISVC
    (`serving.kserve.io/inferenceservice` label), and report `Ready=True`.
    The reconciler needs `get` on `serving.knative.dev/revisions`. Older unmarked
    backends need redeployment. `modelStatus == UpToDate` is no longer a universal
    readiness requirement: ready custom containers and zero-pod services remain callable.
    Source inspection and synthetic regression tests passed; annotation propagation,
    delayed status, actual metric labels and cold-start activation remain cluster gates.
    Failure attribution requires `lastFailureInfo.modelRevisionName` to match the
    verified backend; if the installed version omits it, failure stays PENDING rather
    than marking a new revision FAILED from unbound historical information.
    If a healthy service never turns READY in the platform, print `kubectl get isvc -o yaml`
    and inspect the referenced Knative Revisions;
    (b) the v2 predict path `/v2/models/<name>/infer` and that the control plane can
    reach `status.address.url` (it is a cluster-internal URL: from your laptop use
    `kubectl port-forward` or run the API in-cluster); (c) `storageUri` for MLflow 3:
    `model_artifact_uri` returns the logged model's `artifact_location`; the model
    server needs the directory that contains `MLmodel`, which may be one level deeper;
    (d) the merge-patch used when the InferenceService already exists.
0. **Omitted DAG tasks** — `_step_key`/`_NODE_PHASES` assume a task whose
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
4. **Deadlines** now compile from persisted job/run/pipeline `timeout_seconds` (default 3600);
   real Argo timeout behavior remains a gate.
5. **Idempotent submit** — relies on a 409 from creating a Workflow whose name
   already exists.
6. **Executor permissions** — the project namespace gets Role/RoleBinding
   `mlp-workflow-executor` (`workflowtaskresults` create/patch for SA
   `mlp-workload`). If steps run but the workflow never completes, check the
   Argo version's required RBAC and the pod's `wait` container log.
7. **Provisioner on kind vs. envtest** — only exercised on envtest. Re-run the
   projects table (section 1) on kind (the NetworkPolicy and namespace-deletion rows especially).

---

## 9. Missing pieces (the current list is in [roadmap.md](roadmap.md))

- Argo Workflows install in `make local-up` / Helm / GitOps; the Argo
  controller's `workflowNamespaces`/RBAC must cover `mlp-*` namespaces.
- Build and exercise the prepared **control-plane image/chart**, including migration
  hooks, process commands, RBAC and read-only filesystems. The chart includes Knative
  Revision read permissions; complete topology policies and dependency bootstrap are
  still open. See [installation.md](installation.md).
- CI job running `scripts/envtest.sh` and an integration test for
  `KubernetesClusterProvider` (currently only a manual smoke).
- `docs/evidence/m14/gate.md`, `docs/evidence/m15/gate.md` and README rows once
  the tables above pass.
- Step pods need MLflow/S3 credentials and a reachable tracking URI. Project secret
  management and workload references are implemented ([secrets.md](secrets.md)); verify
  actual credential use and connectivity in the cluster. The original training Secret
  still lives in `ml-platform` and is not automatically copied or adopted.
- No retry for pipeline runs, no pipeline-run history cleanup, no per-step
  resource defaults.
- UI: forms, filters, run paging, activity and role-based access are implemented. Remaining
  checks include large real datasets, Firefox/Safari and screen-reader accessibility;
  keyboard operation and non-colour status cues have local browser coverage.
- No `DELETE` for deployments, no per-revision serving state in the database (the
  rollout row is the only record of the split).
- Rollouts need Serverless KServe + Prometheus; there is no RawDeployment/own-gateway
  fallback, no pause/resume, no manual "promote now" or step skip, and no automatic
  retry of a rollout.
- The Prometheus window is fixed (`2m`) and independent of `step_seconds`.
- The platform's own predict route is a thin pass-through for trying a model; outside
  callers use the gateway (`docs/gateway.md`, §12).
- Per-project serving credentials are manual (see section 5); the control plane
  does not create them.
- No automatic discovery after a pipeline run succeeds (discovery is an explicit
  call); no comparison against the champion beyond recording its id as `baseline`;
  no rollback-to-previous-champion endpoint.
- Run ids are plain UUIDs; the plan's `run_01J…` display form is not done.
- Workflow pods are never garbage-collected; logs depend on pods surviving.
- No log streaming (single read); deadlines are now configurable. No resource defaults for
  jobs that omit `resources`.
- Blocker in cloud development sandboxes: nested `runc`.
  Options: run the gates on your machine, or find a sandbox with a working
  container runtime (e.g. rootless `kind` with a userns-capable kernel).

## 10. Observability: traces, metrics, alerts

Verified in the sandbox with the real binaries (see `docs/observability.md`). What is left is
the in-cluster part.

```bash
make observability-up               # kube-prometheus-stack + Collector + Tempo + datasource + rules
kubectl -n observability get pods   # tempo, otel-collector Running
make alert-rules-test               # promtool: inference + control-plane rules
```

| # | Gate | Status |
| --- | --- | --- |
| 1 | Collector and Tempo configs valid | **verified**: `otelcol-contrib validate`, `tempo -config.verify=true` |
| 2 | Request → reconciler is one trace | **verified** in Tempo (demo, in-memory fakes) and in CI with an in-memory exporter |
| 3 | Metrics reach Prometheus, dashboard queries return data | **verified** (names and labels as documented) |
| 4 | Alerts fire as specified | **verified**: promtool tests |
| 5 | Manifests are valid Kubernetes | **verified**: kubeconform. **Not** applied to a cluster |
| 6 | Pods come up (`tempo` `/ready`, collector `13133`) with `readOnlyRootFilesystem` | **you**: Tempo writes under `/var/tempo` (emptyDir); confirm no other path needs to be writable |
| 7 | Grafana shows the Tempo datasource and the *Control plane* dashboard | **you**: the datasource sidecar label is `grafana_datasource`, set in `kube-prometheus-stack-values.yaml` (rerun `make observability-up`) |
| 8 | `ServiceMonitor` is picked up (`release: monitoring`), `job` is the service name | **you**: Prometheus → Targets; `honorLabels: true` is what makes `job="mlp-controlplane-reconciler"` |
| 9 | With the real stack: `ReconcilerStalled` fires after `kubectl scale deploy/reconciler --replicas=0`, clears after scale up | **you** (needs the control plane deployed; its manifests are still in §9) |
| 10 | Applications reach the Collector (`OTEL_EXPORTER_OTLP_ENDPOINT`) through NetworkPolicy | **you**: `ml-platform` policies may need an egress rule to `observability:4318` |
| 11 | Monitor page reads the platform's own metrics | **verified** against Prometheus 3.1.0 with promtool-written series (heartbeat, stalled reconciler, error ratios, p95). **You**: with the real stack, open `#/monitor` and scale the reconciler to 0; its heartbeat turns critical within ~3 minutes |

## 11. Identity: sign-in and project roles

Verified here, including against a real Keycloak 26.4 (see `docs/identity.md`). Left for you:

| # | Gate | Status |
| --- | --- | --- |
| 1 | Keycloak in kind (`make identity-up`), realm imported, pod ready under the restricted security context | **you** (verified here as a local Keycloak process, not as the pod) |
| 2 | `python scripts/identity_e2e.py` passes against it with the demo on :8080 | **verified here** against a local Keycloak; **you** against the in-cluster one |
| 3 | Your real identity provider: discovery, a `groups` claim (or set `CP_OIDC_GROUPS_CLAIM`), the `mlp` audience on access tokens | **you** |
| 4 | Behind TLS: `CP_PUBLIC_URL=https://...` makes the cookies `Secure`; sign-in still round-trips | **you** |
| 5 | A CI service account (client credentials) calling the API with a membership | **you** |

## 12. Gateway: public endpoints and API keys

Verified here (see `docs/gateway.md`):
* `make gateway-e2e` runs real PostgreSQL, the real gateway process and a model server
  speaking the v2 protocol over HTTP.
* Browser tests cover issuing a key, showing it once, calling with it, revoking it and closing
  the endpoint.
* The usage and health queries ran against Prometheus 3.1.0.
* The alerts pass promtool and the manifests pass kubeconform.

Left for you:

```bash
kubectl apply -f k8s/gateway/gateway.yaml   # after setting the host, the issuer and the image
```

| # | Gate | Status |
| --- | --- | --- |
| 1 | Gateway pods Ready (`/readyz`; `/healthz` is liveness), PDB holds one during a drain | **you** |
| 2 | Through the ingress with TLS: `curl https://<host>/v1/<project>/<endpoint>/predict -H 'Authorization: Bearer <key>'` gives the model's answer | **you**: needs the ingress controller and cert-manager |
| 3 | The gateway reaches KServe at the endpoint's in-cluster URL (`/v2/models/<name>/infer`) through Knative's local gateway | **you**: KServe Serverless; the NetworkPolicy allows `kourier-system`, `istio-system`, `knative-serving` and the project namespaces |
| 4 | A long answer is streamed, not buffered | **you**: `proxy-buffering: off` on the ingress; matters for LLM endpoints later |
| 5 | Two production replicas share PostgreSQL capacity, including concurrent reservations and restarts | **you**: live PostgreSQL/replica acceptance; demo/explicit memory mode remains per process |
| 6 | Prometheus scrapes `mlp_gateway_*` (OTLP → Collector → Prometheus), the usage panel and Monitor show data, `GatewayHighErrorRate` fires when the model is scaled to zero with no activator | **you** |
| 7 | A client-credentials token from Keycloak with the invoker role is accepted, without the role 403 | **you**: same issuer settings as the API |

## 13. LLMs on GPUs

Verified here:
* The KServe manifest for the Hugging Face runtime: model format, `hf://` storage, args,
  GPU resources and an optional token secret.
* GPU quota checks on deploy and canary, and the namespace quota per GPU.
* Chat completions through the gateway: streamed and not, token metering, token limits.
* Token usage queries against a real Prometheus.
* The UI in a real browser: playground, hub registration, GPU quota.

All of it ran against fakes; no model has run on a GPU yet. Left for you:

```bash
# a GPU node with the NVIDIA device plugin, KServe Serverless with the Hugging Face runtime
kubectl get nodes -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'
kubectl get clusterservingruntimes | grep huggingface
```

| # | Gate | Status |
| --- | --- | --- |
| 1 | A platform admin sets a project's GPU quota; the namespace ResourceQuota shows `requests.nvidia.com/gpu` and the CPU and memory that come with it | **you** |
| 2 | Deploy an LLM version (`hf://Qwen/Qwen2.5-0.5B-Instruct@<commit>` is small enough for one GPU): the InferenceService gets `modelFormat: huggingface`, `--model_name=<deployment>`, `nvidia.com/gpu` limits, and goes READY | **you**: the first load downloads the weights; give it time |
| 3 | A gated model (Llama) loads with the token in Secret `mlp-hf-token` (key `token`) in the project namespace, and fails clearly without it | **you** |
| 4 | Playground answers through `POST …/chat`; KServe answers at `/openai/v1/chat/completions` with `usage` | **you** |
| 5 | Through the gateway with `"stream": true`: events arrive one by one (not all at the end), the last one carries `usage`, and `mlp_gateway_tokens_total` grows by those numbers | **you**: also checks that the ingress does not buffer (`proxy-buffering: off`) |
| 6 | A token limit of 500 per minute: a few calls pass, then `429` with `Retry-After`, then calls pass again after a minute | **you** |
| 7 | Canary between two LLM versions needs twice the GPUs; with a quota of 1 it is refused with the numbers, with 2 it runs and the traffic split works | **you** |
| 8 | Deploying a deployment with `--tensor_parallel_size=2` (a model with 2 GPUs) schedules on a node with 2 GPUs | **you**: optional |

## 14. Functions

Verified here:
* The KServe manifest for a function: a custom container, replica range, concurrency, port
  and environment.
* Image versions as candidates, deploys and the `function` endpoint.
* Calls through the platform and through the gateway (`/invoke`).
* The UI in a real browser.

All of it ran against fakes; no function container has run in a cluster yet. Left for you:

| # | Gate | Status |
| --- | --- | --- |
| 1 | A function deploys from a real image: the InferenceService has `containers[0].image`, `minReplicas`, `maxReplicas`, `containerConcurrency`, and goes READY | **you** (KServe Serverless) |
| 2 | With `min_scale: 0`: after a few idle minutes the pod is gone; the next call through the gateway succeeds within the 60 s timeout (cold start) | **you** |
| 3 | Under load it scales up to `max_scale` and no further | **you** |
| 4 | A canary between two images splits traffic and rolls back when the new image returns 5xx | **you** |
| 5 | The image is pulled from a private registry with the project's pull secret | **you**: management API/UI and references implemented; live pull pending ([secrets.md](secrets.md)) |

## Further local remediation verification — 2026-10-05

No container runtime, PostgreSQL server, browser or cluster was started for this batch.
Cluster resources were not applied or removed. See [operations.md](operations.md) and
[recovery.md](recovery.md) for the new behavior and defaults.

- `pytest controlplane/tests --ignore=controlplane/tests/test_ui.py
  --ignore=controlplane/tests/test_ui_auth.py
  --ignore=controlplane/tests/test_persistence_pg.py -k 'not sql'`: **317 passed,
  5 skipped, 172 deselected**. The skips are optional Prometheus tests; PostgreSQL and
  browser cases were excluded to keep memory use low.
- Later targeted checks after adding backup-inventory, empty-serving-topology and retention
  failure-isolation regressions: **22 passed / 8 deselected** for backup/network/token/LLM,
  and **21 passed / 21 deselected** for run/notification memory cases. These overlap the
  broad suite and must not be added to its count as unique tests.
- `ruff check controlplane scripts/controlplane_backup.py`: passed.
- `mypy controlplane scripts/controlplane_backup.py`: all **152 files** passed. Two existing
  SQLAlchemy typing issues were corrected without changing migration SQL behavior.
- OpenAPI TypeScript schema regenerated; `npm run build` passed. The existing bundle-size
  advisory remains; browser interaction was not exercised for the new form/button.
- `helm lint helm/controlplane`: passed. Default chart: **14 schema-valid resources**;
  both isolation toggles enabled with explicit API/external CIDRs: **18 schema-valid
  resources**. Missing API CIDRs correctly refuse isolation rendering.
- PostgreSQL offline upgrade DDL generated through `0015`; the three deadline columns,
  two cleanup markers and retention indexes render. This does not verify applying or
  downgrading the migrations against a live database.
- Backup safeguard tests cover corrupt archives, preview mode, nonempty-target refusal,
  atomic restore flags, schema/count verification and inventory coverage. No actual
  snapshot dump/restore was performed; the recovery acceptance drill remains open.

Pending runtime gates: DB outage/schema mismatch and recovery; real function probes and
resource scheduling; selected CNI ingress/egress; Argo deadline/cancellation/foreground
cleanup; asynchronous KServe removal and GPU reservation release; real LLM streaming and
usage settlement; compatible PostgreSQL snapshot backup/restore with measured RPO/RTO.

## Project secrets and serving drift verification — 2026-10-05

No PostgreSQL server, browser, Docker runtime or cluster was started for this batch.

- `make cp-check-light` contains the low-memory lint/type/memory-test commands. The
  equivalent commands run locally passed: **337 passed, 5 skipped, 179 deselected**.
  Optional Prometheus cases skipped; SQL/browser cases were deliberately excluded.
- Ruff passed; mypy checked **158 files**, including backup tooling, without errors.
- OpenAPI schema validation and UI TypeScript/production build passed. The existing
  bundle-size advisory remains; the new Settings form has not been tested in a browser.
- Helm lint passed; default chart rendered **14 valid resources** with kubeconform.
  API secret/namespace RBAC is prepared and has not been applied to a cluster.
- PostgreSQL offline DDL through head `0016` passed and matches the three new reference
  columns in ORM metadata. No live migration/row-lock concurrency gate was run.
- Regression coverage includes secret admin isolation, cross-project isolation,
  write-only responses/audits, version preconditions for rotation/deletion, redacted
  backend/validation errors, protected/forced deletion, private registry type checks,
  environment conflicts, Argo/KServe references and immutable deployment snapshots.
- Serving regressions verify same-revision repair, complete predictor replacement,
  resource-version preconditions, preservation of unowned metadata, idempotent reapply
  and waiting for the backend with the new apply ID.

Live checks and rotation/recovery limitations are recorded in [secrets.md](secrets.md).

## Shared budgets, discovery and release preparation (2026-10-05)

This batch started no Docker, cluster, PostgreSQL or browser processes. Tests briefly
started two native loopback HTTP servers, then stopped them; SQLite was used for local
shared-limiter concurrency. Live-system checks below remain pending.

- Final lightweight suite: **354 passed, 5 skipped, 181 deselected**. PostgreSQL/browser
  variants were deliberately excluded; skipped provider gates remain pending.
- Ruff passed; mypy passed for 168 source files.
- UI TypeScript/production build passed; the existing >500 KB bundle warning remains.
- Native HTTP tests cover function forwarding, LLM JSON/SSE, first-event streaming,
  concurrent reservations, usage refunds, missing usage and peer disconnection.
- Shared SQLite bucket tests cover two instances, concurrent callers, atomic composite
  admission, debt/refunds and restart persistence. These do not prove PostgreSQL locking.
- Maximum-replica GPU/transition quota tests and full Hub SHA validation passed. Hub
  fixtures are synthetic; no weights were downloaded or GPU workloads started.
- Scoped pipeline discovery retries, durable completion and stale retention updates
  passed with the memory backend; PostgreSQL variants were excluded for RAM.
- Secret role/catalog/reference tests passed; browser interaction and private-image
  pulls/rotation remain untested in this batch.
- Bootstrap tests cover offline preparation, immutable inputs, tampered caches and
  dirty-checkout rejection before cluster writes. All nine cached dependencies passed
  SHA-256 verification; KServe CRD OCI pull by digest was also verified.
- Helm lint and default rendering passed; kubeconform validated 14 chart resources.
  The locally prepared bootstrap render also validates; installer shell syntax passes.
  The preview uses a placeholder image and a dirty checkout, so it cannot be installed.
- PostgreSQL offline DDL rendered through head `0019`; ORM/backup inventory contains
  19 durable tables. Live migration/backup/restore was not run.

Remaining gates: real PostgreSQL migrations/locking/outages, image build and four process
commands, pinned dependency installation on the target Kubernetes version, CNI/RBAC,
real CPU/GPU/TLS serving and canary/rollback, workload credential rotation/private pulls,
UI browser acceptance and recovery drills. See [roadmap.md](roadmap.md).

## Scoped RBAC, availability and release gates (2026-10-05)

No Docker, PostgreSQL, browser or cluster process was started for this batch. Native
loopback HTTP servers used by existing gateway tests were stopped after their tests.

- Lightweight suite: **366 passed, 5 skipped, 184 deselected**. SQL/browser variants and
  two opt-in PostgreSQL acceptance checks remain unexecuted.
- Ruff passed; mypy passed for **177 source files**, including all manual acceptance tools.
- UI typed API regeneration and TypeScript/production build passed; the existing bundle
  size warning remains (about 557 KB).
- Helm default/local lint passed. Default schema validation: **19 valid resources**;
  production profile with synthetic site values: **23 valid resources**. Missing production
  values are deliberately rejected. These are rendering results, not CNI/RBAC evidence.
- Unit gates cover namespace Secret binding shape, API ClusterRole Secret removal, PDB/HA
  settings, migration lock refusal, standby expiry/CAS conflicts and fail-stop on renewal
  outage. These do not prove live Lease failover or pod drain.
- Storage reference validation, S3 annotation preservation on rotation, protected deletion
  and separate revision-owned token-disabled storage accounts passed. No private artifact
  was downloaded; no Secret values appear in responses or reports.
- Image release orchestration was exercised with mocked commands; all four entrypoints,
  image ID reuse, cleanup, scans and mandatory SBOM commands were checked. No actual image,
  Trivy result or SBOM was generated.
- Acceptance CLI help paths and CPU default plan-only behavior passed. Recovery refusal
  and restoration of source libpq environment after failure were tested without a server.

The image, load/outage, recovery and CPU lifecycle commands in [acceptance.md](acceptance.md)
remain live gates. First deployment must stop any Lease-unaware old reconciler before
starting the new leader/standby pair. See [installation.md](installation.md).

Read-only tool inventory: Docker daemon reachable, Docker/Trivy/kubectl installed; Syft
and native `psql`/`pg_dump`/`pg_restore` absent. No fixture, image build or drill was
started. Release preflight rejects missing tools before building or creating fixtures.
