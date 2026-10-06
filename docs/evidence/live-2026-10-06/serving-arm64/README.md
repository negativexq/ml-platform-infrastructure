# ARM64 serving verification

Current result: [full CPU lifecycle](cpu-lifecycle-complete.json) passed all seven phases
in 647.65s; zero-pod activation took 1.25s. [Bad-candidate rollback](canary-rollback.json)
and [scoped storage-account cleanup](storage-account-cleanup.json) also passed.
The dated attempts below preserve failed baselines and the fixes they motivated.


This is an isolated ARM64 acceptance fixture, built from a dirty working tree with
`b7a3420-working-tree` image labels. It is not a clean production release artifact.

- [Local runtime](local-runtime.json): actual training artifact loads on native ARM64;
  readiness, MLflow JSON prediction and native V2 prediction pass.
- [Deployed source check](controlplane-source.json): the first deployed adapter fixes
  match tested working-tree bytes. The subsequent gateway fix is deployed in the
  image digest recorded in the gateway prediction report.
- [CPU partial report](cpu-lifecycle-storage-ref.json): real project/RBAC, training,
  MLflow registration/discovery and evaluation pass. KServe then reaches READY with
  the revision-specific storage account; this historical attempt failed with gateway 422 because its
  HTTP adapter forwarded `instances` to the tensor-only V2 route.
- The S3 storage Secret reference was missing from the acceptance fixture's model;
  the script now includes it. This was a harness error, not a reason to weaken auth.
- [Runtime manifest](mlflow-runtime.yaml) selects the native image for MLflow only.
  [Initializer manifest](s3-storage-initializer.yaml) handles classic S3 only; S3 is
  removed from the upstream default's URI list so selection is unambiguous. Other
  providers still use upstream containers and have no ARM64 acceptance proof.

Security release gate is **blocked**: [serving scan](trivy-serving.json) reports four
fixable HIGH findings (cryptography plus Starlette); [initializer scan](trivy-initializer-current.json)
reports one protobuf finding. No CVE exclusions were added. Upstream dependency bounds
prevent a simple lock update: MLServer caps FastAPI and full MLflow caps cryptography;
KServe 0.15's Python SDK caps protobuf below the fixed version. These images prove lab
architecture/runtime behavior and must not be presented as production-ready.
[Serving SBOM](sbom-serving.spdx.json) is retained. AMD64 dependency resolution matches
ARM64 versions; an AMD64 image runtime/build gate has not been run.

Gateway routing is now fixed in source through a shared helper: MLflow `instances`
uses `/invocations`, native tensor `inputs` uses `/v2/models/<name>/infer`. The gateway,
HTTP and KServe suites passed 58 tests; Ruff and mypy (five changed source files) passed.
[Actual gateway prediction](gateway-prediction.json) now passes after rollout:
HTTP 200 and the expected `-0.9069787623077955` result from the real trained artifact.
This verifies project → training/discovery → serving → gateway, but does not make
the interrupted CPU report a full canary/rollback/cold-start acceptance pass.

## Full lifecycle follow-up

[Healthy canary evidence](cpu-lifecycle-canary-pass.json) passed project/RBAC,
training/discovery, real gateway inference and the 10% → 100% canary promotion.
Prometheus recorded distinct stable/candidate platform revisions with zero error
rates and approximately 4.8–4.9 ms p95 latency at the final observation.
The overall report remains failed: the run was stopped during function readiness
when KServe omitted `readinessProbe.initialDelaySeconds: 0` and the reconciler
incorrectly treated that as drift, repeatedly creating immutable revisions.
A narrow default-aware comparison is deployed; omitted zero and changed nonzero
probe delays have regression coverage (32 KServe adapter tests passed).
The next live run reached function READY without revision churn, then hit a harness
pod-deletion timeout: Knative graceful termination exceeded the subprocess 30s limit.
[Retained report](cpu-lifecycle-delete-timeout.json) preserves that failure. Pod deletion
is now asynchronous and the existing bounded credential poll checks the new instance.
The completed rerun below subsequently passed all seven phases.

[Credential rotation follow-up](cpu-lifecycle-status-type.json) additionally passed
running-container credential retention, new value after restart and protected deletion
(409). Drift repair then exposed a harness type error: Kubernetes `status` is an object,
while application `status` is a string. Polling now checks terminal string states only;
harness regression tests cover Kubernetes progress and FAILED/REJECTED states.
The completed rerun below subsequently passed all seven phases.

[Identity-race report](cpu-lifecycle-identity-race.json) preserves the next harness
failure: an injected intermediate Knative revision became ready before the repaired
backend, while the desired apply-id had already changed. The poll now waits for the
immutable backend's matching apply-id; seven harness tests passed in total.

[Storage account cleanup](storage-account-cleanup.json) passed: both revision accounts
were retained across healthy canary promotion, then removed after deployment deletion.
A foreign-name account with matching labels and another deployment's account remained.
Cleanup conflict/outage retry is not covered by this result.

The separate bad-candidate fixture exposed [another defaulting failure](canary-empty-env-failure.json):
KServe omits empty `env` slices. The drift matcher now treats absent and empty lists as
equivalent while still detecting removed nonempty secret references (33 adapter tests
passed). The updated image and adversarial rerun passed; see the final report below.

[Bad-candidate rollback](canary-rollback.json) passed on the default-aware runtime:
stable error rate remained zero, candidate-only 503s triggered rollback, candidate
became REJECTED, and 30 post-rollback requests returned 200 using the original stable
image/platform revision. Rollback created a fresh backend; it did not retain its boot ID.
Separate instantaneous metric queries produced a small error-ratio overshoot above 1;
[shared-time follow-up](metric-snapshot-time.json) records the corrected adapter reading
exactly 1.0 against the real Prometheus (15m historical window). The [deployed-image rerun](canary-rollback-final.json) passed with candidate error rate
exactly 1.0 and stable error rate zero.

## Completed CPU gate

The passing report covers real project/RBAC/admission, two Argo/MLflow training runs,
automatic discovery/evaluation, serving/gateway, 10% → 100% promotion with timestamped
backend-specific metric samples, credential retention/restart/protected deletion,
same-platform-revision repair with a new matching Knative apply identity, then actual
zero pods and a fresh boot ID on activation. Zero-pod polling includes graceful old-pod
termination; the 647.65s duration is not cold-start latency. Local auth is none and the
control-plane artifact is dirty; production scan, OIDC, GPU and multi-node gates remain
open. Missing/currently empty metric samples do not fabricate a pass: recorded samples
are retained from traffic time, with observation timestamps.

## Forced Secret deletion follow-up

[Isolated lifecycle](secret-force-lifecycle.json) passed protected deletion (409), forced
deletion (204), retained value in the running container, missing-Secret failure on the
replacement pod, then recovery with the restored value and a new boot ID. The fixture
uses min/max scale 1 and contains no older serving deployments competing for its quota.

The retained failed attempts explain fixture interference:
[hard-kill attempt](secret-force-hard-kill.json) poisoned Knative container health, and
[shared-quota attempt](secret-force-quota-delay.json) observed missing-Secret startup
but exceeded its 90s recovery poll while older backends consumed the namespace's 8 CPU
quota during graceful termination. Neither report is presented as a passing gate.

The final control-plane artifact is
`localhost:5201/mlp-controlplane@sha256:69371adc5443b40803cbba3b567ad17d751c1d2e6129b39c32aae5fb5c76f167`.
[Installed source hashes](defaults-metrics-source.json) match the tested workspace.
The Docker disk-full incident was recovered by removing only task-owned temporary Go
caches, restarting Docker, rolling back the interrupted Helm release and deploying
revision 12. The old unrelated lab node was stopped again after Docker auto-started it.
This recovery is not a database backup/restore drill.
