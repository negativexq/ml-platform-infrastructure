#!/usr/bin/env bash
# M8 drill — simulate total volume loss and restore from a backup.
#
#   1. back up   (pg_dump + artifact mirror)
#   2. destroy   (delete StatefulSets AND their PVCs — the data is gone)
#   3. redeploy  (fresh, empty PVCs)
#   4. restore   (load the dump, mirror the artifacts back)
#   5. verify    the model registry and a live prediction
set -euo pipefail

NS="${NAMESPACE:-ml-platform}"
BDIR="${1:-backups/drill-$(date +%s)}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

cd "$REPO_ROOT"

pg() { kubectl -n "$NS" exec platform-postgres-0 -- psql -U mlflow -d mlflow -tA -c "$1"; }
predict() {
  curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:30080/predict \
    -H 'content-type: application/json' -d '{"features":[1.0,2.0,3.0]}'
}

echo "=== 1. BACKUP ==="
./scripts/backup.sh "$BDIR" 2>&1 | sed 's/^/    /'
before_mv=$(pg "select name || ' v' || version from model_versions")
before_runs=$(pg "select count(*) from runs")
echo "    registry: [$before_mv], runs=$before_runs, predict=$(predict)"

echo
echo "=== 2. DESTROY (delete StatefulSets + PVCs) ==="
kubectl -n "$NS" delete statefulset platform-postgres platform-minio --wait=true
kubectl -n "$NS" delete pvc data-platform-postgres-0 data-platform-minio-0 --wait=true
echo "    PVCs gone: $(kubectl -n "$NS" get pvc --no-headers 2>/dev/null | wc -l | tr -d ' ') remain"

echo
echo "=== 3. REDEPLOY (fresh PVCs) ==="
helm upgrade platform-local helm/platform-local -n "$NS" --wait --timeout 5m >/dev/null
kubectl -n "$NS" wait --for=condition=ready pod -l ml-platform.io/tier=platform --timeout=200s
echo "    empty registry now: runs=$(pg 'select count(*) from runs' 2>/dev/null || echo '?')"

echo
echo "=== 4. RESTORE ==="
./scripts/restore.sh "$BDIR" 2>&1 | sed 's/^/    /'

echo
echo "=== 5. VERIFY ==="
after_mv=$(pg "select name || ' v' || version from model_versions")
after_runs=$(pg "select count(*) from runs")
kubectl -n "$NS" rollout restart deploy/ml-platform-inference >/dev/null
kubectl -n "$NS" rollout status deploy/ml-platform-inference --timeout=180s | tail -1
code=$(predict)
echo "    registry: [$after_mv], runs=$after_runs, predict=$code"
echo
if [[ "$before_mv" == "$after_mv" && "$before_runs" == "$after_runs" && "$code" == "200" ]]; then
  echo "RESTORE OK — registry identical, prediction served"
else
  echo "RESTORE MISMATCH" >&2
  exit 1
fi
