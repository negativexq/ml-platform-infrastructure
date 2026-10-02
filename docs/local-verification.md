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

> Sections: 0 setup · 1 M14 gate · 2 M15 gate · 3 M16 gate · 4 untested code ·
> 5 missing pieces.

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
make cp-migrate                    # alembic upgrade head  (0001 → 0004)

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

## 4. Code that has never run against the real thing

Written to the Argo API from knowledge of its schema; unit-tested only as
manifests/dicts. Check each against a real Argo:

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

## 5. Missing pieces (not written yet)

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
- Run ids are plain UUIDs; the plan's `run_01J…` display form is not done.
- Workflow pods are never garbage-collected; logs depend on pods surviving.
- No log streaming (single read), no per-run timeout, no resource defaults for
  jobs that omit `resources`.
- Sandbox blocker for future milestones (M15+ in the cloud): nested `runc`.
  Options: run the gates on your machine, or find a sandbox with a working
  container runtime (e.g. rootless `kind` with a userns-capable kernel).
