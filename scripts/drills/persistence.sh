#!/usr/bin/env bash
# M8 drill — prove that deleting stateful pods does not lose data.
#
# Captures the model registry state, deletes platform-postgres-0,
# platform-minio-0 and the MLflow pod, waits for them to come back on the same
# PVCs, and checks the registry is intact.
set -euo pipefail

NS="${NAMESPACE:-ml-platform}"
MC_IMAGE="minio/mc:RELEASE.2024-09-16T17-43-14Z"

pg() { kubectl -n "$NS" exec platform-postgres-0 -- psql -U mlflow -d mlflow -tA -c "$1"; }

mc() {
  # The minio-shell label is what the NetworkPolicy allows to reach MinIO.
  kubectl -n "$NS" run "mc-$RANDOM" --rm -i --restart=Never --image="$MC_IMAGE" \
    --labels="app.kubernetes.io/name=minio-shell" \
    --env=HOME=/tmp --quiet --command -- sh -c \
    "mc alias set l http://platform-minio:9000 minioadmin minioadmin >/dev/null && $1" 2>/dev/null
}

snapshot() {
  echo "  model_versions : $(pg 'select name, version from model_versions' | tr '\n' ' ')"
  echo "  runs           : $(pg 'select count(*) from runs')"
  echo "  metrics rows   : $(pg 'select count(*) from metrics')"
  echo "  artifact objs  : $(mc 'mc ls --recursive l/mlflow-artifacts/ | wc -l')"
}

echo "=== BEFORE ==="
snapshot

echo
echo "=== deleting platform-postgres-0, platform-minio-0, mlflow pod ==="
start=$(date +%s)
kubectl -n "$NS" delete pod platform-postgres-0 platform-minio-0 --wait=false
kubectl -n "$NS" delete pod -l app.kubernetes.io/name=mlflow --wait=false

echo "--- waiting for all three ready again ---"
sleep 5
kubectl -n "$NS" wait --for=condition=ready pod \
  -l ml-platform.io/tier=platform --timeout=300s
echo "recovered in $(( $(date +%s) - start ))s"

echo
echo "=== AFTER ==="
snapshot

echo
echo "=== inference still serves the model ==="
kubectl -n "$NS" rollout restart deploy/ml-platform-inference >/dev/null
kubectl -n "$NS" rollout status deploy/ml-platform-inference --timeout=180s | tail -1
curl -s -w ' [%{http_code}]\n' -X POST http://localhost:30080/predict \
  -H 'content-type: application/json' -d '{"features":[1.0,2.0,3.0]}'
