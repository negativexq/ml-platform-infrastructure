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
> 5 M18 gate · 6 M19 gate · 7 M20 gate (UI) · 8 untested code · 9 missing pieces.

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
make cp-migrate                    # alembic upgrade head  (0001 → 0007)

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
# KServe. NOTE: M18 alone works in RawDeployment mode (shown below), but M19 canary
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
  because the platform does not yet track per-revision serving (M19).

---

## 6. M19 gate — Canary, promotion safety & rollback

**Setup is heavier than M18.** KServe's native canary split only exists in Serverless
mode (Knative Serving + Kourier/Istio), and the gates read per-revision Knative
metrics from Prometheus:

```bash
# Knative Serving + Kourier, then KServe in Serverless mode (default), instead of RawDeployment
# (follow the Knative and KServe install docs for the versions you pin)
export CP_PROMETHEUS_URL=http://localhost:9090     # your kube-prometheus-stack (M5); port-forward it
make cp-migrate                                    # 0007
make cp-reconcile                                  # now also drives rollouts
```

Check Prometheus actually has the series the gate queries, for a served revision:
`revision_request_count{namespace_name="mlp-credit-risk",revision_name="<rev>"}` and
`revision_request_latencies_bucket{...}` (queue-proxy metrics; names differ between
Knative versions — adjust `adapters/metrics/prometheus.py` if not).

```bash
D=localhost:8080/projects/credit-risk/deployments/credit-risk-prod
# stable revision 1 (champion) is READY and serving (M18). Candidate = version 2:
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

## 7. M20 gate — Minimal platform UI

Unlike M14–M19 this one **was verified here, in a real browser** (Chromium via
Playwright, against the demo control plane on in-memory fakes). What is left for you is
looking at it with *real* data, and the browsers I did not have.

```bash
make cp-demo            # http://localhost:8080 — seeded story, no infrastructure at all
make cp-test            # includes 36 UI tests (static rules + real-browser flows)
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
- Many projects / runs (the lists are capped at 8 recent runs per project; there is no
  pagination UI yet).
- Firefox and Safari. Only Chromium was exercised. The UI uses `<dialog>`,
  ES modules and CSS variables, all supported by current versions of both.
- Behind a reverse proxy / ingress: the CSP and `/ui` prefix assume the UI and API share an
  origin. If you put them on different hosts you must relax `connect-src` deliberately.

Judgement calls to confirm:

- **Actions in the UI:** promote, evaluate, discover versions, cancel run, abort rollout,
  rollback. **Not** in the UI: starting a rollout, creating anything. They stay API-only.
- **No authentication.** Anyone who can reach the UI can promote or roll back, exactly
  like the API today. Do not expose it beyond a trusted network until M21+ adds auth.
- **Live updates are polling** (3 s, only while something is in flight; the deployment
  page always polls because metrics are live). No WebSocket.
- **Styling is deliberately plain:** system fonts, one CSS file, light/dark by OS setting.

---

### UI/UX additions (after M20) and the move to React

The UI is now **React + TypeScript** (Vite, TanStack Query for loading and live refresh,
TanStack Router on the URL hash, native `<dialog>` for modals), in `controlplane/ui/web`.
The API client is typed from the control plane's OpenAPI (`make ui-api`), so a changed API that
the UI no longer matches fails `tsc`. The built bundle is committed under `controlplane/ui/static`
(fixed file names, reproducible build): Python tests and pip installs need no Node, and CI
rebuilds and fails on any difference. `make ui-dev` gives hot reload against `make cp-demo`.
The CSP is unchanged (`script-src 'self'; style-src 'self'; connect-src 'self'`). The rules
(only the API client calls the network, no subsystem names, no inline styles, no
`dangerouslySetInnerHTML`) are checked on the TypeScript source.

Verified in a real browser against the demo, like the rest of M20 (`make cp-test`, 59 UI tests):

- Chrome: light / dark / system theme (remembered; the only thing the UI stores, guarded),
  Ctrl/⌘+K command palette (fuzzy jump to project, model, deployment), `/` focuses the page
  filter, `g p`, `?` help, skip link, skeleton loading, per-page document titles.
- Projects: filter box and status filter (survive live refresh), **New project** form with
  inline validation and server errors.
- Project: **Run pipeline** / **Start job** forms, run filters, *Show more*, recent activity
  with the trace id of each change (copyable; paste it into Tempo).
- Run pages: step timeline on a shared time axis, log tools (wrap, follow while live, copy,
  download), **Run again**, copyable ids.

What is left for you: try it against real data and real browsers other than Chromium; the trace
ids in *Recent activity* only appear when the control plane runs with OTEL export on.

## 8. Code that has never run against the real thing

Written to the Argo API from knowledge of its schema; unit-tested only as
manifests/dicts. Check each against a real Argo:

000. **Canary adapter and metrics (M19)** — `KServeServingProvider` canary support
     (`canaryTrafficPercent` set via merge patch, `null` to remove; the
     `mlp.io/previous-revision` annotation; `components.predictor.latestCreatedRevision` /
     `previousRolledoutRevision` used to label metrics) and all of
     `adapters/metrics/prometheus.py` (metric names, label names `namespace_name` /
     `revision_name`, window `2m`, NaN handling). If gates never pass although traffic
     flows, run the four queries by hand in Prometheus first.
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

## 9. Missing pieces (not written yet)

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
- UI: no pagination, search or filtering; no create/edit forms; no per-user views; the
  audit timeline is shown only on the deployment page; accessibility was checked for
  keyboard operation of the DAG and dialogs and for non-colour status cues, but not with
  a screen reader.
- No `DELETE` for deployments, no per-revision serving state in the database (the
  rollout row is the only record of the split).
- Rollouts need Serverless KServe + Prometheus; there is no RawDeployment/own-gateway
  fallback, no pause/resume, no manual "promote now" or step skip, and no automatic
  retry of a rollout.
- The Prometheus window is fixed (`2m`) and independent of `step_seconds`.
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

## 10. Observability gate — OpenTelemetry (traces, metrics, alerts)

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
