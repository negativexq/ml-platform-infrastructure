# M8 Gate — evidence

Run date: 2026-09-11 · kind `standard` StorageClass (rancher.io/local-path,
`WaitForFirstConsumer`, `Delete` reclaim)

PostgreSQL and MinIO are now StatefulSets with `volumeClaimTemplates`; the
`helm/platform-local` chart renders a Deployment+emptyDir or a
StatefulSet+PVC off one `persistence.enabled` flag.

```
$ kubectl -n ml-platform get pvc
NAME                       STATUS   CAPACITY   SC         MODE
data-platform-minio-0      Bound    5Gi        standard   [ReadWriteOnce]
data-platform-postgres-0   Bound    2Gi        standard   [ReadWriteOnce]

$ kubectl -n ml-platform get sts
NAME                READY
platform-minio      1/1
platform-postgres   1/1
```

Not production HA — single replica each, `Delete` reclaim. AWS uses RDS and S3.
What M8 proves is the **data-loss and restore contract**.

## Drill 1 — pod deletion does not lose data

`scripts/drills/persistence.sh`: delete `platform-postgres-0`,
`platform-minio-0` and the MLflow pod together, wait for all three to come back
on the same PVCs, compare the registry.

```
=== BEFORE ===
  model_versions : ml-platform-model|1
  runs           : 1
  metrics rows   : 3
  artifact objs  : 7

=== deleting platform-postgres-0, platform-minio-0, mlflow pod ===
--- waiting for all three ready again ---
recovered in 15s

=== AFTER ===
  model_versions : ml-platform-model|1
  runs           : 1
  metrics rows   : 3
  artifact objs  : 7

=== inference still serves the model ===
{"prediction":-0.9069787623077955,"model_version":"models:/ml-platform-model/1"} [200]
```

Every count identical. The pods rebound to the same PersistentVolumes, MLflow
reconnected to the restored PostgreSQL, and inference served the same
prediction after a restart.

## Drill 2 — total volume loss, restore from backup

`scripts/drills/backup-restore.sh`: back up, then delete the StatefulSets
**and their PVCs** (the data is genuinely gone), redeploy onto fresh empty
PVCs, restore, verify.

```
=== 1. BACKUP ===
    pg_dump -> backups/.../mlflow.sql  (946 lines)
    mirror MinIO artifacts             (7 artifact files)
    registry: [ml-platform-model v1], runs=1, predict=200

=== 2. DESTROY (delete StatefulSets + PVCs) ===
    persistentvolumeclaim "data-platform-postgres-0" deleted
    persistentvolumeclaim "data-platform-minio-0" deleted
    PVCs gone: 0 remain

=== 3. REDEPLOY (fresh PVCs) ===
    empty registry now: runs=?          <- table does not exist yet

=== 4. RESTORE ===
    restoring PostgreSQL from mlflow.sql   done
    restoring MinIO artifacts (7 objects, 2.02 KiB)   done
    restarting MLflow   done

=== 5. VERIFY ===
    registry: [ml-platform-model v1], runs=1, predict=200

RESTORE OK — registry identical, prediction served
```

The backup mechanism:

- **PostgreSQL** — `pg_dump --clean --if-exists` from `platform-postgres-0`,
  restored with `psql -v ON_ERROR_STOP=1`.
- **MinIO** — a throwaway `minio-shell` pod
  ([`k8s/jobs/minio-shell.yaml`](../../../k8s/jobs/minio-shell.yaml)) with an
  `mc` init container that mirrors the bucket into an emptyDir, a `busybox`
  container (ships `tar`, so `kubectl cp` works — the `mc` image does not) and
  a second `mc` container to mirror back on restore.

## The consistency point

MLflow metadata and artifacts are two separate stores. A restore that brought
them back at different points in time would leave `model_versions` rows
pointing at artifact paths that do not exist, or orphaned artifacts. The drill
checks the pairing survives: `model_versions.run_id` →
`s3://mlflow-artifacts/<exp>/<run_id>/artifacts/model/` resolves, and
`mlflow.pyfunc.load_model("models:/ml-platform-model/1")` inside the inference
pod succeeds — which it did (`/predict` 200).

## Gate result

| Check | Result |
| --- | --- |
| PostgreSQL + MinIO on PVCs (StatefulSets) | PASS |
| pod deletion → same data, same PVC | PASS (15 s recovery, all counts identical) |
| MLflow reconnects after PostgreSQL pod restart | PASS |
| controlled backup (`pg_dump` + artifact mirror) | PASS |
| volume loss → restore → registry identical | PASS |
| metadata ↔ artifact relationship valid after restore | PASS (`/predict` 200) |

## Not done

- No point-in-time recovery, no WAL archiving — `pg_dump` is a full logical
  snapshot only. RDS handles this in AWS.
- The `minio-shell` backup path is kind-appropriate but manual; there is no
  scheduled backup CronJob. M10/M11 could add one, but the roadmap does not
  require it locally.
