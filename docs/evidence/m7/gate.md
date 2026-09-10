# M7 Gate — evidence

Run date: 2026-09-11 · kind v0.33.0 cluster `ml-platform` · MLflow **2.19.0**

**No Docker Compose.** M2–M6 kept MLflow, PostgreSQL and MinIO in Compose on
the host. M7 moves all three into the cluster via `helm/platform-local`, a
chart that is never applied outside kind — in AWS these become RDS, S3 and an
in-cluster MLflow deployment.

## The MLflow downgrade (3.16 → 2.19)

Measured, not guessed. MLflow 3.16's server, one worker, idle, no requests:

```
t1 mem=89 MiB
t2 mem=1.37 GiB
t3 mem=1.56 GiB    <- steady state
```

It climbed to 1.56 GiB within 9 seconds of "Application startup complete" and
OOMKilled (exit 137) under any lab-sized limit. The kind Docker VM has 7.75 GiB
total for three nodes plus everything else; a 1.5 GiB idle dependency does not
fit alongside the Prometheus stack M10 adds.

MLflow 2.19.0, same test:

```
t1 mem=100 MiB
t2 mem=297 MiB     <- steady state
```

5x smaller. 2.19 also drops the DNS-rebinding "allowed hosts" middleware that
complicated M1–M6, and its gunicorn worker model is predictable. The pin moved
in four places: the server image, the training image, `mlflow-skinny` in the
app, and the `train` extra. A 3.16-migrated Postgres schema cannot be read by
2.19 (`alembic revision b7e2c1a4d9f3` does not exist in 2.x) — the emptyDir was
wiped and 2.19 migrated from scratch.

## Everything on Kubernetes

```
$ docker compose ps
NAME   IMAGE   COMMAND   SERVICE   CREATED   STATUS   PORTS
(empty)

$ kubectl -n ml-platform get pods -o wide
NAME                                     STATUS      NODE
ml-platform-inference-6dbd79857d-85rq7   Running     ml-platform-worker2
ml-platform-inference-6dbd79857d-llnkx   Running     ml-platform-worker
platform-minio-6cfb86d76d-spkqx          Running     ml-platform-worker2
platform-mlflow-844f984bf7-fvc9w         Running     ml-platform-worker
platform-postgres-69d995584-c9c4b        Running     ml-platform-worker2
train-1789078380-cfsct                   Completed   ml-platform-worker
```

## Full lifecycle, entirely in-cluster

**Training ran as a Kubernetes Job** (`scripts/train-job.sh` →
`k8s/jobs/train.yaml`, image `ml-platform-training:local`), talking to
`platform-mlflow:5000` over cluster DNS:

```
Successfully registered model 'ml-platform-model'.
Created version '1' of model 'ml-platform-model'.
metrics={'r2': 0.9984040871752516, 'mae': 0.07600518323513163, 'rmse': 0.09519293509032241}
```

Identical metrics to M1 — the training is still deterministic across the move
off Compose.

**Metadata in the PostgreSQL pod:**

```
$ kubectl -n ml-platform exec deploy/platform-postgres -- \
    psql -U mlflow -d mlflow -tA -c "select name, version from model_versions;"
ml-platform-model|1

19 MLflow tables present.
```

**Artifact in the MinIO pod:**

```
$ mc ls --recursive l/mlflow-artifacts/
1/9301e6a98f6b4cd193d1ee3e66de62e1/artifacts/model/MLmodel
1/9301e6a98f6b4cd193d1ee3e66de62e1/artifacts/model/model.pkl
1/9301e6a98f6b4cd193d1ee3e66de62e1/artifacts/model/conda.yaml
...
```

(2.19 serialises sklearn models as `model.pkl`, where 3.16 used `model.skops`.)

**Inference pulled the artifact and serves it:**

```
$ curl -s localhost:30080/ready
{"ready":true,"state":"ready","model_version":"models:/ml-platform-model/1",
 "model_source":"mlflow","detail":null}                              [200]

$ curl -s -X POST localhost:30080/predict -H 'content-type: application/json' \
    -d '{"features":[1.0,2.0,3.0]}'
{"prediction":-0.9069787623077955,"model_version":"models:/ml-platform-model/1"}
```

M1 predicted `-0.9069787623077947` for the same input; the 8th decimal differs
because 2.19 and 3.16 pickle the fitted `Ridge` slightly differently. Same
model, same math.

## Bonus: NetworkPolicy enforcement verified early

M9 depends on kindnet actually enforcing NetworkPolicy — kindnet historically
ignored it silently. Tested now so M9 does not hit a surprise:

```
no-policy round1: open       (busybox → platform-postgres:5432)
no-policy round2: open
# apply deny-all-ingress on postgres
deny-policy round1: closed
deny-policy round2: closed
# mlflow still healthy: 200
```

**kind v0.33.0's kindnet enforces standard NetworkPolicy.** M9 proceeds with
plain NetworkPolicy + allow/deny integration tests; no Calico needed.

## Gate result

| Check | Result |
| --- | --- |
| no Docker Compose service running | PASS |
| platform-local (PG + MinIO + MLflow) healthy in cluster | PASS |
| training runs as a cluster Job | PASS |
| MLflow metadata persisted in the PostgreSQL pod | PASS |
| artifact persisted in the MinIO pod | PASS |
| inference downloads artifact over cluster DNS | PASS |
| `/ready` 200, `/predict` 200 | PASS |

## Not done in M7

- **emptyDir, not PVC.** Pod restart loses the model registry and artifacts.
  That is M8's problem to solve and prove.
- **Argo CD not yet re-pointed.** `gitops/applications/platform-local.yaml` is
  written; `scripts/argocd-up.sh` applies both Applications. Verified in M8+.
