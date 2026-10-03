# Technical review: platform readiness

Review date: 2026-10-03  
Reviewed branch: `v2`  
Reviewed commit: `05c754c`

## Scope and conclusion

The platform aims to let teams train, version, deploy and operate models and functions on
Kubernetes through one control plane. The code covers much of that workflow: project roles,
training and pipelines, model versions, immutable deployment revisions, canary releases,
a public gateway, LLMs and container-based functions.

The main gap is making that workflow installable and proving it against real infrastructure.
Several adapter and networking issues should be resolved before treating the platform as
ready for a shared cluster. Adding more product features would not address those issues.

This is a review of the current implementation, not a record of completed cluster gates.
It complements [the roadmap](roadmap.md) and [the verification checklist](local-verification.md).

## Evidence and limits

The review examined application services, Kubernetes/KServe adapters, reconciliation,
gateway limits, installation scripts, manifests, CI and recovery scripts.

The following targeted tests ran locally:

```bash
.venv/bin/python -m pytest \
  controlplane/tests/test_functions.py \
  controlplane/tests/test_gateway.py \
  controlplane/tests/test_rollouts.py \
  -ra -o addopts='' --disable-warnings
```

Result: **47 passed, 37 skipped**. PostgreSQL variants were skipped because neither
`CP_TEST_DATABASE_URL` nor the embedded PostgreSQL dependency was available. These results
do not validate the SQL migrations, browser flows or real serving infrastructure.

Two KServe status cases were also reproduced by supplying synthetic resource responses to
`KServeServingProvider.get_status()`. Findings below distinguish these reproductions from
static code observations and infrastructure behavior that still needs verification.

## Priority overview

| Priority | Area | Main action |
| --- | --- | --- |
| P0 | Serving readiness | Match readiness and metrics to the backend that actually serves the requested revision |
| P0 | Network connectivity | Define and test the ingress/egress paths for serving, identity and PostgreSQL |
| P0 | Installation | Package and bootstrap the new control plane and its dependencies |
| P1 | Version identity | Pin images to digests and restrict alias synchronization to registry-backed models |
| P1 | Limits and quotas | Enforce shared limits and account for concurrent calls and replicas |
| P1 | Operations | Add recovery, cleanup, readiness and reconciler ownership mechanisms |
| P1 | Verification | Run the real lifecycle gates and make relevant checks automatic |

P0 means a blocker for a dependable shared-cluster installation. P1 means a requirement
before relying on the platform for sustained team workloads.

## 1. Serving readiness can report the wrong revision

**Evidence: locally reproduced with synthetic KServe responses.**

Source: [KServe adapter](../controlplane/adapters/serving/kserve.py), particularly `get_status()`.

The adapter reads the platform revision from `metadata.annotations`, then associates that
revision with readiness and backend names from `status`. It does not establish that the
status reflects the newly requested specification.

Reproduced input:

- Metadata says platform revision `2`.
- Status still reports a ready backend named `old-revision-1`.
- The existing model status is `UpToDate`.

Observed output:

```text
state=READY
deployed_revision=2
ready_revisions=(2,)
backend_revisions={2: 'old-revision-1'}
```

This can mark a deployment ready prematurely and attribute the stable backend's metrics to
the candidate. The exact transition sequence must also be exercised against real KServe.

The second reproduction supplied a custom-container predictor with `Ready=True` but no
`modelStatus`. The adapter returned `PENDING`, because it requires
`modelStatus.transitionStatus == "UpToDate"` for every runtime. Whether the installed KServe
version supplies that field for custom containers remains a cluster verification task.

Recommended changes:

- Establish the correspondence between desired configuration, observed backend revision
  and readiness using the installed KServe version's supported status fields. Do not assume
  an annotation alone proves that a controller has applied a revision.
- Make readiness interpretation appropriate to each serving runtime.
- Associate metrics with the actual backend revision observed serving traffic.
- Preserve callability at zero replicas when Knative can activate the service; zero pods
  alone must not make an endpoint unavailable.

Acceptance: delayed status updates cannot mark a new revision ready; custom containers
reach READY; scale-to-zero followed by a gateway call succeeds; canary metrics identify
the candidate independently of stable traffic.

## 2. Network policies do not yet describe a working platform topology

**Evidence: static manifest and adapter inspection. Connectivity was not tested on a CNI.**

Sources: [namespace provisioner](../controlplane/adapters/kubernetes/provisioner.py) and
[gateway manifest](../k8s/gateway/gateway.yaml).

The project baseline policy admits ingress only from pods in the same namespace. It has
no explicit allowance for Knative's serving path or control-plane callers in other
namespaces. Those paths need to be tested and permitted as appropriate to the chosen
networking implementation.

The gateway's egress policy has no rule for its configured OIDC issuer. Discovery and JWKS
requests can therefore fail under enforced policy. Its PostgreSQL rule uses only a
`podSelector`, which selects pods in the gateway namespace. It does not cover the existing
PostgreSQL instance in `ml-platform` if that instance is reused.

The project baseline also restricts ingress only; it does not provide egress isolation.
These interpretations follow the [Kubernetes NetworkPolicy semantics](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

Recommended changes:

- Write down a connectivity matrix covering API, gateway, workloads, Knative, Argo,
  PostgreSQL, MLflow, storage, identity, DNS and telemetry.
- Generate the necessary policies from the selected deployment topology.
- Define a deliberate workload egress policy, including artifact and image-related needs.
- Check workload security contexts and namespace admission settings for both training
  containers and custom serving containers.

Acceptance: required connections succeed, prohibited cross-project connections fail,
and the same checks pass with NetworkPolicy enforcement enabled.

## 3. The new platform cannot yet be installed as a complete release

**Evidence: static inspection of packaging and bootstrap files.**

Sources: [Dockerfile](../Dockerfile), [local bootstrap](../scripts/local-up.sh),
[Helm charts](../helm/) and [GitOps applications](../gitops/applications/).

The current Dockerfile packages the original inference service. There is no control-plane
image or chart that deploys the API, reconciler, gateway and database migrations together.
The gateway manifest references an image that the repository does not build.

`local-up` bootstraps the original inference infrastructure. It does not install the new
control plane, Argo Workflows or KServe/Knative. The existing GitOps applications track
`main`; running them from a `v2` checkout does not deploy the new platform automatically.

Recommended deliverables:

- A control-plane image with explicit process entrypoints.
- A Helm chart for API, reconciler, gateway, migration Job, RBAC, secrets, probes and ingress.
- Pinned dependency installation and a clear supported version matrix.
- Bootstrap and acceptance commands for the complete platform, with optional GPU support.
- A release/source revision configuration for GitOps.

The API and reconciler can still be run on a laptop for development. Packaging is a blocker
for reproducible deployment, not a prerequisite for every local integration test.

Acceptance: a clean checkout can install the platform into a fresh cluster and pass its
acceptance suite without undocumented manual resources.

## 4. Version identity and registry ownership need stronger boundaries

**Evidence: static code inspection.**

Sources: [image validation](../controlplane/domain/entities.py),
[model service](../controlplane/application/models.py) and
[alias reconciler](../controlplane/reconciliation/model_aliases.py).

Function image registration accepts tags and deduplicates registrations by image reference.
A moved tag can make the same platform version execute different code. Keeping the tag in
an immutable deployment row does not make the referenced image immutable.

The alias reconciler processes all model kinds. Functions and Hub-based LLMs store source
references rather than MLflow registry version numbers, so they should not be synchronized
as MLflow aliases.

Function serving also currently uses fixed CPU/memory values. The contract lacks configurable
resource requests/limits, a readiness probe and managed private-registry credentials.

Recommended changes:

- Resolve tags to digests at registration, store the digest and retain the submitted tag
  only as display metadata. Alternatively, require digest references initially.
- Restrict registry alias synchronization to registry-backed versions.
- Keep execution settings immutable with the deployment revision and expose the settings
  needed to run realistic functions.
- Manage project pull secrets through references rather than plaintext configuration.

Acceptance: rollback restores the same executable artifact; non-registry models make no
MLflow alias calls; private images and configured resource requirements work in a cluster.

## 5. Limits and GPU accounting are not strict shared budgets

**Evidence: static code inspection; basic limiter behavior is covered by the targeted tests.**

Sources: [gateway service](../controlplane/application/gateway.py),
[in-memory limiter](../controlplane/adapters/gateway/limits.py) and
[deployment service](../controlplane/application/deployments.py).

Each gateway replica holds its own rate-limit buckets. Adding replicas increases the
effective limit. The supplied gateway manifest already requests two replicas.

LLM admission checks available budget without reserving tokens. Actual usage is charged
after the reply, allowing concurrent requests to enter against the same remaining budget.
Missing upstream `usage` is counted as zero. This is best-effort metering, not a strict token
budget.

GPU admission sums GPUs per deployment revision, including stable and candidate revisions,
but does not multiply by autoscaled replica counts. Kubernetes ResourceQuota provides a
separate enforcement boundary; the platform's reported/admitted capacity still needs to
match the intended replica policy.

Recommended changes:

- Use an atomic shared limiter behind the existing port.
- Specify whether token limits are soft metering limits or hard budgets. For hard budgets,
  reserve capacity and reconcile it with reported usage.
- Define handling for missing usage, interrupted streams and simultaneous requests.
- Account for replica ranges, canary overlap and serving overhead in resource admission.

Acceptance: configured budgets hold across replicas and concurrent callers, and quota
decisions agree with what Kubernetes can schedule.

## 6. Operational lifecycle and recovery are incomplete

**Evidence: static code and script inspection.**

Sources: [reconciler entrypoint](../controlplane/reconciler_main.py),
[deployment reconciler](../controlplane/reconciliation/deployments.py),
[workflow adapter](../controlplane/adapters/workflow/argo.py),
[gateway HTTP surface](../controlplane/gateway/app.py) and [backup script](../scripts/backup.sh).

Current gaps:

- No deployment deletion lifecycle or run/workflow retention policy.
- Logs depend on workflow pods surviving; no durable log archive or streaming interface.
- Workflow deadlines are fixed rather than configured per run.
- Several reconciliation loops catch conflicts per entity but let other provider failures
  abort the remainder of that reconciler's pass. The outer loop keeps other reconcilers
  running, but affected entities later in the pass can be delayed repeatedly.
- No explicit leader election or work-claim mechanism for multiple reconciler processes.
  Existing optimistic updates and deterministic names help, but do not establish safe
  ownership of every external side effect.
- API/gateway health endpoints return a constant liveness response. There is no separate
  readiness check for required database/schema availability.
- Backup scripts cover the MLflow database and artifacts, not the control-plane database
  holding projects, memberships, API keys, revisions and audit history.
- Deployment convergence checks status and revision identity rather than comparing the
  full desired serving specification. Manual spec changes retaining those identifiers
  may escape drift repair.

Recommended changes: deletion and retention state machines, durable logs, configurable
deadlines, per-entity error isolation/backoff, an explicit reconciler concurrency policy,
readiness checks, full-spec drift handling and a control-plane backup/restore drill.

Acceptance: one failing workload does not starve unrelated work; restart and concurrent
reconciliation do not duplicate effects; deleting resources releases capacity; restoring
the control-plane database preserves lifecycle and access state.

## 7. Verification and CI do not yet close the integration gap

**Evidence: repository documentation and workflow inspection.**

Sources: [CI workflow](../.github/workflows/ci.yml),
[gateway e2e script](../scripts/gateway_e2e.py) and
[local verification](local-verification.md).

The remaining real-system gates include KServe readiness, Knative traffic splitting and
scale-to-zero, Argo jobs/DAGs, GPU serving, ingress/TLS streaming, NetworkPolicy behavior
and in-cluster telemetry/alerts.

The gateway e2e script covers a classic model over real HTTP, but not LLM streaming/token
limits or function invocation. CI currently runs only through `workflow_dispatch`; gateway
e2e and envtest are not included as workflow checks.

The verification document contains stale claims about authentication, UI forms and filtering,
test counts and migration versions. Consolidating these with the later sections will make
the checklist usable as an acceptance record.

Recommended changes:

- Add real-HTTP gateway cases for functions and LLMs.
- Restore automatic checks when the intentional CI pause is lifted.
- Add a repeatable cluster smoke/acceptance job where the execution environment permits it.
- Record dependency versions, commands, results and recovery measurements for each gate.
- Keep GPU-specific gates separate from the CPU-only acceptance path.

## Suggested implementation order

1. Correct serving readiness/revision attribution and registry-kind handling.
2. Define and validate networking for the chosen cluster topology.
3. Build the image, chart and bootstrap path.
4. Prove the CPU-only lifecycle: project → training → version → deployment → gateway call
   → canary success/failure → rollback → function scale-to-zero and reactivation.
5. Add shared limits, replica-aware admission, cleanup and control-plane recovery.
6. Run GPU/LLM gates and expand continuous integration coverage.

The next release should be judged by that complete lifecycle and its failure behavior.
GPU infrastructure and AWS deployment can remain separate milestones; they need not block
the CPU-only platform from becoming reproducible and verifiable.
