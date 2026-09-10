#!/usr/bin/env bash
# Restore MLflow state from a backup directory produced by scripts/backup.sh.
#
#   ./scripts/restore.sh backups/20260911T101500
#
# Assumes platform-local is deployed with fresh (empty) PVCs. Loads the
# PostgreSQL dump, mirrors the artifacts back into MinIO, and restarts MLflow.
set -euo pipefail

NS="${NAMESPACE:-ml-platform}"
SRC="${1:?usage: restore.sh <backup-dir>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"
[[ -f "$SRC/mlflow.sql" ]] || { echo "no mlflow.sql in $SRC" >&2; exit 1; }
[[ -d "$SRC/artifacts" ]]  || { echo "no artifacts/ in $SRC" >&2; exit 1; }

echo "==> restoring PostgreSQL from $SRC/mlflow.sql"
kubectl -n "$NS" exec -i platform-postgres-0 -- \
  psql -U mlflow -d mlflow -v ON_ERROR_STOP=1 -q < "$SRC/mlflow.sql" >/dev/null
echo "    done"

echo "==> restoring MinIO artifacts (via minio-shell pod)"
kubectl -n "$NS" delete pod minio-shell --ignore-not-found --wait=true >/dev/null 2>&1
kubectl apply -f k8s/jobs/minio-shell.yaml >/dev/null
kubectl -n "$NS" wait --for=condition=ready pod/minio-shell --timeout=120s >/dev/null
# Clear whatever the init mirror pulled, stage the backup, mirror it back.
kubectl -n "$NS" exec minio-shell -c cp -- sh -c 'rm -rf /art/* /art/..?* 2>/dev/null; true'
kubectl -n "$NS" cp "$SRC/artifacts/." "minio-shell:art" -c cp >/dev/null
kubectl -n "$NS" exec minio-shell -c mc -- sh -c '
  mc alias set l http://platform-minio:9000 minioadmin minioadmin >/dev/null
  mc mb --ignore-existing l/mlflow-artifacts >/dev/null
  mc mirror --quiet --overwrite /art l/mlflow-artifacts'
kubectl -n "$NS" delete pod minio-shell --wait=false >/dev/null
echo "    done"

echo "==> restarting MLflow"
kubectl -n "$NS" rollout restart deploy/platform-mlflow >/dev/null
kubectl -n "$NS" rollout status deploy/platform-mlflow --timeout=180s | tail -1
echo "==> restore complete"
