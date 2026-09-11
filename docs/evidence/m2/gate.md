# M2 Gate — evidence

Run date: 2026-09-10 · kind v1 cluster `ml-platform` (1 control-plane, 2 workers)
· image `ml-platform-inference:dev` loaded with `kind load docker-image`

Platform services (MLflow, MinIO, PostgreSQL) stayed in Docker Compose on the
host, as the roadmap allows for M2. Pods reach them through a `hostAliases`
entry pointing at the kind bridge gateway, injected by
[`scripts/kind-up.sh`](../../../scripts/kind-up.sh). M3 turns that substitution
into a Helm value.

## Reproducibility

```
$ make kind-up
==> Creating kind cluster 'ml-platform' (idempotent)
==> Detecting the address kind nodes use to reach the host
platform host: 172.21.0.1  # Docker-assigned kind bridge gateway; machine-specific, not stable
==> Ensuring the M1 platform accepts requests from that address
==> Building and loading the image into the cluster
==> Applying manifests
namespace/ml-platform created
serviceaccount/inference created
secret/inference-artifact-store created
configmap/inference-config created
deployment.apps/inference created
service/inference created
==> Waiting for rollout
deployment "inference" successfully rolled out
```

The script is idempotent — rerunning it against an existing cluster reuses it.

## Deployment healthy, 2 replicas, Service routing

```
NAME                        READY   UP-TO-DATE   AVAILABLE   IMAGES
deployment.apps/inference   2/2     2            2           ml-platform-inference:dev

NAME                             READY   STATUS    IP           NODE
pod/inference-7546c7d5b4-fz9g5   1/1     Running   10.244.1.3   ml-platform-worker2
pod/inference-7546c7d5b4-vt8nh   1/1     Running   10.244.2.3   ml-platform-worker

NAME                TYPE       CLUSTER-IP     PORT(S)        AGE
service/inference   NodePort   10.96.32.155   80:30080/TCP   7m11s
```

```
$ make smoke
  PASS  GET /health                        200
  PASS  GET /ready                         200
  PASS  POST /predict (valid)              200
  PASS  POST /predict (invalid)            422
  PASS  ready replicas                     2
  PASS  ready Service endpoints            2
smoke test PASSED
```

Note the two replicas landed on different worker nodes, so the Service is
genuinely load-balancing across hosts rather than across processes on one.

## Failure tests

| Test | Evidence |
| --- | --- |
| Pod deletion self-recovers | [pod-recovery.md](pod-recovery.md) — replaced and ready at t+13s, Service never non-200 |
| Readiness failure prevents traffic | [readiness-failure.md](readiness-failure.md) — bad pod `ready=false`, **0 restarts**, rollout stalls, old pods keep serving |
| Rolling update maintains availability | [rolling-update.md](rolling-update.md) — first run dropped 1/90 requests; `preStop` drain added; re-measured 120/120 |

## Gate result

| Check | Result |
| --- | --- |
| kind cluster reproducible | PASS (`make kind-up`, idempotent) |
| deployment healthy | PASS |
| 2 replicas available | PASS (on separate nodes) |
| service routing works | PASS |
| pod deletion self-recovers | PASS (13s) |
| readiness failure prevents traffic | PASS (0 restarts, endpoint `ready=false`) |
| rolling update works | PASS after `preStop` fix (120/120 requests OK) |
