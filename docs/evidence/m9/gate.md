# M9 Gate — evidence

Run date: 2026-09-11 · kind v0.33.0 (kindnet enforces NetworkPolicy) ·
trivy 0.67 · kubeconform 0.8.0

## NetworkPolicy — enforced, and proven edge by edge

The approach: **don't trust the CNI, test it.** kindnet's NetworkPolicy support
was verified empirically in [m7/gate.md](../m7/gate.md), then the full matrix
is checked from probe pods carrying the same labels the real workloads use
(`scripts/drills/network-policy.sh`).

```
NetworkPolicy matrix (5 policies active)

   OK   ml-platform → platform-mlflow    :5000   -> ALLOW
   OK   ml-platform → platform-minio     :9000   -> ALLOW
   OK   ml-platform → platform-postgres  :5432   -> DENY
   OK   train       → platform-mlflow    :5000   -> ALLOW
   OK   train       → platform-postgres  :5432   -> DENY
   OK   mlflow      → platform-postgres  :5432   -> ALLOW
   OK   mlflow      → platform-minio     :9000   -> ALLOW
   OK   bystander   → platform-postgres  :5432   -> DENY
   OK   bystander   → platform-minio     :9000   -> DENY

all edges match the intended matrix
```

Nine edges — four ALLOW, five DENY. The load-bearing denial: **the inference
application cannot open a connection to the metadata database.** It has no
reason to, and now it provably can't. `platform-mlflow:5000` is intentionally
reachable from any pod and the node port — it is the shared tracking API — so
the precise controls sit on PostgreSQL (MLflow only) and MinIO (an explicit
four-workload allow-list).

Policies (`helm/platform-local/templates/networkpolicy.yaml`):
`default-deny-ingress` (all pods) + per-service allow-lists.

## Pod security — every workload

```
workload               nonRoot noToken noPrivEsc dropALL seccomp        roRootFS
ml-platform-inference  True    SA      True      True    RuntimeDefault True
platform-minio-0       True    yes     True      True    RuntimeDefault (writable)
platform-mlflow        True    yes     True      True    RuntimeDefault (writable)
platform-postgres-0    True    yes     True      True    RuntimeDefault (writable)
train (Job)            True    yes     True      True    RuntimeDefault (writable)
```

- **`runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop:
  [ALL]`, `seccompProfile: RuntimeDefault`** — on all five.
- **ServiceAccount token not mounted.** Inference disables it at the
  ServiceAccount (`automountServiceAccountToken: false`, from M2) — verified
  in-pod: `/var/run/secrets/kubernetes.io/serviceaccount/` does not exist. The
  platform pods and Jobs disable it at the pod spec. Nothing in this namespace
  needs the Kubernetes API, so nothing has a token — which is the RBAC posture:
  zero access, no Role required.
- **`readOnlyRootFilesystem: true`** on inference, with `emptyDir` mounts for
  `/tmp` and `~/.cache` where MLflow's artifact download writes. The stateful
  services keep a writable root — PostgreSQL and MinIO write sockets and lock
  files outside their data volume, MLflow writes temp files — and are called
  out here rather than pretended otherwise.

## Image scanning

`scripts/security-scan.sh` (mirrored by the `security` and `image-scan` CI
jobs):

```
==> kubeconform — rendered manifests
   helm/ml-platform   : Valid: 13, Invalid: 0
   helm/platform-local: Valid: 5,  Invalid: 0

==> trivy — gate on FIXABLE HIGH/CRITICAL
   ml-platform-inference:dev  : GATE PASS (0 fixable)
   ml-platform-mlflow:local   : GATE PASS (0 fixable)
   ml-platform-training:local : GATE PASS (0 fixable)

==> trivy — embedded secrets
   all three images : clean
```

The gate runs `--ignore-unfixed`: it fails on any HIGH/CRITICAL that has a fix
available, so adding a vulnerable dependency breaks CI. The full (unfiltered)
count is ~51–57 per image, all in the Debian base — tracked with rationale in
[`.trivyignore`](../../../.trivyignore).

### The MLflow CVE trade

M7 ran MLflow **2.19** for its 297 MiB footprint. The trivy scan found **7
CRITICAL** in it — path traversal (CVE-2025-15036), arbitrary command
execution (CVE-2025-15379), and five more. Every fix landed only in 3.x; the
last 2.x release (2.22.1) still carries all seven.

Measured footprints, one gunicorn worker, idle:

| MLflow | RSS steady | fixable CRITICAL |
| --- | --- | --- |
| 2.19.0 | 297 MiB | 7 |
| 3.1.4 | 240 MiB | 7 |
| 3.4.0 | 295 MiB | 8 |
| 3.15.0 | **1.46 GiB** | **0** |

The memory floor jumps between 3.4 and 3.15 (the bundled job / genai
subsystem). `mlflow-skinny` as the server is not viable — its server module
hard-imports the genai job code.

**Decision: MLflow 3.15.0.** A security milestone shipping known-critical
software and ignoring it is the wrong message. The MLflow pod limit went to
1792 MiB; M10/M11 tune the rest of the namespace around it.

Two fixable HIGH remained after the bump (`cryptography`, `setuptools`,
`msgpack`, `wheel`, `jaraco.context`) — resolved by upgrading the transitive
deps in the image builds. What trivy still reports for `msgpack`/`setuptools`
is **pip's vendored copies** (`pip/_vendor/vendor.txt`), not the runtime
packages (which are patched — verified in the image filesystem); pip is never
invoked at container runtime. Documented in `.trivyignore`.

## Pod Security Standards — `restricted`, enforced

The namespace carries `pod-security.kubernetes.io/enforce: restricted`
([`k8s/base/namespace.yaml`](../../../k8s/base/namespace.yaml)). After applying
it, every Deployment, StatefulSet and Job was restarted:

```
$ kubectl apply -f k8s/base/namespace.yaml && kubectl -n ml-platform rollout restart ...
deployment "platform-mlflow" successfully rolled out
statefulset "platform-postgres" rolled out
statefulset "platform-minio" rolled out
deployment "ml-platform-inference" successfully rolled out
training job train-... completed

$ kubectl -n ml-platform get events --field-selector reason=FailedCreate
No resources found
```

No violation, no warning. The API server now **rejects** a non-compliant pod
applied to this namespace by hand — the hardening is not just in the charts.

## Regressions checked

The M8 persistence drill was re-run under the full M9 hardening
(NetworkPolicy + readOnlyRootFilesystem + no token):

```
=== BEFORE ===  model_versions: ml-platform-model|1  runs: 1  artifact objs: 7
recovered in 18s
=== AFTER ===   model_versions: ml-platform-model|1  runs: 1  artifact objs: 7
/predict → 200
```

(The drill's `mc` helper pod needed the `minio-shell` label to pass the new
NetworkPolicy — a real catch: the policy works, and ad-hoc tooling has to
declare itself.)

## Gate result

| Check | Result |
| --- | --- |
| container hardening on every workload | PASS (nonRoot, no privesc, drop ALL, seccomp) |
| ServiceAccount token not mounted | PASS (verified in-pod) |
| NetworkPolicy enforced | PASS (kindnet, verified) |
| allow/deny matrix proven | PASS (9/9 edges, real workload labels) |
| `inference → PostgreSQL` blocked | PASS |
| kubeconform | PASS |
| trivy gate (fixable HIGH/CRITICAL) | PASS (0 across all images) |
| trivy secret scan | PASS (clean) |
| MLflow on a version with all CVE fixes | PASS (3.15.0) |
| CI jobs added | PASS (`security`, `image-scan` with SBOM) |
| Pod Security Standards enforced (`restricted`) | PASS (all workloads comply) |

## Not done

- `readOnlyRootFilesystem` on the stateful services — they write outside their
  data volume; would need tmpfs mounts for `/var/run`, `/tmp`, lock dirs.
- No runtime security (Falco). Out of scope for a lab.
